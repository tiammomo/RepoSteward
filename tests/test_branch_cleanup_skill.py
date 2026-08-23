from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPT = (
    Path(__file__).parents[1]
    / ".agents"
    / "skills"
    / "reposteward-branch-cleanup"
    / "scripts"
    / "branch_cleanup.py"
)
SPEC = importlib.util.spec_from_file_location("branch_cleanup_skill", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
branch_cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(branch_cleanup)


def pull(
    number: int,
    branch: str,
    sha: str,
    *,
    state: str = "closed",
    merged: bool = True,
    repository: str = "owner/repo",
) -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "merged_at": "2026-08-23T00:00:00Z" if merged else None,
        "html_url": f"https://github.com/owner/repo/pull/{number}",
        "head": {
            "ref": branch,
            "sha": sha,
            "repo": {"full_name": repository},
        },
    }


def managed(
    run_id: str,
    branch: str,
    sha: str,
    pull_number: int,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "branch": branch,
        "head_sha": sha,
        "pull_number": pull_number,
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.repository_value = {
            "full_name": "owner/repo",
            "default_branch": "main",
            "delete_branch_on_merge": True,
            "permissions": {"push": True},
        }
        self.branch_values: dict[str, dict[str, Any]] = {
            "merged": {
                "name": "merged",
                "protected": False,
                "commit": {"sha": "a" * 40},
            }
        }
        self.pull_values = [pull(7, "merged", "a" * 40)]
        self.ambiguous_delete = False
        self.concurrent_update_on_delete = False
        self.fail_branch_reads: set[str] = set()
        self.fail_absent_reads = False
        self.deleted: list[str] = []

    def authenticated_login(self) -> str:
        return "owner"

    def repository(self) -> dict[str, Any]:
        return self.repository_value

    def branches(self) -> list[dict[str, Any]]:
        return list(self.branch_values.values())

    def pull_requests(
        self, *, state: str = "all", head: str = ""
    ) -> list[dict[str, Any]]:
        values = self.pull_values
        if state != "all":
            values = [value for value in values if value["state"] == state]
        if head:
            _owner, branch = head.split(":", 1)
            values = [value for value in values if value["head"]["ref"] == branch]
        return values

    def branch(self, name: str) -> dict[str, Any] | None:
        if name in self.fail_branch_reads or (
            self.fail_absent_reads and name not in self.branch_values
        ):
            raise branch_cleanup.BranchCleanupError("fixture read failure")
        return self.branch_values.get(name)

    def pull_request(self, number: int) -> dict[str, Any]:
        return next(value for value in self.pull_values if value["number"] == number)

    def delete_branch(self, name: str, expected_sha: str) -> None:
        if self.concurrent_update_on_delete:
            self.branch_values[name]["commit"]["sha"] = "c" * 40
            raise branch_cleanup.BranchCleanupError("lease rejected")
        if self.branch_values[name]["commit"]["sha"] != expected_sha:
            raise branch_cleanup.BranchCleanupError("lease rejected")
        self.deleted.append(name)
        self.branch_values.pop(name, None)
        if self.ambiguous_delete:
            raise branch_cleanup.BranchCleanupError("ambiguous transport failure")


class BranchCleanupSkillTests(unittest.TestCase):
    def test_plan_only_deletes_exact_merged_same_repository_heads(self) -> None:
        branches = [
            {"name": "main", "protected": True, "commit": {"sha": "0" * 40}},
            {"name": "eligible", "protected": False, "commit": {"sha": "1" * 40}},
            {"name": "active", "protected": False, "commit": {"sha": "2" * 40}},
            {"name": "moved", "protected": False, "commit": {"sha": "3" * 40}},
            {"name": "unmerged", "protected": False, "commit": {"sha": "4" * 40}},
            {"name": "fork", "protected": False, "commit": {"sha": "5" * 40}},
            {"name": "orphan", "protected": False, "commit": {"sha": "6" * 40}},
            {"name": "shared", "protected": False, "commit": {"sha": "7" * 40}},
            {"name": "release", "protected": False, "commit": {"sha": "8" * 40}},
            {"name": "unknown", "commit": {"sha": "9" * 40}},
        ]
        pulls = [
            pull(1, "eligible", "1" * 40),
            pull(2, "active", "2" * 40, state="open", merged=False),
            pull(3, "moved", "7" * 40),
            pull(4, "unmerged", "4" * 40, merged=False),
            pull(5, "fork", "5" * 40, repository="someone/fork"),
            pull(6, "shared", "7" * 40),
            pull(7, "shared", "7" * 40, merged=False),
            pull(8, "release", "8" * 40),
        ]
        repository = {
            "full_name": "owner/repo",
            "default_branch": "main",
            "delete_branch_on_merge": True,
        }
        managed_runs = [
            managed("1" * 32, "eligible", "1" * 40, 1),
            managed("2" * 32, "active", "2" * 40, 2),
            managed("3" * 32, "moved", "3" * 40, 3),
            managed("4" * 32, "unmerged", "4" * 40, 4),
            managed("5" * 32, "fork", "5" * 40, 5),
            managed("6" * 32, "orphan", "6" * 40, 6),
            managed("7" * 32, "shared", "7" * 40, 6),
            managed("9" * 32, "unknown", "9" * 40, 9),
        ]

        plan = branch_cleanup.build_plan(repository, branches, pulls, managed_runs)

        self.assertEqual(
            [value["branch"] for value in plan["candidates"]], ["eligible"]
        )
        retained = {value["branch"]: value["reasons"] for value in plan["retained"]}
        self.assertIn("default_branch", retained["main"])
        self.assertIn("protected_branch", retained["main"])
        self.assertIn("open_pull_request", retained["active"])
        for name in ("moved", "unmerged", "fork", "orphan"):
            self.assertIn("no_exact_merged_pull", retained[name])
        self.assertIn("shared_branch_history", retained["shared"])
        self.assertIn("not_selected_managed_run", retained["release"])
        self.assertIn("protection_unknown", retained["unknown"])
        self.assertFalse(plan["public_write"])

    def test_plan_digest_is_stable_across_snapshot_order(self) -> None:
        client = FakeGitHub()
        first = branch_cleanup.build_plan(
            client.repository(),
            client.branches(),
            client.pull_requests(),
            [managed("a" * 32, "merged", "a" * 40, 7)],
        )
        second = branch_cleanup.build_plan(
            client.repository(),
            list(reversed(client.branches())),
            list(reversed(client.pull_requests())),
            [managed("a" * 32, "merged", "a" * 40, 7)],
        )
        self.assertEqual(first["plan_digest"], second["plan_digest"])

    def test_apply_requires_gate_and_exact_digest(self) -> None:
        client = FakeGitHub()
        plan = branch_cleanup.plan_from_client(
            client, [managed("a" * 32, "merged", "a" * 40, 7)]
        )
        with self.assertRaisesRegex(
            branch_cleanup.BranchCleanupError, "REPOSTEWARD_ENABLE_BRANCH_CLEANUP"
        ):
            branch_cleanup.apply_plan(
                client,
                plan,
                expected_digest=plan["plan_digest"],
                gate_enabled=False,
                reviewed_by="owner",
            )
        with self.assertRaisesRegex(
            branch_cleanup.BranchCleanupError, "expected-digest"
        ):
            branch_cleanup.apply_plan(
                client,
                plan,
                expected_digest="f" * 64,
                gate_enabled=True,
                reviewed_by="owner",
            )
        with self.assertRaisesRegex(branch_cleanup.BranchCleanupError, "reviewed-by"):
            branch_cleanup.apply_plan(
                client,
                plan,
                expected_digest=plan["plan_digest"],
                gate_enabled=True,
                reviewed_by="someone-else",
            )
        self.assertEqual(client.deleted, [])

    def test_selected_runs_are_bound_from_local_reposteward_records(self) -> None:
        usage = {
            "runs": [
                {
                    "run_id": "a" * 32,
                    "repository": "owner/repo",
                    "status": "submitted",
                    "pull_number": 7,
                }
            ]
        }
        inspected = {
            "repository": "owner/repo",
            "status": "submitted",
            "branch": "merged",
            "commit_sha": "a" * 40,
        }
        with patch.object(
            branch_cleanup, "_local_json", side_effect=[usage, inspected]
        ):
            result = branch_cleanup.load_managed_runs("owner/repo", ["a" * 32])

        self.assertEqual(result, [managed("a" * 32, "merged", "a" * 40, 7)])

    def test_git_delete_uses_atomic_lease_without_token_or_hooks(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            patch.object(
                branch_cleanup.subprocess, "run", return_value=completed
            ) as run,
            patch.dict(branch_cleanup.os.environ, {"GH_TOKEN": "secret"}),
        ):
            branch_cleanup.GhAPI("owner/repo").delete_branch("owner/feature", "a" * 40)

        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertIn("core.hooksPath=/dev/null", command)
        self.assertIn(
            f"--force-with-lease=refs/heads/owner/feature:{'a' * 40}", command
        )
        self.assertEqual(command[-1], ":refs/heads/owner/feature")
        self.assertNotIn("GH_TOKEN", environment)

    def test_apply_revalidates_changed_head_before_delete(self) -> None:
        client = FakeGitHub()
        plan = branch_cleanup.plan_from_client(
            client, [managed("a" * 32, "merged", "a" * 40, 7)]
        )
        client.branch_values["merged"]["commit"]["sha"] = "b" * 40

        result = branch_cleanup.apply_plan(
            client,
            plan,
            expected_digest=plan["plan_digest"],
            gate_enabled=True,
            reviewed_by="owner",
        )

        self.assertFalse(result["complete"])
        self.assertEqual(result["actions"][0]["status"], "blocked")
        self.assertIn("head_changed", result["actions"][0]["reasons"])
        self.assertEqual(client.deleted, [])

    def test_apply_reconciles_ambiguous_delete(self) -> None:
        client = FakeGitHub()
        client.ambiguous_delete = True
        plan = branch_cleanup.plan_from_client(
            client, [managed("a" * 32, "merged", "a" * 40, 7)]
        )

        result = branch_cleanup.apply_plan(
            client,
            plan,
            expected_digest=plan["plan_digest"],
            gate_enabled=True,
            reviewed_by="owner",
        )

        self.assertTrue(result["complete"])
        self.assertTrue(result["public_write"])
        self.assertEqual(result["actions"][0]["status"], "reconciled_deleted")

    def test_atomic_lease_rejects_a_concurrent_head_update(self) -> None:
        client = FakeGitHub()
        client.concurrent_update_on_delete = True
        plan = branch_cleanup.plan_from_client(
            client, [managed("a" * 32, "merged", "a" * 40, 7)]
        )

        result = branch_cleanup.apply_plan(
            client,
            plan,
            expected_digest=plan["plan_digest"],
            gate_enabled=True,
            reviewed_by="owner",
        )

        self.assertFalse(result["complete"])
        self.assertEqual(result["actions"][0]["status"], "failed")
        self.assertEqual(client.branch_values["merged"]["commit"]["sha"], "c" * 40)

    def test_partial_success_is_preserved_when_next_freshness_read_fails(self) -> None:
        client = FakeGitHub()
        client.branch_values["second"] = {
            "name": "second",
            "protected": False,
            "commit": {"sha": "b" * 40},
        }
        client.pull_values.append(pull(8, "second", "b" * 40))
        plan = branch_cleanup.plan_from_client(
            client,
            [
                managed("a" * 32, "merged", "a" * 40, 7),
                managed("b" * 32, "second", "b" * 40, 8),
            ],
        )
        client.fail_branch_reads.add("second")

        result = branch_cleanup.apply_plan(
            client,
            plan,
            expected_digest=plan["plan_digest"],
            gate_enabled=True,
            reviewed_by="owner",
        )

        self.assertFalse(result["complete"])
        self.assertTrue(result["public_write"])
        self.assertEqual(
            [value["status"] for value in result["actions"]], ["deleted", "failed"]
        )

    def test_confirmation_failure_reports_unknown_write_outcome(self) -> None:
        client = FakeGitHub()
        plan = branch_cleanup.plan_from_client(
            client, [managed("a" * 32, "merged", "a" * 40, 7)]
        )
        client.fail_absent_reads = True

        result = branch_cleanup.apply_plan(
            client,
            plan,
            expected_digest=plan["plan_digest"],
            gate_enabled=True,
            reviewed_by="owner",
        )

        self.assertFalse(result["complete"])
        self.assertTrue(result["public_write"])
        self.assertEqual(result["actions"][0]["status"], "outcome_unknown")


if __name__ == "__main__":
    unittest.main()
