from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reposteward.config import RepositoryPolicy
from reposteward.dependencies import (
    build_dependency_plan,
    direct_dependency_requirements,
    parse_dependency_declarations,
    render_dependency_plan_text,
)
from reposteward.github import PullRequest
from reposteward.pipeline import Pipeline
from reposteward.policy import PolicyError
from reposteward.store import Store


def _snapshot(numbers: list[int]) -> dict:
    ordered = sorted(numbers)
    return {
        "repository": "owner/repo",
        "snapshot_digest": "f" * 64,
        "complete": True,
        "pull_requests": [
            {"number": number, "head_sha": str(number) * 40} for number in numbers
        ],
        "overlaps": [
            {
                "left": ordered[0],
                "right": ordered[-1],
                "file_count": 1,
                "files": ["src/shared.py"],
            }
        ]
        if len(numbers) > 1
        else [],
    }


def _declared(pull_number: int, body: str) -> list[dict]:
    return parse_dependency_declarations(
        "owner/repo", pull_number, str(pull_number) * 40, body
    )


class DependencyDeclarationTests(unittest.TestCase):
    def test_only_standalone_lines_outside_quotes_and_fences_are_authoritative(
        self,
    ) -> None:
        body = """
Text saying Depends on #9 is not a declaration.
- Depends on #2
Depends on owner/repo#3
Depends on other/project#4
> Depends on #5
```text
Depends on #6
```
<!--
Depends on #7
-->
````text
```
Depends on #8
````
"""

        declarations = _declared(1, body)

        self.assertEqual(
            [
                (value["dependency_repository"], value["dependency_number"])
                for value in declarations
            ],
            [("other/project", 4), ("owner/repo", 2), ("owner/repo", 3)],
        )
        self.assertTrue(
            all(value["source"] == "explicit_pr_body" for value in declarations)
        )

    def test_source_records_the_pull_request_author(self) -> None:
        declarations = parse_dependency_declarations(
            "owner/repo", 1, "1" * 40, "Depends on #2", actor="alice"
        )

        self.assertEqual(declarations[0]["actor"], "alice")

    def test_duplicate_declarations_collapse_deterministically(self) -> None:
        first = _declared(1, "Depends on #2\n- depends on #2")
        second = _declared(1, "- depends on #2\nDepends on #2")

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["source_digest"], second[0]["source_digest"])


class DependencyPlanTests(unittest.TestCase):
    def test_acyclic_dependencies_have_stable_prerequisite_first_order(self) -> None:
        declarations = [
            *_declared(3, "Depends on #2"),
            *_declared(2, "Depends on #1"),
        ]

        first = build_dependency_plan(_snapshot([3, 1, 2]), declarations, [], {})
        second = build_dependency_plan(
            _snapshot([2, 3, 1]), list(reversed(declarations)), [], {}
        )

        self.assertEqual(first["plan_digest"], second["plan_digest"])
        self.assertEqual(first["suggested_merge_order"], [1, 2, 3])
        self.assertEqual(first["dependency_ready_pull_requests"], [1])
        self.assertEqual(first["ready_blockers"]["2"], ["dependency_open:#1"])
        self.assertFalse(first["suggestions"][0]["authoritative"])

    def test_cycles_are_exact_and_stable(self) -> None:
        declarations = [
            *_declared(1, "Depends on #2"),
            *_declared(2, "Depends on #1"),
            *_declared(3, "Depends on #2"),
        ]

        plan = build_dependency_plan(_snapshot([1, 2, 3]), declarations, [], {})

        self.assertEqual(plan["cycles"], [[1, 2]])
        self.assertEqual(plan["suggested_merge_order"], [])
        self.assertEqual(plan["unscheduled_pull_requests"], [1, 2, 3])
        self.assertIn("dependency_cycle:#1,#2", plan["ready_blockers"]["1"])

    def test_missing_closed_cross_repository_and_merged_targets_are_distinct(
        self,
    ) -> None:
        declarations = [
            *_declared(1, "Depends on #10"),
            *_declared(2, "Depends on #11"),
            *_declared(3, "Depends on other/repo#12"),
            *_declared(4, "Depends on #13"),
        ]
        target_states = {
            ("owner/repo", 10): {"state": "missing"},
            ("owner/repo", 11): {"state": "closed", "merged": False},
            ("owner/repo", 13): {"state": "merged", "merged": True},
        }

        plan = build_dependency_plan(
            _snapshot([1, 2, 3, 4]), declarations, [], target_states
        )

        statuses = {
            value["pull_number"]: value["status"]
            for value in plan["authoritative_edges"]
        }
        self.assertEqual(
            statuses,
            {1: "missing", 2: "closed_unmerged", 3: "cross_repository", 4: "merged"},
        )
        self.assertEqual(plan["revalidation_recommended"], {"4": [13]})
        self.assertNotIn("4", plan["ready_blockers"])
        self.assertEqual(plan["suggested_merge_order"], [4])
        self.assertEqual(plan["unscheduled_pull_requests"], [1, 2, 3])

    def test_self_dependency_and_cycle_downstream_are_not_scheduled(self) -> None:
        declarations = [
            *_declared(1, "Depends on #1"),
            *_declared(2, "Depends on #3"),
            *_declared(3, "Depends on #2"),
            *_declared(4, "Depends on #3"),
        ]

        plan = build_dependency_plan(_snapshot([1, 2, 3, 4]), declarations, [], {})

        self.assertEqual(plan["cycles"], [[1], [2, 3]])
        self.assertEqual(plan["suggested_merge_order"], [])
        self.assertEqual(plan["unscheduled_pull_requests"], [1, 2, 3, 4])
        self.assertIn("dependency_prerequisite_blocked:#3", plan["ready_blockers"]["4"])

    def test_long_reverse_chain_propagates_without_recursion_or_rescans(self) -> None:
        count = 1_000
        declarations = [
            *_declared(count, f"Depends on #{count + 1}"),
            *(
                declaration
                for number in range(1, count)
                for declaration in _declared(number, f"Depends on #{number + 1}")
            ),
        ]

        plan = build_dependency_plan(
            _snapshot(list(range(1, count + 1))),
            declarations,
            [],
            {("owner/repo", count + 1): {"state": "missing"}},
        )

        self.assertEqual(plan["suggested_merge_order"], [])
        self.assertEqual(len(plan["unscheduled_pull_requests"]), count)
        self.assertEqual(
            plan["ready_blockers"]["1"],
            ["dependency_open:#2", "dependency_prerequisite_blocked:#2"],
        )

    def test_head_change_expires_a_maintainer_confirmation(self) -> None:
        attestation = {
            "pull_number": 1,
            "dependency_number": 2,
            "head_sha": "0" * 40,
            "action": "confirm",
            "actor": "alice",
            "event_digest": "e" * 64,
        }

        plan = build_dependency_plan(_snapshot([1, 2]), [], [attestation], {})

        self.assertEqual(plan["authoritative_edges"], [])
        self.assertEqual(len(plan["stale_confirmations"]), 1)
        self.assertEqual(
            plan["ready_blockers"]["1"], ["dependency_confirmation_stale:#2"]
        )

    def test_current_body_declaration_supersedes_stale_confirmation(self) -> None:
        attestation = {
            "pull_number": 1,
            "dependency_number": 2,
            "head_sha": "0" * 40,
            "action": "confirm",
            "actor": "alice",
            "event_digest": "e" * 64,
        }

        plan = build_dependency_plan(
            _snapshot([1, 2]), _declared(1, "Depends on #2"), [attestation], {}
        )

        self.assertEqual(plan["stale_confirmations"], [])
        self.assertEqual(plan["ready_blockers"]["1"], ["dependency_open:#2"])

    def test_text_plan_reports_digest_edges_and_blockers(self) -> None:
        plan = build_dependency_plan(
            _snapshot([1, 2]), _declared(2, "Depends on #1"), [], {}
        )
        digest = str(plan.pop("plan_digest"))
        text = render_dependency_plan_text(
            {"plan_digest": digest, "expected_digest": "0" * 64, "plan": plan}
        )

        self.assertIn("Expected plan: stale", text)
        self.assertIn("#2 depends on owner/repo#1", text)
        self.assertIn("Blocked #2: dependency_open:#1", text)


class _PlanGitHub:
    def open_pull_requests(self, _repository: str) -> tuple[PullRequest, ...]:
        return (
            PullRequest(
                number=1,
                url="https://example.test/pulls/1",
                state="open",
                draft=False,
                body="",
                head_sha="1" * 40,
                base_sha="b" * 40,
                base_branch="main",
            ),
            PullRequest(
                number=2,
                url="https://example.test/pulls/2",
                state="open",
                draft=True,
                body="Depends on #1",
                author="alice",
                head_sha="2" * 40,
                base_sha="b" * 40,
                base_branch="main",
            ),
        )

    def pull_request_merge_snapshot(
        self, _repository: str, number: int
    ) -> dict[str, object]:
        return {
            "repository": "owner/repo",
            "pull_number": number,
            "title": f"Pull {number}",
            "url": f"https://example.test/pulls/{number}",
            "state": "OPEN",
            "draft": number == 2,
            "head_branch": f"change-{number}",
            "head_sha": str(number) * 40,
            "base_branch": "main",
            "base_sha": "b" * 40,
            "files": ["src/shared.py"],
            "checks": [],
            "files_complete": True,
            "conversations_complete": True,
            "checks_complete": True,
        }


class DependencyPlanPipelineTests(unittest.TestCase):
    def test_plan_reuses_one_open_pull_listing_and_has_no_side_effects(self) -> None:
        pipeline = object.__new__(Pipeline)
        pipeline.config = SimpleNamespace(
            repositories={"owner/repo": RepositoryPolicy(name="owner/repo")}
        )
        pipeline.github = _PlanGitHub()
        pipeline.store = SimpleNamespace(
            latest_portfolio_dependency_events=lambda _repository: [
                {
                    "pull_number": 99,
                    "dependency_number": 98,
                    "head_sha": "9" * 40,
                    "action": "confirm",
                    "actor": "alice",
                    "event_digest": "e" * 64,
                }
            ]
        )

        result = pipeline.portfolio_dependency_plan("owner/repo")

        self.assertEqual(result["plan"]["suggested_merge_order"], [1, 2])
        self.assertEqual(result["plan"]["ready_blockers"]["2"], ["dependency_open:#1"])
        self.assertEqual(
            result["plan"]["authoritative_edges"][0]["sources"][0]["actor"],
            "alice",
        )
        self.assertFalse(result["harness_invoked"])
        self.assertFalse(result["workspace_modified"])
        self.assertFalse(result["local_write"])
        self.assertFalse(result["public_write"])


class DirectDependencyRequirementTests(unittest.TestCase):
    def test_open_dependency_blocks_merge_but_merged_dependency_does_not(self) -> None:
        common = {
            "repository": "owner/repo",
            "pull_number": 2,
            "head_sha": "2" * 40,
            "body": "Depends on #1",
            "attestations": [],
        }
        blocked = direct_dependency_requirements(
            **common,
            target_states={("owner/repo", 1): {"state": "open", "merged": False}},
        )
        satisfied = direct_dependency_requirements(
            **common,
            target_states={("owner/repo", 1): {"state": "merged", "merged": True}},
        )

        self.assertEqual(blocked["blockers"], ["dependency_open:#1"])
        self.assertEqual(satisfied["blockers"], [])
        self.assertTrue(satisfied["complete"])

    def test_unknown_dependency_fails_closed(self) -> None:
        result = direct_dependency_requirements(
            repository="owner/repo",
            pull_number=2,
            head_sha="2" * 40,
            body="Depends on #1",
            attestations=[],
            target_states={("owner/repo", 1): {"state": "unknown"}},
        )

        self.assertFalse(result["complete"])
        self.assertEqual(result["blockers"], ["dependency_unknown:#1"])


class _AttestationGitHub:
    def authenticated_login(self) -> str:
        return "alice"

    def repository(self, _repository: str) -> SimpleNamespace:
        return SimpleNamespace(can_push=True)

    def pull_request(self, _repository: str, number: int) -> PullRequest:
        return PullRequest(
            number=number,
            url=f"https://example.test/pulls/{number}",
            state="open" if number == 2 else "closed",
            draft=number == 2,
            merged=number == 1,
            head_sha=str(number) * 40,
        )


class DependencyAttestationPipelineTests(unittest.TestCase):
    def test_confirmation_and_revocation_require_explicit_identity_checked_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline = object.__new__(Pipeline)
            pipeline.config = SimpleNamespace(
                repositories={
                    "owner/repo": RepositoryPolicy(
                        name="owner/repo",
                        mode="maintainer",
                        submission_strategy="same-repository",
                    )
                },
                github=SimpleNamespace(login="alice"),
            )
            pipeline.github = _AttestationGitHub()
            pipeline.store = Store(Path(directory) / "state.sqlite3")

            with (
                patch.dict(os.environ, {}, clear=True),
                self.assertRaisesRegex(PolicyError, "attestation is disabled"),
            ):
                pipeline.attest_portfolio_dependency(
                    "owner/repo",
                    pull_number=2,
                    dependency_number=1,
                    action="confirm",
                    reviewed_by="alice",
                )

            with patch.dict(
                os.environ, {"REPOSTEWARD_ENABLE_DEPENDENCY_ATTESTATION": "1"}
            ):
                confirmed = pipeline.attest_portfolio_dependency(
                    "owner/repo",
                    pull_number=2,
                    dependency_number=1,
                    action="confirm",
                    reviewed_by="alice",
                )
                repeated = pipeline.attest_portfolio_dependency(
                    "owner/repo",
                    pull_number=2,
                    dependency_number=1,
                    action="confirm",
                    reviewed_by="alice",
                )
                revoked = pipeline.attest_portfolio_dependency(
                    "owner/repo",
                    pull_number=2,
                    dependency_number=1,
                    action="revoke",
                    reviewed_by="alice",
                )
                repeated_revoke = pipeline.attest_portfolio_dependency(
                    "owner/repo",
                    pull_number=2,
                    dependency_number=1,
                    action="revoke",
                    reviewed_by="alice",
                )
                audit_log = pipeline.portfolio_dependency_events(
                    "owner/repo", pull_number=2
                )

        self.assertFalse(confirmed["public_write"])
        self.assertTrue(confirmed["local_write"])
        self.assertEqual(confirmed["audit"]["id"], repeated["audit"]["id"])
        self.assertTrue(repeated["audit"]["idempotent"])
        self.assertEqual(revoked["action"], "revoke")
        self.assertEqual(revoked["audit"]["id"], repeated_revoke["audit"]["id"])
        self.assertTrue(repeated_revoke["audit"]["idempotent"])
        self.assertEqual(
            [value["action"] for value in audit_log["events"]],
            ["revoke", "confirm"],
        )


class DependencyAttestationConcurrencyTests(unittest.TestCase):
    def test_concurrent_identical_confirmations_append_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")

            def append(_index: int) -> dict:
                return store.append_portfolio_dependency_event(
                    repository="owner/repo",
                    pull_number=2,
                    dependency_number=1,
                    head_sha="2" * 40,
                    action="confirm",
                    actor="alice",
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(append, range(2)))
            history = store.portfolio_dependency_events("owner/repo", pull_number=2)

        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(len(history), 1)


if __name__ == "__main__":
    unittest.main()
