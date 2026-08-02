from __future__ import annotations

import unittest
from typing import Any

from starfix.config import GitHubConfig
from starfix.github import GitHubClient


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


if __name__ == "__main__":
    unittest.main()
