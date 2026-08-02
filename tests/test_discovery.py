from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from starfix.config import load_config
from starfix.discovery import score_issue
from starfix.models import Issue, RepositoryInfo

ROOT = Path(__file__).resolve().parents[1]


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(ROOT / "starfix.toml")
        self.policy = self.config.repositories["langchain-ai/deepagents"]
        self.repository = RepositoryInfo(
            full_name="langchain-ai/deepagents",
            default_branch="main",
            stars=27_000,
            forks=3_000,
            open_issues=180,
            pushed_at="2026-08-02T00:00:00Z",
            archived=False,
            is_fork=False,
            license_spdx="MIT",
        )
        self.issue = Issue(
            repository="langchain-ai/deepagents",
            number=5112,
            node_id=1,
            title="BaseSandbox.grep path globs fail",
            body="Reproduction steps show expected and actual behavior with a test case. "
            * 8,
            url="https://github.com/langchain-ai/deepagents/issues/5112",
            labels=("bug", "deepagents", "external"),
            comments=2,
            created_at="2026-07-28T00:00:00Z",
            updated_at="2026-08-01T00:00:00Z",
            author_login="reporter",
            author_association="NONE",
        )

    def test_high_signal_external_bug_scores_well(self) -> None:
        candidate = score_issue(self.issue, self.repository, self.policy, self.config)

        self.assertGreater(candidate.score, 50)
        self.assertEqual(candidate.blockers, ())

    def test_assignment_to_configured_login_is_not_a_blocker(self) -> None:
        issue = replace(self.issue, assignees=("tiammomo",))

        candidate = score_issue(issue, self.repository, self.policy, self.config)

        self.assertEqual(candidate.blockers, ())
        self.assertTrue(
            any("assigned to tiammomo" in item for item in candidate.reasons)
        )

    def test_assignment_to_someone_else_is_blocked(self) -> None:
        issue = replace(self.issue, assignees=("another-user",))

        candidate = score_issue(issue, self.repository, self.policy, self.config)

        self.assertTrue(any("already assigned" in item for item in candidate.blockers))

    def test_security_report_is_blocked(self) -> None:
        issue = replace(self.issue, title="GHSA-abcd security vulnerability")

        candidate = score_issue(issue, self.repository, self.policy, self.config)

        self.assertTrue(
            any("private disclosure" in item for item in candidate.blockers)
        )


if __name__ == "__main__":
    unittest.main()
