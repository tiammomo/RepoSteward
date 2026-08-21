from __future__ import annotations

import unittest

from reposteward.context_budget import (
    ContextBudgetError,
    build_follow_up_context,
    estimate_tokens,
)


def _activity(
    *, checks: list[dict] | None = None, reviews: list[dict] | None = None
) -> dict:
    return {
        "pull_request": {
            "number": 12,
            "url": "https://example.test/pull/12",
            "state": "open",
            "draft": True,
            "head_sha": "a" * 40,
            "base_branch": "main",
            "merged": False,
        },
        "comments": [],
        "reviews": reviews or [],
        "review_comments": [],
        "checks": checks or [],
    }


def _event(
    sequence: int,
    *,
    event_id: int = 1,
    kind: str = "review_comment",
    body: str = "Handle this case.",
) -> dict:
    return {
        "sequence": sequence,
        "event_type": kind,
        "external_id": str(event_id),
        "version_digest": f"{sequence:064x}",
        "payload": {
            "id": event_id,
            "author": "reviewer",
            "association": "MEMBER",
            "path": "src/example.py",
            "line": 10,
            "body": body,
        },
    }


class ContextBudgetTests(unittest.TestCase):
    def test_latest_version_and_repeated_content_are_deduplicated(self) -> None:
        plan = build_follow_up_context(
            activity=_activity(),
            events=[
                _event(1, body="old"),
                _event(2, body="current"),
                _event(3, event_id=2, body="current"),
            ],
            budget_tokens=5_000,
        )

        self.assertEqual([item["sequence"] for item in plan["events"]], [3])
        self.assertEqual(plan["stats"]["trim_reasons"]["superseded_event_version"], 1)
        self.assertEqual(plan["stats"]["trim_reasons"]["duplicate_event_content"], 1)
        self.assertEqual(
            plan["estimated_tokens"],
            estimate_tokens(plan) + plan["transport_overhead_tokens"],
        )

    def test_latest_review_state_replaces_an_older_change_request(self) -> None:
        old = _event(1, event_id=10, kind="review", body="Please change this.")
        old["payload"].update({"state": "changes_requested", "author": "reviewer"})
        approved = _event(2, event_id=11, kind="review", body="")
        approved["payload"].update({"state": "approved", "author": "reviewer"})
        plan = build_follow_up_context(
            activity=_activity(
                reviews=[
                    {
                        "id": 10,
                        "author": "reviewer",
                        "state": "changes_requested",
                        "submitted_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "id": 11,
                        "author": "reviewer",
                        "state": "approved",
                        "submitted_at": "2026-01-02T00:00:00Z",
                    },
                ]
            ),
            events=[old, approved],
            budget_tokens=2_000,
        )

        self.assertEqual(plan["mandatory"]["blocking_reviews"], [])
        self.assertEqual(plan["events"], [])
        self.assertFalse(plan["actionable"])
        self.assertEqual(plan["stats"]["trim_reasons"]["superseded_reviewer_state"], 1)

    def test_failed_checks_and_safety_facts_are_never_budget_trimmed(self) -> None:
        plan = build_follow_up_context(
            activity=_activity(
                checks=[
                    {
                        "id": 9,
                        "name": "quality",
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ]
            ),
            events=[
                _event(index, event_id=index, body=f"{index}-" + "x" * 2_000)
                for index in range(1, 20)
            ],
            budget_tokens=5_000,
            safety_blockers=("head_changed",),
        )

        self.assertEqual(plan["mandatory"]["failed_checks"][0]["name"], "quality")
        self.assertEqual(plan["mandatory"]["safety_blockers"], ["head_changed"])
        self.assertLessEqual(plan["estimated_tokens"], 5_000)
        self.assertGreater(plan["stats"]["trim_reasons"]["token_budget"], 0)

    def test_impossibly_small_mandatory_set_fails_clearly(self) -> None:
        checks = [
            {
                "id": index,
                "name": f"required-{index}-" + "x" * 300,
                "status": "completed",
                "conclusion": "failure",
            }
            for index in range(30)
        ]

        with self.assertRaisesRegex(ContextBudgetError, "mandatory.*require"):
            build_follow_up_context(
                activity=_activity(checks=checks),
                events=[],
                budget_tokens=3_000,
            )

    def test_budget_never_turns_trimmed_feedback_into_no_activity(self) -> None:
        with self.assertRaisesRegex(ContextBudgetError, "cannot retain any actionable"):
            build_follow_up_context(
                activity=_activity(),
                events=[_event(1, body="x" * 4_000)],
                budget_tokens=3_000,
                checkpoint={"implementation_notes": "x" * 100},
            )

    def test_synthetic_long_pr_is_deterministic_and_bounded(self) -> None:
        events = []
        for index in range(1, 10_001):
            if index % 10 == 0:
                kind = "check"
            elif index % 5 == 0:
                kind = "issue_comment"
            elif index % 3 == 0:
                kind = "review"
            else:
                kind = "review_comment"
            body = (
                "repeated feedback"
                if index % 7 == 0
                else f"feedback-{index}-" + "x" * 4_000
            )
            events.append(_event(index, event_id=index, kind=kind, body=body))

        first = build_follow_up_context(
            activity=_activity(), events=events, budget_tokens=5_000
        )
        second = build_follow_up_context(
            activity=_activity(), events=events, budget_tokens=5_000
        )

        self.assertEqual(first, second)
        self.assertLessEqual(first["estimated_tokens"], 5_000)
        self.assertLess(len(first["events"]), len(events))
        self.assertEqual(first["stats"]["input_events"], 10_000)
        self.assertGreater(first["stats"]["trim_reasons"]["duplicate_event_content"], 0)
        self.assertGreater(first["stats"]["trim_reasons"]["deterministic_fact_only"], 0)

    def test_related_diff_is_bounded_and_audited(self) -> None:
        plan = build_follow_up_context(
            activity=_activity(),
            events=[_event(1)],
            budget_tokens=7_000,
            diff_snippets={"src/example.py": "diff\n" + "x" * 10_000},
        )

        self.assertEqual(plan["diff_snippets"][0]["path"], "src/example.py")
        self.assertEqual(plan["stats"]["trim_reasons"]["diff_snippet_clipped"], 1)

    def test_compact_checkpoint_is_included_in_the_total_budget(self) -> None:
        checkpoint = {
            "id": "checkpoint-1",
            "status": "submitted",
            "implementation_notes": "verified earlier behavior",
            "remaining": ["address reviewer feedback"],
        }
        plan = build_follow_up_context(
            activity=_activity(),
            events=[_event(1)],
            budget_tokens=5_000,
            checkpoint=checkpoint,
        )

        self.assertEqual(plan["checkpoint"]["id"], checkpoint["id"])
        self.assertEqual(
            plan["checkpoint"]["implementation_notes"],
            checkpoint["implementation_notes"],
        )
        self.assertEqual(plan["transport_overhead_tokens"], 2_048)
        self.assertLessEqual(plan["estimated_tokens"], plan["budget_tokens"])


if __name__ == "__main__":
    unittest.main()
