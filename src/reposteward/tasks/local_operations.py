"""Application service for explicit, durable local workbench commands."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing
from threading import Event, Thread
from urllib.parse import urlsplit

from reposteward.github.client import GitHubClient, GitHubReadError
from reposteward.github.sync import GitHubSync, account_key, observations
from reposteward.projects.imports import ProjectImports, import_problem
from reposteward.storage.local_queue import digest, enqueue, hex_id, plan_for
from reposteward.storage.store import SCHEMA_VERSION, Store, StoreError, utc_now
from reposteward.web.workbench import Workbench


class OperationError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def revision(task: dict) -> str:
    return digest(
        {
            key: task[key]
            for key in (
                "id",
                "state",
                "lease_generation",
                "updated_at",
                "attempt_count",
            )
        }
    )


class LocalOperations:
    def __init__(self, workbench: Workbench, *, client_factory=GitHubClient):
        self.workbench, self.config = workbench, workbench.config
        self.account = account_key(self.config)
        self.path = self.config.state_dir / "reposteward.sqlite3"
        self.client_factory = client_factory
        self.worker = "web-" + uuid.uuid4().hex
        self.stopping, self.wake = Event(), Event()
        self.thread: Thread | None = None
        self.imports = ProjectImports(self)

    def store(self, *, write: bool = False) -> Store | None:
        self.workbench.check_configuration()
        if not write:
            return self._open_store()
        # Serialize first-use initialization across tabs/processes. A second
        # explicit command must not mistake the first writer's schema 0 for an
        # old daily database. GET never creates this lock or initializes state.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            self.path.parent / ".local-state-init",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "r+") as lock:
            info = os.fstat(lock.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
            ):
                raise OperationError(
                    "unsafe_state_lock", "状态目录锁不可用，请核对目录权限。"
                )
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._open_store(write=True)

    def _open_store(self, *, write: bool = False) -> Store | None:
        self.workbench.check_configuration()
        if self.path.exists():
            with closing(
                sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
            ) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise OperationError(
                    "migration_required",
                    "本地状态需要升级。请在设置页核对状态目录，停止工作台后执行 state plan，再按计划摘要执行 state upgrade（会先备份）。",
                )
        elif not write:
            return None
        return Store(self.path, read_only=not write)

    def project(self, project_id: str) -> dict:
        self.workbench.check_configuration()
        project = self.workbench._project(project_id)
        host = urlsplit(self.config.github.api_url).hostname
        if project["host"] != ("github.com" if host == "api.github.com" else host):
            raise OperationError(
                "host_mismatch", "项目与当前 GitHub 账号的主机不一致。"
            )
        return project

    def sync(self, project_id: str, key: str) -> dict:
        project = self.project(project_id)
        task = enqueue(
            self.store(write=True),
            account=self.account,
            project=project_id,
            repository=project["repository"],
            action="github.sync",
            payload={"project_identity": project["identity"], "sync_version": 1},
            idempotency_key=key,
            actor=self.config.github.login,
        )
        self.wake.set()
        return self.operation(task["id"])

    def _task(self, store: Store, task_id: str) -> dict:
        if not hex_id(task_id):
            raise ValueError("invalid operation id")
        rows = store.queue_tasks(
            task_id=task_id, operation_family="local", account_digest=self.account
        )
        if not rows:
            raise KeyError("local operation not found")
        task = rows[0]
        with store._connection() as db:
            plan_for(db, task)
        if task["scope_kind"] == "project":
            self.project(task["scope_key"])
        else:
            self.imports.draft(task["scope_key"])
        return task

    def operation(self, task_id: str, *, history: bool = True) -> dict:
        store = self.store()
        if store is None:
            raise KeyError("local operation not found")
        task = self._task(store, task_id)
        attempts = store.queue_attempts(task_id, limit=50) if history else []
        with store._connection() as db:
            rows = (
                db.execute(
                    "SELECT * FROM local_operation_results WHERE task_id=? ORDER BY generation DESC,created_at DESC LIMIT 100",
                    (task_id,),
                ).fetchall()
                if history
                else []
            )
        stages = []
        for row in reversed(rows):
            body = json.loads(row["result"])
            if digest(body) != row["result_digest"]:
                raise ValueError("operation result changed")
            stages.append(
                {
                    "stage": row["stage"],
                    "generation": row["generation"],
                    "created_at": row["created_at"],
                    "result": body,
                }
            )
        if history and task["scope_kind"] == "import":
            with store._connection() as db:
                import_steps = db.execute(
                    "SELECT * FROM project_import_steps WHERE plan_id=? ORDER BY created_at LIMIT 30",
                    (task["plan_id"],),
                ).fetchall()
            for row in import_steps:
                body = json.loads(row["body"])
                if digest(body) != row["body_digest"]:
                    raise ValueError("import result changed")
                stages.append(
                    {
                        "stage": row["stage"],
                        "generation": 0,
                        "created_at": row["created_at"],
                        "result": {"status": "completed", **body},
                    }
                )
            stages.sort(key=lambda stage: stage["created_at"])
        return {
            key: task[key]
            for key in (
                "id",
                "sequence",
                "action",
                "repository",
                "state",
                "attempt_count",
                "max_attempts",
                "created_at",
                "updated_at",
                "available_at",
                "last_error_code",
                "manual_required",
            )
        } | {
            "project_id": task["scope_key"] if task["scope_kind"] == "project" else "",
            "import_id": task["scope_key"] if task["scope_kind"] == "import" else "",
            "revision": revision(task),
            "can_cancel": task["state"] == "pending",
            "can_retry": task["state"] in {"failed", "cancelled"},
            "lease_expired": task["lease_expired"],
            "attempts": attempts,
            "stages": stages,
            "public_write": False,
        }

    def listing(self, project_id: str = "", before: int = 0) -> dict:
        if project_id:
            self.project(project_id)
        if before < 0:
            raise ValueError("invalid operation cursor")
        store = self.store()
        if store is None:
            return {"items": [], "next_before": 0}
        with store._connection() as db:
            rows = db.execute(
                """SELECT id,sequence FROM queue_tasks WHERE operation_family='local'
                AND account_digest=? AND (?='' OR scope_key=?) AND (?=0 OR sequence<?)
                ORDER BY sequence DESC LIMIT 26""",
                (self.account, project_id, project_id, before, before),
            ).fetchall()
        return {
            "items": [self.operation(row["id"], history=False) for row in rows[:25]],
            "next_before": rows[24]["sequence"] if len(rows) > 25 else 0,
        }

    def control(
        self, task_id: str, action: str, expected_revision: str, key: str
    ) -> dict:
        if (
            action not in {"cancel", "retry"}
            or not hex_id(expected_revision, 64)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key)
        ):
            raise ValueError("invalid operation control")
        store = self.store(write=True)
        with store.atomic(), store._connection() as db:
            task = self._task(store, task_id)
            scope = (
                self.account,
                action + ":" + task_id,
                task["scope_kind"],
                task["scope_key"],
                hashlib.sha256(key.encode()).hexdigest(),
            )
            request = digest(
                {
                    "id": task_id,
                    "action": action,
                    "expected_revision": expected_revision,
                }
            )
            old = db.execute(
                "SELECT request_digest,task_id FROM local_operation_requests WHERE account_digest=? AND action=? AND scope_kind=? AND scope_key=? AND key_digest=?",
                scope,
            ).fetchone()
            if old:
                if old["request_digest"] != request or old["task_id"] != task_id:
                    raise OperationError(
                        "idempotency_conflict", "此请求编号已用于不同的操作。"
                    )
            else:
                if revision(task) != expected_revision:
                    raise OperationError(
                        "revision_changed", "操作状态已变化，请刷新后重试。"
                    )
                if action == "cancel":
                    if task["state"] != "pending":
                        raise OperationError(
                            "not_cancellable", "只有尚未开始的操作可以取消。"
                        )
                    store.cancel_queue_task(
                        task_id,
                        cancelled_by=self.config.github.login,
                        operation_family="local",
                        account_digest=self.account,
                    )
                else:
                    if task["state"] not in {"failed", "cancelled"}:
                        raise OperationError("not_retryable", "当前操作尚不需要重试。")
                    store.requeue_queue_task(
                        task_id,
                        requeued_by=self.config.github.login,
                        operation_family="local",
                        account_digest=self.account,
                    )
                db.execute(
                    "INSERT INTO local_operation_requests VALUES (?,?,?,?,?,?,?)",
                    (*scope, request, task_id),
                )
        self.wake.set()
        return self.operation(task_id)

    def github(
        self, project_id: str, *, kind: str = "pulls", cursor: str = "", number: int = 0
    ) -> dict:
        project = self.project(project_id)
        if kind not in {"pulls", "issues", "activity"} or number < 0:
            raise ValueError("invalid GitHub query")
        store = self.store()
        sources = observations(store, self.account, project_id) if store else {}
        snapshot = digest({key: row["observation_id"] for key, row in sources.items()})
        offset = 0
        if cursor:
            match = re.fullmatch(
                r"([0-9a-f]{64}):([0-9]{1,6}):(pulls|issues|activity)", cursor
            )
            if not match or match[1] != snapshot or match[3] != kind:
                raise OperationError(
                    "snapshot_changed", "同步快照已变化，请从第一页重新读取。"
                )
            offset = int(match[2])
        items = {}
        for source in sorted(
            sources.values(), key=lambda value: value["fetched_at"] or ""
        ):
            body = source["body"]
            if not body:
                continue
            if source["source"] == kind:
                for item in body["items"]:
                    items[item["number"]] = {
                        **item,
                        "observed_at": source["fetched_at"],
                    }
            if kind == "pulls" and source["source"].startswith("pull:"):
                items[body["number"]] = {**body, "observed_at": source["fetched_at"]}
        ordered = sorted(
            items.values(),
            key=lambda value: (value["updated_at"], value["number"]),
            reverse=True,
        )
        for item in ordered:
            item["url"] = (
                f"https://{project['host']}/{project['repository']}/{'issues' if kind == 'issues' else 'pull'}/{item['number']}"
            )
            checks = (
                sources.get(f"checks:{item['number']}") if kind != "issues" else None
            )
            item["checks"] = (
                checks["body"]
                if checks
                and checks["body"]
                and checks["body"]["head_sha"] == item["head_sha"]
                else None
            )
            item["checks_fetched_at"] = checks["fetched_at"] if item["checks"] else ""
            item["checks_error"] = checks["error_code"] if checks else "not_synced"
        selected = sources.get(kind, {})
        body = selected.get("body") or {}
        source_status = [
            {
                key: row[key]
                for key in (
                    "source",
                    "last_attempt",
                    "last_success",
                    "error_code",
                    "retry_at",
                    "complete",
                    "status",
                    "age_seconds",
                    "stale",
                )
            }
            for row in sources.values()
        ]
        if not number:
            for item in ordered:
                item.pop("body", None)
                item["checks"] = (
                    None  # Full CI/review evidence is a bounded detail read.
                )
        if number:
            ordered = [item for item in ordered if item["number"] == number]
            if not ordered:
                raise KeyError("PR not present in local observations")
            if store and kind != "issues":
                pull_url = (
                    f"https://{project['host']}/{project['repository']}/pull/{number}"
                )
                with store._connection() as db:
                    linked = db.execute(
                        """SELECT id,issue_number,status FROM runs WHERE repository=?
                        AND lower(json_extract(details,'$.pr_url'))=? ORDER BY updated_at DESC LIMIT 21""",
                        (
                            project["repository"].casefold(),
                            pull_url.casefold(),
                        ),
                    ).fetchall()
                ordered[0]["native_runs"] = [
                    {
                        **dict(row),
                        "trace_command": self.workbench._command(
                            "trace", project["repository"], str(row["issue_number"])
                        ),
                    }
                    for row in linked[:20]
                ]
                ordered[0]["native_runs_omitted"] = max(0, len(linked) - 20)
        total_sources = 0
        if store:
            with store._connection() as db:
                total_sources = db.execute(
                    "SELECT count(*) FROM github_sources WHERE account_digest=? AND project_id=?",
                    (self.account, project_id),
                ).fetchone()[0]
        return {
            "project_id": project_id,
            "repository": project["repository"],
            "kind": kind,
            "items": ordered[offset : offset + 50],
            "sources": source_status,
            "snapshot": snapshot,
            "next_cursor": f"{snapshot}:{offset + 50}:{kind}"
            if offset + 50 < len(ordered)
            else "",
            "has_more_remote": body.get("has_more", False),
            "total": body.get("total") if kind != "pulls" else None,
            "observed_count": len(items),
            "sources_omitted": max(0, total_sources - len(sources)),
            "never_synced": not sources,
            "coverage": "开放 PR 窗口、最近更新记录和已知 PR 的独立观测；不代表全部历史。"
            if kind == "pulls"
            else "最多两页最近更新记录。",
            "public_write": False,
        }

    def process_once(self) -> bool:
        store = self.store()
        if store is None:
            return False
        with store._connection() as db:
            ready = db.execute(
                """SELECT 1 FROM queue_tasks WHERE operation_family='local' AND account_digest=?
                AND action IN ('github.sync','project.inspect','project.apply') AND manual_required=0 AND ((state IN ('pending','failed') AND available_at<=?)
                OR (state='running' AND lease_expires_at<=?)) LIMIT 1""",
                (self.account, utc_now(), utc_now()),
            ).fetchone()
            limit = db.execute(
                "SELECT retry_at FROM github_account_limits WHERE account_digest=?",
                (self.account,),
            ).fetchone()
        if not ready or (limit and limit[0] > utc_now()):
            return False
        store = self.store(write=True)
        try:
            account_lease = store.acquire_run_lease(
                "local-github:" + self.account, owner=self.worker, ttl_seconds=120
            )
        except StoreError:
            return False
        task = None
        try:
            claimed = store.claim_queue_tasks(
                worker=self.worker,
                operation_family="local",
                account_digest=self.account,
                lease_seconds=120,
            )
            if not claimed:
                return False
            task = claimed[0]
            with store._connection() as db:
                plan = plan_for(db, task)

            def guard():
                nonlocal account_lease
                if self.stopping.is_set():
                    raise OperationError("interrupted", "同步已停止，可重新尝试。")
                self.workbench.check_configuration()
                account_lease = store.renew_run_lease(account_lease, ttl_seconds=120)
                task["lease"] = store.renew_queue_lease(
                    task["lease"], lease_seconds=120
                )

            if task["action"] == "github.sync":
                project = self.project(task["scope_key"])
                if (
                    plan["payload"]
                    != {"project_identity": project["identity"], "sync_version": 1}
                    or project["repository"] != plan["repository"]
                ):
                    raise OperationError("plan_changed", "项目身份与同步计划不一致。")
                result = GitHubSync(
                    store,
                    self.client_factory(self.config.github),
                    account=self.account,
                    project=project,
                    operation=task,
                    guard=guard,
                ).execute()
            elif task["action"] in {"project.inspect", "project.apply"}:
                result = self.imports.execute(
                    store, task, plan, self.client_factory(self.config.github), guard
                )
            else:
                raise OperationError("unsupported_action", "不支持此操作。")
            with store.atomic(), store._connection() as db:
                guard()
                from reposteward.storage.local_queue import encoded

                db.execute(
                    "INSERT INTO local_operation_results VALUES (?,?,?,?,?,?)",
                    (
                        task["id"],
                        task["lease_generation"],
                        "summary",
                        encoded(result),
                        digest(result),
                        utc_now(),
                    ),
                )
                if result["status"] == "partial":
                    store.fail_queue_task(
                        task["lease"], error_code="partial_sync", retryable=False
                    )
                else:
                    store.complete_queue_task(
                        task["lease"],
                        result={"status": "completed", "public_write": False},
                    )
            return True
        except (
            GitHubReadError,
            OperationError,
            ValueError,
            RuntimeError,
            OSError,
            sqlite3.Error,
        ) as exc:
            if task:
                code = (
                    exc.code
                    if isinstance(exc, (GitHubReadError, OperationError))
                    else "project_import_failed"
                    if task["scope_kind"] == "import"
                    else "local_sync_failed"
                )
                try:
                    with store.atomic(), store._connection() as db:
                        store.validate_run_lease(account_lease)
                        if task["scope_kind"] == "import" and not isinstance(
                            exc, GitHubReadError
                        ):
                            code, message = import_problem(exc)
                            from reposteward.storage.local_queue import encoded

                            detail = {
                                "status": "failed",
                                "error_code": code,
                                "message": message,
                            }
                            db.execute(
                                "INSERT OR IGNORE INTO local_operation_results VALUES (?,?,?,?,?,?)",
                                (
                                    task["id"],
                                    task["lease_generation"],
                                    "failure",
                                    encoded(detail),
                                    digest(detail),
                                    utc_now(),
                                ),
                            )
                        store.fail_queue_task(
                            task["lease"],
                            error_code=code,
                            retryable=bool(getattr(exc, "retry_at", "")),
                        )
                        if getattr(exc, "retry_at", ""):
                            db.execute(
                                "UPDATE queue_tasks SET available_at=? WHERE id=?",
                                (exc.retry_at, task["id"]),
                            )
                            db.execute(
                                "INSERT INTO github_account_limits VALUES (?,?,?) ON CONFLICT(account_digest) DO UPDATE SET retry_at=excluded.retry_at,reason=excluded.reason",
                                (self.account, exc.retry_at, code),
                            )
                except (StoreError, sqlite3.Error):
                    pass  # The next lease holder reconciles a task whose lease was lost.
            return bool(task)
        finally:
            try:
                store.release_run_lease(account_lease)
            except StoreError:
                pass

    def start(self):
        if self.thread is not None:
            return
        self.stopping.clear()

        def run():
            while not self.stopping.is_set():
                try:
                    progressed = self.process_once()
                except (OSError, RuntimeError, ValueError, sqlite3.Error):
                    progressed = False
                if not progressed:
                    self.wake.wait(2)
                    self.wake.clear()

        self.thread = Thread(target=run, name=self.worker, daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=35)
            if self.thread.is_alive():
                raise RuntimeError("local operation is still shutting down")
            self.thread = None
