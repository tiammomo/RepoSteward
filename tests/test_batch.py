from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from reposteward.batch import build_batch_plan, render_batch_plan_text
from reposteward.github import PullRequest
from reposteward.pipeline import BatchConflictError, BatchDeferred, Pipeline
from reposteward.policy import PolicyError


def _pull(number: int, files: list[str], *, draft: bool = False) -> dict:
    return {
        "number": number,
        "draft": draft,
        "head_branch": f"tiammomo/change-{number}",
        "head_sha": str(number) * 40,
        "base_branch": "main",
        "base_sha": "b" * 40,
        "files": files,
        "facts_complete": True,
    }


def _run(number: int, *, status: str = "submitted") -> dict:
    return {
        "id": f"run-{number}",
        "repository": "owner/repo",
        "issue_number": number,
        "status": status,
        "submission_pr_url": f"https://github.com/owner/repo/pull/{number}",
        "details": {
            "branch": f"tiammomo/change-{number}",
            "base_branch": "main",
            "base_commit": "b" * 40,
            "commit_sha": str(number) * 40,
        },
    }


class BatchPlanTests(unittest.TestCase):
    def inputs(self):
        snapshot = {
            "repository": "owner/repo",
            "snapshot_digest": "s" * 64,
            "complete": True,
            "pull_requests": [
                _pull(3, ["three.py"]),
                _pull(1, ["shared.py"]),
                _pull(4, ["four.py"], draft=True),
                _pull(2, ["shared.py"]),
            ],
            "overlaps": [
                {"left": 1, "right": 2, "file_count": 1, "files": ["shared.py"]}
            ],
        }
        dependency = {
            "repository": "owner/repo",
            "portfolio_snapshot_digest": "s" * 64,
            "plan_digest": "d" * 64,
            "complete": True,
            "suggested_merge_order": [1, 2, 3, 4],
            "ready_blockers": {"3": ["dependency_open:#1"]},
            "authoritative_edges": [
                {
                    "pull_number": 3,
                    "dependency_number": 1,
                    "status": "open",
                }
            ],
        }
        return snapshot, dependency

    def test_plan_is_stable_and_overlap_does_not_become_a_dependency(self) -> None:
        snapshot, dependency = self.inputs()

        first = build_batch_plan(
            snapshot,
            dependency,
            [_run(4), _run(2), _run(1), _run(3)],
            wip_limit=3,
            max_parallel=2,
        )
        second = build_batch_plan(
            {**snapshot, "pull_requests": list(reversed(snapshot["pull_requests"]))},
            dependency,
            [_run(3), _run(1), _run(2), _run(4)],
            wip_limit=3,
            max_parallel=2,
        )

        self.assertEqual(first, second)
        self.assertEqual(first["queue_order"], [1, 2, 3])
        self.assertEqual(first["parallel_sets"], [[1], [2, 3]])
        self.assertEqual(first["overlap_groups"], [[1, 2]])
        self.assertFalse(first["overlap_is_authoritative"])
        self.assertEqual(first["blocked_pull_requests"], {"4": ["pull_is_draft"]})
        self.assertTrue(first["wip"]["over_limit"])
        text = render_batch_plan_text(
            {"batch_digest": first.pop("batch_digest"), "plan": first}
        )
        self.assertIn("Queue order: #1 -> #2 -> #3", text)

    def test_incomplete_or_ambiguous_facts_fail_closed_per_pull(self) -> None:
        snapshot, dependency = self.inputs()
        snapshot["complete"] = False
        runs = [_run(1), _run(1), _run(2), _run(3), _run(4)]

        plan = build_batch_plan(snapshot, dependency, runs, wip_limit=8, max_parallel=4)

        self.assertFalse(plan["complete"])
        self.assertEqual(plan["queue_order"], [])
        self.assertIn("ambiguous_local_run", plan["blocked_pull_requests"]["1"])
        self.assertTrue(
            all(
                "portfolio_snapshot_incomplete" in blockers
                for blockers in plan["blocked_pull_requests"].values()
            )
        )

    def test_local_blocker_propagates_to_dependent_pull_requests(self) -> None:
        snapshot, dependency = self.inputs()
        dependency["authoritative_edges"] = [
            {"pull_number": 3, "dependency_number": 1, "status": "open"}
        ]

        plan = build_batch_plan(
            snapshot,
            dependency,
            [_run(2), _run(3), _run(4)],
            wip_limit=8,
            max_parallel=4,
        )

        self.assertEqual(plan["queue_order"], [2])
        self.assertEqual(plan["blocked_pull_requests"]["1"], ["local_run_missing"])
        self.assertIn(
            "dependency_prerequisite_not_runnable:#1",
            plan["blocked_pull_requests"]["3"],
        )

    def test_replay_requires_recoverable_worktree_and_verification_commands(
        self,
    ) -> None:
        snapshot, dependency = self.inputs()
        snapshot["pull_requests"] = [_pull(1, ["one.py"])]
        snapshot["pull_requests"][0]["base_sha"] = "c" * 40
        snapshot["overlaps"] = []
        dependency["suggested_merge_order"] = [1]
        dependency["ready_blockers"] = {}
        dependency["authoritative_edges"] = []
        run = _run(1)
        run["details"]["agent_result"] = {
            "verification_commands": ["python -m unittest"]
        }
        run["batch_worktree_available"] = False

        blocked = build_batch_plan(
            snapshot, dependency, [run], wip_limit=8, max_parallel=4
        )
        run["batch_worktree_available"] = True
        ready = build_batch_plan(
            snapshot, dependency, [run], wip_limit=8, max_parallel=4
        )

        self.assertEqual(
            blocked["blocked_pull_requests"]["1"], ["replay_worktree_missing"]
        )
        self.assertEqual(ready["queue_order"], [1])
        self.assertTrue(ready["pull_requests"][0]["replay_required"])

    def test_mismatched_snapshot_digest_is_rejected(self) -> None:
        snapshot, dependency = self.inputs()
        dependency["portfolio_snapshot_digest"] = "x" * 64

        with self.assertRaisesRegex(ValueError, "different portfolio snapshots"):
            build_batch_plan(snapshot, dependency, [], wip_limit=4, max_parallel=1)

    def test_long_dependency_chain_uses_iterative_ready_sets(self) -> None:
        count = 1_000
        pulls = []
        runs = []
        for number in range(1, count + 1):
            sha = f"{number:040x}"
            pull = _pull(number, [f"src/{number}.py"])
            pull["head_sha"] = sha
            run = _run(number)
            run["details"]["commit_sha"] = sha
            pulls.append(pull)
            runs.append(run)
        snapshot = {
            "repository": "owner/repo",
            "snapshot_digest": "s" * 64,
            "complete": True,
            "pull_requests": pulls,
            "overlaps": [],
        }
        dependency = {
            "repository": "owner/repo",
            "portfolio_snapshot_digest": "s" * 64,
            "plan_digest": "d" * 64,
            "complete": True,
            "suggested_merge_order": list(range(1, count + 1)),
            "ready_blockers": {
                str(number): [f"dependency_open:#{number - 1}"]
                for number in range(2, count + 1)
            },
            "authoritative_edges": [
                {
                    "pull_number": number,
                    "dependency_number": number - 1,
                    "status": "open",
                }
                for number in range(2, count + 1)
            ],
        }

        plan = build_batch_plan(
            snapshot, dependency, runs, wip_limit=2_000, max_parallel=32
        )

        self.assertEqual(len(plan["parallel_sets"]), count)
        self.assertEqual(plan["parallel_sets"][0], [1])
        self.assertEqual(plan["parallel_sets"][-1], [count])


class BatchApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline = object.__new__(Pipeline)
        self.pipeline.config = SimpleNamespace(github=SimpleNamespace(login="alice"))
        self.pipeline.enqueue_task = Mock()

    @staticmethod
    def plan(digest: str, *, matches: bool = True) -> dict:
        return {
            "batch_digest": digest,
            "matches_expected_digest": matches,
            "plan": {
                "repository": "owner/repo",
                "complete": True,
                "queue_order": [1, 2, 3],
                "blocked_pull_requests": {},
                "pull_requests": [
                    {
                        "pull_number": number,
                        "run_id": f"run-{number}",
                        "issue_number": number,
                        "head_sha": str(number) * 40,
                        "base_sha": "b" * 40,
                        "verified_base_sha": "b" * 40,
                    }
                    for number in (1, 2, 3)
                ],
            },
        }

    def test_apply_rechecks_digest_and_serializes_repository_writes(self) -> None:
        digest = "d" * 64
        self.pipeline.batch_plan = Mock(return_value=self.plan(digest))
        self.pipeline.enqueue_task.side_effect = [
            {
                "task": {
                    "id": f"task-{number}",
                    "action": "batch-advance",
                    "pull_number": number,
                    "state": "pending",
                    "depends_on_task_id": "" if number == 1 else f"task-{number - 1}",
                    "idempotent": False,
                }
            }
            for number in (1, 2, 3)
        ]

        with unittest.mock.patch.dict(
            os.environ, {"REPOSTEWARD_ENABLE_BATCH_APPLY": "1"}
        ):
            result = self.pipeline.batch_apply(
                "owner/repo", expected_digest=digest, reviewed_by="alice"
            )

        self.assertEqual(result["queue_order"], [1, 2, 3])
        self.assertEqual(
            [
                call.kwargs["depends_on_task_id"]
                for call in self.pipeline.enqueue_task.call_args_list
            ],
            ["", "task-1", "task-2"],
        )
        self.assertTrue(
            all(
                call.kwargs["max_attempts"] == 20
                for call in self.pipeline.enqueue_task.call_args_list
            )
        )
        self.assertFalse(result["public_write"])

    def test_apply_rejects_stale_plan_before_enqueue(self) -> None:
        self.pipeline.batch_plan = Mock(return_value=self.plan("d" * 64, matches=False))

        with (
            unittest.mock.patch.dict(
                os.environ, {"REPOSTEWARD_ENABLE_BATCH_APPLY": "1"}
            ),
            self.assertRaisesRegex(PolicyError, "stale"),
        ):
            self.pipeline.batch_apply(
                "owner/repo", expected_digest="d" * 64, reviewed_by="alice"
            )

        self.pipeline.enqueue_task.assert_not_called()


class BatchAdvanceTests(unittest.TestCase):
    def pipeline(self, reason_codes: tuple[str, ...]):
        run = {
            "id": "root-run",
            "repository": "owner/repo",
            "issue_number": 7,
            "status": "submitted",
            "details": {
                "branch": "tiammomo/change",
                "base_branch": "main",
                "base_commit": "b" * 40,
                "commit_sha": "a" * 40,
            },
        }
        pipeline = object.__new__(Pipeline)
        pipeline.config = SimpleNamespace(github=SimpleNamespace(login="alice"))
        pipeline.store = SimpleNamespace(
            run=Mock(return_value=run), latest_run=Mock(return_value=run)
        )
        pipeline.github = SimpleNamespace(
            pull_request=Mock(
                return_value=PullRequest(
                    number=7,
                    url="https://github.com/owner/repo/pull/7",
                    state="open",
                    draft=False,
                    head_branch="tiammomo/change",
                    head_sha="a" * 40,
                    base_branch="main",
                    base_sha="b" * 40,
                )
            )
        )
        snapshot = SimpleNamespace(mergeable="MERGEABLE")
        decision = SimpleNamespace(
            reasons=tuple(SimpleNamespace(code=value) for value in reason_codes)
        )
        policy = SimpleNamespace(owner_attestation=True)
        pipeline._current_merge_evaluation = Mock(
            return_value=(run, run["details"], policy, 7, snapshot, decision, {}, {})
        )
        pipeline.attest_owner_review = Mock(return_value={})
        pipeline.merge_decision = Mock(
            return_value={"eligible": True, "audit": {"id": "e" * 32}}
        )
        pipeline.execute_merge = Mock(
            return_value={
                "run_id": "root-run",
                "merged": True,
                "merge_commit_sha": "c" * 40,
                "public_write": True,
            }
        )
        return pipeline

    def advance(self, pipeline: Pipeline):
        return pipeline.batch_advance(
            "root-run",
            pull_number=7,
            reviewed_by="alice",
            batch_plan_digest="d" * 64,
            planned_head_sha="a" * 40,
            planned_base_sha="b" * 40,
            planned_verified_base_sha="b" * 40,
        )

    def test_pending_checks_defer_without_attesting_or_merging(self) -> None:
        pipeline = self.pipeline(("required_check_pending", "review_not_approved"))

        with (
            unittest.mock.patch.dict(
                os.environ, {"REPOSTEWARD_ENABLE_BATCH_APPLY": "1"}
            ),
            self.assertRaises(BatchDeferred),
        ):
            self.advance(pipeline)

        pipeline.attest_owner_review.assert_not_called()
        pipeline.execute_merge.assert_not_called()

    def test_owner_review_and_merge_reuse_existing_exact_gates(self) -> None:
        pipeline = self.pipeline(("review_not_approved",))

        with unittest.mock.patch.dict(
            os.environ, {"REPOSTEWARD_ENABLE_BATCH_APPLY": "1"}
        ):
            result = self.advance(pipeline)

        self.assertTrue(result["merged"])
        pipeline.attest_owner_review.assert_called_once_with(
            "root-run", reviewed_by="alice"
        )
        pipeline.merge_decision.assert_called_once_with("root-run")
        pipeline.execute_merge.assert_called_once_with(
            "root-run", decision_id="e" * 32, reviewed_by="alice"
        )

    def test_external_head_change_fails_closed_before_merge_reads(self) -> None:
        pipeline = self.pipeline(())
        pipeline.github.pull_request.return_value = PullRequest(
            number=7,
            url="https://github.com/owner/repo/pull/7",
            state="open",
            draft=False,
            head_branch="tiammomo/change",
            head_sha="f" * 40,
            base_branch="main",
            base_sha="b" * 40,
        )

        with (
            unittest.mock.patch.dict(
                os.environ, {"REPOSTEWARD_ENABLE_BATCH_APPLY": "1"}
            ),
            self.assertRaisesRegex(PolicyError, "head changed after planning"),
        ):
            self.advance(pipeline)

        pipeline._current_merge_evaluation.assert_not_called()

    def test_new_online_base_uses_the_separately_bound_verified_base(self) -> None:
        pipeline = self.pipeline(())
        pipeline.github.pull_request.return_value = PullRequest(
            number=7,
            url="https://github.com/owner/repo/pull/7",
            state="open",
            draft=False,
            head_branch="tiammomo/change",
            head_sha="a" * 40,
            base_branch="main",
            base_sha="c" * 40,
        )
        pipeline._batch_replay = Mock(return_value={"public_write": True})

        with (
            unittest.mock.patch.dict(
                os.environ, {"REPOSTEWARD_ENABLE_BATCH_APPLY": "1"}
            ),
            self.assertRaises(BatchDeferred),
        ):
            pipeline.batch_advance(
                "root-run",
                pull_number=7,
                reviewed_by="alice",
                batch_plan_digest="d" * 64,
                planned_head_sha="a" * 40,
                planned_base_sha="c" * 40,
                planned_verified_base_sha="b" * 40,
            )

        pipeline._batch_replay.assert_called_once()


class _ReplayStore:
    def __init__(self, source: dict) -> None:
        self.current = source

    def latest_run(self, _repository: str, _issue_number: int):
        return self.current

    def update_run(self, run_id: str, **values) -> None:
        self.current = {**self.current, "id": run_id, **values}


class _ReplayGitHub:
    def __init__(self, head: str, base: str) -> None:
        self.head = head
        self.base = base

    def pull_request_merge_snapshot(self, _repository: str, _number: int) -> dict:
        return {
            "state": "OPEN",
            "head_sha": self.head,
            "base_branch": "main",
            "base_sha": self.base,
        }


class BatchReplayTests(unittest.TestCase):
    def git(self, worktree: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def repository(self, root: Path, *, conflict: bool) -> tuple[Path, str, str, str]:
        remote = root / "remote.git"
        seed = root / "seed"
        worktree = root / "worktree"
        subprocess.run(
            ["git", "init", "--bare", remote], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "init", "-b", "main", seed], check=True, capture_output=True
        )
        self.git(seed, "config", "user.name", "Test")
        self.git(seed, "config", "user.email", "test@example.com")
        (seed / "shared.txt").write_text("base\n")
        self.git(seed, "add", "shared.txt")
        self.git(seed, "commit", "-m", "base")
        old_base = self.git(seed, "rev-parse", "HEAD")
        self.git(seed, "remote", "add", "origin", str(remote))
        self.git(seed, "push", "-u", "origin", "main")
        subprocess.run(
            ["git", "clone", "--branch", "main", str(remote), worktree],
            check=True,
            capture_output=True,
        )
        self.git(worktree, "config", "user.name", "Test")
        self.git(worktree, "config", "user.email", "test@example.com")
        self.git(worktree, "switch", "-c", "tiammomo/change")
        target = worktree / ("shared.txt" if conflict else "feature.txt")
        target.write_text("feature\n")
        self.git(worktree, "add", target.name)
        self.git(worktree, "commit", "-s", "-m", "feat: change")
        head = self.git(worktree, "rev-parse", "HEAD")
        if conflict:
            (seed / "shared.txt").write_text("main\n")
        else:
            (seed / "main.txt").write_text("main\n")
        self.git(seed, "add", ".")
        self.git(seed, "commit", "-m", "advance main")
        new_base = self.git(seed, "rev-parse", "HEAD")
        self.git(seed, "push", "origin", "main")
        return worktree, old_base, head, new_base

    def pipeline(self, worktree: Path, old_base: str, head: str, new_base: str):
        source = {
            "id": "root-run",
            "repository": "owner/repo",
            "issue_number": 7,
            "status": "submitted",
            "details": {
                "worktree": str(worktree),
                "base_branch": "main",
                "base_commit": old_base,
                "branch": "tiammomo/change",
                "commit_sha": head,
                "agent_result": {
                    "summary": "change",
                    "verification_commands": ["python -m unittest"],
                },
            },
        }
        pipeline = object.__new__(Pipeline)
        pipeline.config = SimpleNamespace(github=SimpleNamespace(sign_commits=False))
        pipeline.store = _ReplayStore(source)
        pipeline.github = _ReplayGitHub(head, new_base)

        @contextmanager
        def lease(_repository: str, _issue_number: int):
            yield SimpleNamespace(generation=1)

        pipeline._mutation_lease = lease

        def adopt(_repository, _issue, **_values):
            successor = {
                **source,
                "id": "successor-run",
                "status": "ready",
                "details": {
                    **source["details"],
                    "base_commit": new_base,
                    "commit_sha": pipeline._revision(worktree),
                },
            }
            pipeline.store.current = successor
            return {"run_id": "successor-run"}

        pipeline._adopt_leased = Mock(side_effect=adopt)
        pipeline._submit_leased = Mock(return_value={"public_write": True})
        return pipeline, source

    def test_clean_replay_reuses_verification_without_harness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree, old_base, head, new_base = self.repository(
                Path(directory), conflict=False
            )
            pipeline, source = self.pipeline(worktree, old_base, head, new_base)

            result = pipeline._batch_replay(
                source,
                pull_number=7,
                reviewed_by="tiammomo",
                batch_plan_digest="d" * 64,
                root_run_id="root-run",
                planned_base_sha=old_base,
            )

            self.assertTrue(result["public_write"])
            self.assertEqual(self.git(worktree, "status", "--porcelain"), "")
            self.assertEqual(
                self.git(worktree, "merge-base", "--is-ancestor", new_base, "HEAD"),
                "",
            )
            pipeline._adopt_leased.assert_called_once()
            self.assertEqual(
                pipeline._adopt_leased.call_args.kwargs["verification_commands"],
                ("python -m unittest",),
            )

    def test_conflict_aborts_and_restores_the_verified_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree, old_base, head, new_base = self.repository(
                Path(directory), conflict=True
            )
            pipeline, source = self.pipeline(worktree, old_base, head, new_base)

            with self.assertRaises(BatchConflictError):
                pipeline._batch_replay(
                    source,
                    pull_number=7,
                    reviewed_by="tiammomo",
                    batch_plan_digest="d" * 64,
                    root_run_id="root-run",
                    planned_base_sha=old_base,
                )

            self.assertEqual(self.git(worktree, "rev-parse", "HEAD"), head)
            self.assertEqual(self.git(worktree, "status", "--porcelain"), "")
            pipeline._adopt_leased.assert_not_called()


if __name__ == "__main__":
    unittest.main()
