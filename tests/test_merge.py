from __future__ import annotations

import unittest
from dataclasses import replace

from reposteward.merge import MergeCheck, MergeSnapshot, evaluate_merge


class MergeDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = MergeSnapshot(
            repository="owner/repo",
            pull_number=12,
            head_sha="a" * 40,
            base_sha="b" * 40,
            policy_digest="c" * 64,
            state="OPEN",
            draft=False,
            mergeable="MERGEABLE",
            review_decision="APPROVED",
            unresolved_conversations=0,
            files=("src/example.py",),
            additions=20,
            deletions=5,
            checks=(MergeCheck("quality", "COMPLETED", "SUCCESS"),),
            activity_digest="d" * 64,
        )

    def evaluate(self, snapshot: MergeSnapshot | None = None, **kwargs):
        return evaluate_merge(
            snapshot or self.snapshot,
            expected_head_sha="a" * 40,
            expected_base_sha="b" * 40,
            expected_policy_digest="c" * 64,
            max_files_changed=18,
            max_diff_lines=700,
            **kwargs,
        )

    def test_verified_approved_low_risk_snapshot_is_eligible_and_stable(self) -> None:
        first = self.evaluate()
        second = self.evaluate()

        self.assertTrue(first.eligible)
        self.assertEqual(first, second)
        self.assertEqual(first.reasons, ())

    def test_changed_revisions_and_incomplete_data_fail_closed(self) -> None:
        result = self.evaluate(
            replace(
                self.snapshot,
                head_sha="d" * 40,
                base_sha="e" * 40,
                policy_digest="f" * 64,
                files_complete=False,
                conversations_complete=False,
                checks_complete=False,
            )
        )

        missing_activity = self.evaluate(replace(self.snapshot, activity_digest=""))
        self.assertEqual(
            [reason.code for reason in missing_activity.reasons],
            ["activity_incomplete"],
        )

        self.assertFalse(result.eligible)
        self.assertEqual(
            {reason.code for reason in result.reasons},
            {
                "head_changed",
                "base_changed",
                "policy_changed",
                "files_incomplete",
                "conversations_incomplete",
                "checks_incomplete",
            },
        )

    def test_review_conversations_and_checks_block_merge(self) -> None:
        result = self.evaluate(
            replace(
                self.snapshot,
                review_decision="REVIEW_REQUIRED",
                unresolved_conversations=2,
                checks=(
                    MergeCheck("build", "IN_PROGRESS", ""),
                    MergeCheck("test", "COMPLETED", "FAILURE"),
                    MergeCheck("optional", "COMPLETED", "FAILURE", required=False),
                ),
            )
        )

        self.assertEqual(
            [reason.code for reason in result.reasons],
            [
                "review_not_approved",
                "unresolved_conversations",
                "required_check_pending",
                "required_check_failed",
            ],
        )

    def test_dependency_blockers_and_incomplete_dependency_facts_block_merge(
        self,
    ) -> None:
        result = self.evaluate(
            replace(
                self.snapshot,
                dependency_digest="e" * 64,
                dependency_blockers=("dependency_open:#11",),
                dependencies_complete=False,
            )
        )

        self.assertEqual(
            [reason.code for reason in result.reasons],
            ["dependencies_incomplete", "dependency_blocked"],
        )

    def test_builtin_high_risk_paths_cannot_be_disabled(self) -> None:
        result = self.evaluate(
            replace(self.snapshot, files=(".github/workflows/quality.yml",))
        )

        self.assertFalse(result.eligible)
        self.assertIn("ci", result.risk_categories)
        self.assertEqual(result.risk_files, (".github/workflows/quality.yml",))

    def test_repository_policy_can_only_add_high_risk_paths(self) -> None:
        result = self.evaluate(
            replace(self.snapshot, files=("docs/operator-guide.md",)),
            extra_risk_patterns=("docs/**",),
        )

        self.assertFalse(result.eligible)
        self.assertEqual(result.risk_categories, ("repository_policy",))

    def test_size_limits_and_pull_state_block_merge(self) -> None:
        files = tuple(f"src/file-{index}.py" for index in range(19))
        result = self.evaluate(
            replace(
                self.snapshot,
                state="CLOSED",
                draft=True,
                mergeable="CONFLICTING",
                files=files,
                additions=701,
            )
        )

        self.assertEqual(
            {reason.code for reason in result.reasons},
            {
                "pull_not_open",
                "pull_is_draft",
                "pull_not_mergeable",
                "file_limit_exceeded",
                "diff_limit_exceeded",
            },
        )
