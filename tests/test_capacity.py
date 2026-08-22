from __future__ import annotations

import unittest

from reposteward.capacity import effective_capacity_limit, pull_request_capacity
from reposteward.github import PullRequest


def _pull(
    number: int,
    *,
    author: str = "alice",
    state: str = "open",
    draft: bool = False,
) -> PullRequest:
    return PullRequest(
        number=number,
        url=f"https://github.com/owner/repo/pull/{number}",
        state=state,
        draft=draft,
        author=author,
    )


class PullRequestCapacityTests(unittest.TestCase):
    def test_default_boundary_counts_draft_and_ready_pulls(self) -> None:
        capacity = pull_request_capacity(
            (
                _pull(1, draft=True),
                _pull(2),
                _pull(3),
                _pull(4),
                _pull(5, author="bob"),
                _pull(6, state="closed"),
            ),
            login="ALICE",
            limit=4,
        )

        self.assertEqual(capacity["active_count"], 4)
        self.assertEqual(capacity["available"], 0)
        self.assertFalse(capacity["allows_new_pull_request"])
        self.assertTrue(capacity["active_pull_requests"][0]["draft"])
        self.assertEqual(
            [value["number"] for value in capacity["active_pull_requests"]],
            [1, 2, 3, 4],
        )

        over_capacity = pull_request_capacity(
            tuple(_pull(number) for number in range(1, 6)),
            login="alice",
            limit=4,
        )
        self.assertEqual(over_capacity["active_count"], 5)
        self.assertFalse(over_capacity["allows_new_pull_request"])

    def test_user_can_raise_capacity_while_repository_can_only_tighten_it(self) -> None:
        self.assertEqual(effective_capacity_limit(8, None), 8)
        self.assertEqual(effective_capacity_limit(8, 5), 5)
        self.assertEqual(effective_capacity_limit(8, 12), 8)

        capacity = pull_request_capacity(
            tuple(_pull(number) for number in range(1, 6)),
            login="alice",
            limit=8,
        )
        self.assertEqual(capacity["active_count"], 5)
        self.assertTrue(capacity["allows_new_pull_request"])

    def test_capacity_details_are_bounded_without_losing_the_total(self) -> None:
        capacity = pull_request_capacity(
            tuple(_pull(number) for number in range(1, 26)),
            login="alice",
            limit=30,
        )

        self.assertEqual(capacity["active_count"], 25)
        self.assertEqual(len(capacity["active_pull_requests"]), 20)
        self.assertEqual(capacity["active_pull_requests_omitted"], 5)

    def test_non_positive_limit_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            pull_request_capacity((), login="alice", limit=0)


if __name__ == "__main__":
    unittest.main()
