from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from reposteward.config import RepositoryPolicy
from reposteward.github import GitHubError, PullRequest
from reposteward.pipeline import Pipeline
from reposteward.policy import PolicyError


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

    def latest_run(self, _repository: str, _issue_number: int) -> dict:
        return {"id": "run-1", "status": "ready", "details": self.details}

    def update_run(self, _run_id: str, **values) -> None:
        self.updated = values

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

    def create_pull_request(self, *_args, **_kwargs) -> PullRequest:
        self.create_calls += 1
        return _pull(99)

    def pull_request(self, _repository: str, _number: int) -> PullRequest:
        pull = self.closed or self.existing
        assert pull is not None
        return pull

    def reopen_pull_request(self, *_args, **_kwargs) -> PullRequest:
        self.reopen_calls += 1
        assert self.closed is not None
        return self.closed


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
            head_sha=self.head,
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


if __name__ == "__main__":
    unittest.main()
