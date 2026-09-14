"""Local, explicit external-turn attribution, independent of the task DB schema."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .codex_usage import TOKEN_FIELDS, UsageSourceError, read_codex_turns
from .config import AppConfig
from .projects import canonical_digest, workspace_metadata
from .store import Store, utc_now
from .usage import usage_cost

EXTERNAL_USAGE_SCHEMA_VERSION = 1


class ExternalUsage:
    def __init__(self, config: AppConfig):
        self.config = config
        self.path = config.state_dir / "external-usage.sqlite3"

    @contextmanager
    def _connection(
        self, *, write: bool = False
    ) -> Iterator[sqlite3.Connection | None]:
        if not write and not self.path.exists():
            yield None
            return
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
        db = sqlite3.connect(
            str(self.path) if write else self.path.as_uri() + "?mode=ro",
            uri=not write,
            timeout=30,
            isolation_level=None,
        )
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if write and version == 0:
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                ).fetchone():
                    raise UsageSourceError("unknown external usage database schema")
                db.execute("""CREATE TABLE sources (
                    session_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                    prefix_bytes INTEGER NOT NULL, prefix_sha256 TEXT NOT NULL,
                    cli_version TEXT NOT NULL)""")
                db.execute("""CREATE TABLE turns (
                    session_id TEXT NOT NULL REFERENCES sources(session_id),
                    turn_id TEXT NOT NULL, work_item_id TEXT NOT NULL,
                    repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
                    first_run_id TEXT NOT NULL, payload TEXT NOT NULL,
                    observation_digest TEXT NOT NULL, collected_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, turn_id))""")
                db.execute("""CREATE TABLE observations (
                    sequence INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL, digest TEXT NOT NULL,
                    payload TEXT NOT NULL, collected_at TEXT NOT NULL,
                    UNIQUE(session_id, turn_id, digest),
                    FOREIGN KEY(session_id,turn_id) REFERENCES turns(session_id,turn_id))""")
                db.execute(f"PRAGMA user_version={EXTERNAL_USAGE_SCHEMA_VERSION}")
                version = EXTERNAL_USAGE_SCHEMA_VERSION
            if version != EXTERNAL_USAGE_SCHEMA_VERSION:
                raise UsageSourceError("unsupported external usage database schema")
            yield db
            db.commit() if write else db.rollback()
        except BaseException as exc:
            db.rollback()
            if isinstance(exc, sqlite3.Error):
                raise UsageSourceError(
                    "external usage database operation failed"
                ) from exc
            raise
        finally:
            db.close()

    def _task(self, run_id: str) -> dict[str, Any]:
        store = Store(self.config.state_dir / "reposteward.sqlite3", read_only=True)
        bundle = store.context_bundle(run_id)
        if bundle is None:
            raise UsageSourceError("run has no registered WorkItem context")
        if bundle["harness_run"]["harness"] not in {
            "external-agent",
            "external-workspace",
        }:
            raise UsageSourceError(
                "collection requires an external task or adopted workspace run"
            )
        work = bundle["work_item"]
        if work["kind"] != "github_issue" or not str(work["external_id"]).isdigit():
            raise UsageSourceError("collection requires a GitHub Issue WorkItem")
        return work

    def collect(
        self,
        run_id: str,
        *,
        codex_session: Path | None = None,
        turn_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        work = self._task(run_id)
        if (codex_session is None) != (not turn_ids):
            raise UsageSourceError(
                "provide both --codex-session and --turn-id, or neither"
            )
        if codex_session is not None:
            selections = {
                str(codex_session.expanduser().resolve(strict=True)): set(turn_ids)
            }
        else:
            selections: dict[str, set[str]] = {}
            with self._connection() as db:
                if db is not None:
                    for row in db.execute(
                        """SELECT s.path, t.turn_id FROM turns t JOIN sources s
                        ON s.session_id=t.session_id WHERE t.work_item_id=?""",
                        (work["id"],),
                    ):
                        selections.setdefault(row["path"], set()).add(row["turn_id"])
            if not selections:
                raise UsageSourceError(
                    "task has no selected sources; bind a session and turn first"
                )
        results = []
        # Each source snapshot and its selected turns commit atomically. A failed
        # later source can be retried without duplicating already collected ones.
        for path, selected in selections.items():
            results.append(self._collect_source(run_id, work, Path(path), selected))
        return {"work_item_id": work["id"], "sources": results, "public_write": False}

    def _collect_source(
        self, run_id: str, work: dict, path: Path, selected: set[str]
    ) -> dict:
        with self._connection(write=True) as db:
            assert db is not None
            previous = db.execute(
                "SELECT * FROM sources WHERE path=?", (str(path),)
            ).fetchone()
            prefix = (
                (previous["prefix_bytes"], previous["prefix_sha256"])
                if previous
                else None
            )
            source = read_codex_turns(path, selected, previous_prefix=prefix)
            session_id = source["session_id"]
            existing = db.execute(
                "SELECT * FROM sources WHERE session_id=?", (session_id,)
            ).fetchone()
            if existing and existing["path"] != str(path):
                raise UsageSourceError(
                    "session already registered at another source path"
                )
            if previous and previous["session_id"] != session_id:
                raise UsageSourceError("source session identity changed")
            db.execute(
                """INSERT INTO sources VALUES (?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
                prefix_bytes=excluded.prefix_bytes, prefix_sha256=excluded.prefix_sha256""",
                (
                    session_id,
                    str(path),
                    source["prefix_bytes"],
                    source["prefix_sha256"],
                    source["cli_version"],
                ),
            )
            api_host = urlsplit(self.config.github.api_url).hostname
            host = "github.com" if api_host == "api.github.com" else api_host
            checked = set()
            changed = 0
            for turn_id, value in source["turns"].items():
                for cwd in value.pop("workspaces"):
                    if cwd not in checked:
                        metadata = workspace_metadata(Path(cwd))
                        if (
                            metadata["host"] != host
                            or metadata["repository"].casefold()
                            != work["repository"].casefold()
                        ):
                            raise UsageSourceError(
                                "selected turn workspace belongs to another project"
                            )
                        checked.add(cwd)
                prior = db.execute(
                    "SELECT * FROM turns WHERE session_id=? AND turn_id=?",
                    (session_id, turn_id),
                ).fetchone()
                if prior and prior["work_item_id"] != work["id"]:
                    raise UsageSourceError(
                        "selected turn already belongs to another WorkItem"
                    )
                observation = {
                    **value,
                    "session_id": session_id,
                    "adapter_version": source["adapter_version"],
                    "cli_version": source["cli_version"],
                    "trust": "local_client_counters",
                }
                digest = canonical_digest(observation)
                if prior and prior["observation_digest"] == digest:
                    db.execute(
                        "UPDATE turns SET collected_at=? WHERE session_id=? AND turn_id=?",
                        (utc_now(), session_id, turn_id),
                    )
                    continue
                payload = json.dumps(observation, sort_keys=True)
                now = utc_now()
                db.execute(
                    """INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(session_id,turn_id) DO UPDATE SET payload=excluded.payload,
                    observation_digest=excluded.observation_digest, collected_at=excluded.collected_at""",
                    (
                        session_id,
                        turn_id,
                        work["id"],
                        work["repository"].casefold(),
                        int(work["external_id"]),
                        run_id,
                        payload,
                        digest,
                        now,
                    ),
                )
                db.execute(
                    "INSERT OR IGNORE INTO observations(session_id,turn_id,digest,payload,collected_at) VALUES (?,?,?,?,?)",
                    (session_id, turn_id, digest, payload, now),
                )
                changed += 1
        return {
            "session_id": session_id,
            "selected_turns": len(selected),
            "updated_turns": changed,
            "idempotent": changed == 0,
            "partial_tail": source["partial_tail"],
            "source_prefix_sha256": source["prefix_sha256"],
        }

    def report(
        self,
        repository: str,
        *,
        issue: int = 0,
        group_by: str = "work-item",
        include_turns: bool = False,
    ) -> dict:
        if issue < 0 or group_by not in {"work-item", "model", "issue", "none"}:
            raise UsageSourceError("invalid external usage report filter")
        rows = []
        with self._connection() as db:
            if db is not None:
                for stored in db.execute(
                    "SELECT * FROM turns WHERE repository=? AND (?=0 OR issue_number=?) ORDER BY session_id,turn_id",
                    (repository.casefold(), issue, issue),
                ):
                    row = dict(stored)
                    row.update(json.loads(row.pop("payload")))
                    row["cost_estimate"] = self._cost(row)
                    rows.append(row)
        groups: dict[str, list[dict]] = {}
        field = {
            "work-item": "work_item_id",
            "model": "model",
            "issue": "issue_number",
        }.get(group_by)
        if field:
            for row in rows:
                groups.setdefault(str(row[field] or "unknown"), []).append(row)
        result = {
            "schema_version": EXTERNAL_USAGE_SCHEMA_VERSION,
            "repository": repository.casefold(),
            "issue_number": issue or None,
            "source": "external_codex_turns",
            "group_by": group_by,
            "summary": _summary(rows),
            "groups": [
                {"key": key, "summary": _summary(value)}
                for key, value in sorted(groups.items())
            ],
            "raw_prompts_stored": False,
            "public_write": False,
            "scope": "explicitly selected turns; snapshots require collection to refresh",
        }
        if include_turns:
            result["turns"] = rows
        return result

    def _cost(self, row: dict) -> dict:
        if not row["model"]:
            return {"status": "unknown", "reason": "model_ambiguous"}
        if row["metrics"].get("cache_write_input_tokens") is None:
            return {"status": "unknown", "reason": "metrics_missing"}
        if row["metrics"].get("cache_write_input_tokens") != 0:
            return {"status": "unknown", "reason": "cache_write_pricing_unsupported"}
        return usage_cost(
            {**row, "harness": "codex-local", "created_at": row["started_at"]},
            self.config.observability.prices,
        )


def _summary(rows: list[dict]) -> dict:
    metrics = {}
    for key in TOKEN_FIELDS:
        known = [row["metrics"][key] for row in rows if row["metrics"][key] is not None]
        metrics[key] = {
            "value": sum(known) if known else None,
            "known_turns": len(known),
            "unknown_turns": len(rows) - len(known),
        }
    costs: dict[str, Decimal] = {}
    for row in rows:
        cost = row["cost_estimate"]
        if cost["status"] == "known":
            costs[cost["currency"]] = costs.get(cost["currency"], Decimal(0)) + Decimal(
                cost["value"]
            )
    return {
        "turns": len(rows),
        "work_items": len({row["work_item_id"] for row in rows}),
        "completion": dict(Counter(row["completion"] for row in rows)),
        "metrics": metrics,
        "cost_estimate": {
            "by_currency": {key: str(value) for key, value in sorted(costs.items())},
            "unknown_turns": sum(
                row["cost_estimate"]["status"] == "unknown" for row in rows
            ),
        },
        "tool_call_count": None,
        "active_duration_seconds": None,
        "last_collected_at": max((row["collected_at"] for row in rows), default=None),
    }
