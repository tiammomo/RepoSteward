from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .agent import CodexAgent
from .config import AppConfig, RepositoryPolicy
from .discovery import score_issue
from .github import GitHubClient, GitHubError, resolve_token
from .models import AgentResult, Candidate
from .policy import PolicyError, conventional_scope, enforce_change_policy
from .store import Store
from .verifier import DockerVerifier
from .workspace import WorkspaceManager


class Pipeline:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.store = Store(config.state_dir / "starfix.sqlite3")
        self.github = GitHubClient(config.github)
        self.agent = CodexAgent(config.agent)
        self.verifier = DockerVerifier(config)
        self.workspaces = WorkspaceManager(config)

    def policy(self, repository: str) -> RepositoryPolicy:
        try:
            return self.config.repositories[repository.casefold()]
        except KeyError as exc:
            raise PolicyError(f"repository is not allowlisted: {repository}") from exc

    def ensure_candidate(self, repository: str, issue_number: int) -> Candidate:
        policy = self.policy(repository)
        current_issue = self.github.issue(policy.name, issue_number)
        repository_info = self.github.repository(policy.name)
        candidate = score_issue(current_issue, repository_info, policy, self.config)
        self.store.upsert_candidate(candidate)
        return candidate

    def gate_status(self, repository: str, issue_number: int) -> dict[str, Any]:
        policy = self.policy(repository)
        issue = self.github.issue(policy.name, issue_number)
        assigned = self.config.github.login.casefold() in {
            login.casefold() for login in issue.assignees
        }
        approval = True
        if policy.maintainer_approval:
            approval = self.github.has_maintainer_approval(
                policy.name,
                issue_number,
                policy.maintainer_approval,
                policy.allowed_approver_associations,
            )
        competing_work = ()
        if policy.require_no_competing_work:
            competing_work = self.github.competing_work(
                policy.name,
                issue_number,
                own_login=self.config.github.login,
            )
        return {
            "repository": policy.name,
            "issue": issue_number,
            "state": issue.state,
            "assignees": list(issue.assignees),
            "assignment_required": policy.require_assignment_before_submit,
            "assigned_to_login": assigned,
            "approval_command": policy.maintainer_approval,
            "maintainer_approval": approval,
            "competing_work_required_absent": policy.require_no_competing_work,
            "competing_work": [asdict(value) for value in competing_work],
            "submission_ready": (
                issue.state == "open"
                and (not policy.require_assignment_before_submit or assigned)
                and approval
                and not competing_work
            ),
        }

    def _ensure_no_competing_work(
        self, policy: RepositoryPolicy, issue_number: int
    ) -> None:
        if not policy.require_no_competing_work:
            return
        conflicts = self.github.competing_work(
            policy.name,
            issue_number,
            own_login=self.config.github.login,
        )
        if conflicts:
            detail = "; ".join(
                f"{value.kind} by {value.actor}: {value.url}" for value in conflicts
            )
            raise PolicyError(f"issue has competing work: {detail}")

    @staticmethod
    def _validate_contribution_contract(
        worktree: Path, policy: RepositoryPolicy
    ) -> None:
        missing = [
            value
            for value in policy.required_contribution_files
            if not (worktree / value).is_file()
        ]
        if missing:
            raise PolicyError(
                "required contribution guidance is missing: " + ", ".join(missing)
            )
        if policy.pull_request_template_path:
            template = worktree / policy.pull_request_template_path
            if not template.is_file():
                raise PolicyError(f"pull request template is missing: {template}")
            digest = hashlib.sha256(template.read_bytes()).hexdigest()
            if digest != policy.pull_request_template_sha256:
                raise PolicyError(
                    f"{policy.name} changed {policy.pull_request_template_path}; "
                    "update the repository adapter before preparing or submitting"
                )

    def prepare(self, repository: str, issue_number: int) -> dict[str, Any]:
        policy = self.policy(repository)
        candidate = self.ensure_candidate(repository, issue_number)
        if candidate.blockers:
            raise PolicyError("; ".join(candidate.blockers))
        if candidate.issue.state != "open":
            raise PolicyError("issue is not open")
        self._ensure_no_competing_work(policy, issue_number)

        run_id = self.store.start_run(policy.name, issue_number, "clone")
        worktree: Path | None = None
        try:
            worktree = self.workspaces.clone(candidate)
            self._validate_contribution_contract(worktree, policy)
            self.store.update_run(
                run_id, status="running", stage="agent", worktree=str(worktree)
            )
            run_dir = self.config.state_dir / "runs" / run_id
            agent_result = self.agent.run(worktree, run_dir, candidate, policy)
            self.store.update_run(run_id, status="running", stage="verification")
            verification = self.verifier.verify(worktree, policy, agent_result)
            summary = enforce_change_policy(worktree, verification, policy, self.config)
            scope = conventional_scope(agent_result.pr_title, policy.default_scope)
            branch = self.workspaces.create_branch(worktree, candidate, policy, scope)
            commit_sha = self.workspaces.commit(worktree, agent_result.pr_title)
            details = {
                "worktree": str(worktree),
                "base_branch": candidate.repository.default_branch,
                "branch": branch,
                "commit_sha": commit_sha,
                "changed_files": list(summary.files),
                "added_lines": summary.added_lines,
                "deleted_lines": summary.deleted_lines,
                "agent_result": asdict(agent_result),
                "verification": asdict(verification),
            }
            self.store.update_run(
                run_id,
                status="ready",
                stage="review",
                worktree=str(worktree),
                details=details,
            )
            self.store.set_candidate_status(policy.name, issue_number, "ready")
            return {"run_id": run_id, **details}
        except Exception as exc:
            self.store.update_run(
                run_id,
                status="failed",
                stage="failed",
                worktree=str(worktree or ""),
                details={"error": str(exc)},
            )
            try:
                self.store.set_candidate_status(policy.name, issue_number, "failed")
            except KeyError:
                pass
            raise

    def adopt(
        self,
        repository: str,
        issue_number: int,
        *,
        worktree: Path,
        summary_text: str,
        implementation_notes: str,
        verification_commands: tuple[str, ...],
    ) -> dict[str, Any]:
        """Verify and register a clean, existing local commit for later review."""
        policy = self.policy(repository)
        candidate = self.ensure_candidate(repository, issue_number)
        if candidate.blockers:
            raise PolicyError("; ".join(candidate.blockers))
        self._ensure_no_competing_work(policy, issue_number)
        worktree = worktree.expanduser().resolve()
        if not (worktree / ".git").exists():
            raise PolicyError(f"not a Git worktree: {worktree}")
        self._validate_contribution_contract(worktree, policy)
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if dirty:
            raise PolicyError("existing worktree has uncommitted changes")
        commit_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        title = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        conventional_scope(title, policy.default_scope)
        base_ref = f"origin/{candidate.repository.default_branch}"
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not branch.startswith(f"{self.config.github.login}/"):
            raise PolicyError(
                f"branch must start with {self.config.github.login!r}: {branch}"
            )
        run_id = self.store.start_run(policy.name, issue_number, "verification")
        agent_result = AgentResult(
            summary=summary_text,
            pr_title=title,
            implementation_notes=implementation_notes,
            verification_commands=verification_commands,
            tests_observed=verification_commands,
            risks=(),
        )
        try:
            verification = self.verifier.verify(worktree, policy, agent_result)
            diff = enforce_change_policy(
                worktree,
                verification,
                policy,
                self.config,
                base_ref=base_ref,
            )
            details = {
                "worktree": str(worktree),
                "base_branch": candidate.repository.default_branch,
                "branch": branch,
                "commit_sha": commit_sha,
                "changed_files": list(diff.files),
                "added_lines": diff.added_lines,
                "deleted_lines": diff.deleted_lines,
                "agent_result": asdict(agent_result),
                "verification": asdict(verification),
            }
            self.store.update_run(
                run_id,
                status="ready",
                stage="review",
                worktree=str(worktree),
                details=details,
            )
            self.store.set_candidate_status(policy.name, issue_number, "ready")
            return {"run_id": run_id, **details}
        except Exception as exc:
            self.store.update_run(
                run_id,
                status="failed",
                stage="failed",
                worktree=str(worktree),
                details={"error": str(exc)},
            )
            raise

    def submit(
        self, repository: str, issue_number: int, *, reviewed_by: str
    ) -> dict[str, Any]:
        policy = self.policy(repository)
        if os.environ.get("STARFIX_ENABLE_SUBMIT") != "1":
            raise PolicyError(
                "submission is disabled; set STARFIX_ENABLE_SUBMIT=1 for this command"
            )
        if reviewed_by.casefold() != self.config.github.login.casefold():
            raise PolicyError(
                f"--reviewed-by must attest the configured account {self.config.github.login!r}"
            )
        token = resolve_token(self.config.github, required=True)
        authenticated = GitHubClient(self.config.github, token).authenticated_login()
        if authenticated.casefold() != self.config.github.login.casefold():
            raise GitHubError(
                f"token belongs to {authenticated!r}, expected {self.config.github.login!r}"
            )
        if (
            self.store.recent_submission_count()
            >= self.config.safety.max_daily_submissions
        ):
            raise PolicyError("daily pull-request submission limit reached")
        gate = self.gate_status(policy.name, issue_number)
        if not gate["submission_ready"]:
            raise PolicyError(f"repository contribution gate is not satisfied: {gate}")

        run = self.store.latest_run(policy.name, issue_number)
        if run is None or run["status"] != "ready":
            raise PolicyError(
                "no verified change is ready for human review and submission"
            )
        details = run["details"]
        worktree = Path(details["worktree"])
        if not (worktree / ".git").exists():
            raise PolicyError(f"prepared worktree is missing: {worktree}")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if head != details["commit_sha"]:
            raise PolicyError("prepared worktree HEAD changed after verification")
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if dirty:
            raise PolicyError("prepared worktree has uncommitted changes")

        client = GitHubClient(self.config.github, token)
        fork = client.ensure_fork(policy.name, self.config.github.login)
        self._validate_contribution_contract(worktree, policy)
        self.workspaces.push(worktree, fork, details["branch"])
        body = self._pull_request_body(
            issue_number, details, reviewed_by, policy=policy
        )
        pull_request = client.create_pull_request(
            policy.name,
            owner=self.config.github.login,
            branch=details["branch"],
            base=details["base_branch"],
            title=details["agent_result"]["pr_title"],
            body=body,
            draft=self.config.safety.draft_pull_requests,
        )
        self.store.update_run(
            run["id"],
            status="submitted",
            stage="pull_request",
            details={**details, "pr_url": pull_request.url},
        )
        self.store.set_candidate_status(policy.name, issue_number, "submitted")
        self.store.record_submission(policy.name, issue_number, pull_request.url)
        return {
            "pr_url": pull_request.url,
            "pr_number": pull_request.number,
            "draft": pull_request.draft,
            "branch": details["branch"],
        }

    @staticmethod
    def _pull_request_body(
        issue_number: int,
        details: dict[str, Any],
        reviewed_by: str,
        *,
        policy: RepositoryPolicy | None = None,
    ) -> str:
        if policy is not None and policy.pull_request_body_style == "deer-flow":
            return Pipeline._deer_flow_pull_request_body(
                issue_number, details, reviewed_by
            )
        agent = details["agent_result"]
        commands = agent["verification_commands"]
        verification_text = ", ".join(f"`{value}`" for value in commands)
        risk_text = "\n".join(f"- {value}" for value in agent["risks"])
        if not risk_text:
            risk_text = "- No known behavior changes outside the reported bug."
        return f"""Closes #{issue_number}

{agent["summary"]}

---

{agent["implementation_notes"]}

Verified with {verification_text}.

Areas for careful review:
{risk_text}

AI assistance: OpenAI Codex CLI was used to investigate the issue, implement the
change, and draft tests. `{reviewed_by}` reviewed the final diff, understands the
change, and takes responsibility for this contribution.
"""

    @staticmethod
    def _deer_flow_pull_request_body(
        issue_number: int, details: dict[str, Any], reviewed_by: str
    ) -> str:
        agent = details["agent_result"]
        files = tuple(str(value) for value in details.get("changed_files", ()))
        frontend = any(value.startswith("frontend/") for value in files)
        backend_api = any(value.startswith("backend/app/") for value in files)
        agents = any(
            value.startswith("backend/packages/harness/deerflow/agents/")
            or value.endswith("langgraph.json")
            for value in files
        )
        sandbox = any(
            value.startswith("docker/") or "sandbox" in value.casefold()
            for value in files
        )
        skills = any(value.startswith("skills/") for value in files)
        dependencies = any(
            value.endswith(
                ("pyproject.toml", "package.json", "uv.lock", "pnpm-lock.yaml")
            )
            for value in files
        )
        docs_tests_only = bool(files) and all(
            value.startswith(("docs/", "tests/", "frontend/tests/", "backend/tests/"))
            or "/tests/" in value
            or value.casefold().endswith((".md", ".mdx"))
            for value in files
        )

        def checked(value: bool) -> str:
            return "x" if value else " "

        validation = (
            "\n".join(f"- `{value}`" for value in agent["verification_commands"])
            or "- No executable validation command was recorded."
        )
        regression_tests = [value for value in files if "test" in value.casefold()]
        regression_path = ", ".join(f"`{value}`" for value in regression_tests)
        if not regression_path:
            regression_path = "No dedicated regression-test path was recorded."
        screenshot_note = (
            "Draft PR: attach entry-point and before/after screenshots before marking "
            "ready for review."
            if frontend
            else "Not applicable — no frontend UI files changed."
        )
        return f"""Fixes #{issue_number}

## Why

{agent["summary"]}

## What changed

{agent["implementation_notes"]}

## Surface area

- [{checked(frontend)}] **Frontend UI** — page / component / setting / interaction under `frontend/`
- [{checked(backend_api)}] **Backend API** — endpoint / SSE event / request-response shape under `backend/app`
- [{checked(agents)}] **Agents / LangGraph** — agent node, graph wiring, `langgraph.json`, or prompt change
- [{checked(sandbox)}] **Sandbox** — `docker/` or sandboxed execution
- [{checked(skills)}] **Skills** — change under `skills/`
- [{checked(dependencies)}] **Dependencies** — new/upgraded dependency
- [ ] **Default behavior change** — changes existing behavior without the user opting in
- [{checked(docs_tests_only)}] **Docs / tests / CI only** — no runtime behavior change

## Screenshots / Recording

{screenshot_note}

## Bug fix verification

- Test path that reproduces the bug: {regression_path}
- Red on `main`, green on this branch: confirm from the recorded review evidence before marking this draft ready.

## Validation

{validation}

## AI assistance

**Tool(s) used:** OpenAI Codex CLI

**How you used it:** Codex investigated the issue, implemented the focused change, and drafted regression tests. `{reviewed_by}` then reviewed the final diff and validation evidence.

- [x] I've read and understand every line of this change and take responsibility for it — it's not unreviewed AI output.
"""
