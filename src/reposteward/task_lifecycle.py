"""Reviewed local terminal transitions shared by adapters; no GitHub writes."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .config import AppConfig
from .context import repository_policy_digest
from .external_tasks import ExternalTasks, TaskConflict
from .policy import PolicyError
from .projects import canonical_digest
from .snapshots import snapshot_summary
from .task_lifecycle_store import TaskLifecycleRecords

OUTCOMES = {"completed", "cancelled", "superseded"}


class TaskLifecycle:
    def __init__(self, config: AppConfig):
        self.config = config
        self.tasks = ExternalTasks(config)

    def _scope(self, repository: str) -> dict:
        policy = self.config.repositories.get(repository.casefold())
        return {
            "account": self.config.github.login.casefold(),
            "api_url": self.config.github.api_url,
            "state_dir": str(self.config.state_dir.resolve()),
            "policy_digest": repository_policy_digest(policy) if policy else None,
        }

    def plan(
        self, run_id: str, *, outcome: str, reason: str, target_run_id: str = ""
    ) -> dict:
        return self._plan(
            self.tasks._store(),
            run_id,
            outcome=outcome,
            reason=reason,
            target_run_id=target_run_id,
        )

    def _plan(
        self, store, run_id: str, *, outcome: str, reason: str, target_run_id: str
    ) -> dict:
        if outcome not in OUTCOMES:
            raise ValueError("unsupported task outcome")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("resolution requires a reason of 1 to 2000 characters")
        if outcome == "cancelled" and target_run_id:
            raise ValueError("cancellation has no target run")
        if outcome != "cancelled" and not re.fullmatch(r"[a-f0-9]{32}", target_run_id):
            raise ValueError("completion or supersession requires a target run ID")
        record = self.tasks._record(store, run_id)
        run = store.run(run_id)
        records = TaskLifecycleRecords(store)
        reasons = []
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
            reasons.append("task_host_changed")
        if (
            run["stage"] != "external"
            or run["status"] != "running"
            or records.get(run_id)
        ):
            reasons.append("attempt_is_terminal")
        if (
            run["details"].get("task_reviewed_by", "").casefold()
            != self.config.github.login.casefold()
        ):
            reasons.append("task_account_changed")
        if records.active_verifications(run_id):
            reasons.append("verification_in_progress_or_unreconciled")
        target = store.run(target_run_id) if target_run_id else None
        evidence = None
        if target_run_id:
            if target is None:
                reasons.append("target_not_found")
            elif (target["repository"], target["issue_number"]) != (
                run["repository"],
                run["issue_number"],
            ):
                reasons.append("target_is_another_work_item")
            elif target_run_id == run_id:
                reasons.append("target_is_same_attempt")
            elif outcome == "superseded":
                if target["stage"] != "external" or target["status"] != "running":
                    reasons.append("successor_is_not_active")
                else:
                    successor = self.tasks._record(store, target_run_id)
                    fields = ("project_id", "binding_id", "binding_fingerprint")
                    if any(successor[k] != record[k] for k in fields):
                        reasons.append("successor_workspace_differs")
                    if (
                        target["details"].get("task_reviewed_by", "").casefold()
                        != self.config.github.login.casefold()
                    ):
                        reasons.append("successor_account_differs")
                    evidence = {
                        "run_id": target_run_id,
                        "revision": successor["checkpoint_revision"],
                        "snapshot_digest": successor["snapshot"]["digest"],
                        "status": target["status"],
                        "updated_at": target["updated_at"],
                    }
            else:
                evidence = records.merged_delivery(target_run_id)
                if (
                    target["stage"] == "external"
                    or target["status"] != "submitted"
                    or not evidence
                ):
                    reasons.append("native_merged_delivery_required")
                elif (
                    evidence["repository"] != run["repository"]
                    or evidence["head_sha"] != record["snapshot"]["head"]
                    or target["details"].get("commit_sha") != evidence["head_sha"]
                    or record["snapshot"]["dirty"]
                    or not re.fullmatch(r"[a-f0-9]{40}", evidence["merge_commit_sha"])
                ):
                    reasons.append("delivery_does_not_match_clean_checkpoint")
        plan = {
            "schema_version": 1,
            "operation": {
                "run_id": run_id,
                "outcome": outcome,
                "reason": reason,
                "target_run_id": target_run_id,
            },
            "scope": self._scope(run["repository"]),
            "task": {
                "repository": run["repository"],
                "issue": run["issue_number"],
                "project_id": record["project_id"],
                "binding_id": record["binding_id"],
                "binding_fingerprint": record["binding_fingerprint"],
                "revision": record["checkpoint_revision"],
                "status": run["status"],
                "updated_at": run["updated_at"],
                "snapshot": snapshot_summary(record["snapshot"]),
            },
            "evidence": evidence,
            "eligible": not reasons,
            "reasons": reasons,
            "public_write": False,
            "writes_files": False,
            "note": "Ends this attempt using recorded evidence; does not verify current files, close an Issue or change another attempt.",
        }
        plan["plan_digest"] = canonical_digest(plan)
        return plan

    def resolve(
        self,
        run_id: str,
        *,
        outcome: str,
        reason: str,
        target_run_id: str = "",
        plan_digest: str,
        reviewed_by: str,
        idempotency_key: str,
    ) -> dict:
        if (
            reviewed_by.casefold() != self.config.github.login.casefold()
            or not reviewed_by
        ):
            raise PolicyError(
                "resolution reviewer must match the configured GitHub login"
            )
        if not re.fullmatch(r"[a-f0-9]{64}", plan_digest):
            raise ValueError("resolution requires a plan digest")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key):
            raise ValueError("invalid resolution idempotency key")
        # Refuse old/missing stores before opening any write-capable connection.
        self.tasks._record(self.tasks._store(), run_id)
        request = {
            "run_id": run_id,
            "outcome": outcome,
            "reason": reason,
            "target_run_id": target_run_id,
            "plan_digest": plan_digest,
            "reviewed_by": reviewed_by.casefold(),
        }
        digest = canonical_digest(request)
        store = self.tasks._store(write=True)
        with store.atomic():
            records = TaskLifecycleRecords(store)
            existing = records.get(run_id)
            if existing:
                scope = self._scope(existing["payload"]["task"]["repository"])
                if (
                    existing["idempotency_key"] != idempotency_key
                    or existing["request_digest"] != digest
                    or existing["payload"]["scope"] != scope
                ):
                    raise TaskConflict(
                        "attempt already resolved by a different request or scope"
                    )
                return {
                    "resolution": existing,
                    "idempotent": True,
                    "public_write": False,
                }
            plan = self._plan(
                store,
                run_id,
                outcome=outcome,
                reason=reason,
                target_run_id=target_run_id,
            )
            if plan["plan_digest"] != plan_digest:
                raise TaskConflict(
                    "resolution plan changed; inspect and review a new plan"
                )
            if not plan["eligible"]:
                raise TaskConflict("resolution blocked: " + ", ".join(plan["reasons"]))
            saved = records.append(plan, key=idempotency_key, request_digest=digest)
            # Keep stage, revision, work item, checkpoints and verification evidence intact.
            store.update_run(run_id, status=outcome)
            return {
                "resolution": saved,
                "idempotent": False,
                "public_write": False,
                "local_write": True,
            }
