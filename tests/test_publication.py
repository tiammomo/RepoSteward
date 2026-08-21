from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from reposteward.config import RepositoryPolicy, load_config
from reposteward.models import RepositoryInfo
from reposteward.pipeline import Pipeline
from reposteward.policy import PolicyError

ROOT = Path(__file__).resolve().parents[1]


class PublicationStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline = Pipeline.__new__(Pipeline)
        self.pipeline.config = load_config(ROOT / "examples" / "tiammomo.toml")

    def test_contributor_strategy_uses_authenticated_users_fork(self) -> None:
        client = Mock()
        client.ensure_fork.return_value = "tiammomo/example"
        policy = RepositoryPolicy(name="owner/example")

        destination, owner = self.pipeline._publication_target(client, policy)

        self.assertEqual(destination, "tiammomo/example")
        self.assertEqual(owner, "tiammomo")
        client.ensure_fork.assert_called_once_with("owner/example", "tiammomo")

    def test_maintainer_strategy_uses_original_repository_after_permission_check(
        self,
    ) -> None:
        client = Mock()
        client.repository.return_value = RepositoryInfo(
            full_name="owner/example",
            default_branch="main",
            stars=1,
            forks=0,
            open_issues=1,
            pushed_at="2026-08-21T00:00:00Z",
            archived=False,
            is_fork=False,
            can_push=True,
        )
        policy = RepositoryPolicy(
            name="owner/example",
            mode="maintainer",
            submission_strategy="same-repository",
        )

        destination, owner = self.pipeline._publication_target(client, policy)

        self.assertEqual(destination, "owner/example")
        self.assertEqual(owner, "owner")
        client.ensure_fork.assert_not_called()

    def test_maintainer_strategy_fails_closed_without_push_permission(self) -> None:
        client = Mock()
        repository = RepositoryInfo(
            full_name="owner/example",
            default_branch="main",
            stars=1,
            forks=0,
            open_issues=1,
            pushed_at="2026-08-21T00:00:00Z",
            archived=False,
            is_fork=False,
        )
        client.repository.return_value = replace(repository, can_push=False)
        policy = RepositoryPolicy(
            name="owner/example",
            mode="maintainer",
            submission_strategy="same-repository",
        )

        with self.assertRaisesRegex(PolicyError, "cannot push"):
            self.pipeline._publication_target(client, policy)


if __name__ == "__main__":
    unittest.main()
