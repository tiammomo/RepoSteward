"""Durable feedback processing, independent of observation watermarks.

Only trusted Store operations can attach verification. A verified item means its
selected repair passed local verification, not that a reviewer approved the PR.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from .context_budget import FAILED_CHECK_CONCLUSIONS

FEEDBACK_MIGRATION = (
    """CREATE TABLE IF NOT EXISTS feedback_items (
        event_sequence INTEGER PRIMARY KEY REFERENCES github_pr_events(sequence),
        status TEXT NOT NULL CHECK(status IN
          ('pending','deferred','addressed','verified','superseded','dismissed')),
        reason TEXT NOT NULL, successor_sequence INTEGER,
        handled_run_id TEXT NOT NULL DEFAULT '', commit_sha TEXT NOT NULL DEFAULT '',
        verification_digest TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS feedback_pending ON feedback_items(status,event_sequence)""",
    # Old observation watermarks cannot attest processing. Replay conservatively.
    """INSERT OR IGNORE INTO feedback_items(event_sequence,status,reason,updated_at)
        SELECT sequence,'pending','historical_processing_unknown',ingested_at
        FROM github_pr_events WHERE event_type IN
        ('issue_comment','review_comment','review','check')""",
)


def sync_feedback(db: sqlite3.Connection, repository: str, pull: int, now: str) -> None:
    """Register once, supersede older versions, and classify deterministic facts."""
    rows = db.execute(
        """SELECT e.*, b.payload AS blob_payload FROM github_pr_events e
        LEFT JOIN content_blobs b ON b.digest=e.payload_digest
        WHERE repository=? AND pull_number=? AND event_type IN
        ('issue_comment','review_comment','review','check')
        ORDER BY source_updated_at,source_created_at,sequence""",
        (repository, pull),
    ).fetchall()
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (row["event_type"], row["external_id"])
        latest[key] = row
        db.execute(
            """INSERT OR IGNORE INTO feedback_items(event_sequence,status,reason,updated_at)
            VALUES (?,'pending','unprocessed',?)""",
            (row["sequence"], now),
        )
    for row in rows:
        successor = latest[(row["event_type"], row["external_id"])]
        if successor["sequence"] != row["sequence"]:
            db.execute(
                """UPDATE feedback_items SET status='superseded',reason='new_event_version',
                successor_sequence=?,updated_at=? WHERE event_sequence=? AND status!='superseded'""",
                (successor["sequence"], now, row["sequence"]),
            )
            continue
        if row["blob_payload"] is None:
            continue
        payload = json.loads(bytes(row["blob_payload"]))
        kind = row["event_type"]
        body = str(payload.get("body") or "").strip()
        actionable = (
            str(payload.get("conclusion") or "").casefold() in FAILED_CHECK_CONCLUSIONS
            if kind == "check"
            else bool(body)
            or (
                kind == "review"
                and str(payload.get("state") or "").casefold() == "changes_requested"
            )
        )
        if not actionable:
            db.execute(
                """UPDATE feedback_items SET status='dismissed',reason='non_actionable_fact',
                updated_at=? WHERE event_sequence=? AND status IN ('pending','deferred')""",
                (now, row["sequence"]),
            )


def feedback_report(
    db: sqlite3.Connection, repository: str, pull: int, *, limit: int = 1000
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("feedback limit must be between 1 and 1000")
    counts = {
        row["status"]: row["n"]
        for row in db.execute(
            """SELECT f.status,count(*) AS n FROM feedback_items f JOIN github_pr_events e
        ON e.sequence=f.event_sequence WHERE e.repository=? AND e.pull_number=? GROUP BY f.status""",
            (repository, pull),
        )
    }
    rows = db.execute(
        """SELECT e.*,f.status AS processing_status,f.reason AS processing_reason,
        b.payload AS blob_payload FROM feedback_items f JOIN github_pr_events e
        ON e.sequence=f.event_sequence LEFT JOIN content_blobs b ON b.digest=e.payload_digest
        WHERE e.repository=? AND e.pull_number=? AND f.status IN ('pending','deferred','addressed')
        ORDER BY CASE f.status WHEN 'pending' THEN 0 ELSE 1 END,e.sequence LIMIT ?""",
        (repository, pull, limit),
    ).fetchall()
    events, unknown = [], []
    for row in rows:
        event = dict(row)
        payload = event.pop("blob_payload")
        if payload is None:
            unknown.append(
                {
                    "sequence": event["sequence"],
                    "digest": event["payload_digest"],
                    "status": "unknown",
                    "reason": "payload_unavailable",
                }
            )
            continue
        event["payload"] = json.loads(bytes(payload))
        event["payload_available"] = True
        events.append(event)
    total = sum(
        counts.get(status, 0) for status in ("pending", "deferred", "addressed")
    )
    return {
        "events": events,
        "unknown": unknown,
        "counts": counts,
        "omitted": max(0, total - len(rows)),
    }


def verify_feedback(
    db: sqlite3.Connection, run_id: str, details: dict[str, Any], now: str
) -> None:
    """Called in the same transaction as ready: failures roll back both records."""
    sequences = details.get("feedback_sequences", [])
    if not sequences:
        return
    if (
        not isinstance(sequences, list)
        or len(sequences) > 1000
        or any(type(value) is not int or value < 1 for value in sequences)
    ):
        raise ValueError("invalid repair feedback selection")
    verification = details.get("verification", {})
    commit = str(details.get("commit_sha") or "")
    guard = details.get("repair_guard", {})
    parent = str(guard.get("parent_commit") or "")
    run = db.execute("SELECT repository FROM runs WHERE id=?", (run_id,)).fetchone()
    source = db.execute(
        "SELECT repository,details FROM runs WHERE id=?",
        (guard.get("source_run_id", ""),),
    ).fetchone()
    if (
        not isinstance(verification, dict)
        or verification.get("passed") is not True
        or not re.fullmatch("[0-9a-f]{40,64}", commit)
        or commit == parent
        or run is None
        or source is None
        or run["repository"] != source["repository"]
        or json.loads(source["details"]).get("commit_sha") != parent
    ):
        raise ValueError(
            "feedback completion requires a verified repair commit and source"
        )
    digest = hashlib.sha256(
        json.dumps(verification, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for sequence in set(sequences):
        row = db.execute(
            """SELECT e.repository,e.pull_number,f.status FROM feedback_items f
            JOIN github_pr_events e ON e.sequence=f.event_sequence WHERE event_sequence=?""",
            (sequence,),
        ).fetchone()
        if (
            row is None
            or row["repository"] != run["repository"]
            or row["pull_number"] != guard.get("pull_number")
        ):
            raise ValueError("feedback selection belongs to a different pull request")
        db.execute(
            """UPDATE feedback_items SET status='verified',reason='selected_repair_verified',
            handled_run_id=?,commit_sha=?,verification_digest=?,updated_at=?
            WHERE event_sequence=? AND status IN ('pending','deferred','addressed')""",
            (run_id, commit, digest, now, sequence),
        )
