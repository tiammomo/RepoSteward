"""Workspace-bound assistance jobs on the shared durable local queue."""

from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from threading import Event, Thread

from reposteward.projects.registry import (
    ProjectError,
    canonical_digest,
    workspace_metadata,
)
from reposteward.projects.understanding import Understanding
from reposteward.storage.local_queue import (
    ASSISTANCE_ACTIONS,
    digest,
    encoded,
    enqueue,
    plan_for,
)
from reposteward.storage.store import StoreError, utc_now
from reposteward.tasks.local_operations import LocalOperations, OperationError, revision
from reposteward.verification.external import ExternalVerification
from reposteward.web.workbench import Workbench


class AssistanceOperations:
    def __init__(self, bridge):
        self.bridge = bridge
        self.config = bridge.config
        self.local = LocalOperations(Workbench(self.config))
        self.scope = bridge.scope_digest()
        self.project = bridge.bound["project"]["id"]
        self.verification = ExternalVerification(self.config)
        self.understanding = Understanding(self.config.state_dir / "understanding")

    def _task(self, identifier):
        self.bridge._scope()
        store = self.local.store()
        if store is None:
            raise KeyError("operation not found")
        task = self.local._task(store, identifier)
        with store._connection() as db:
            plan = plan_for(db, task)
        if (
            task["action"] not in ASSISTANCE_ACTIONS
            or plan["payload"].get("scope_digest") != self.scope
        ):
            raise ProjectError("operation is outside this workspace scope")
        self.bridge._scope(plan["payload"].get("run_id"))
        return store, task, plan["payload"]

    def start(self, kind: str, *, idempotency_key: str, **parameters) -> dict:
        self.bridge._scope(parameters.get("run_id"))
        if kind == "verification":
            if set(parameters) != {
                "run_id",
                "profile",
                "expected_revision",
                "expected_snapshot",
            }:
                raise ValueError("verification requires an exact task snapshot")
            if (
                type(parameters["expected_revision"]) is not int
                or parameters["expected_revision"] < 0
            ):
                raise ValueError("invalid checkpoint revision")
            from reposteward.storage.local_queue import hex_id

            if not hex_id(parameters["expected_snapshot"], 64):
                raise ValueError("invalid snapshot digest")
            selected = self.verification._profile(
                self.bridge.bound["project"]["repository"].casefold(),
                parameters["profile"],
            )
            parameters["profile_digest"] = self.verification._profile_digest(selected)
        elif kind == "understanding":
            if set(parameters) - {"mode", "focus", "limit"}:
                raise ValueError("invalid report parameters")
            parameters = {"mode": "maintainer", "focus": "", "limit": 12, **parameters}
            if (
                parameters["mode"] not in {"maintainer", "contributor"}
                or type(parameters["limit"]) is not int
                or not 1 <= parameters["limit"] <= 20
                or not isinstance(parameters["focus"], str)
                or len(parameters["focus"]) > 2000
            ):
                raise ValueError("invalid report parameters")
            index = self.understanding.index.load(workspace_metadata(self.bridge.root))
            if index is None:
                raise ValueError("scan the linked workspace before requesting a report")
            parameters["index_digest"] = index["digest"]
        else:
            raise ValueError("unknown assistance operation")
        store = self.local.store(write=True)
        task = enqueue(
            store,
            account=self.local.account,
            project=self.project,
            repository=self.bridge.bound["project"]["repository"],
            action="assistance." + kind,
            payload={"scope_digest": self.scope, **parameters},
            idempotency_key=idempotency_key,
            actor=self.config.github.login,
        )
        return self.get(task["id"])

    def get(self, identifier: str) -> dict:
        return self._view(identifier, history=True)

    def _view(self, identifier: str, *, history: bool) -> dict:
        self._task(identifier)
        result = self.local.operation(identifier, history=history)
        return {
            **result,
            "schema_version": 1,
            "scope_digest": self.scope,
            "task_completion": False,
            "next_action": "Run operation worker for this workspace when pending; poll this operation ID.",
        }

    def listing(self, *, before: int = 0) -> dict:
        self.bridge._scope()
        if type(before) is not int or before < 0:
            raise ValueError("invalid operation cursor")
        store = self.local.store()
        rows = []
        if store:
            with store._connection() as db:
                rows = db.execute(
                    """SELECT q.id,q.sequence FROM queue_tasks q
                    JOIN local_operation_plans p ON p.id=q.plan_id
                    WHERE q.operation_family='local' AND q.account_digest=? AND q.scope_key=?
                    AND json_extract(p.payload,'$.scope_digest')=?
                    AND (?=0 OR q.sequence<?) ORDER BY q.sequence DESC LIMIT 26""",
                    (self.local.account, self.project, self.scope, before, before),
                ).fetchall()
        return {
            "schema_version": 1,
            "items": [self._view(r["id"], history=False) for r in rows[:25]],
            "next_before": rows[24]["sequence"] if len(rows) > 25 else 0,
            "public_write": False,
        }

    def wait(
        self, identifier: str, *, timeout: int = 10, cancel_event: Event | None = None
    ) -> dict:
        if type(timeout) is not int or not 0 <= timeout <= 30:
            raise ValueError("wait timeout must be between 0 and 30 seconds")
        deadline = time.monotonic() + timeout
        signal = cancel_event or Event()
        while True:
            result = self.get(identifier)
            if (
                result["state"] not in {"pending", "running"}
                or time.monotonic() >= deadline
                or signal.is_set()
            ):
                return result
            signal.wait(min(0.25, max(0, deadline - time.monotonic())))

    def cancel(
        self, identifier: str, *, expected_revision: str, idempotency_key: str
    ) -> dict:
        self._task(identifier)
        self.local.control(identifier, "cancel", expected_revision, idempotency_key)
        return self.get(identifier)

    @contextmanager
    def _lock(self, identifier):
        from reposteward.storage.local_queue import hex_id

        if not hex_id(identifier):
            raise ValueError("invalid operation id")
        directory = self.config.state_dir / "assistance-operations"
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            directory / (identifier + ".lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def _verification_identity(self, task, payload):
        key = "operation:" + task["id"]
        return key, canonical_digest(
            {"run_id": payload["run_id"], "idempotency_key": key}
        )[:32]

    def _existing_verification(self, store, task, payload):
        _, identifier = self._verification_identity(task, payload)
        with store._connection() as db:
            row = db.execute(
                "SELECT * FROM external_verifications WHERE id=?", (identifier,)
            ).fetchone()
        if row is None:
            return None
        if (
            row["run_id"] != payload["run_id"]
            or row["profile_digest"] != payload["profile_digest"]
            or row["request_digest"]
            != canonical_digest(
                {
                    "run_id": payload["run_id"],
                    "profile_digest": payload["profile_digest"],
                    "revision": payload["expected_revision"],
                    "snapshot": payload["expected_snapshot"],
                }
            )
        ):
            raise ValueError("verification identity changed")
        if row["outcome"] == "running":
            raise OperationError(
                "reconciliation_required",
                "Reconcile the original verification before resuming the operation.",
            )
        return self.verification.inspect(payload["run_id"], identifier, live=False)

    def reconcile(
        self, identifier: str, *, expected_revision: str, idempotency_key: str
    ) -> dict:
        """Resume result recording only after an original terminal verification exists."""
        import re

        from reposteward.storage.local_queue import hex_id

        if not hex_id(expected_revision, 64) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", idempotency_key
        ):
            raise ValueError("invalid reconciliation request")
        with self._lock(identifier):
            store, task, payload = self._task(identifier)
            if task["action"] != "assistance.verification":
                raise ValueError("only verification results require reconciliation")
            result = self._existing_verification(store, task, payload)
            if result is None:
                raise ValueError("no terminal verification result to reconcile")
            scope = (
                self.local.account,
                "reconcile:" + identifier,
                "project",
                self.project,
                hashlib.sha256(idempotency_key.encode()).hexdigest(),
            )
            request = digest({"id": identifier, "revision": expected_revision})
            store = self.local.store(write=True)
            with store.atomic(), store._connection() as db:
                old = db.execute(
                    "SELECT * FROM local_operation_requests WHERE account_digest=? AND action=? AND scope_kind=? AND scope_key=? AND key_digest=?",
                    scope,
                ).fetchone()
                if old:
                    if old["request_digest"] != request or old["task_id"] != identifier:
                        raise ValueError("idempotency_conflict")
                else:
                    task = self.local._task(store, identifier)
                    if revision(task) != expected_revision or task["state"] != "failed":
                        raise ValueError(
                            "operation changed or does not require reconciliation"
                        )
                    store.requeue_queue_task(
                        identifier,
                        requeued_by=self.config.github.login,
                        operation_family="local",
                        account_digest=self.local.account,
                    )
                    db.execute(
                        "INSERT INTO local_operation_requests VALUES (?,?,?,?,?,?,?)",
                        (*scope, request, identifier),
                    )
        return self.get(identifier)

    def _execute(self, store, task, payload, cancel):
        if task["action"] == "assistance.verification":
            existing = self._existing_verification(store, task, payload)
            if existing is not None:
                return existing
            if cancel.is_set():
                return {
                    "outcome": "cancelled",
                    "execution_started": False,
                    "public_write": False,
                }
            profile = self.verification._profile(task["repository"], payload["profile"])
            if self.verification._profile_digest(profile) != payload["profile_digest"]:
                raise ValueError("verification profile changed after enqueue")
            key, _ = self._verification_identity(task, payload)
            return self.verification.request(
                **{
                    k: payload[k]
                    for k in (
                        "run_id",
                        "profile",
                        "expected_revision",
                        "expected_snapshot",
                    )
                },
                idempotency_key=key,
                cancel_event=cancel,
            )
        if cancel.is_set():
            return {
                "outcome": "cancelled",
                "execution_started": False,
                "public_write": False,
            }
        result = self.understanding.guide(
            self.bridge.root, **{k: payload[k] for k in ("mode", "focus", "limit")}
        )
        if (
            result.get("index_digest") != payload["index_digest"]
            or result["status"] != "current"
        ):
            raise ValueError(
                "understanding source changed after enqueue; scan and request a new report"
            )
        return {
            **result,
            "evidence_kind": "historical_project_report",
            "current_applicability": "not_checked",
        }

    def process_once(self) -> bool:
        self.bridge._scope()
        store = self.local.store()
        if store is None:
            return False
        with store._connection() as db:
            rows = db.execute(
                """SELECT q.id FROM queue_tasks q JOIN local_operation_plans p ON p.id=q.plan_id
                WHERE q.operation_family='local' AND q.account_digest=? AND q.scope_key=?
                AND json_extract(p.payload,'$.scope_digest')=? AND q.manual_required=0
                AND ((q.state IN ('pending','failed') AND q.available_at<=?)
                OR (q.state='running' AND q.lease_expires_at<=?)) ORDER BY q.sequence LIMIT 25""",
                (self.local.account, self.project, self.scope, utc_now(), utc_now()),
            ).fetchall()
        for row in rows:
            try:
                with self._lock(row["id"]):
                    return self._process(row["id"])
            except BlockingIOError:
                continue
        return False

    def _process(self, identifier) -> bool:
        _, _, payload = self._task(identifier)
        store = self.local.store(write=True)
        claimed = store.claim_queue_tasks(
            worker=self.local.worker,
            operation_family="local",
            account_digest=self.local.account,
            task_id=identifier,
            actions=tuple(sorted(ASSISTANCE_ACTIONS)),
            lease_seconds=120,
        )
        if not claimed:
            return False
        task = claimed[0]
        stop, cancel = Event(), Event()
        failures = []

        def guard():
            self.bridge._scope(payload.get("run_id"))
            self.local.workbench.check_configuration()
            store.renew_queue_lease(task["lease"], lease_seconds=120)
            if self.get(identifier)["cancel_requested"]:
                cancel.set()

        def heartbeat():
            while not stop.wait(1):
                try:
                    guard()
                except (
                    RuntimeError,
                    ValueError,
                    KeyError,
                    OSError,
                    sqlite3.Error,
                ) as exc:
                    failures.append(exc)
                    cancel.set()
                    return

        thread = Thread(target=heartbeat, daemon=True)
        try:
            guard()
            thread.start()
            result = self._execute(store, task, payload, cancel)
            if "current_applicability" in result:
                result = {
                    **result,
                    "applicability_at_execution": result["current_applicability"],
                    "current_applicability": "not_checked",
                }
            stop.set()
            thread.join()
            if failures:
                raise StoreError("operation lease or scope changed during execution")
            guard()
            if len(encoded(result).encode()) > 200_000:
                raise ValueError("operation result exceeds bounded artifact limit")
            with store.atomic(), store._connection() as db:
                db.execute(
                    "INSERT INTO local_operation_results VALUES (?,?,?,?,?,?)",
                    (
                        identifier,
                        task["lease_generation"],
                        "result",
                        encoded(result),
                        digest(result),
                        utc_now(),
                    ),
                )
                if result.get("outcome") == "cancelled":
                    store.acknowledge_queue_cancellation(task["lease"])
                elif result.get("outcome") == "unknown":
                    store.fail_queue_task(
                        task["lease"],
                        error_code="verification_outcome_unknown",
                        retryable=False,
                    )
                else:
                    store.complete_queue_task(
                        task["lease"],
                        result={"status": "completed", "public_write": False},
                    )
        except (RuntimeError, ValueError, KeyError, OSError, sqlite3.Error) as exc:
            try:
                store.fail_queue_task(
                    task["lease"],
                    error_code=exc.code
                    if isinstance(exc, OperationError)
                    else "assistance_failed",
                    retryable=False,
                )
            except StoreError:
                pass  # A future lease holder reconciles the deterministic execution ID.
        finally:
            stop.set()
            if thread.is_alive():
                thread.join()
        return True
