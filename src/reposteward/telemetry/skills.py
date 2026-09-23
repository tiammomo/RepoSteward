"""Bounded, explicitly reported skill use; never inferred execution or causality."""

from __future__ import annotations

import base64
import json
import re

from reposteward.core.config import AppConfig
from reposteward.projects.registry import canonical_digest
from reposteward.storage.store import utc_now
from reposteward.tasks.external import ExternalTasks, TaskConflict
from reposteward.verification.external import ExternalVerification

PHASES = ("visible", "loaded", "executed", "skipped")
FIELDS = {
    "event_id",
    "attempt_id",
    "revision",
    "skill_name",
    "skill_digest",
    "phase",
    "client",
    "model",
    "trigger",
    "verification_id",
}


def _tag(value: object, label: str, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value)
        or len(value) > maximum
    ):
        raise ValueError(f"invalid skill usage {label}")
    return value


class SkillUsage:
    def __init__(self, config: AppConfig):
        self.tasks = ExternalTasks(config)
        self.verification = ExternalVerification(config)

    @staticmethod
    def _event(row) -> dict:
        payload = json.loads(row["payload"])
        if canonical_digest(payload) != row["payload_digest"]:
            raise TaskConflict("skill usage event integrity check failed")
        return {
            **payload,
            "sequence": row["sequence"],
            "run_id": row["run_id"],
            "project_id": row["project_id"],
            "created_at": row["created_at"],
            "claim_trust": "agent_reported",
            "skill_digest_trust": "caller_reported",
        }

    def record(self, run_id: str, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) - FIELDS:
            raise ValueError("unsupported skill usage fields")
        value = {
            key: _tag(payload.get(key), key)
            for key in ("event_id", "attempt_id", "skill_name", "client", "model")
        }
        digest = payload.get("skill_digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("skill usage requires a SHA256 skill digest")
        revision = payload.get("revision")
        if type(revision) is not int or not 0 <= revision <= 2**31 - 1:
            raise ValueError("skill usage revision must be a nonnegative integer")
        phase, trigger = payload.get("phase"), payload.get("trigger")
        if phase not in PHASES or trigger not in (
            "project_understanding",
            "task_resume",
            "change_verification",
            "pr_maintenance",
            "other",
        ):
            raise ValueError("invalid skill usage phase or trigger")
        proof_id = payload.get("verification_id", "")
        if not isinstance(proof_id, str) or (
            proof_id
            and (
                phase != "executed"
                or not re.fullmatch(r"verification:[0-9a-f]{32}", proof_id)
            )
        ):
            raise ValueError("verification reference requires an executed skill event")
        value.update(
            skill_digest=digest,
            revision=revision,
            phase=phase,
            trigger=trigger,
            verification_id=proof_id,
        )
        # Request digest excludes server-derived evidence, so exact retry can recover
        # the recorded result after evidence or task revision changes.
        request_digest = canonical_digest(value)
        store = self.tasks._store(write=True)
        with store.atomic():
            record = self.tasks._record(store, run_id)
            with store._connection() as db:
                previous = db.execute(
                    "SELECT * FROM skill_usage_events WHERE run_id=? AND event_id=?",
                    (run_id, value["event_id"]),
                ).fetchone()
                if previous:
                    if previous["request_digest"] != request_digest:
                        raise TaskConflict(
                            "skill usage event ID already has different content"
                        )
                    return {
                        **self._event(previous),
                        "idempotent": True,
                        "public_write": False,
                    }
            current = self.tasks.inspect(run_id, live=True)
            if current["revision"] != revision or any(
                reason != "workspace_changed_since_checkpoint"
                for reason in current["validity"]
            ):
                raise TaskConflict(
                    "skill usage requires the current task revision and binding"
                )
            value["verification_digest"] = ""
            if proof_id:
                proof = self.verification.inspect(run_id, proof_id.split(":")[1])
                if proof["revision"] != revision or proof["outcome"] not in (
                    "passed",
                    "failed",
                    "cancelled",
                ):
                    raise TaskConflict(
                        "skill usage needs terminal verification from this revision"
                    )
                evidence = self.verification.evidence(run_id, proof_id, limit=1)
                if evidence["availability"] != "available":
                    raise TaskConflict("skill usage verification evidence unavailable")
                value["verification_digest"] = evidence["digest"]
            with store._connection() as db:
                repeated = db.execute(
                    "SELECT 1 FROM skill_usage_events WHERE run_id=? AND attempt_id=? AND skill_name=? AND skill_digest=? AND phase=?",
                    (run_id, value["attempt_id"], value["skill_name"], digest, phase),
                ).fetchone()
                if repeated:
                    raise TaskConflict(
                        "skill phase already recorded for this attempt; reuse its event ID"
                    )
                cursor = db.execute(
                    """INSERT INTO skill_usage_events
                    (run_id,project_id,event_id,attempt_id,skill_name,skill_digest,phase,request_digest,payload,payload_digest,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        record["project_id"],
                        value["event_id"],
                        value["attempt_id"],
                        value["skill_name"],
                        digest,
                        phase,
                        request_digest,
                        json.dumps(value),
                        canonical_digest(value),
                        utc_now(),
                    ),
                )
                row = db.execute(
                    "SELECT * FROM skill_usage_events WHERE sequence=?",
                    (cursor.lastrowid,),
                ).fetchone()
        return {**self._event(row), "idempotent": False, "public_write": False}

    def report(self, run_id: str, *, limit: int = 50, cursor: str = "") -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("skill usage limit must be between 1 and 100")
        store = self.tasks._store()
        record = self.tasks._record(store, run_id)
        query = canonical_digest(
            {
                "run_id": run_id,
                "project_id": record["project_id"],
                "binding_id": record["binding_id"],
            }
        )
        after = 0
        if not isinstance(cursor, str) or len(cursor) > 1024:
            raise ValueError("invalid skill usage cursor")
        if cursor:
            try:
                decoded = json.loads(
                    base64.b64decode(cursor, altchars=b"-_", validate=True)
                )
                if (
                    not isinstance(decoded, dict)
                    or set(decoded) != {"query", "after"}
                    or decoded["query"] != query
                    or type(decoded["after"]) is not int
                    or not 0 < decoded["after"] <= 2**63 - 1
                ):
                    raise ValueError("cursor scope or position differs")
                after = decoded["after"]
            except (ValueError, UnicodeError) as exc:
                raise ValueError("invalid skill usage cursor") from exc
        with store._connection() as db:
            rows = db.execute(
                "SELECT * FROM skill_usage_events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (run_id, after, limit + 1),
            ).fetchall()
        events = [self._event(row) for row in rows[:limit]]
        for event in events:
            event["verification"] = {
                "availability": "not_linked",
                "current_applicability": "not_checked",
            }
            if event["verification_id"]:
                try:
                    proof = self.verification.evidence(
                        run_id, event["verification_id"], limit=1
                    )
                    if (
                        proof["availability"] != "available"
                        or proof["digest"] != event["verification_digest"]
                    ):
                        raise TaskConflict("verification changed")
                    observed = self.verification.inspect(
                        run_id, event["verification_id"].split(":")[1]
                    )
                    event["verification"].update(
                        availability="available", outcome=observed["outcome"]
                    )
                except (OSError, ValueError, RuntimeError, KeyError):
                    event["verification"]["availability"] = "changed_or_unavailable"
        continuation = ""
        if len(rows) > limit:
            continuation = base64.urlsafe_b64encode(
                json.dumps({"query": query, "after": events[-1]["sequence"]}).encode()
            ).decode()
        return {
            "schema_version": 1,
            "run_id": run_id,
            "project_id": record["project_id"],
            "events": events,
            "next_cursor": continuation,
            "page_counts": {
                "events": len(events),
                "attempts": len({event["attempt_id"] for event in events}),
                "phases": {
                    phase: sum(event["phase"] == phase for event in events)
                    for phase in PHASES
                },
            },
            "coverage": "explicit_reports_only",
            "causal_effect": "not_established",
            "public_write": False,
        }
