from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from reposteward.github import GitHubError
from reposteward.inbox import build_maintainer_inbox, render_inbox_text
from reposteward.pipeline import Pipeline


def _pull(
    number: int,
    *,
    checks: list[dict] | None = None,
    review: str = "APPROVED",
    unresolved: int = 0,
    complete: bool = True,
) -> dict:
    return {
        "number": number,
        "updated_at": "2026-08-22T00:00:00Z",
        "facts_complete": complete,
        "checks": checks or [],
        "review_decision": review,
        "unresolved_conversations": unresolved,
    }


def _run(run_id: str, status: str, *, issue: int, pull: int = 0) -> dict:
    return {
        "id": run_id,
        "issue_number": issue,
        "status": status,
        "updated_at": "2026-08-22T00:00:00Z",
        "details": {
            "pr_url": f"https://github.com/owner/repo/pull/{pull}" if pull else ""
        },
        "submission_pr_url": "",
    }


class MaintainerInboxTests(unittest.TestCase):
    def test_attention_is_deduplicated_and_sorted_by_priority(self) -> None:
        checks = [
            {
                "name": "quality",
                "required": True,
                "conclusion": "failure",
            }
        ]
        portfolio = {
            "snapshot": {
                "complete": True,
                "pull_requests": [
                    _pull(10, checks=checks),
                    _pull(11, unresolved=1),
                    _pull(12),
                    _pull(99),
                ],
            }
        }
        result = build_maintainer_inbox(
            "Owner/Repo",
            proposals=[
                {
                    "project_item_id": "proposal-1",
                    "updated_at": "2026-08-21T00:00:00Z",
                }
            ],
            runs=[
                _run("failed", "failed", issue=1),
                _run("ready", "ready", issue=2),
                _run("ci", "submitted", issue=3, pull=10),
                _run("review", "submitted", issue=4, pull=11),
                _run("merge", "submitted", issue=5, pull=12),
            ],
            portfolio=portfolio,
            observed_at="2026-08-22T01:00:00+00:00",
        )

        self.assertTrue(result["complete"])
        self.assertEqual(
            [item["reason_code"] for item in result["items"]],
            [
                "required_ci_failed",
                "review_feedback_required",
                "run_failed",
                "issue_proposal_review_required",
                "local_review_required",
                "merge_check_required",
                "untracked_pull_request",
            ],
        )
        self.assertEqual(len({item["id"] for item in result["items"]}), 7)
        self.assertFalse(result["harness_invoked"])
        self.assertFalse(result["workspace_modified"])
        self.assertFalse(result["public_write"])

    def test_partial_github_failure_fails_closed_and_obeys_limit(self) -> None:
        result = build_maintainer_inbox(
            "owner/repo",
            proposals=[],
            runs=[_run("submitted", "submitted", issue=1, pull=10)],
            portfolio=None,
            observed_at="2026-08-22T01:00:00+00:00",
            error="rate limit",
            limit=1,
        )

        self.assertFalse(result["complete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["reason_code"], "github_refresh_failed")
        self.assertEqual(result["omitted_count"], 1)

    def test_incomplete_untracked_pull_requires_refresh(self) -> None:
        result = build_maintainer_inbox(
            "owner/repo",
            proposals=[],
            runs=[],
            portfolio={
                "snapshot": {
                    "complete": False,
                    "pull_requests": [_pull(99, complete=False)],
                }
            },
            observed_at="2026-08-22T01:00:00+00:00",
        )

        self.assertFalse(result["complete"])
        self.assertEqual(result["items"][0]["reason_code"], "refresh_required")
        self.assertEqual(result["items"][0]["priority"], 80)

    def test_native_merged_outcomes_suppress_absent_tracked_pulls(self) -> None:
        result = build_maintainer_inbox(
            "owner/repo",
            proposals=[],
            runs=[
                _run("merged", "submitted", issue=1, pull=10),
                _run("already", "submitted", issue=2, pull=11),
            ],
            portfolio={"snapshot": {"complete": True, "pull_requests": []}},
            merge_outcomes={10: "merged", 11: "already_merged"},
            observed_at="2026-08-22T01:00:00+00:00",
        )

        self.assertTrue(result["complete"])
        self.assertEqual(result["items"], [])

    def test_merged_outcome_does_not_suppress_without_complete_portfolio(self) -> None:
        for portfolio, error in (
            (None, "rate limit"),
            ({"snapshot": {"complete": False, "pull_requests": []}}, ""),
        ):
            with self.subTest(portfolio=portfolio):
                result = build_maintainer_inbox(
                    "owner/repo",
                    proposals=[],
                    runs=[_run("merged", "submitted", issue=1, pull=10)],
                    portfolio=portfolio,
                    merge_outcomes={10: "merged"},
                    observed_at="2026-08-22T01:00:00+00:00",
                    error=error,
                )

                self.assertIn(
                    (10, "refresh_required"),
                    [
                        (item["pull_number"], item["reason_code"])
                        for item in result["items"]
                    ],
                )

    def test_open_facts_and_non_merged_outcomes_remain_visible(self) -> None:
        result = build_maintainer_inbox(
            "owner/repo",
            proposals=[],
            runs=[
                _run("open", "submitted", issue=1, pull=10),
                _run("failed", "submitted", issue=2, pull=20),
                _run("unknown", "submitted", issue=3, pull=30),
            ],
            portfolio={
                "snapshot": {
                    "complete": True,
                    "pull_requests": [_pull(10)],
                }
            },
            merge_outcomes={10: "merged", 20: "failed", 30: "outcome_unknown"},
            observed_at="2026-08-22T01:00:00+00:00",
        )

        self.assertEqual(
            [(item["pull_number"], item["reason_code"]) for item in result["items"]],
            [
                (20, "refresh_required"),
                (30, "refresh_required"),
                (10, "merge_check_required"),
            ],
        )

    def test_pipeline_passes_native_merge_outcomes_without_extra_github_reads(
        self,
    ) -> None:
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.policy = Mock(return_value=SimpleNamespace(name="owner/repo"))
        pipeline.portfolio_snapshot = Mock(
            return_value={"snapshot": {"complete": True, "pull_requests": []}}
        )
        pipeline.store = Mock()
        pipeline.store.staged_issue_proposals.return_value = []
        pipeline.store.latest_runs_for_repository.return_value = [
            _run("merged", "submitted", issue=1, pull=10)
        ]
        pipeline.store.latest_merge_outcomes.return_value = {10: "merged"}

        result = pipeline.maintainer_inbox("OWNER/REPO")

        self.assertEqual(result["items"], [])
        pipeline.portfolio_snapshot.assert_called_once_with("owner/repo")
        pipeline.store.latest_merge_outcomes.assert_called_once_with("owner/repo")

    def test_pipeline_refresh_failure_keeps_locally_merged_run_visible(self) -> None:
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.policy = Mock(return_value=SimpleNamespace(name="owner/repo"))
        pipeline.portfolio_snapshot = Mock(side_effect=GitHubError("rate limit"))
        pipeline.store = Mock()
        pipeline.store.staged_issue_proposals.return_value = []
        pipeline.store.latest_runs_for_repository.return_value = [
            _run("merged", "submitted", issue=1, pull=10)
        ]
        pipeline.store.latest_merge_outcomes.return_value = {10: "merged"}

        result = pipeline.maintainer_inbox("owner/repo")

        self.assertFalse(result["complete"])
        self.assertEqual(
            [item["reason_code"] for item in result["items"]],
            ["github_refresh_failed", "refresh_required"],
        )

    def test_empty_inbox_has_stable_text_and_digest(self) -> None:
        arguments = {
            "repository": "owner/repo",
            "proposals": [],
            "runs": [],
            "portfolio": {"snapshot": {"complete": True, "pull_requests": []}},
            "observed_at": "2026-08-22T01:00:00+00:00",
        }
        first = build_maintainer_inbox(**arguments)
        second = build_maintainer_inbox(**arguments)

        self.assertEqual(first["inbox_digest"], second["inbox_digest"])
        self.assertIn("Items: 0", render_inbox_text(first))


if __name__ == "__main__":
    unittest.main()
