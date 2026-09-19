from __future__ import annotations

import io
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from dataclasses import replace
from unittest.mock import patch

import test_external_tasks
import test_external_verification
from test_projects import git, repository

from reposteward.cli import main
from reposteward.integrations.mcp import ScopedBridge
from reposteward.projects.registry import canonical_digest
from reposteward.storage.store import Store, StoreError
from reposteward.tasks.external import TaskConflict
from reposteward.tasks.lifecycle import TaskLifecycle
from reposteward.tasks.lifecycle_store import TaskLifecycleRecords
from reposteward.web.workbench import Workbench
from reposteward.workflows.policy import PolicyError


class TaskLifecycleTests(unittest.TestCase):
    container_run = test_external_verification.ExternalVerificationTests.container_run

    def setUp(self):
        test_external_tasks.ExternalTaskTests.setUp(self)
        self.task = self.service.start(self.repo, issue_number=7, reviewed_by="owner")
        self.lifecycle = TaskLifecycle(self.config)
        self.store = Store(self.service.path)

    def plan(self, outcome="cancelled", target=""):
        return self.lifecycle.plan(
            self.task["run_id"],
            outcome=outcome,
            reason="Reviewed outcome",
            target_run_id=target,
        )

    def apply(self, plan, key="finish"):
        return self.lifecycle.resolve(
            **plan["operation"],
            plan_digest=plan["plan_digest"],
            reviewed_by="owner",
            idempotency_key=key,
        )

    def delivery(self, *, issue=7, head=None, outcome="merged", payload=None):
        head = head or self.task["snapshot"]["head"]
        run = self.store.start_run("owner/repo", issue, "publish")
        self.store.update_run(run, status="submitted", details={"commit_sha": head})
        snapshot = {
            "repository": "owner/repo",
            "pull_number": 8,
            "head_sha": head,
            "base_sha": "a" * 40,
            "policy_digest": "b" * 64,
        }
        decision = {
            "eligible": True,
            "reasons": [],
            "risk_categories": [],
            "risk_files": [],
            "snapshot_digest": canonical_digest(snapshot),
        }
        decision["decision_digest"] = canonical_digest(decision)
        decision["snapshot"] = snapshot
        saved = self.store.append_merge_decision(**snapshot, decision=decision)
        self.store.append_merge_execution(
            attempt_id=run,
            run_id=run,
            decision_id=saved["id"],
            repository="owner/repo",
            pull_number=8,
            actor="owner",
            merge_method="squash",
            stage="completed",
            outcome=outcome,
            reason="fixture",
            decision_digest=decision["decision_digest"],
            head_sha=head,
            payload=payload
            if payload is not None
            else {"github_result": {"merged": True, "sha": "c" * 40}},
        )
        return run

    def test_cancellation_is_atomic_idempotent_and_preserves_checkpoint(self):
        before = self.service.inspect(self.task["run_id"])
        raw = self.service.path.read_bytes()
        with patch(
            "reposteward.github.client.GitHubClient",
            side_effect=AssertionError("offline"),
        ):
            plan = self.plan()
            self.assertEqual(raw, self.service.path.read_bytes())
            result = self.apply(plan)
            retry = self.apply(plan)
        self.assertTrue(plan["eligible"])
        self.assertEqual(result["resolution"], retry["resolution"])
        self.assertTrue(retry["idempotent"])
        after = self.service.inspect(self.task["run_id"])
        self.assertEqual(after["status"], "cancelled")
        self.assertEqual(before["checkpoint"], after["checkpoint"])
        self.assertEqual(before["revision"], after["revision"])
        self.assertIsNotNone(after["resolution"])
        with self.assertRaises(TaskConflict):
            self.apply(plan, key="another")
        with self.assertRaises(KeyError):
            self.service.current(self.repo)
        with self.assertRaises(TaskConflict):
            self.service.checkpoint(
                self.task["run_id"],
                expected_revision=0,
                expected_snapshot=self.task["snapshot"]["digest"],
                idempotency_key="after-end",
                payload={"next_action": "must reject"},
            )

    def test_cli_mcp_and_workbench_read_the_same_resolution(self):
        self.apply(self.plan())
        report = self.service.context(self.task["run_id"], live=True)
        bridge = ScopedBridge(self.config, self.repo)
        mcp = bridge.call("context", {"run_id": self.task["run_id"]})
        web = Workbench(self.config).task(self.task["project_id"], self.task["run_id"])
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(
                main(["task", "context", self.task["run_id"], "--live"]), 0
            )
        cli = json.loads(stdout.getvalue())
        for value in (mcp, web["context"], cli):
            self.assertEqual(value["resolution"], report["resolution"])
            self.assertEqual(value["open_work"], [])
            self.assertEqual(value["status"], "cancelled")
            self.assertEqual(value["claim_trust"], "agent_unverified")

    def test_completed_requires_exact_native_merge_and_survives_base_advance(self):
        run = self.delivery()
        # A real new base commit invalidates development, not a historical resolution.
        (self.repo / "new.txt").write_text("new base")
        git(self.repo, "add", "new.txt")
        git(self.repo, "commit", "-m", "base advanced")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertIn(
            "base_changed",
            self.service.inspect(self.task["run_id"], live=True)["validity"],
        )
        plan = self.plan("completed", run)
        self.assertTrue(plan["eligible"], plan["reasons"])
        self.apply(plan)
        self.assertEqual(
            self.service.inspect(self.task["run_id"])["status"], "completed"
        )
        self.assertEqual(
            self.service.inspect(self.task["run_id"])["checkpoint"]["status"], "running"
        )

    def test_completion_rejects_wrong_issue_head_missing_or_failed_merge(self):
        for run in (
            self.delivery(issue=99),
            self.delivery(head="d" * 40),
            self.delivery(outcome="failed"),
            self.delivery(payload={}),
            "0" * 32,
        ):
            with self.subTest(run=run):
                plan = self.plan("completed", run)
                self.assertFalse(plan["eligible"])
                with self.assertRaises(TaskConflict):
                    self.apply(plan)
        self.assertIsNone(TaskLifecycleRecords(self.store).get(self.task["run_id"]))

    def test_dirty_checkpoint_is_not_delivery_evidence(self):
        (self.repo / "source.txt").write_text("not delivered")
        current = self.service.inspect(self.task["run_id"], live=True)
        self.service.checkpoint(
            self.task["run_id"],
            expected_revision=0,
            expected_snapshot=current["current_snapshot"]["digest"],
            idempotency_key="dirty",
            payload={"next_action": "verify"},
        )
        self.assertFalse(self.plan("completed", self.delivery())["eligible"])

    def test_already_merged_receipt_is_supported(self):
        target = self.delivery(
            outcome="already_merged", payload={"merge_commit_sha": "c" * 40}
        )
        self.assertTrue(self.plan("completed", target)["eligible"])

    def test_supersession_requires_same_workspace_and_active_successor(self):
        successor = self.service.start(self.repo, issue_number=7, reviewed_by="owner")
        plan = self.plan("superseded", successor["run_id"])
        self.assertTrue(plan["eligible"])
        self.apply(plan)
        self.assertEqual(self.service.current(self.repo)["run_id"], successor["run_id"])
        self.assertEqual(
            self.service.inspect(self.task["run_id"])["status"], "superseded"
        )
        self.assertFalse(
            self.lifecycle.plan(
                successor["run_id"],
                outcome="superseded",
                target_run_id=self.task["run_id"],
                reason="no cycle",
            )["eligible"]
        )

    def test_other_workspace_and_self_cannot_be_successors(self):
        other = repository(self.root / "other")
        git(other, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(other, "switch", "-c", "feature")
        self.github.branch_head_sha.return_value = git(other, "rev-parse", "HEAD")
        self.service.registry.link(other)
        successor = self.service.start(other, issue_number=7, reviewed_by="owner")
        for target in (successor["run_id"], self.task["run_id"]):
            self.assertFalse(self.plan("superseded", target)["eligible"])

    def test_stale_checkpoint_or_successor_invalidates_plan(self):
        plan = self.plan()
        self.service.checkpoint(
            self.task["run_id"],
            expected_revision=0,
            expected_snapshot=self.task["snapshot"]["digest"],
            idempotency_key="new",
            payload={"next_action": "updated requirement"},
        )
        with self.assertRaisesRegex(TaskConflict, "plan changed"):
            self.apply(plan)
        successor = self.service.start(self.repo, issue_number=7, reviewed_by="owner")
        plan = self.plan("superseded", successor["run_id"])
        other_plan = self.lifecycle.plan(
            successor["run_id"], outcome="cancelled", reason="stop"
        )
        self.apply(other_plan)
        with self.assertRaisesRegex(TaskConflict, "plan changed"):
            self.apply(plan)

    def test_reviewer_account_host_and_reason_are_checked(self):
        plan = self.plan()
        with self.assertRaises(PolicyError):
            self.lifecycle.resolve(
                **plan["operation"],
                plan_digest=plan["plan_digest"],
                reviewed_by="someone",
                idempotency_key="no",
            )
        for github in (
            replace(self.config.github, login="someone"),
            replace(
                self.config.github, api_url="https://github.example.invalid/api/v3"
            ),
        ):
            other = TaskLifecycle(replace(self.config, github=github))
            self.assertFalse(other.plan(**plan["operation"])["eligible"])
        with self.assertRaises(ValueError):
            self.lifecycle.plan(self.task["run_id"], outcome="cancelled", reason="")

    def test_transaction_failure_rolls_back_receipt_and_status(self):
        plan = self.plan()
        with (
            patch(
                "reposteward.storage.store.Store.update_run",
                side_effect=RuntimeError("interrupted"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.apply(plan)
        self.assertIsNone(TaskLifecycleRecords(self.store).get(self.task["run_id"]))
        self.assertEqual(self.store.run(self.task["run_id"])["status"], "running")
        self.assertFalse(self.apply(plan)["idempotent"])

    def test_concurrent_identical_resolution_is_recorded_once(self):
        plan = self.plan()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.apply(plan), range(2)))
        self.assertEqual(sum(not result["idempotent"] for result in results), 1)
        with self.store._connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM external_task_resolutions").fetchone()[
                    0
                ],
                1,
            )

    def test_old_schema_is_not_migrated_by_plan_or_resolve(self):
        plan = self.plan()
        with closing(sqlite3.connect(self.service.path)) as db:
            db.execute("DROP TABLE external_task_resolutions")
            db.execute("PRAGMA user_version=22")
            db.commit()
        original = self.service.path.read_bytes()
        with self.assertRaises(StoreError):
            self.plan()
        with self.assertRaises(StoreError):
            self.apply(plan)
        self.assertEqual(original, self.service.path.read_bytes())
        Store(self.service.path)
        self.assertTrue(self.plan()["eligible"])
        self.assertEqual(self.service.inspect(self.task["run_id"])["revision"], 0)

    def test_cli_plan_and_resolve(self):
        args = [
            self.task["run_id"],
            "--outcome",
            "cancelled",
            "--reason",
            "Reviewed outcome",
        ]
        with patch("reposteward.cli.load_config", return_value=self.config):
            with redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(["task", "resolve-plan", *args]), 0)
            plan = json.loads(stdout.getvalue())
            with redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(
                    main(
                        [
                            "task",
                            "resolve",
                            *args,
                            "--plan-digest",
                            plan["plan_digest"],
                            "--reviewed-by",
                            "owner",
                            "--idempotency-key",
                            "cli",
                        ]
                    ),
                    0,
                )
            self.assertFalse(json.loads(stdout.getvalue())["public_write"])

    def test_inflight_verification_blocks_resolution_and_terminal_blocks_new_checks(
        self,
    ):
        test_external_verification.ExternalVerificationTests.setUp(self)
        self.lifecycle = TaskLifecycle(self.config)
        self.store = Store(self.service.path)
        evidence = self.check.request(
            self.task["run_id"],
            profile="test",
            expected_revision=0,
            expected_snapshot=self.task["snapshot"]["digest"],
            idempotency_key="verify",
        )
        identifier = evidence["evidence_id"].split(":")[1]
        with self.store._connection() as db:
            db.execute(
                "UPDATE external_verifications SET outcome='running' WHERE id=?",
                (identifier,),
            )
        plan = self.plan()
        self.assertIn("verification_in_progress_or_unreconciled", plan["reasons"])
        with self.assertRaises(TaskConflict):
            self.apply(plan)
        with self.store._connection() as db:
            db.execute(
                "UPDATE external_verifications SET outcome='passed' WHERE id=?",
                (identifier,),
            )
        self.apply(self.plan())
        self.mock_container.reset_mock()
        with self.assertRaises(TaskConflict):
            self.check.request(
                self.task["run_id"],
                profile="test",
                expected_revision=0,
                expected_snapshot=self.task["snapshot"]["digest"],
                idempotency_key="after-end",
            )
        self.mock_container.assert_not_called()
