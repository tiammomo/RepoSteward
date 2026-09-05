"""Local project assistance for coding agents started by the user."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import AppConfig
from .context import build_context_pack, repository_policy_digest, running_checkpoint
from .context_budget import ContextBudgetError, estimate_tokens
from .discovery import score_issue
from .github import GitHubClient
from .policy import PolicyError
from .projects import ProjectRegistry, canonical_digest, local_git
from .prompt_budget import fit_context
from .snapshots import snapshot_summary, workspace_snapshot
from .store import Store, utc_now
from .task_contract import review_contract
from .task_intake import contribution_gate

MAX_CHECKPOINT_BYTES = 100_000


class TaskConflict(RuntimeError):
    """The requested task revision, binding, or snapshot is no longer current."""


class ExternalTasks:
    def __init__(self, config: AppConfig, *, github: GitHubClient | None = None):
        self.config = config
        self.registry = ProjectRegistry(config.state_dir / "projects.sqlite3")
        self.path = config.state_dir / "reposteward.sqlite3"
        self._github = github

    def _store(self, *, write: bool = False) -> Store:
        return Store(self.path, read_only=not write)

    def _policy(self, repository: str):
        policy = self.config.repositories.get(repository.casefold())
        if policy is None or not policy.enabled:
            raise PolicyError("linked project needs an enabled repository policy")
        return policy

    def _snapshot(self, root: Path, repository: str) -> dict[str, Any]:
        return workspace_snapshot(
            root,
            trusted_sensitive_paths=self.config.safety.tracked_sensitive_paths_for(
                repository
            ),
        )

    def start(
        self,
        path: Path,
        *,
        issue_number: int,
        reviewed_by: str,
        contract_proposal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if type(issue_number) is not int or issue_number < 1:
            raise ValueError("task needs a positive Issue number")
        if (
            not reviewed_by
            or reviewed_by.casefold() != self.config.github.login.casefold()
        ):
            raise PolicyError("--reviewed-by must match the configured GitHub login")
        linked = self.registry.inspect(path)
        project, binding = linked["project"], linked["binding"]
        api_host = urlsplit(self.config.github.api_url).hostname
        expected_host = "github.com" if api_host == "api.github.com" else api_host
        if project["host"] != expected_host:
            raise PolicyError("project host does not match the configured GitHub API")
        policy = self._policy(project["repository"])
        github = self._github or GitHubClient(self.config.github)
        if github.authenticated_login().casefold() != reviewed_by.casefold():
            raise PolicyError("authenticated identity differs from the task reviewer")
        issue = github.issue(policy.name, issue_number)
        repository = github.repository(policy.name)
        candidate = score_issue(issue, repository, policy, self.config)
        gate = contribution_gate(self.config, github, policy, issue_number, issue=issue)
        if candidate.blockers or not gate["submission_ready"]:
            raise PolicyError(
                "Issue contribution gates block external development: "
                + "; ".join(
                    candidate.blockers
                    or ("assignment, approval, state or competing work",)
                )
            )
        root = Path(binding["root"])
        snapshot = self._snapshot(root, policy.name)
        if snapshot["branch"] == repository.default_branch or not snapshot["branch"]:
            raise PolicyError("external development requires a separate feature branch")
        base = local_git(
            root,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{repository.default_branch}^{{commit}}",
        )
        if github.branch_head_sha(policy.name, repository.default_branch) != base:
            raise TaskConflict(
                "local origin base is stale; fetch it before starting the task"
            )
        contract = (
            review_contract(issue, contract_proposal, reviewed_by=reviewed_by)
            if contract_proposal is not None
            else None
        )
        store = self._store(write=True)
        with store.atomic():
            work = store.ensure_work_item(
                policy.name,
                kind="github_issue",
                external_id=str(issue_number),
                title=issue.title,
                payload={
                    "url": issue.url,
                    "updated_at": issue.updated_at,
                    "state": issue.state,
                },
            )
            run_id = store.start_run(policy.name, issue_number, "external")
            previous = self._latest_local_checkpoint(store, work["id"])
            context = build_context_pack(
                candidate,
                policy,
                work_item_id=work["id"],
                run_id=run_id,
                worktree=root,
                base_commit=base,
                harness="external-agent",
                model="",
                task_contract=contract,
                previous_checkpoint=previous,
            )
            context, _ = fit_context(context, self.config.context.prepare_max_tokens)
            if self._snapshot(root, policy.name)["digest"] != snapshot["digest"]:
                raise TaskConflict("workspace changed while freezing task context")
            store.upsert_candidate(candidate)
            store.save_context_run(
                pack_id=context.id,
                work_item_id=work["id"],
                run_id=run_id,
                schema_version=context.schema_version,
                source_digest=context.source_digest,
                base_commit=base,
                payload=context.to_dict(),
                harness="external-agent",
            )
            checkpoint = running_checkpoint(
                context,
                head_commit=snapshot["head"],
                completed=(),
                next_action="develop_in_existing_agent",
            )
            checkpoint["remaining"] = list(
                context.task_contract.acceptance_criteria
            ) or ["Satisfy the complete source-bound task requirements."]
            if previous is not None:
                checkpoint["remaining"] = list(
                    dict.fromkeys(
                        (*checkpoint["remaining"], *previous.get("remaining", ()))
                    )
                )
                checkpoint["decisions"] = list(previous.get("decisions", ()))
                checkpoint["blockers"] = list(previous.get("blockers", ()))
            checkpoint["evidence"] = [
                {
                    "kind": "development_snapshot",
                    "locator": f"task:{run_id}:snapshot:0",
                    "status": "observed",
                    "digest": snapshot["digest"],
                    "summary": "Local code observation; development is not yet verified.",
                }
            ]
            stored = store.save_checkpoint(
                work_item_id=work["id"],
                run_id=run_id,
                context_pack_id=context.id,
                status="running",
                payload=checkpoint,
            )
            now = utc_now()
            with store._connection() as db:
                db.execute(
                    """INSERT INTO external_task_runs VALUES (?,?,?,?,?,0,?,?,?,?,?)""",
                    (
                        run_id,
                        project["id"],
                        binding["id"],
                        binding["root"],
                        binding["fingerprint"],
                        json.dumps(snapshot),
                        repository_policy_digest(policy),
                        base,
                        now,
                        now,
                    ),
                )
                source = {
                    key: getattr(issue, key)
                    for key in ("repository", "number", "title", "body", "updated_at")
                }
                payload = json.dumps(
                    source, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
                source_digest = canonical_digest(source)
                db.execute(
                    "INSERT OR IGNORE INTO content_blobs(digest,payload,size_bytes,created_at) VALUES (?,?,?,?)",
                    (source_digest, payload, len(payload), now),
                )
                db.execute(
                    "INSERT INTO external_task_sources VALUES (?,?,?)",
                    (run_id, "issue", source_digest),
                )
                db.execute(
                    "INSERT INTO external_task_checkpoints VALUES (?,0,?,?,?,?,?)",
                    (
                        run_id,
                        stored["id"],
                        "start",
                        canonical_digest(
                            {"snapshot": snapshot["digest"], "source": source_digest}
                        ),
                        json.dumps(snapshot),
                        now,
                    ),
                )
            store.update_run(
                run_id,
                status="running",
                stage="external",
                worktree=str(root),
                details={
                    "source": "external-development",
                    "worktree": str(root),
                    "base_branch": repository.default_branch,
                    "base_commit": base,
                    "branch": snapshot["branch"],
                    "commit_sha": snapshot["head"],
                    "project_id": project["id"],
                    "binding_id": binding["id"],
                    "external_revision": 0,
                    "snapshot_digest": snapshot["digest"],
                    "issue_gate": gate,
                    "task_reviewed_by": reviewed_by,
                    "claim_trust": "agent_unverified",
                    "public_write": False,
                },
            )
        return self.inspect(run_id)

    @staticmethod
    def _latest_local_checkpoint(
        store: Store, work_item_id: str
    ) -> dict[str, Any] | None:
        with store._connection() as db:
            row = db.execute(
                """SELECT c.payload FROM external_task_runs e
                JOIN harness_runs h ON h.run_id=e.run_id
                JOIN external_task_checkpoints x ON x.run_id=e.run_id AND x.revision=e.checkpoint_revision
                JOIN checkpoints c ON c.id=x.checkpoint_id
                WHERE h.work_item_id=? ORDER BY e.updated_at DESC,e.created_at DESC,e.run_id DESC LIMIT 1""",
                (work_item_id,),
            ).fetchone()
        if row is not None:
            return json.loads(row["payload"])
        return store.latest_checkpoint_for_work_item(work_item_id)

    def _record(self, store: Store, run_id: str) -> dict[str, Any]:
        if not re.fullmatch("[0-9a-f]{32}", run_id):
            raise ValueError("invalid external task run ID")
        with store._connection() as db:
            row = db.execute(
                "SELECT * FROM external_task_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError("external task run not found")
        return {**dict(row), "snapshot": json.loads(row["snapshot"])}

    def inspect(self, run_id: str, *, live: bool = False) -> dict[str, Any]:
        store = self._store()
        record = self._record(store, run_id)
        bundle = store.context_bundle(run_id)
        run = store.run(run_id)
        if bundle is None or run is None:
            raise TaskConflict("external task context is unavailable")
        validity = []
        current = None
        if live:
            linked = self.registry.inspect(Path(record["workspace_root"]))
            if (
                linked["project"]["id"] != record["project_id"]
                or linked["binding"]["fingerprint"] != record["binding_fingerprint"]
            ):
                raise TaskConflict("task workspace binding changed")
            policy = self._policy(run["repository"])
            current = self._snapshot(Path(record["workspace_root"]), policy.name)
            if repository_policy_digest(policy) != record["policy_digest"]:
                validity.append("policy_changed")
            base = local_git(
                Path(record["workspace_root"]),
                "rev-parse",
                "--verify",
                f"refs/remotes/origin/{run['details']['base_branch']}^{{commit}}",
            )
            if base != record["base_commit"]:
                validity.append("base_changed")
            if current["digest"] != record["snapshot"]["digest"]:
                validity.append("workspace_changed_since_checkpoint")
        return {
            "schema_version": 1,
            "run_id": run_id,
            "work_item_id": bundle["work_item"]["id"],
            "project_id": record["project_id"],
            "binding_id": record["binding_id"],
            "revision": record["checkpoint_revision"],
            "status": run["status"],
            "snapshot": snapshot_summary(record["snapshot"]),
            "current_snapshot": snapshot_summary(current) if current else None,
            "validity": validity,
            "remote_freshness": "not_refreshed",
            "claim_trust": "agent_unverified",
            "context_pack": bundle["context_pack"],
            "checkpoint": bundle["checkpoint"],
            "public_write": False,
        }

    def checkpoint(
        self,
        run_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        expected_snapshot: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected revision must be non-negative")
        if (
            not re.fullmatch("[A-Za-z0-9_.:-]{1,128}", idempotency_key)
            or idempotency_key == "start"
        ):
            raise ValueError("invalid checkpoint idempotency key")
        if not re.fullmatch("[a-f0-9]{64}", expected_snapshot):
            raise ValueError("expected snapshot must be a SHA-256 digest")
        allowed = {
            "completed",
            "remaining",
            "decisions",
            "next_action",
            "blockers",
            "notes",
            "tests_observed",
            "risks",
        }
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError(
                "checkpoint contains unsupported fields or authority claims"
            )
        if estimate_tokens(payload) > MAX_CHECKPOINT_BYTES:
            raise ValueError("checkpoint exceeds the input limit")
        if (
            not isinstance(payload.get("next_action"), str)
            or not payload["next_action"].strip()
        ):
            raise ValueError("checkpoint requires a concrete next action")
        for name in ("completed", "remaining", "blockers", "tests_observed", "risks"):
            values = payload.get(name, [])
            if (
                not isinstance(values, list)
                or len(values) > 128
                or any(not isinstance(v, str) or len(v) > 2000 for v in values)
            ):
                raise ValueError(f"invalid checkpoint {name}")
        request_digest = canonical_digest(
            {
                "expected_revision": expected_revision,
                "snapshot": expected_snapshot,
                "payload": payload,
            }
        )
        store = self._store(write=True)
        with store.atomic():
            record = self._record(store, run_id)
            with store._connection() as db:
                existing = db.execute(
                    "SELECT * FROM external_task_checkpoints WHERE run_id=? AND idempotency_key=?",
                    (run_id, idempotency_key),
                ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise TaskConflict(
                        "idempotency key already belongs to a different checkpoint request"
                    )
                return {
                    "run_id": run_id,
                    "revision": existing["revision"],
                    "checkpoint_id": existing["checkpoint_id"],
                    "snapshot": snapshot_summary(json.loads(existing["snapshot"])),
                    "idempotent": True,
                    "public_write": False,
                }
            if record["checkpoint_revision"] != expected_revision:
                raise TaskConflict(
                    "checkpoint revision changed; reload the current task"
                )
            observed = self.inspect(run_id, live=True)
            if any(
                reason != "workspace_changed_since_checkpoint"
                for reason in observed["validity"]
            ):
                raise TaskConflict(
                    "task baseline or policy changed; start a refreshed attempt"
                )
            if observed["current_snapshot"]["digest"] != expected_snapshot:
                raise TaskConflict("workspace differs from the expected snapshot")
            run = store.run(run_id)
            if run["status"] != "running" or run["stage"] != "external":
                raise TaskConflict(
                    "external attempt no longer accepts development checkpoints"
                )
            snapshot = self._snapshot(Path(record["workspace_root"]), run["repository"])
            if snapshot["digest"] != expected_snapshot:
                raise TaskConflict("workspace changed before saving the checkpoint")
            bundle = store.context_bundle(run_id)
            previous = bundle["checkpoint"] or {}
            checkpoint = {
                "schema_version": 1,
                "work_item_id": bundle["work_item"]["id"],
                "run_id": run_id,
                "context_pack_id": bundle["context_pack"]["id"],
                "status": "running",
                "head_commit": snapshot["head"],
                "completed": payload.get("completed", previous.get("completed", [])),
                "remaining": payload.get("remaining", previous.get("remaining", [])),
                "next_action": payload["next_action"],
                "blockers": payload.get("blockers", previous.get("blockers", [])),
                "decisions": payload.get("decisions", previous.get("decisions", [])),
                "implementation_notes": payload.get(
                    "notes", previous.get("implementation_notes", "")
                ),
                "tests_observed": payload.get(
                    "tests_observed", previous.get("tests_observed", [])
                ),
                "risks": payload.get("risks", previous.get("risks", [])),
                "evidence": [
                    {
                        "kind": "development_snapshot",
                        "locator": f"task:{run_id}:snapshot:{expected_revision + 1}",
                        "status": "observed",
                        "digest": snapshot["digest"],
                        "summary": "Code observed by RepoSteward; Agent completion and tests remain unverified.",
                    }
                ],
            }
            saved = store.save_checkpoint(
                work_item_id=bundle["work_item"]["id"],
                run_id=run_id,
                context_pack_id=bundle["context_pack"]["id"],
                status="running",
                payload=checkpoint,
            )
            revision = expected_revision + 1
            now = utc_now()
            with store._connection() as db:
                updated = db.execute(
                    """UPDATE external_task_runs SET checkpoint_revision=?,snapshot=?,updated_at=?
                    WHERE run_id=? AND checkpoint_revision=?""",
                    (revision, json.dumps(snapshot), now, run_id, expected_revision),
                )
                if updated.rowcount != 1:
                    raise TaskConflict("checkpoint revision changed concurrently")
                db.execute(
                    "INSERT INTO external_task_checkpoints VALUES (?,?,?,?,?,?,?)",
                    (
                        run_id,
                        revision,
                        saved["id"],
                        idempotency_key,
                        request_digest,
                        json.dumps(snapshot),
                        now,
                    ),
                )
            store.update_run(
                run_id,
                status="running",
                stage="external",
                details={
                    **run["details"],
                    "commit_sha": snapshot["head"],
                    "external_revision": revision,
                    "snapshot_digest": snapshot["digest"],
                },
            )
        return {
            "run_id": run_id,
            "revision": revision,
            "checkpoint_id": saved["id"],
            "snapshot": snapshot_summary(snapshot),
            "idempotent": False,
            "public_write": False,
        }

    def context(
        self, run_id: str, *, budget: int = 24_000, live: bool = False
    ) -> dict[str, Any]:
        if type(budget) is not int or not 512 <= budget <= 100_000:
            raise ValueError("context budget must be between 512 and 100000")
        report = self.inspect(run_id, live=live)
        pack = report.pop("context_pack")
        checkpoint = report.pop("checkpoint")
        # Open work and decisions come directly from the current ledger, never from a summary of a summary.
        mandatory = {
            **report,
            "task": {"title": pack["task"]["title"], "url": pack["task"]["url"]},
            "contract": pack.get("task_contract"),
            "project": pack["project"],
            "sources": pack["sources"],
            "open_work": checkpoint.get("remaining", []) if checkpoint else [],
            "decisions": checkpoint.get("decisions", []) if checkpoint else [],
            "next_action": checkpoint.get("next_action", "") if checkpoint else "",
            "blockers": checkpoint.get("blockers", []) if checkpoint else [],
            "evidence": checkpoint.get("evidence", []) if checkpoint else [],
            "agent_claims": {
                "completed": checkpoint.get("completed", []) if checkpoint else [],
                "notes": checkpoint.get("implementation_notes", "")
                if checkpoint
                else "",
                "tests_observed": checkpoint.get("tests_observed", [])
                if checkpoint
                else [],
                "trust": "agent_unverified",
            },
            "coverage": list(pack.get("coverage", [])),
            "budget": budget,
            "estimated_tokens": 0,
        }
        for _ in range(8):
            size = estimate_tokens(mandatory)
            if mandatory["estimated_tokens"] == size:
                break
            mandatory["estimated_tokens"] = size
        if estimate_tokens(mandatory) > budget:
            claims = mandatory["agent_claims"]
            count = len(json.dumps(claims, ensure_ascii=False, sort_keys=True))
            mandatory["agent_claims"] = {
                "trust": "agent_unverified",
                "omitted": count,
                "evidence_id": f"checkpoint:{run_id}:{report['revision']}",
            }
            mandatory["coverage"].append(
                {
                    "field": "agent_claims",
                    "reason": "context_budget",
                    "omitted": count,
                    "unit": "serialized_characters",
                    "locator": f"checkpoint:{run_id}:{report['revision']}",
                    "digest": canonical_digest(checkpoint),
                }
            )
            for _ in range(8):
                size = estimate_tokens(mandatory)
                if mandatory["estimated_tokens"] == size:
                    break
                mandatory["estimated_tokens"] = size
        if estimate_tokens(mandatory) > budget:
            raise ContextBudgetError(
                "task contract, open work, decisions and current evidence exceed the context budget"
            )
        return mandatory

    def current(self, path: Path, *, budget: int = 24_000) -> dict[str, Any]:
        linked = self.registry.inspect(path)
        store = self._store()
        with store._connection() as db:
            row = db.execute(
                """SELECT e.run_id FROM external_task_runs e JOIN runs r ON r.id=e.run_id
                WHERE e.binding_id=? AND e.project_id=? AND r.stage='external' AND r.status='running'
                ORDER BY e.updated_at DESC,e.created_at DESC,e.rowid DESC LIMIT 1""",
                (linked["binding"]["id"], linked["project"]["id"]),
            ).fetchone()
        if row is None:
            raise KeyError(
                "no active external task; start a reviewed Issue in this workspace"
            )
        return self.context(row["run_id"], budget=budget, live=True)
