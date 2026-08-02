from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from starfix.config import GitHubConfig
from starfix.github import GitHubClient, resolve_authentication


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
    @patch("starfix.github.shutil.which", return_value="/usr/bin/gh")
    @patch("starfix.github.subprocess.run")
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


if __name__ == "__main__":
    unittest.main()
