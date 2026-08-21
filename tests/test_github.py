from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from reposteward.config import GitHubConfig
from reposteward.github import GitHubClient, resolve_authentication


class StubGitHubClient(GitHubClient):
    def __init__(self, comments: list[dict[str, Any]]) -> None:
        super().__init__(GitHubConfig(), token="test-token")
        self.comments = comments

    def _request(self, *args: Any, **kwargs: Any) -> tuple[Any, Any]:
        return self.comments, None


class GitHubApprovalTests(unittest.TestCase):
    def test_maintainer_command_at_end_is_accepted(self) -> None:
        client = StubGitHubClient(
            [{"author_association": "MEMBER", "body": "Looks reasonable.\n\nlgtm"}]
        )

        self.assertTrue(
            client.has_maintainer_approval("owner/repo", 1, "lgtm", ("OWNER", "MEMBER"))
        )

    def test_non_maintainer_command_is_rejected(self) -> None:
        client = StubGitHubClient([{"author_association": "NONE", "body": "lgtm"}])

        self.assertFalse(
            client.has_maintainer_approval("owner/repo", 1, "lgtm", ("OWNER", "MEMBER"))
        )

    def test_command_as_substring_is_rejected(self) -> None:
        client = StubGitHubClient(
            [{"author_association": "OWNER", "body": "not-lgtm-yet"}]
        )

        self.assertFalse(
            client.has_maintainer_approval("owner/repo", 1, "lgtm", ("OWNER",))
        )


class GitHubAuthenticationTests(unittest.TestCase):
    @patch("reposteward.github.shutil.which", return_value="/usr/bin/gh")
    @patch("reposteward.github.subprocess.run")
    def test_gh_oauth_is_used_when_environment_token_is_absent(
        self, run: Any, _which: Any
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "oauth-token\n"

        with patch.dict("os.environ", {}, clear=True):
            token, source = resolve_authentication(GitHubConfig())

        self.assertEqual(token, "oauth-token")
        self.assertEqual(source, "gh OAuth")
        run.assert_called_once()


class GitHubCompetingWorkTests(unittest.TestCase):
    def test_claim_comment_and_linked_pull_request_are_blockers(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")

        def request(method: str, path: str, **kwargs: Any) -> tuple[Any, Any]:
            if path.endswith("/comments"):
                return [
                    {
                        "user": {"login": "other"},
                        "body": "I can take this and open a PR.",
                        "html_url": "https://example.test/comment",
                    }
                ], None
            return [
                {
                    "number": 9,
                    "title": "fix: existing work",
                    "body": "Fixes #7",
                    "html_url": "https://example.test/pr",
                    "user": {"login": "another"},
                    "head": {"repo": {"owner": {"login": "another"}}},
                }
            ], None

        with patch.object(client, "_request", side_effect=request):
            conflicts = client.competing_work("owner/repo", 7, own_login="tiammomo")

        self.assertEqual(
            {value.kind for value in conflicts},
            {"claim_comment", "open_pull_request"},
        )


class GitHubIssueSearchTests(unittest.TestCase):
    def test_similar_issue_search_is_read_only_and_compact(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        payload = {
            "items": [
                {
                    "number": 7,
                    "title": "Watcher repeatedly reads the same trajectory",
                    "state": "open",
                    "html_url": "https://example.test/issues/7",
                    "body": "large body that should not be returned",
                }
            ]
        }

        with patch.object(client, "_request", return_value=(payload, None)) as request:
            issues = client.similar_issues(
                "owner/repo", "Watcher repeatedly reads the same trajectory"
            )

        self.assertEqual(issues[0]["number"], 7)
        self.assertNotIn("body", issues[0])
        request.assert_called_once()
        args, kwargs = request.call_args
        self.assertEqual(args[:2], ("GET", "/search/issues"))
        self.assertIn("repo:owner/repo is:issue", kwargs["query"]["q"])


class GitHubPullRequestTests(unittest.TestCase):
    def test_pull_request_activity_returns_compact_structured_state(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        pull = {
            "number": 12,
            "html_url": "https://example.test/pull/12",
            "state": "open",
            "draft": True,
            "updated_at": "2026-08-20T00:00:00Z",
            "head": {"sha": "a" * 40},
            "base": {"ref": "main"},
            "mergeable": True,
            "mergeable_state": "clean",
            "merged_at": None,
        }
        comments = [
            {
                "id": 21,
                "user": {"login": "reviewer"},
                "author_association": "MEMBER",
                "created_at": "2026-08-20T00:00:00Z",
                "updated_at": "2026-08-20T00:00:00Z",
                "html_url": "https://example.test/comment/21",
                "body": "Please add a regression test.",
            }
        ]
        reviews = [
            {
                "id": 31,
                "user": {"login": "reviewer"},
                "author_association": "MEMBER",
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-08-20T00:00:00Z",
                "html_url": "https://example.test/review/31",
                "body": "One requested change.",
            }
        ]
        review_comments = [
            {
                "id": 35,
                "user": {"login": "reviewer"},
                "author_association": "MEMBER",
                "created_at": "2026-08-20T00:00:00Z",
                "updated_at": "2026-08-20T00:00:00Z",
                "html_url": "https://example.test/review-comment/35",
                "path": "src/example.py",
                "line": 42,
                "body": "Handle this branch explicitly.",
            }
        ]
        checks = {
            "check_runs": [
                {
                    "id": 41,
                    "name": "tests",
                    "status": "completed",
                    "conclusion": "success",
                    "details_url": "https://example.test/check/41",
                }
            ]
        }

        with patch.object(
            client,
            "_request",
            side_effect=[
                (pull, None),
                (comments, None),
                (reviews, None),
                (review_comments, None),
                (checks, None),
            ],
        ):
            activity = client.pull_request_activity("owner/repo", 12)

        self.assertEqual(activity["pull_request"]["head_sha"], "a" * 40)
        self.assertEqual(activity["comments"][0]["id"], 21)
        self.assertEqual(activity["reviews"][0]["state"], "CHANGES_REQUESTED")
        self.assertEqual(activity["reviews"][0]["association"], "MEMBER")
        self.assertEqual(activity["review_comments"][0]["line"], 42)
        self.assertEqual(activity["checks"][0]["conclusion"], "success")

    def test_reopen_updates_only_the_matching_pull_request(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        closed = {
            "number": 12,
            "html_url": "https://example.test/pull/12",
            "state": "closed",
            "draft": False,
            "head": {
                "ref": "tiammomo/docs/example",
                "sha": "a" * 40,
                "repo": {"owner": {"login": "tiammomo"}},
            },
            "base": {"ref": "main"},
        }
        reopened = {**closed, "state": "open", "title": "docs: example"}

        with patch.object(
            client, "_request", side_effect=[(closed, None), (reopened, None)]
        ) as request:
            result = client.reopen_pull_request(
                "owner/repo",
                12,
                owner="tiammomo",
                branch="tiammomo/docs/example",
                base="main",
                title="docs: example",
                body="Closes #1",
            )

        self.assertEqual(result.state, "open")
        request.assert_called_with(
            "PATCH",
            "/repos/owner/repo/pulls/12",
            data={
                "title": "docs: example",
                "body": "Closes #1",
                "state": "open",
            },
        )

    def test_reopen_rejects_a_different_head_branch(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        closed = {
            "number": 12,
            "html_url": "https://example.test/pull/12",
            "state": "closed",
            "draft": False,
            "head": {
                "ref": "other/branch",
                "sha": "a" * 40,
                "repo": {"owner": {"login": "tiammomo"}},
            },
            "base": {"ref": "main"},
        }

        with (
            patch.object(client, "_request", return_value=(closed, None)),
            self.assertRaisesRegex(RuntimeError, "does not match"),
        ):
            client.reopen_pull_request(
                "owner/repo",
                12,
                owner="tiammomo",
                branch="tiammomo/docs/example",
                base="main",
                title="docs: example",
                body="Closes #1",
            )


if __name__ == "__main__":
    unittest.main()
