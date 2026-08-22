from __future__ import annotations

import unittest

from reposteward.inbox import build_maintainer_inbox, render_inbox_text


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
