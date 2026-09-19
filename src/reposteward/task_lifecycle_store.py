"""Persistence for immutable external-attempt resolutions, separate from checkpoints."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store

TASK_RESOLUTION_MIGRATION = (
    """CREATE TABLE IF NOT EXISTS external_task_resolutions (
        run_id TEXT PRIMARY KEY REFERENCES external_task_runs(run_id),
        id TEXT NOT NULL UNIQUE, idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL, payload TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
)


class TaskLifecycleRecords:
    def __init__(self, store: Store):
        self.store = store

    def get(self, run_id: str) -> dict | None:
        with self.store._connection() as db:
            row = db.execute(
                "SELECT * FROM external_task_resolutions WHERE run_id=?", (run_id,)
            ).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def active_verifications(self, run_id: str) -> int:
        with self.store._connection() as db:
            return db.execute(
                "SELECT count(*) FROM external_verifications WHERE run_id=? AND outcome='running'",
                (run_id,),
            ).fetchone()[0]

    def merged_delivery(self, run_id: str) -> dict | None:
        with self.store._connection() as db:
            row = db.execute(
                """SELECT id,run_id,repository,pull_number,head_sha,outcome,payload
                FROM merge_executions WHERE run_id=? AND stage='completed'
                ORDER BY created_at DESC,rowid DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
        if row is None or row["outcome"] not in {"merged", "already_merged"}:
            return None
        payload = json.loads(row["payload"])
        result = payload.get("github_result", {})
        merge_commit = payload.get("merge_commit_sha", "")
        if row["outcome"] == "merged":
            merge_commit = result.get("sha", "") if result.get("merged") is True else ""
        return {
            key: row[key]
            for key in (
                "id",
                "run_id",
                "repository",
                "pull_number",
                "head_sha",
                "outcome",
            )
        } | {"merge_commit_sha": merge_commit}

    def append(self, plan: dict, *, key: str, request_digest: str) -> dict:
        from .store import utc_now

        value = {
            "run_id": plan["operation"]["run_id"],
            "id": uuid.uuid4().hex,
            "idempotency_key": key,
            "request_digest": request_digest,
            "payload": plan,
            "created_at": utc_now(),
        }
        with self.store._connection() as db:
            db.execute(
                "INSERT INTO external_task_resolutions VALUES (?,?,?,?,?,?)",
                (
                    value["run_id"],
                    value["id"],
                    key,
                    request_digest,
                    json.dumps(plan, ensure_ascii=False),
                    value["created_at"],
                ),
            )
        return value
