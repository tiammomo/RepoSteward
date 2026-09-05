from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from reposteward.branch_cleanup import (
    build_branch_cleanup_plan,
    fresh_candidate_blockers,
    render_branch_cleanup_text,
)
from reposteward.config import RepositoryPolicy, load_config
from reposteward.github import GitHubError, PullRequest
from reposteward.models import RepositoryInfo
from reposteward.pipeline import Pipeline
from reposteward.policy import PolicyError
from reposteward.store import SCHEMA_VERSION, Store
from reposteward.workspace import WorkspaceError, WorkspaceManager

ROOT = Path(__file__).parents[1]


def repository() -> RepositoryInfo:
    return RepositoryInfo(
        full_name="owner/repo",
        default_branch="main",
        stars=1,
        forks=0,
        open_issues=0,
        pushed_at="2026-01-01T00:00:00Z",
        archived=False,
        is_fork=False,
        can_push=True,
        owner_login="owner",
        delete_branch_on_merge=True,
    )


def pull(
    number: int,
    branch: str,
    sha: str,
    *,
    state: str = "closed",
    merged: bool = True,
    head_repository: str = "owner/repo",
) -> PullRequest:
    return PullRequest(
        number=number,
        url=f"https://github.com/owner/repo/pull/{number}",
        state=state,
        draft=False,
        merged=merged,
        head_owner=head_repository.split("/", 1)[0],
        head_repository=head_repository,
        head_branch=branch,
        head_sha=sha,
        base_branch="main",
        base_sha="0" * 40,
    )


def managed(
    run_id: str,
    branch: str,
    sha: str,
    pull_number: int,
    *,
    issue_number: int | None = None,
    merge_outcome: str = "merged",
    merge_head_sha: str | None = None,
    cleanup_outcome: str = "",
) -> dict:
    return {
        "run_id": run_id,
        "issue_number": issue_number or pull_number,
        "branch": branch,
        "head_sha": sha,
        "pull_number": pull_number,
        "merge_outcome": merge_outcome,
        "merge_head_sha": merge_head_sha or sha,
        "cleanup_outcome": cleanup_outcome,
    }


class BranchCleanupPlanTests(unittest.TestCase):
    def test_plan_classifies_only_exact_managed_terminal_heads(self) -> None:
        branches = (
            {"name": "eligible", "head_sha": "1" * 40, "protected": False},
            {"name": "active", "head_sha": "2" * 40, "protected": False},
            {"name": "moved", "head_sha": "9" * 40, "protected": False},
            {"name": "unmerged", "head_sha": "4" * 40, "protected": False},
            {"name": "fork", "head_sha": "5" * 40, "protected": False},
            {"name": "shared", "head_sha": "6" * 40, "protected": False},
            {"name": "protected", "head_sha": "7" * 40, "protected": True},
            {"name": "recreated", "head_sha": "8" * 40, "protected": False},
        )
        pulls = (
            pull(1, "eligible", "1" * 40),
            pull(2, "active", "2" * 40, state="open", merged=False),
            pull(3, "moved", "9" * 40),
            pull(4, "unmerged", "4" * 40, merged=False),
            pull(5, "fork", "5" * 40, head_repository="someone/fork"),
            pull(6, "shared", "6" * 40),
            pull(16, "shared", "6" * 40, merged=False),
            pull(7, "protected", "7" * 40),
            pull(8, "absent", "8" * 40),
            pull(9, "done", "9" * 40),
            pull(10, "recreated", "8" * 40),
        )
        runs = [
            managed("1" * 32, "eligible", "1" * 40, 1),
            managed("2" * 32, "active", "2" * 40, 2, merge_outcome=""),
            managed("3" * 32, "moved", "3" * 40, 3),
            managed("4" * 32, "unmerged", "4" * 40, 4, merge_outcome=""),
            managed("5" * 32, "fork", "5" * 40, 5),
            managed("6" * 32, "shared", "6" * 40, 6),
            managed("7" * 32, "protected", "7" * 40, 7),
            managed("8" * 32, "absent", "8" * 40, 8),
            managed(
                "9" * 32,
                "done",
                "9" * 40,
                9,
                cleanup_outcome="deleted",
            ),
            managed(
                "a" * 32,
                "recreated",
                "8" * 40,
                10,
                cleanup_outcome="deleted",
            ),
        ]

        plan = build_branch_cleanup_plan(
            repository(), branches, pulls, runs, [], "a" * 64
        )

        self.assertEqual(
            [value["branch"] for value in plan["candidates"]], ["eligible"]
        )
        self.assertEqual([value["branch"] for value in plan["absent"]], ["absent"])
        self.assertEqual([value["branch"] for value in plan["completed"]], ["done"])
        retained = {value["branch"]: value["reasons"] for value in plan["retained"]}
        self.assertIn("open_pull_request", retained["active"])
        self.assertIn("head_changed", retained["moved"])
        self.assertIn("closed_unmerged_or_open", retained["unmerged"])
        self.assertIn("no_exact_merged_pull", retained["fork"])
        self.assertIn("shared_branch_history", retained["shared"])
        self.assertIn("protected_branch", retained["protected"])
        self.assertEqual(retained["recreated"], ["branch_recreated_after_cleanup"])
        self.assertFalse(plan["public_write"])

    def test_pending_attempt_replaces_candidate_and_digest_is_stable(self) -> None:
        run = managed("a" * 32, "merged", "a" * 40, 7)
        pending = {
            **run,
            "attempt_id": "1" * 32,
            "plan_digest": "b" * 64,
            "actor": "owner",
        }
        branches = ({"name": "merged", "head_sha": "a" * 40, "protected": False},)
        pulls = (pull(7, "merged", "a" * 40),)

        first = build_branch_cleanup_plan(
            repository(), branches, pulls, [run], [pending], "c" * 64
        )
        second = build_branch_cleanup_plan(
            repository(),
            tuple(reversed(branches)),
            tuple(reversed(pulls)),
            [run],
            [pending],
            "c" * 64,
        )

        self.assertEqual(first["plan_digest"], second["plan_digest"])
        self.assertEqual(first["candidates"], [])
        self.assertEqual(first["pending"][0]["attempt_id"], "1" * 32)

    def test_pending_attempt_must_keep_an_exact_submitted_run_binding(self) -> None:
        run = managed("a" * 32, "merged", "a" * 40, 7)
        pending = {
            **run,
            "attempt_id": "1" * 32,
            "plan_digest": "b" * 64,
            "actor": "owner",
            "branch": "changed",
        }

        with self.assertRaisesRegex(ValueError, "changed its run binding"):
            build_branch_cleanup_plan(
                repository(),
                ({"name": "merged", "head_sha": "a" * 40, "protected": False},),
                (pull(7, "merged", "a" * 40),),
                [run],
                [pending],
                "c" * 64,
            )

    def test_freshness_blocks_shared_open_moved_and_changed_pull_facts(self) -> None:
        candidate = managed("a" * 32, "merged", "a" * 40, 7)
        changed = pull(7, "other", "b" * 40, state="open", merged=False)
        blockers, absent = fresh_candidate_blockers(
            repository(),
            {"name": "merged", "head_sha": "b" * 40, "protected": True},
            changed,
            (changed, pull(8, "merged", "a" * 40, state="open", merged=False)),
            candidate,
        )

        self.assertFalse(absent)
        for reason in (
            "protected_branch",
            "head_changed",
            "open_pull_request",
            "pull_head_changed",
            "pull_sha_changed",
            "pull_not_merged",
            "pull_history_changed",
        ):
            self.assertIn(reason, blockers)

    def test_absent_branch_still_requires_fresh_exact_pull_history(self) -> None:
        candidate = managed("a" * 32, "merged", "a" * 40, 7)
        changed = pull(7, "other", "b" * 40, state="open", merged=False)

        blockers, absent = fresh_candidate_blockers(
            repository(), None, changed, (changed,), candidate
        )

        self.assertTrue(absent)
        self.assertIn("pull_head_changed", blockers)
        self.assertIn("pull_not_merged", blockers)
        self.assertIn("pull_history_changed", blockers)

    def test_text_is_bounded(self) -> None:
        runs = [
            managed(f"{number:032x}", f"branch-{number}", f"{number:040x}", number)
            for number in range(1, 151)
        ]
        branches = tuple(
            {
                "name": value["branch"],
                "head_sha": value["head_sha"],
                "protected": False,
            }
            for value in runs
        )
        pulls = tuple(
            pull(value["pull_number"], value["branch"], value["head_sha"])
            for value in runs
        )
        plan = build_branch_cleanup_plan(
            repository(), branches, pulls, runs, [], "a" * 64
        )

        text = render_branch_cleanup_text(plan)

        self.assertLessEqual(len(text), 20_000)
        self.assertIn("items omitted", text)


class FakeGitHub:
    def __init__(self) -> None:
        self.repository_value = repository()
        self.branch_values = {
            "merged": {"name": "merged", "head_sha": "a" * 40, "protected": False}
        }
        self.pull_values = [pull(7, "merged", "a" * 40)]
        self.targeted_sha = ""
        self.fail_absent_read = False
        self.login = "owner"
        self.reappear_on_authentication = False

    def authenticated_login(self) -> str:
        if self.reappear_on_authentication:
            self.branch_values["merged"] = {
                "name": "merged",
                "head_sha": "a" * 40,
                "protected": False,
            }
            self.reappear_on_authentication = False
        return self.login

    def repository(self, _name: str) -> RepositoryInfo:
        return self.repository_value

    def repository_branches(self, _name: str) -> tuple[dict, ...]:
        return tuple(self.branch_values.values())

    def all_pull_requests(self, _name: str) -> tuple[PullRequest, ...]:
        return tuple(self.pull_values)

    def repository_branch(self, _name: str, branch: str) -> dict | None:
        if self.fail_absent_read and branch not in self.branch_values:
            raise GitHubError("fixture read failed")
        value = self.branch_values.get(branch)
        if value is None:
            return None
        if self.targeted_sha:
            return {**value, "head_sha": self.targeted_sha}
        return value

    def pull_requests_for_head(
        self, _name: str, *, owner: str, branch: str
    ) -> tuple[PullRequest, ...]:
        del owner
        return tuple(value for value in self.pull_values if value.head_branch == branch)

    def pull_request(self, _name: str, number: int) -> PullRequest:
        return next(value for value in self.pull_values if value.number == number)


class FakeWorkspaces:
    def __init__(self, github: FakeGitHub) -> None:
        self.github = github
        self.deleted: list[str] = []
        self.ambiguous = False

    def delete_remote_branch(
        self, _repository: str, branch: str, *, expected_sha: str
    ) -> None:
        if self.github.branch_values[branch]["head_sha"] != expected_sha:
            raise WorkspaceError("lease rejected")
        self.github.branch_values.pop(branch)
        self.deleted.append(branch)
        if self.ambiguous:
            raise WorkspaceError("response lost")


def _seed_merged_run(store: Store) -> str:
    run_id = store.start_run("owner/repo", 7, "pull_request")
    store.update_run(
        run_id,
        status="submitted",
        details={
            "branch": "merged",
            "commit_sha": "a" * 40,
            "pr_url": "https://github.com/owner/repo/pull/7",
        },
    )
    store.record_submission("owner/repo", 7, "https://github.com/owner/repo/pull/7")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO merge_decisions(
                id, repository, pull_number, head_sha, base_sha, policy_digest,
                snapshot_digest, eligible, decision_digest, payload, created_at
            ) VALUES ('decision', 'owner/repo', 7, ?, ?, ?, ?, 1, ?, '{}',
                      '2026-01-01T00:00:00Z')
            """,
            ("a" * 40, "0" * 40, "b" * 64, "c" * 64, "d" * 64),
        )
        connection.execute(
            """
            INSERT INTO merge_executions(
                id, attempt_id, run_id, decision_id, repository, pull_number,
                actor, merge_method, stage, outcome, reason, decision_digest,
                head_sha, payload, created_at
            ) VALUES ('merge', 'merge-attempt', ?, 'decision', 'owner/repo', 7,
                      'owner', 'squash', 'completed', 'merged', 'merged', ?, ?, '{}',
                      '2026-01-01T00:00:01Z')
            """,
            (run_id, "d" * 64, "a" * 40),
        )
        connection.commit()
    return run_id


class BranchCleanupPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        base = load_config(ROOT / "examples" / "tiammomo.toml")
        policy = RepositoryPolicy(
            name="owner/repo",
            mode="maintainer",
            submission_strategy="same-repository",
            branch_cleanup=True,
        )
        config = replace(
            base,
            state_dir=root / "state",
            workspace_dir=root / "workspaces",
            github=replace(base.github, login="owner"),
            repositories={"owner/repo": policy},
        )
        self.pipeline = Pipeline(config)
        self.run_id = _seed_merged_run(self.pipeline.store)
        self.github = FakeGitHub()
        self.workspaces = FakeWorkspaces(self.github)
        self.pipeline.github = self.github
        self.pipeline.workspaces = self.workspaces

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _apply(self, digest: str, *, reviewed_by: str = "owner") -> dict:
        with patch.dict(
            "os.environ", {"REPOSTEWARD_ENABLE_BRANCH_CLEANUP": "1"}, clear=False
        ):
            return self.pipeline.apply_branch_cleanup(
                "owner/repo", expected_digest=digest, reviewed_by=reviewed_by
            )

    def test_apply_records_intent_completion_and_fresh_post_plan(self) -> None:
        plan = self.pipeline.branch_cleanup_plan("owner/repo")

        result = self._apply(plan["plan_digest"])

        self.assertTrue(result["complete"])
        self.assertTrue(result["public_write"])
        self.assertEqual(self.workspaces.deleted, ["merged"])
        self.assertEqual(result["actions"][0]["outcome"], "deleted")
        self.assertEqual(result["remaining_plan"]["counts"]["completed"], 1)
        audits = self.pipeline.store.branch_cleanup_attempts(self.run_id)
        self.assertEqual(
            [(value["stage"], value["outcome"]) for value in audits],
            [("applying", "pending"), ("completed", "deleted")],
        )

    def test_stale_targeted_head_blocks_without_delete(self) -> None:
        plan = self.pipeline.branch_cleanup_plan("owner/repo")
        self.github.targeted_sha = "b" * 40

        result = self._apply(plan["plan_digest"])

        self.assertFalse(result["complete"])
        self.assertFalse(result["public_write"])
        self.assertEqual(result["actions"][0]["outcome"], "blocked")
        self.assertIn("head_changed", result["actions"][0]["reasons"])
        self.assertEqual(self.workspaces.deleted, [])

    def test_ambiguous_delete_is_reconciled(self) -> None:
        plan = self.pipeline.branch_cleanup_plan("owner/repo")
        self.workspaces.ambiguous = True

        result = self._apply(plan["plan_digest"])

        self.assertTrue(result["complete"])
        self.assertEqual(result["actions"][0]["outcome"], "reconciled_deleted")

    def test_unknown_outcome_remains_pending_then_reconciles_without_new_write(
        self,
    ) -> None:
        plan = self.pipeline.branch_cleanup_plan("owner/repo")
        self.github.fail_absent_read = True

        first = self._apply(plan["plan_digest"])

        self.assertFalse(first["complete"])
        self.assertEqual(first["actions"][0]["outcome"], "outcome_unknown")
        self.github.fail_absent_read = False
        pending = self.pipeline.branch_cleanup_plan("owner/repo")
        self.assertEqual(pending["counts"]["pending"], 1)

        second = self._apply(pending["plan_digest"])

        self.assertTrue(second["complete"])
        self.assertFalse(second["public_write"])
        self.assertEqual(second["actions"][0]["outcome"], "reconciled_deleted")
        self.assertEqual(self.workspaces.deleted, ["merged"])

    def test_new_maintainer_can_reconcile_old_pending_intent_without_write(
        self,
    ) -> None:
        plan = self.pipeline.branch_cleanup_plan("owner/repo")
        self.github.fail_absent_read = True
        first = self._apply(plan["plan_digest"])
        self.assertEqual(first["actions"][0]["outcome"], "outcome_unknown")
        self.github.fail_absent_read = False
        self.github.login = "new-owner"
        self.pipeline.config = replace(
            self.pipeline.config,
            github=replace(self.pipeline.config.github, login="new-owner"),
        )
        pending = self.pipeline.branch_cleanup_plan("owner/repo")

        second = self._apply(pending["plan_digest"], reviewed_by="new-owner")

        self.assertTrue(second["complete"])
        self.assertFalse(second["public_write"])
        self.assertEqual(second["actions"][0]["outcome"], "reconciled_deleted")
        audits = self.pipeline.store.branch_cleanup_attempts(self.run_id)
        self.assertEqual(audits[-1]["actor"], "owner")
        self.assertEqual(audits[-1]["payload"]["observed_by"], "new-owner")

    def test_absent_branch_reappearing_after_review_is_not_deleted(self) -> None:
        self.github.branch_values.clear()
        plan = self.pipeline.branch_cleanup_plan("owner/repo")
        self.assertEqual(plan["counts"]["already_absent"], 1)
        self.github.reappear_on_authentication = True

        result = self._apply(plan["plan_digest"])

        self.assertFalse(result["complete"])
        self.assertFalse(result["public_write"])
        self.assertEqual(result["actions"][0]["outcome"], "blocked")
        self.assertIn("branch_reappeared_after_plan", result["actions"][0]["reasons"])
        self.assertEqual(self.workspaces.deleted, [])

    def test_apply_requires_config_environment_digest_and_identity(self) -> None:
        plan = self.pipeline.branch_cleanup_plan("owner/repo")
        with self.assertRaisesRegex(PolicyError, "does not match"):
            self._apply("f" * 64)
        with self.assertRaisesRegex(PolicyError, "disabled"):
            self.pipeline.apply_branch_cleanup(
                "owner/repo",
                expected_digest=plan["plan_digest"],
                reviewed_by="owner",
            )
        with (
            patch.dict(
                "os.environ",
                {"REPOSTEWARD_ENABLE_BRANCH_CLEANUP": "1"},
                clear=False,
            ),
            self.assertRaisesRegex(PolicyError, "reviewed-by"),
        ):
            self.pipeline.apply_branch_cleanup(
                "owner/repo",
                expected_digest=plan["plan_digest"],
                reviewed_by="someone-else",
            )
        self.assertEqual(self.workspaces.deleted, [])


class NativeDeleteTests(unittest.TestCase):
    def test_git_delete_uses_atomic_lease_without_tokens_or_hooks(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            patch(
                "reposteward.workspace.subprocess.run", return_value=completed
            ) as run,
            patch.dict("os.environ", {"GH_TOKEN": "secret"}),
        ):
            WorkspaceManager.delete_remote_branch(
                "owner/repo", "owner/feature", expected_sha="a" * 40
            )

        push = run.call_args_list[-1]
        command = push.args[0]
        environment = push.kwargs["env"]
        self.assertIn("core.hooksPath=/dev/null", command)
        self.assertIn(
            f"--force-with-lease=refs/heads/owner/feature:{'a' * 40}", command
        )
        self.assertEqual(command[-1], ":refs/heads/owner/feature")
        self.assertNotIn("GH_TOKEN", environment)


class BranchCleanupMigrationTests(unittest.TestCase):
    def test_cleanup_backlog_keeps_every_submitted_run_for_one_issue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            first = _seed_merged_run(store)
            second = store.start_run("owner/repo", 7, "pull_request")
            store.update_run(
                second,
                status="submitted",
                details={
                    "branch": "second",
                    "commit_sha": "b" * 40,
                    "pr_url": "https://github.com/owner/repo/pull/8",
                },
            )

            runs = store.branch_cleanup_runs("owner/repo")

        self.assertEqual({value["run_id"] for value in runs}, {first, second})

    def test_version_sixteen_database_receives_cleanup_audit_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            Store(path)
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TABLE branch_cleanup_attempts")
                connection.execute("PRAGMA user_version=16")

            migrated = Store(path)
            with sqlite3.connect(path) as connection:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(branch_cleanup_attempts)"
                    )
                }
            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertIn("attempt_id", columns)
            self.assertIn("lease_generation", columns)


if __name__ == "__main__":
    unittest.main()
