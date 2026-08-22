from __future__ import annotations

import os
import subprocess
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from reposteward.config import RepositoryPolicy
from reposteward.github import GitHubError, PullRequest
from reposteward.pipeline import Pipeline
from reposteward.policy import PolicyError
from reposteward.store import RunLease, StoreError
from reposteward.workspace import WorkspaceError


def _pull(number: int, *, author: str = "alice", draft: bool = False) -> PullRequest:
    return PullRequest(
        number=number,
        url=f"https://github.com/owner/repo/pull/{number}",
        state="open",
        draft=draft,
        author=author,
        head_owner="owner",
        head_branch=f"alice/feat/change-{number}",
        head_sha=str(number) * 40,
        base_branch="main",
    )


class _SubmissionStore:
    def __init__(self, details: dict) -> None:
        self.details = details
        self.updated: dict | None = None
        self.publication: list[dict] = []
        self.status = "ready"
        self.fail_local_update_once = False

    def latest_run(self, _repository: str, _issue_number: int) -> dict:
        return {"id": "run-1", "status": self.status, "details": self.details}

    @staticmethod
    def acquire_run_lease(scope: str, *, owner: str, ttl_seconds: int) -> RunLease:
        return RunLease(scope, owner, 1, "2999-01-01T00:00:00+00:00")

    @staticmethod
    def renew_run_lease(lease: RunLease, *, ttl_seconds: int) -> RunLease:
        return lease

    @staticmethod
    def bind_run_lease(_lease: RunLease):
        return nullcontext()

    @staticmethod
    def validate_run_lease(_lease: RunLease) -> None:
        return None

    @staticmethod
    def release_run_lease(_lease: RunLease) -> None:
        return None

    def update_run(self, _run_id: str, **values) -> None:
        if self.fail_local_update_once:
            self.fail_local_update_once = False
            raise ValueError("simulated local interruption")
        self.updated = values
        self.status = str(values["status"])
        self.details = dict(values["details"])

    @staticmethod
    def set_candidate_status(
        _repository: str, _issue_number: int, _status: str
    ) -> None:
        return None

    @staticmethod
    def record_submission(_repository: str, _issue_number: int, _url: str) -> None:
        return None

    @staticmethod
    def context_bundle(_run_id: str) -> None:
        return None

    def append_publication_attempt(self, **values) -> dict:
        record = {**values, "id": f"event-{len(self.publication) + 1}"}
        self.publication.append(record)
        return record

    def incomplete_publication_attempts(
        self, run_id: str, *, limit: int = 100
    ) -> list[dict]:
        completed = {
            value["step_id"]
            for value in self.publication
            if value["run_id"] == run_id and value["stage"] == "completed"
        }
        return [
            value
            for value in self.publication
            if value["run_id"] == run_id
            and value["stage"] == "applying"
            and value["step_id"] not in completed
        ][:limit]

    def publication_attempts(self, run_id: str, *, limit: int = 100) -> list[dict]:
        return [value for value in self.publication if value["run_id"] == run_id][
            :limit
        ]

    def publication_action_completed(
        self, run_id: str, *, action: str, target_pull_number: int = 0
    ) -> bool:
        return any(
            value["run_id"] == run_id
            and value["action"] == action
            and value["stage"] == "completed"
            and value["outcome"] in {"succeeded", "reconciled", "already_current"}
            and (
                not target_pull_number
                or value["target_pull_number"] == target_pull_number
            )
            for value in self.publication
        )


class _SubmissionGitHub:
    def __init__(
        self,
        *,
        existing: PullRequest | None = None,
        closed: PullRequest | None = None,
        pulls: tuple[PullRequest, ...] = (),
        open_error: Exception | None = None,
    ) -> None:
        self.existing = existing
        self.closed = closed
        self.pulls = pulls
        self.open_error = open_error
        self.open_calls = 0
        self.create_calls = 0
        self.repository_calls = 0
        self.reopen_calls = 0
        self.close_calls = 0
        self.create_error_after_write = False
        self.reopen_error_after_write = False
        self.push_error_after_write = False
        self.remote_head = (
            existing.head_sha
            if existing is not None
            else closed.head_sha
            if closed
            else ""
        )
        self.new_head = ""

    @staticmethod
    def authenticated_login() -> str:
        return "alice"

    def existing_pull_request(
        self, _repository: str, *, owner: str, branch: str
    ) -> PullRequest | None:
        self.requested_head = (owner, branch)
        return self.existing

    def open_pull_requests(self, _repository: str) -> tuple[PullRequest, ...]:
        self.open_calls += 1
        if self.open_error is not None:
            raise self.open_error
        return self.pulls

    def repository(self, _repository: str) -> SimpleNamespace:
        self.repository_calls += 1
        return SimpleNamespace(can_push=True)

    def create_pull_request(self, _repository: str, **kwargs) -> PullRequest:
        self.create_calls += 1
        self.existing = PullRequest(
            number=99,
            url="https://github.com/owner/repo/pull/99",
            state="open",
            draft=bool(kwargs["draft"]),
            author="alice",
            head_owner=str(kwargs["owner"]),
            head_branch=str(kwargs["branch"]),
            head_sha=self.new_head,
            base_branch=str(kwargs["base"]),
        )
        if self.create_error_after_write:
            raise GitHubError("simulated create timeout")
        return self.existing

    def pull_request(self, _repository: str, _number: int) -> PullRequest:
        pull = self.closed or self.existing
        assert pull is not None
        return pull

    def reopen_pull_request(self, *_args, **_kwargs) -> PullRequest:
        self.reopen_calls += 1
        assert self.closed is not None
        self.closed = replace(self.closed, state="open")
        if self.reopen_error_after_write:
            raise GitHubError("simulated reopen timeout")
        return self.closed

    def close_pull_request(self, *_args, **_kwargs) -> PullRequest:
        self.close_calls += 1
        assert self.closed is not None
        self.closed = replace(self.closed, state="closed")
        return self.closed

    def branch_head_sha(self, _repository: str, _branch: str) -> str:
        return self.remote_head

    def mark_pushed(self, head_sha: str) -> None:
        self.remote_head = head_sha
        if self.existing is not None:
            self.existing = replace(self.existing, head_sha=head_sha)
        if self.closed is not None:
            self.closed = replace(self.closed, head_sha=head_sha)


class SubmissionCapacityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.worktree = Path(self.temporary.name)
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.worktree)], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.worktree), "config", "user.name", "Alice"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.worktree),
                "config",
                "user.email",
                "alice@example.com",
            ],
            check=True,
        )
        (self.worktree / "example.txt").write_text("example\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.worktree), "add", "example.txt"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.worktree), "commit", "-qm", "initial"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.worktree),
                "checkout",
                "-qb",
                "alice/feat/example",
            ],
            check=True,
        )
        self.head = subprocess.run(
            ["git", "-C", str(self.worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def pipeline(self) -> Pipeline:
        policy = RepositoryPolicy(
            name="owner/repo",
            mode="maintainer",
            submission_strategy="same-repository",
        )
        details = {
            "worktree": str(self.worktree),
            "branch": "alice/feat/example",
            "base_branch": "main",
            "commit_sha": self.head,
            "agent_result": {
                "summary": "Implement the capacity policy.",
                "pr_title": "feat(workflow): configure capacity",
                "implementation_notes": "Use fresh GitHub facts.",
                "verification_commands": ["python -m unittest"],
                "risks": [],
            },
        }
        pipeline = object.__new__(Pipeline)
        pipeline.config = SimpleNamespace(
            repositories={"owner/repo": policy},
            github=SimpleNamespace(login="alice"),
            safety=SimpleNamespace(
                max_active_pull_requests=4,
                draft_pull_requests=True,
            ),
        )
        pipeline.store = _SubmissionStore(details)
        pipeline.workspaces = Mock()
        pipeline.gate_status = Mock(return_value={"submission_ready": True})
        return pipeline

    def submit(
        self, pipeline: Pipeline, client: _SubmissionGitHub, *, reopen: int = 0
    ) -> dict:
        client.new_head = pipeline.store.details["commit_sha"]

        def push(*_args, **_kwargs) -> None:
            client.mark_pushed(client.new_head)
            if client.push_error_after_write:
                client.push_error_after_write = False
                raise WorkspaceError("simulated push transport failure")

        pipeline.workspaces.push.side_effect = push
        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_SUBMIT": "1"}),
            patch("reposteward.pipeline.resolve_token", return_value="token"),
            patch("reposteward.pipeline.GitHubClient", return_value=client),
        ):
            return pipeline.submit(
                "owner/repo",
                42,
                reviewed_by="alice",
                reopen_pull_request=reopen,
            )

    def test_new_pull_is_blocked_at_capacity_before_any_public_write(self) -> None:
        pipeline = self.pipeline()
        client = _SubmissionGitHub(
            pulls=tuple(_pull(number, draft=number == 1) for number in range(1, 5))
        )

        with self.assertRaisesRegex(PolicyError, "4/4 open PRs.*#1, #2, #3, #4"):
            self.submit(pipeline, client)

        self.assertEqual(client.open_calls, 1)
        self.assertEqual(client.repository_calls, 0)
        self.assertEqual(client.create_calls, 0)
        pipeline.workspaces.push.assert_not_called()

    def test_existing_pull_update_bypasses_new_pull_capacity(self) -> None:
        pipeline = self.pipeline()
        existing = PullRequest(
            number=41,
            url="https://github.com/owner/repo/pull/41",
            state="open",
            draft=True,
            author="alice",
            head_owner="owner",
            head_branch="alice/feat/example",
            head_sha="f" * 40,
            base_branch="main",
        )
        client = _SubmissionGitHub(
            existing=existing,
            pulls=tuple(_pull(number) for number in range(1, 5)),
        )

        result = self.submit(pipeline, client)

        self.assertEqual(result["pr_number"], 41)
        self.assertEqual(client.open_calls, 0)
        self.assertEqual(client.create_calls, 0)
        pipeline.workspaces.push.assert_called_once()

    def test_capacity_read_failure_is_closed_before_push(self) -> None:
        pipeline = self.pipeline()
        client = _SubmissionGitHub(open_error=GitHubError("pagination failed"))

        with self.assertRaisesRegex(GitHubError, "pagination failed"):
            self.submit(pipeline, client)

        self.assertEqual(client.repository_calls, 0)
        self.assertEqual(client.create_calls, 0)
        pipeline.workspaces.push.assert_not_called()

    def test_reopening_a_closed_pull_consumes_new_capacity(self) -> None:
        pipeline = self.pipeline()
        closed = PullRequest(
            number=40,
            url="https://github.com/owner/repo/pull/40",
            state="closed",
            draft=True,
            author="alice",
            head_owner="owner",
            head_branch="alice/feat/example",
            head_sha=self.head,
            base_branch="main",
        )
        client = _SubmissionGitHub(
            closed=closed,
            pulls=tuple(_pull(number) for number in range(1, 5)),
        )

        with self.assertRaisesRegex(PolicyError, "capacity reached"):
            self.submit(pipeline, client, reopen=40)

        self.assertEqual(client.open_calls, 1)
        self.assertEqual(client.reopen_calls, 0)
        pipeline.workspaces.push.assert_not_called()

    def test_new_pull_records_each_external_action_before_and_after_write(
        self,
    ) -> None:
        pipeline = self.pipeline()
        client = _SubmissionGitHub()

        result = self.submit(pipeline, client)

        events = pipeline.store.publication
        self.assertTrue(result["public_write"])
        self.assertFalse(result["idempotent"])
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(
            [(value["action"], value["stage"]) for value in events],
            [
                ("push", "applying"),
                ("push", "completed"),
                ("create", "applying"),
                ("create", "completed"),
            ],
        )
        self.assertTrue(all("token" not in value["payload"] for value in events))
        self.assertTrue(all("body" not in value["payload"] for value in events))

    def test_uncertain_push_is_reconciled_before_the_pull_is_created(self) -> None:
        pipeline = self.pipeline()
        client = _SubmissionGitHub()
        client.push_error_after_write = True

        result = self.submit(pipeline, client)

        push_results = [
            value
            for value in pipeline.store.publication
            if value["action"] == "push" and value["stage"] == "completed"
        ]
        self.assertEqual(push_results[0]["outcome"], "reconciled")
        self.assertEqual(result["pr_number"], 99)
        self.assertEqual(client.create_calls, 1)

    def test_uncertain_create_is_reconciled_without_a_duplicate_request(self) -> None:
        pipeline = self.pipeline()
        client = _SubmissionGitHub()
        client.create_error_after_write = True

        result = self.submit(pipeline, client)

        create_results = [
            value
            for value in pipeline.store.publication
            if value["action"] == "create" and value["stage"] == "completed"
        ]
        self.assertEqual(create_results[0]["outcome"], "reconciled")
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(result["pr_number"], 99)

    def test_retry_repairs_local_state_without_repeating_remote_writes(self) -> None:
        pipeline = self.pipeline()
        client = _SubmissionGitHub()
        pipeline.store.fail_local_update_once = True

        first = self.submit(pipeline, client)
        push_calls = pipeline.workspaces.push.call_count
        second = self.submit(pipeline, client)

        self.assertIn("local state update failed", first["warning"])
        self.assertTrue(second["idempotent"])
        self.assertFalse(second["public_write"])
        self.assertEqual(pipeline.workspaces.push.call_count, push_calls)
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(pipeline.store.status, "submitted")

    def test_uncertain_reopen_is_reconciled_before_updating_the_branch(self) -> None:
        pipeline = self.pipeline()
        closed = PullRequest(
            number=40,
            url="https://github.com/owner/repo/pull/40",
            state="closed",
            draft=True,
            author="alice",
            head_owner="owner",
            head_branch="alice/feat/example",
            head_sha="f" * 40,
            base_branch="main",
        )
        client = _SubmissionGitHub(closed=closed)
        client.reopen_error_after_write = True

        result = self.submit(pipeline, client, reopen=40)

        reopen_results = [
            value
            for value in pipeline.store.publication
            if value["action"] == "reopen" and value["stage"] == "completed"
        ]
        self.assertEqual(reopen_results[0]["outcome"], "reconciled")
        self.assertEqual(client.reopen_calls, 1)
        self.assertEqual(result["pr_number"], 40)

    def test_lost_lease_after_reopen_does_not_issue_a_rollback_write(self) -> None:
        pipeline = self.pipeline()
        closed = PullRequest(
            number=40,
            url="https://github.com/owner/repo/pull/40",
            state="closed",
            draft=True,
            author="alice",
            head_owner="owner",
            head_branch="alice/feat/example",
            head_sha="f" * 40,
            base_branch="main",
        )
        client = _SubmissionGitHub(closed=closed)
        pipeline.store.validate_run_lease = Mock(
            side_effect=[None, StoreError("stale publication lease")]
        )

        with self.assertRaisesRegex(StoreError, "stale publication lease"):
            self.submit(pipeline, client, reopen=40)

        self.assertEqual(client.reopen_calls, 1)
        self.assertEqual(client.close_calls, 0)
        pipeline.workspaces.push.assert_not_called()

    def test_incomplete_attempt_for_an_old_head_fails_closed(self) -> None:
        pipeline = self.pipeline()
        client = _SubmissionGitHub()
        pipeline.store.append_publication_attempt(
            attempt_id="attempt-old",
            step_id="step-old",
            run_id="run-1",
            repository="owner/repo",
            issue_number=42,
            actor="alice",
            action="push",
            stage="applying",
            outcome="pending",
            destination="owner/repo",
            branch="alice/feat/example",
            head_sha="e" * 40,
            base_branch="main",
            expected_remote_sha="",
            target_pull_number=0,
            lease=RunLease("issue:owner/repo#42", "old", 1, "expired"),
            payload={},
        )

        with self.assertRaisesRegex(PolicyError, "different verified scope"):
            self.submit(pipeline, client)

        self.assertEqual(pipeline.store.publication[-1]["outcome"], "blocked")
        pipeline.workspaces.push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
