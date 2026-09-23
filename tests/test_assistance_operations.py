from __future__ import annotations

import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from threading import Event
from unittest.mock import patch

import test_external_verification
from test_projects import git

from reposteward.cli import main
from reposteward.integrations.mcp import ScopedBridge
from reposteward.projects.registry import ProjectError
from reposteward.tasks.assistance_operations import AssistanceOperations
from reposteward.tasks.local_operations import OperationError
from reposteward.verification.recovery import VerificationRecovery


class AssistanceOperationTests(unittest.TestCase):
    container_run = test_external_verification.ExternalVerificationTests.container_run

    def setUp(self):
        test_external_verification.ExternalVerificationTests.setUp(self)
        self.bridge = ScopedBridge(self.config, self.repo)
        self.ops = AssistanceOperations(self.bridge)
        self.ops.verification = self.check

    def start_operation(self, key="one"):
        current = self.service.inspect(self.task["run_id"], live=True)
        return self.ops.start(
            "verification",
            run_id=self.task["run_id"],
            profile="test",
            expected_revision=current["revision"],
            expected_snapshot=current["current_snapshot"]["digest"],
            idempotency_key=key,
        )

    def test_enqueue_is_durable_deduplicated_and_does_not_run_tests(self):
        pending = self.start_operation()
        self.assertFalse(self.calls)
        self.assertEqual(self.start_operation()["id"], pending["id"])
        self.assertEqual(self.start_operation("different-key")["id"], pending["id"])
        self.assertTrue(self.ops.process_once())
        result = AssistanceOperations(ScopedBridge(self.config, self.repo)).get(
            pending["id"]
        )
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["stages"][-1]["result"]["outcome"], "passed")
        self.assertFalse(result["task_completion"])
        self.assertEqual(self.service.inspect(self.task["run_id"])["status"], "running")
        self.assertFalse(self.ops.process_once())
        self.assertEqual(len(self.calls), 2)

    def test_idempotency_conflict_and_scope_revocation(self):
        pending = self.start_operation()
        (self.repo / "source.txt").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.start_operation()
        self.service.registry.unlink(self.task["binding_id"])
        with self.assertRaises(ProjectError):
            self.ops.get(pending["id"])

    def test_cross_binding_and_cross_account_are_not_listed_or_read(self):
        pending = self.start_operation()
        other = self.root / "other"
        git(self.repo, "worktree", "add", "-b", "owner/other", str(other))
        self.service.registry.link(other)
        scoped = AssistanceOperations(ScopedBridge(self.config, other))
        self.assertEqual(scoped.listing()["items"], [])
        with self.assertRaises(ProjectError):
            scoped.get(pending["id"])
        account = replace(
            self.config, github=replace(self.config.github, login="someone")
        )
        with self.assertRaises(KeyError):
            AssistanceOperations(ScopedBridge(account, self.repo)).get(pending["id"])

    def test_pending_cancel_is_atomic_and_wait_does_not_execute(self):
        pending = self.start_operation()
        self.assertEqual(self.ops.wait(pending["id"], timeout=0)["state"], "pending")
        cancelled = self.ops.cancel(
            pending["id"],
            expected_revision=pending["revision"],
            idempotency_key="cancel",
        )
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(
            self.ops.cancel(
                pending["id"],
                expected_revision=pending["revision"],
                idempotency_key="cancel",
            )["state"],
            "cancelled",
        )
        self.assertFalse(self.ops.process_once())
        self.assertFalse(self.calls)

    def test_running_cancellation_waits_for_executor_acknowledgment(self):
        pending = self.start_operation()
        entered, release = Event(), Event()

        def execute(store, task, payload, cancel):
            entered.set()
            self.assertTrue(cancel.wait(5))
            self.assertTrue(release.wait(5))
            return {"outcome": "cancelled", "public_write": False}

        with (
            patch.object(self.ops, "_execute", side_effect=execute),
            ThreadPoolExecutor() as pool,
        ):
            future = pool.submit(self.ops.process_once)
            self.assertTrue(entered.wait(5))
            running = self.ops.get(pending["id"])
            requested = self.ops.cancel(
                pending["id"],
                expected_revision=running["revision"],
                idempotency_key="cancel",
            )
            self.assertEqual(requested["state"], "running")
            self.assertTrue(requested["cancel_requested"])
            release.set()
            self.assertTrue(future.result(8))
        self.assertEqual(self.ops.get(pending["id"])["state"], "cancelled")

    def test_late_cancel_preserves_actual_result_and_exclusive_worker(self):
        pending = self.start_operation()
        entered, release = Event(), Event()
        execute = self.ops._execute

        def delayed(*args):
            actual = execute(*args)
            entered.set()
            self.assertTrue(release.wait(5))
            return actual

        with (
            patch.object(self.ops, "_execute", side_effect=delayed),
            ThreadPoolExecutor() as pool,
        ):
            future = pool.submit(self.ops.process_once)
            self.assertTrue(entered.wait(5))
            self.assertFalse(AssistanceOperations(self.bridge).process_once())
            running = self.ops.get(pending["id"])
            self.ops.cancel(
                pending["id"],
                expected_revision=running["revision"],
                idempotency_key="late-request",
            )
            release.set()
            future.result(8)
        result = self.ops.get(pending["id"])
        self.assertEqual(result["state"], "completed")
        self.assertTrue(result["cancel_requested"])
        with self.assertRaises(OperationError):
            self.ops.cancel(
                pending["id"],
                expected_revision=result["revision"],
                idempotency_key="late",
            )
        self.assertEqual(result["stages"][-1]["result"]["outcome"], "passed")

    def test_original_result_survives_worker_response_loss_without_reexecution(self):
        pending = self.start_operation()
        with patch.object(self.ops.local, "store", wraps=self.ops.local.store):
            # Simulate loss after native verification persisted, before queue completion.
            execute = self.ops._execute

            def lose(*args):
                execute(*args)
                raise RuntimeError("lost result")

            with patch.object(self.ops, "_execute", side_effect=lose):
                self.ops.process_once()
        failed = self.ops.get(pending["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertFalse(failed["can_retry"])
        self.ops.reconcile(
            pending["id"],
            expected_revision=failed["revision"],
            idempotency_key="recover",
        )
        self.assertTrue(self.ops.process_once())
        self.assertEqual(self.ops.get(pending["id"])["state"], "completed")
        self.assertEqual(len(self.calls), 2)

    def test_orphan_requires_reviewed_verification_reconciliation(self):
        pending = self.start_operation()
        execute = self.ops._execute

        def orphan(store, task, payload, cancel):
            result = execute(store, task, payload, cancel)
            with store._connection() as db:
                db.execute(
                    "UPDATE external_verifications SET outcome='running' WHERE id=?",
                    (result["evidence_id"].split(":")[1],),
                )
            raise RuntimeError("interrupted")

        with patch.object(self.ops, "_execute", side_effect=orphan):
            self.ops.process_once()
        failed = self.ops.get(pending["id"])
        with self.assertRaises(OperationError):
            self.ops.reconcile(
                pending["id"],
                expected_revision=failed["revision"],
                idempotency_key="recover",
            )
        _store, task, payload = self.ops._task(pending["id"])
        _, identifier = self.ops._verification_identity(task, payload)
        recovery = VerificationRecovery(self.config)
        plan = recovery.plan(
            payload["run_id"], identifier, reason="confirm stopped execution"
        )
        recovery.reconcile(
            payload["run_id"],
            identifier,
            reason="confirm stopped execution",
            reviewed_by="owner",
            plan_digest=plan["plan_digest"],
            idempotency_key="review",
        )
        self.ops.reconcile(
            pending["id"],
            expected_revision=failed["revision"],
            idempotency_key="recover",
        )
        self.ops.process_once()
        result = self.ops.get(pending["id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["stages"][-1]["result"]["outcome"], "unknown")
        self.assertEqual(len(self.calls), 2)

    def test_snapshot_and_profile_changes_block_execution(self):
        pending = self.start_operation()
        (self.repo / "source.txt").write_text("changed\n")
        self.ops.process_once()
        self.assertEqual(self.ops.get(pending["id"])["state"], "failed")
        self.assertFalse(self.calls)
        pending = self.start_operation("new")
        self.ops.verification.config = replace(
            self.config,
            verification_profiles=(
                replace(self.config.verification_profiles[0], commands=("python -V",)),
            ),
        )
        self.ops.process_once()
        self.assertEqual(self.ops.get(pending["id"])["state"], "failed")
        self.assertFalse(self.calls)

    def test_report_uses_pinned_index_and_mcp_cli_share_facts(self):
        self.ops.understanding.scan(self.repo)
        pending = self.bridge.call(
            "operation", {"action": "start_understanding", "idempotency_key": "guide"}
        )
        self.assertTrue(self.ops.process_once())
        result = self.bridge.call(
            "operation", {"action": "get", "operation_id": pending["id"]}
        )
        self.assertEqual(result["state"], "completed")
        self.assertEqual(
            result["stages"][-1]["result"]["evidence_kind"], "historical_project_report"
        )
        output = io.StringIO()
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            patch(
                "reposteward.cli.Pipeline",
                side_effect=AssertionError("must not initialize Pipeline"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(
                    [
                        "--json-envelope",
                        "operation",
                        "--workspace",
                        str(self.repo),
                        "get",
                        pending["id"],
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(output.getvalue())["data"], result)

    def test_no_implicit_schema_upgrade_or_arbitrary_files(self):
        pending = self.start_operation()
        store = self.ops.local.store(write=True)
        with store._connection() as db:
            db.execute("PRAGMA user_version=22")
        with self.assertRaises(OperationError):
            self.ops.get(pending["id"])
        with self.assertRaises(ValueError):
            self.ops.reconcile(
                "../../unexpected",
                expected_revision="a" * 64,
                idempotency_key="recover",
            )
        with store._connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 22)

    def test_github_worker_cannot_claim_assistance_jobs(self):
        pending = self.start_operation()
        store = self.ops.local.store(write=True)
        self.assertEqual(
            store.claim_queue_tasks(
                worker="github",
                operation_family="local",
                account_digest=self.ops.local.account,
                actions=("github.sync",),
            ),
            [],
        )
        self.assertEqual(self.ops.get(pending["id"])["state"], "pending")

    def test_expired_lease_still_cannot_steal_active_execution_lock(self):
        pending = self.start_operation()
        store = self.ops.local.store(write=True)
        claimed = store.claim_queue_tasks(
            worker="old-worker",
            operation_family="local",
            account_digest=self.ops.local.account,
            task_id=pending["id"],
        )[0]
        with store._connection() as db:
            db.execute(
                "UPDATE queue_tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (pending["id"],),
            )
        with self.ops._lock(pending["id"]):
            self.assertFalse(AssistanceOperations(self.bridge).process_once())
        self.assertTrue(self.ops.process_once())
        final = self.ops.get(pending["id"])
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["attempt_count"], claimed["attempt_count"] + 1)
        self.assertEqual(len(self.calls), 2)

    def test_queue_result_failure_rolls_back_but_preserves_native_evidence(self):
        pending = self.start_operation()
        store = self.ops.local.store(write=True)
        with store._connection() as db:
            db.execute(
                "CREATE TRIGGER reject_complete BEFORE UPDATE ON queue_tasks WHEN NEW.state='completed' BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        self.ops.process_once()
        result = self.ops.get(pending["id"])
        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["stages"])
        with store._connection() as db:
            self.assertEqual(
                db.execute("SELECT outcome FROM external_verifications").fetchone()[0],
                "passed",
            )

    def test_preexisting_verification_must_match_the_entire_queued_request(self):
        pending = self.start_operation()
        (self.repo / "source.txt").write_text("different snapshot\n")
        current = self.service.inspect(self.task["run_id"], live=True)
        self.check.request(
            self.task["run_id"],
            profile="test",
            expected_revision=0,
            expected_snapshot=current["current_snapshot"]["digest"],
            idempotency_key="operation:" + pending["id"],
        )
        self.ops.process_once()
        result = self.ops.get(pending["id"])
        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["stages"])
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
