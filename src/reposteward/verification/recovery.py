"""Reviewed reconciliation of inactive verification attempts; never reruns tests."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from urllib.parse import urlsplit

from reposteward.projects.registry import canonical_digest
from reposteward.storage.store import utc_now
from reposteward.tasks.external import TaskConflict
from reposteward.tasks.lifecycle import TaskLifecycle
from reposteward.verification.execution import observe
from reposteward.verification.external import ExternalVerification
from reposteward.workflows.policy import PolicyError


class VerificationRecovery:
    def __init__(self, config):
        self.config = config
        self.verification = ExternalVerification(config)
        self.tasks = self.verification.tasks

    def _scope(self, run_id, store):
        run = store.run(run_id)
        return TaskLifecycle(self.config)._scope(run["repository"])

    def plan(self, run_id: str, verification_id: str, *, reason: str) -> dict:
        return self._plan(run_id, verification_id, reason=reason)

    def _plan(
        self,
        run_id: str,
        verification_id: str,
        *,
        reason: str,
        lock_owned: bool = False,
    ) -> dict:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("reconciliation needs a reason of 1 to 2000 characters")
        store = self.tasks._store()
        row = self.verification._row(store, run_id, verification_id)
        run = store.run(run_id)
        record = self.tasks._record(store, run_id)
        blockers = []
        if (
            run["details"].get("task_reviewed_by", "").casefold()
            != self.config.github.login.casefold()
        ):
            blockers.append("task_account_changed")
        with self.tasks.registry.connection() as db:
            project = (
                db.execute(
                    "SELECT host FROM projects WHERE id=?", (record["project_id"],)
                ).fetchone()
                if db
                else None
            )
        host = urlsplit(self.config.github.api_url).hostname
        if not project or project["host"] != (
            "github.com" if host == "api.github.com" else host
        ):
            blockers.append("task_host_changed")
        if row["outcome"] != "running":
            blockers.append("verification_already_terminal")
        evidence = None
        try:
            if not lock_owned and self.verification._active(verification_id):
                blockers.append("verification_active")
            else:
                evidence = observe(self.verification._directory(verification_id))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            blockers.append("execution_unconfirmed")
        value = {
            "schema_version": 1,
            "operation": {
                "run_id": run_id,
                "verification_id": verification_id,
                "reason": reason,
            },
            "scope": self._scope(run_id, store),
            "record_digest": canonical_digest(row),
            "execution": evidence,
            "eligible": not blockers,
            "reasons": blockers,
            "outcome": "unknown",
            "public_write": False,
        }
        value["plan_digest"] = canonical_digest(value)
        return value

    @staticmethod
    def _existing(store, identifier):
        with store._connection() as db:
            row = db.execute(
                "SELECT * FROM verification_reconciliations WHERE verification_id=?",
                (identifier,),
            ).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def reconcile(
        self,
        run_id: str,
        verification_id: str,
        *,
        reason: str,
        plan_digest: str,
        reviewed_by: str,
        idempotency_key: str,
    ) -> dict:
        if (
            not reviewed_by
            or reviewed_by.casefold() != self.config.github.login.casefold()
        ):
            raise PolicyError("reviewer must match the configured account")
        if not re.fullmatch("[a-f0-9]{64}", plan_digest) or not re.fullmatch(
            "[A-Za-z0-9_.:-]{1,128}", idempotency_key
        ):
            raise ValueError("invalid reconciliation digest or idempotency key")
        readonly = self.tasks._store()
        self.verification._row(readonly, run_id, verification_id)
        request = canonical_digest(
            {
                "run_id": run_id,
                "verification_id": verification_id,
                "reason": reason,
                "plan_digest": plan_digest,
                "reviewed_by": reviewed_by.casefold(),
            }
        )

        def replay(store):
            saved = self._existing(store, verification_id)
            if saved and (
                saved["idempotency_key"] != idempotency_key
                or saved["request_digest"] != request
                or saved["payload"]["scope"] != self._scope(run_id, store)
            ):
                raise TaskConflict(
                    "verification was reconciled by another request or scope"
                )
            return (
                {"reconciliation": saved, "idempotent": True, "public_write": False}
                if saved
                else None
            )

        previous = replay(readonly)
        if previous:
            return previous
        directory = self.verification._directory(verification_id)
        if any(p.is_symlink() for p in (directory, *directory.parents)):
            raise ValueError("execution directory contains a symlink")
        fd = os.open(directory / "lease", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r+") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TaskConflict("verification still active") from exc
            previous = replay(readonly)
            if previous:
                return previous
            plan = self._plan(run_id, verification_id, reason=reason, lock_owned=True)
            if plan["plan_digest"] != plan_digest or not plan["eligible"]:
                raise TaskConflict("reconciliation plan changed or remains blocked")
            store = self.tasks._store(write=True)
            with store.atomic():
                row = self.verification._row(store, run_id, verification_id)
                if canonical_digest(row) != plan["record_digest"]:
                    raise TaskConflict("verification changed before reconciliation")
                now = utc_now()
                with store._connection() as db:
                    db.execute(
                        "INSERT INTO verification_reconciliations VALUES (?,?,?,?,?)",
                        (
                            verification_id,
                            idempotency_key,
                            request,
                            json.dumps(plan),
                            now,
                        ),
                    )
                    db.execute(
                        "UPDATE external_verifications SET outcome='unknown',reason=?,updated_at=? WHERE id=? AND outcome='running'",
                        (
                            "Interrupted execution reconciled; test outcome remains unknown",
                            now,
                            verification_id,
                        ),
                    )
            return {
                "reconciliation": self._existing(store, verification_id),
                "idempotent": False,
                "public_write": False,
            }
