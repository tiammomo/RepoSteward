"""Explicit workspace index operations bound to reviewed local source identities."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path

from reposteward.projects.code_facts import PARSER_VERSION
from reposteward.projects.code_index import (
    INDEX_VERSION,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    inventory,
)
from reposteward.projects.registry import (
    ProjectError,
    workspace_metadata,
    workspace_state,
)
from reposteward.storage.local_queue import digest, encoded, enqueue, plan_for
from reposteward.storage.store import utc_now


class WorkspaceScans:
    def __init__(self, operations):
        self.operations = operations
        self.workbench = operations.workbench

    def preview(
        self, project_id: str, binding_id: str, *, rebuild: bool = False
    ) -> dict:
        linked = self.workbench._binding(project_id, binding_id)
        root = Path(linked["binding"]["root"])
        metadata = workspace_metadata(root)
        state = workspace_state(root)
        snapshot, _ = inventory(metadata)
        repeated, _ = inventory(metadata)
        if (
            snapshot != repeated
            or workspace_metadata(root) != metadata
            or workspace_state(root) != state
            or self.workbench._binding(project_id, binding_id)["binding"]
            != linked["binding"]
        ):
            raise ProjectError("workspace changed during scan planning")
        result = {
            "project_id": project_id,
            "binding_id": binding_id,
            "repository": linked["project"]["repository"],
            "root": str(root),
            "fingerprint": metadata["fingerprint"],
            "identity": metadata["identity"],
            "source_digest": digest(snapshot),
            "state": state,
            "rebuild": rebuild,
            "index_version": INDEX_VERSION,
            "parser_version": PARSER_VERSION,
            "coverage": {
                key: value for key, value in snapshot.items() if key != "records"
            },
            "limits": {
                "files": MAX_FILES,
                "file_bytes": MAX_FILE_BYTES,
                "total_bytes": MAX_TOTAL_BYTES,
            },
        }
        return {**result, "revision": digest(result), "public_write": False}

    def enqueue(
        self,
        project_id: str,
        binding_id: str,
        *,
        rebuild: bool,
        expected_revision: str,
        key: str,
    ) -> dict:
        from reposteward.tasks.local_operations import OperationError

        store = self.operations.store()
        if store:
            with store._connection() as db:
                previous = db.execute(
                    "SELECT task_id FROM local_operation_requests WHERE account_digest=? AND action='workspace.scan' AND scope_kind='project' AND scope_key=? AND key_digest=?",
                    (
                        self.operations.account,
                        project_id,
                        hashlib.sha256(key.encode()).hexdigest(),
                    ),
                ).fetchone()
                if previous:
                    task = self.operations._task(store, previous["task_id"])
                    payload = plan_for(db, task)["payload"]
                    if (
                        payload["binding_id"],
                        payload["rebuild"],
                        payload["revision"],
                    ) != (binding_id, rebuild, expected_revision):
                        raise OperationError(
                            "idempotency_conflict", "请求编号已用于另一份扫描计划。"
                        )
                    return self.operations.operation(task["id"])
        current = self.preview(project_id, binding_id, rebuild=rebuild)
        if current["revision"] != expected_revision:
            raise OperationError(
                "workspace_changed", "工作区已变化，请重新预览扫描范围。"
            )
        task = enqueue(
            self.operations.store(write=True),
            account=self.operations.account,
            project=project_id,
            repository=current["repository"],
            action="workspace.scan",
            payload=current,
            idempotency_key=key,
            actor=self.operations.config.github.login,
        )
        self.operations.wake.set()
        return self.operations.operation(task["id"])

    def execute(self, store, task: dict, plan: dict, guard) -> dict:
        from reposteward.tasks.local_operations import OperationError

        payload = plan["payload"]
        if payload["project_id"] != task["scope_key"]:
            raise OperationError("plan_changed", "扫描计划不属于当前项目。")

        def check():
            guard()
            current = self.preview(
                payload["project_id"], payload["binding_id"], rebuild=payload["rebuild"]
            )
            if current != payload:
                raise OperationError(
                    "workspace_changed",
                    "工作区已变化，之前的索引保留；请重新预览并发起扫描。",
                )
            guard()

        def record(stage, body):
            with store.atomic(), store._connection() as db:
                guard()
                db.execute(
                    "INSERT INTO local_operation_results VALUES (?,?,?,?,?,?)",
                    (
                        task["id"],
                        task["lease_generation"],
                        stage,
                        encoded(body),
                        digest(body),
                        utc_now(),
                    ),
                )

        @contextmanager
        def publication():
            # The SQLite transaction fences queue ownership across the atomic
            # index replacement. Parsing never holds a ledger transaction.
            with store.atomic():
                check()
                yield
                guard()

        check()
        record(
            "scan_started",
            {
                "status": "running",
                "binding_id": payload["binding_id"],
                "source_digest": payload["source_digest"],
                "coverage": payload["coverage"],
            },
        )
        result = self.workbench.understanding.scan(
            Path(payload["root"]),
            rebuild=payload["rebuild"],
            guard=guard,
            publication=publication,
        )
        check()
        record("index_published", {"status": "completed", **result})
        return {
            "status": "completed",
            "project_id": payload["project_id"],
            "binding_id": payload["binding_id"],
            "source_digest": payload["source_digest"],
            "index_digest": result["digest"],
            "coverage": result["coverage"],
            "cache": result["cache"],
            "scanned_at": result["scanned_at"],
        }
