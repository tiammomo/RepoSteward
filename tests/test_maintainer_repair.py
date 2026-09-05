from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace

import test_feedback

from reposteward.context import repository_policy_digest
from reposteward.pipeline import _canonical_digest
from reposteward.policy import PolicyError


class MaintainerRepairTests(test_feedback.FeedbackTests):
    def setUp(self) -> None:
        super().setUp()
        self.policy = replace(
            self.policy, mode="maintainer", submission_strategy="same-repository"
        )
        self.pipeline.config.repositories = {"owner/repo": self.policy}
        self.pipeline.config.github = SimpleNamespace(login="owner")
        self.pipeline.github.authenticated_login.return_value = "owner"
        self.pipeline.github.repository.return_value = SimpleNamespace(
            can_push=True, default_branch="main"
        )
        self.identity = {
            **self.activity["pull_request"],
            "author": "owner",
            "head_owner": "owner",
            "head_repository": "owner/repo",
            "head_branch": "fix",
        }
        self.pipeline.github.pull_request_head_identity.side_effect = lambda *args: {
            **self.identity,
            "head_sha": self.activity["pull_request"]["head_sha"],
        }
        self.pipeline.github.issue.return_value = replace(
            test_feedback._candidate().issue, assignees=("owner",)
        )
        self.pipeline.github.has_maintainer_approval.return_value = True
        self.pipeline.github.competing_work.return_value = ()
        # The inherited fixture's frozen policy must describe this maintainer workflow.
        bundle = self.store.context_bundle(self.run_id)
        pack = bundle["context_pack"]
        pack["project"]["policy_digest"] = repository_policy_digest(self.policy)
        original_bundle = self.store.context_bundle
        self.store.context_bundle = lambda run_id: (
            {**original_bundle(run_id), "context_pack": pack}
            if run_id == self.run_id
            else original_bundle(run_id)
        )

    def test_unowned_fork_and_default_heads_stop_before_harness(self) -> None:
        self.pipeline.follow_up(self.run_id)
        for changes in (
            {"author": "other"},
            {"head_repository": "fork/repo"},
            {"head_branch": "other"},
            {"head_branch": "main"},
            {"head_owner": "other"},
        ):
            original = dict(self.identity)
            self.identity.update(changes)
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(PolicyError, "exact owned"),
            ):
                self.prepare()
            self.identity = original
        self.pipeline.harness.run.assert_not_called()

    def test_permission_authentication_and_issue_gates_stop_before_harness(
        self,
    ) -> None:
        self.pipeline.follow_up(self.run_id)
        self.pipeline.github.repository.return_value.can_push = False
        with self.assertRaisesRegex(PolicyError, "push permission"):
            self.prepare()
        self.pipeline.github.repository.return_value.can_push = True
        self.pipeline.github.authenticated_login.return_value = "different"
        with self.assertRaisesRegex(PolicyError, "configured login"):
            self.prepare()
        self.pipeline.github.authenticated_login.return_value = "owner"
        self.pipeline.github.issue.return_value = replace(
            self.pipeline.github.issue.return_value, state="closed"
        )
        with self.assertRaisesRegex(PolicyError, "contribution gates"):
            self.prepare()
        self.pipeline.harness.run.assert_not_called()

    def test_prepared_maintainer_repair_keeps_fresh_submission_guard(self) -> None:
        self.pipeline.follow_up(self.run_id)
        ready = self.prepare()
        details = ready["details"]
        # Same snapshot and observation watermark are eligible for the separate submit path.
        self.assertEqual(
            details["repair_guard"]["snapshot_digest"],
            _canonical_digest(
                self.pipeline.github.pull_request_merge_snapshot.return_value
            ),
        )
        self.pipeline._validate_repair_submission(
            client=self.pipeline.github, policy=self.policy, details=details
        )
        self.identity["author"] = "different"
        with self.assertRaisesRegex(PolicyError, "exact owned"):
            self.pipeline._validate_repair_submission(
                client=self.pipeline.github, policy=self.policy, details=details
            )
        self.pipeline.github.create_pull_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
