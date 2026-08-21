from __future__ import annotations

import unittest
from types import SimpleNamespace

from reposteward.config import RepositoryPolicy
from reposteward.github import GitHubError, PullRequest
from reposteward.pipeline import Pipeline
from reposteward.portfolio import build_portfolio_snapshot, render_portfolio_text


def _pull(
    number: int,
    files: list[str],
    *,
    head_sha: str = "a" * 40,
    complete: bool = True,
) -> dict[str, object]:
    return {
        "repository": "owner/repo",
        "pull_number": number,
        "title": f"Pull {number}",
        "url": f"https://example.test/pulls/{number}",
        "state": "OPEN",
        "draft": number == 2,
        "updated_at": "2026-08-22T00:00:00Z",
        "head_branch": f"change-{number}",
        "head_sha": head_sha,
        "base_branch": "main",
        "base_sha": "b" * 40,
        "mergeable": "MERGEABLE",
        "review_decision": "APPROVED" if number == 1 else "",
        "unresolved_conversations": 0,
        "files": files,
        "additions": number,
        "deletions": 0,
        "checks": [
            {
                "name": "test",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "required": True,
            }
        ],
        "files_complete": complete,
        "conversations_complete": complete,
        "checks_complete": complete,
    }


class PortfolioSnapshotTests(unittest.TestCase):
    def test_snapshot_is_stable_and_indexes_only_actual_overlaps(self) -> None:
        pulls = [
            _pull(3, ["shared/b.py", "only-three.py"]),
            _pull(1, ["only-one.py", "shared/a.py"]),
            _pull(2, ["shared/b.py", "shared/a.py", "only-two.py"]),
            _pull(4, ["shared/a.py"], complete=False),
        ]

        first = build_portfolio_snapshot("Owner/Repo", pulls)
        second = build_portfolio_snapshot(
            "owner/repo",
            [
                {
                    **pulls[2],
                    "files": list(reversed(pulls[2]["files"])),
                    "checks": list(reversed(pulls[2]["checks"])),
                },
                pulls[3],
                pulls[1],
                pulls[0],
            ],
        )

        self.assertEqual(first, second)
        self.assertEqual(
            first["overlaps"],
            [
                {"left": 1, "right": 2, "file_count": 1, "files": ["shared/a.py"]},
                {"left": 2, "right": 3, "file_count": 1, "files": ["shared/b.py"]},
            ],
        )
        self.assertEqual(first["stats"]["files_in_overlaps"], 2)
        self.assertEqual(first["stats"]["incomplete_pull_requests"], 1)
        self.assertFalse(first["complete"])

    def test_digest_changes_when_decision_facts_change(self) -> None:
        first = build_portfolio_snapshot("owner/repo", [_pull(1, ["a.py"])])
        second = build_portfolio_snapshot(
            "owner/repo", [_pull(1, ["a.py"], head_sha="c" * 40)]
        )

        self.assertEqual(len(first["snapshot_digest"]), 64)
        self.assertNotEqual(first["snapshot_digest"], second["snapshot_digest"])

    def test_disjoint_files_produce_no_overlap_edges(self) -> None:
        snapshot = build_portfolio_snapshot(
            "owner/repo", [_pull(1, ["a.py"]), _pull(2, ["b.py"])]
        )

        self.assertEqual(snapshot["overlaps"], [])
        self.assertEqual(snapshot["stats"]["files_in_overlaps"], 0)

    def test_duplicate_pull_numbers_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate pull requests"):
            build_portfolio_snapshot(
                "owner/repo", [_pull(1, ["a.py"]), _pull(1, ["b.py"])]
            )

    def test_text_output_is_compact_and_reports_staleness(self) -> None:
        shared = [f"shared/{number}.py" for number in range(7)]
        snapshot = build_portfolio_snapshot(
            "owner/repo", [_pull(1, shared), _pull(2, list(reversed(shared)))]
        )
        digest = str(snapshot.pop("snapshot_digest"))
        text = render_portfolio_text(
            {
                "snapshot_digest": digest,
                "expected_digest": "0" * 64,
                "matches_expected_digest": False,
                "snapshot": snapshot,
            }
        )

        self.assertIn("Expected snapshot: stale", text)
        self.assertIn("#1 <-> #2:", text)
        self.assertIn("(+2 more)", text)

    def test_text_output_bounds_large_portfolios(self) -> None:
        snapshot = build_portfolio_snapshot(
            "owner/repo",
            [_pull(number, ["shared.py"]) for number in range(1, 53)],
            errors=[
                {"pull_number": number, "message": "unavailable"}
                for number in range(1, 23)
            ],
        )
        digest = str(snapshot.pop("snapshot_digest"))
        text = render_portfolio_text(
            {"snapshot_digest": digest, "expected_digest": "", "snapshot": snapshot}
        )

        self.assertIn("2 additional pull requests omitted", text)
        self.assertIn("additional overlaps omitted", text)
        self.assertIn("2 additional errors omitted", text)


class _PortfolioGitHub:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def open_pull_requests(self, _repository: str) -> tuple[PullRequest, ...]:
        return (
            PullRequest(
                number=1,
                url="https://example.test/pulls/1",
                state="open",
                draft=False,
                title="Complete",
                head_sha="a" * 40,
                base_branch="main",
                base_sha="b" * 40,
            ),
            PullRequest(
                number=2,
                url="https://example.test/pulls/2",
                state="open",
                draft=True,
                title="Unavailable",
                head_sha="c" * 40,
                base_branch="main",
                base_sha="b" * 40,
            ),
        )

    def pull_request_merge_snapshot(
        self, _repository: str, number: int
    ) -> dict[str, object]:
        self.calls.append(number)
        if number == 2:
            raise GitHubError("permission denied")
        return _pull(1, ["src/a.py"])


class PortfolioPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline = object.__new__(Pipeline)
        self.pipeline.config = SimpleNamespace(
            repositories={"owner/repo": RepositoryPolicy(name="Owner/Repo")}
        )
        self.pipeline.github = _PortfolioGitHub()

    def test_pipeline_surfaces_incomplete_facts_without_side_effects(self) -> None:
        result = self.pipeline.portfolio_snapshot("OWNER/REPO")

        self.assertEqual(self.pipeline.github.calls, [1, 2])
        self.assertFalse(result["snapshot"]["complete"])
        self.assertEqual(result["snapshot"]["stats"]["pull_requests"], 2)
        self.assertEqual(result["snapshot"]["errors"][0]["pull_number"], 2)
        self.assertFalse(result["harness_invoked"])
        self.assertFalse(result["workspace_modified"])
        self.assertFalse(result["public_write"])

    def test_expected_digest_is_compared_to_current_facts(self) -> None:
        current = self.pipeline.portfolio_snapshot("owner/repo")
        repeated = self.pipeline.portfolio_snapshot(
            "owner/repo", expected_digest=current["snapshot_digest"]
        )

        self.assertTrue(repeated["matches_expected_digest"])

    def test_state_change_during_collection_marks_snapshot_incomplete(self) -> None:
        github = self.pipeline.github

        def changed_state(_repository: str, number: int) -> dict[str, object]:
            snapshot = _pull(number, [f"src/{number}.py"])
            snapshot["state"] = "CLOSED" if number == 1 else "OPEN"
            return snapshot

        github.pull_request_merge_snapshot = changed_state
        result = self.pipeline.portfolio_snapshot("owner/repo")

        self.assertFalse(result["snapshot"]["complete"])
        self.assertIn("changed state", result["snapshot"]["errors"][0]["message"])

    def test_invalid_expected_digest_is_rejected_before_github_reads(self) -> None:
        with self.assertRaisesRegex(ValueError, "64 lowercase hex"):
            self.pipeline.portfolio_snapshot("owner/repo", expected_digest="invalid")

        self.assertEqual(self.pipeline.github.calls, [])


if __name__ == "__main__":
    unittest.main()
