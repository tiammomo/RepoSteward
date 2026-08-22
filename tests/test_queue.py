from __future__ import annotations

import os
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from reposteward.config import RepositoryPolicy
from reposteward.pipeline import BatchConflictError, BatchDeferred, Pipeline
from reposteward.policy import PolicyError
from reposteward.store import SCHEMA_VERSION, QueueLease, Store, StoreError


class QueueStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def enqueue(self, issue_number: int, **values):
        return self.store.enqueue_queue_task(
            "owner/repo",
            action="prepare",
            enqueued_by="alice",
            issue_number=issue_number,
            parameters={},
            **values,
        )

    def test_version_fifteen_database_receives_queue_migration(self) -> None:
        path = self.store.path
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DROP TABLE queue_attempts")
            connection.execute("DROP TABLE queue_tasks")
            connection.execute("PRAGMA user_version=15")

        migrated = Store(path)
        with closing(sqlite3.connect(path)) as connection, connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            claim_index = connection.execute(
                "SELECT name FROM sqlite_master WHERE name='queue_tasks_claim'"
            ).fetchone()
            lease_index = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE name='queue_tasks_expired_leases'
                """
            ).fetchone()
            lease_plan = " ".join(
                str(row[3])
                for row in connection.execute(
                    """
                    EXPLAIN QUERY PLAN
                    SELECT id FROM queue_tasks
                    WHERE state='running' AND lease_expires_at<=?
                      AND attempt_count>=max_attempts
                    """,
                    ("2999-01-01T00:00:00+00:00",),
                )
            )

        self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
        self.assertIn("queue_tasks", tables)
        self.assertIn("queue_attempts", tables)
        self.assertIsNotNone(claim_index)
        self.assertIsNotNone(lease_index)
        self.assertIn("queue_tasks_expired_leases", lease_plan)

    def test_enqueue_is_idempotent_bounded_and_dependency_aware(self) -> None:
        first = self.enqueue(1, priority=5)
        repeated = self.enqueue(1, priority=99)
        dependent = self.enqueue(2, depends_on_task_id=first["id"], priority=100)

        claimed = self.store.claim_queue_tasks(
            worker="worker-1", limit=10, lease_seconds=30
        )

        self.assertEqual(first["id"], repeated["id"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual([value["id"] for value in claimed], [first["id"]])
        self.assertIsInstance(claimed[0]["lease"], QueueLease)
        visible = self.store.queue_tasks(task_id=dependent["id"])[0]
        summary = self.store.queue_task_summary(repository="owner/repo")
        self.assertTrue(visible["blocked_by_dependency"])
        self.assertEqual(visible["attention"], "dependency")
        self.assertEqual(summary["counts"]["running"], 1)
        self.assertEqual(summary["counts"]["pending"], 1)
        self.assertEqual(summary["dependency_blocked"], 1)

        completed = self.store.complete_queue_task(
            claimed[0]["lease"], result={"status": "ready", "public_write": False}
        )
        next_claim = self.store.claim_queue_tasks(
            worker="worker-1", limit=10, lease_seconds=30
        )

        self.assertEqual(completed["state"], "completed")
        self.assertEqual([value["id"] for value in next_claim], [dependent["id"]])
        with self.assertRaisesRegex(ValueError, "unexpected parameters"):
            self.store.enqueue_queue_task(
                "owner/repo",
                action="submit",
                enqueued_by="alice",
                issue_number=3,
                parameters={"reviewed_by": "alice", "token": "secret"},
            )
        with self.assertRaisesRegex(ValueError, "absolute paths"):
            self.store.enqueue_queue_task(
                "owner/repo",
                action="submit",
                enqueued_by="alice",
                issue_number=3,
                parameters={"reviewed_by": "/tmp/account"},
            )

        run_id = self.store.start_run("owner/repo", 3, "follow-up")
        self.store.update_run(
            run_id,
            status="submitted",
            details={"pr_url": "https://github.com/owner/repo/pull/44"},
        )
        with self.assertRaisesRegex(StoreError, "pull number does not match"):
            self.store.enqueue_queue_task(
                "owner/repo",
                action="follow-up",
                enqueued_by="alice",
                run_id=run_id,
                pull_number=43,
                parameters={},
            )
        follow_up = self.store.enqueue_queue_task(
            "owner/repo",
            action="follow-up",
            enqueued_by="alice",
            run_id=run_id,
            pull_number=44,
            parameters={},
        )
        self.assertEqual(follow_up["issue_number"], 3)

    def test_batch_parameters_are_exact_bounded_hashes(self) -> None:
        parameters = {
            "reviewed_by": "alice",
            "batch_plan_digest": "d" * 64,
            "batch_head_sha": "a" * 40,
            "batch_base_sha": "b" * 40,
            "batch_verified_base_sha": "b" * 40,
        }

        Store._validate_queue_parameters("batch-advance", parameters)
        with self.assertRaisesRegex(ValueError, "batch_head_sha"):
            Store._validate_queue_parameters(
                "batch-advance", {**parameters, "batch_head_sha": "not-a-sha"}
            )

    def test_expired_claim_is_taken_over_and_old_generation_is_fenced(self) -> None:
        task = self.enqueue(3, max_attempts=2)
        started = datetime.now(UTC) + timedelta(seconds=1)
        first = self.store.claim_queue_tasks(
            worker="worker-1", limit=1, lease_seconds=5, now=started
        )[0]
        takeover_time = started + timedelta(seconds=6)
        second = self.store.claim_queue_tasks(
            worker="worker-2", limit=1, lease_seconds=5, now=takeover_time
        )[0]

        self.assertEqual(first["id"], task["id"])
        self.assertEqual(second["lease"].generation, 2)
        with self.assertRaisesRegex(StoreError, "lease is stale"):
            self.store.complete_queue_task(
                first["lease"],
                result={"status": "ready"},
                now=takeover_time,
            )
        completed = self.store.complete_queue_task(
            second["lease"],
            result={"status": "ready"},
            now=takeover_time + timedelta(seconds=1),
        )
        events = self.store.queue_attempts(task["id"])

        self.assertEqual(completed["state"], "completed")
        self.assertEqual(
            [value["event"] for value in events],
            ["enqueued", "claimed", "taken_over", "completed"],
        )

    def test_retry_limit_requires_manual_requeue_and_cancel_is_audited(self) -> None:
        task = self.enqueue(4, max_attempts=1)
        started = datetime.now(UTC) + timedelta(seconds=1)
        claimed = self.store.claim_queue_tasks(
            worker="worker", lease_seconds=30, now=started
        )[0]
        failed = self.store.fail_queue_task(
            claimed["lease"],
            error_code="github_transient",
            retryable=True,
            now=started + timedelta(seconds=1),
        )

        self.assertTrue(failed["manual_required"])
        self.assertEqual(
            self.store.claim_queue_tasks(
                worker="other", lease_seconds=30, now=started + timedelta(minutes=1)
            ),
            [],
        )
        requeued = self.store.requeue_queue_task(task["id"], requeued_by="alice")
        first_cancel = self.store.cancel_queue_task(
            task["id"], cancelled_by="alice", reason_code="superseded"
        )
        second_requeue = self.store.requeue_queue_task(task["id"], requeued_by="alice")
        cancelled = self.store.cancel_queue_task(
            task["id"], cancelled_by="alice", reason_code="still_superseded"
        )

        self.assertEqual(requeued["max_attempts"], 2)
        self.assertEqual(first_cancel["state"], "cancelled")
        self.assertEqual(second_requeue["state"], "pending")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(
            [value["event"] for value in self.store.queue_attempts(task["id"])],
            [
                "enqueued",
                "claimed",
                "failed",
                "requeued",
                "cancelled",
                "requeued",
                "cancelled",
            ],
        )

    def test_retryable_failure_backs_off_and_exhausted_crash_needs_attention(
        self,
    ) -> None:
        retryable = self.enqueue(5, max_attempts=2)
        crashed = self.enqueue(6, max_attempts=1)
        started = datetime.now(UTC) + timedelta(seconds=1)
        claimed = self.store.claim_queue_tasks(
            worker="worker-1", limit=2, lease_seconds=5, now=started
        )
        by_id = {value["id"]: value for value in claimed}
        failed = self.store.fail_queue_task(
            by_id[retryable["id"]]["lease"],
            error_code="github_transient",
            retryable=True,
            now=started + timedelta(seconds=1),
        )

        self.assertFalse(failed["manual_required"])
        before_backoff = self.store.claim_queue_tasks(
            worker="worker-2",
            limit=2,
            lease_seconds=5,
            now=started + timedelta(seconds=2),
        )
        after_backoff = self.store.claim_queue_tasks(
            worker="worker-2",
            limit=2,
            lease_seconds=5,
            now=started + timedelta(seconds=7),
        )
        crashed_state = self.store.queue_tasks(task_id=crashed["id"])[0]

        self.assertEqual(before_backoff, [])
        self.assertEqual([value["id"] for value in after_backoff], [retryable["id"]])
        self.assertEqual(crashed_state["state"], "failed")
        self.assertTrue(crashed_state["manual_required"])
        self.assertEqual(crashed_state["last_error_code"], "attempt_limit_exhausted")

    def test_atomic_batch_claim_does_not_duplicate_tasks_between_workers(self) -> None:
        task_ids = {self.enqueue(number)["id"] for number in range(10, 30)}

        def claim(worker: str) -> set[str]:
            return {
                value["id"]
                for value in self.store.claim_queue_tasks(
                    worker=worker, limit=20, lease_seconds=30
                )
            }

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(claim, "worker-1")
            second = executor.submit(claim, "worker-2")
            claimed = first.result() | second.result()
            overlap = first.result() & second.result()

        self.assertEqual(claimed, task_ids)
        self.assertEqual(overlap, set())

    def test_queue_records_are_reported_as_protected_storage(self) -> None:
        task = self.enqueue(30)
        statistics = {
            value["category"]: value
            for value in self.store.storage_statistics(repository="owner/repo")
        }

        self.assertEqual(statistics["task_queue_control"]["records"], 1)
        self.assertEqual(statistics["task_queue_attempt_audit"]["records"], 1)
        self.assertEqual(task["state"], "pending")

    def test_queue_readers_reject_tampered_intent_and_attempt_payloads(self) -> None:
        task = self.store.enqueue_queue_task(
            "owner/repo",
            action="submit",
            enqueued_by="alice",
            issue_number=31,
            parameters={"reviewed_by": "alice"},
        )
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                "UPDATE queue_tasks SET parameters=? WHERE id=?",
                ('{"reviewed_by":"mallory"}', task["id"]),
            )

        with self.assertRaisesRegex(StoreError, "parameters were modified"):
            self.store.queue_tasks(task_id=task["id"])

        other = self.enqueue(32)
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                "UPDATE queue_attempts SET payload=? WHERE task_id=?",
                ('{"public_write":true}', other["id"]),
            )

        with self.assertRaisesRegex(StoreError, "attempt payload was modified"):
            self.store.queue_attempts(other["id"])


class QueuePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        policy = RepositoryPolicy(name="owner/repo", mode="maintainer")
        self.pipeline = object.__new__(Pipeline)
        self.pipeline.config = SimpleNamespace(
            repositories={"owner/repo": policy},
            github=SimpleNamespace(login="alice"),
        )
        self.pipeline.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_apply_requires_gate_and_completes_through_existing_action(self) -> None:
        task = self.pipeline.enqueue_task(
            "owner/repo", action="prepare", issue_number=41
        )["task"]
        self.pipeline.prepare = Mock(
            return_value={
                "run_id": "run-result",
                "status": "ready",
                "stage": "review",
                "public_write": False,
            }
        )

        with self.assertRaisesRegex(PolicyError, "queue apply is disabled"):
            self.pipeline.apply_queue(worker="worker")
        with patch.dict(os.environ, {"REPOSTEWARD_ENABLE_QUEUE_APPLY": "1"}):
            result = self.pipeline.apply_queue(worker="worker")

        self.pipeline.prepare.assert_called_once_with("owner/repo", 41)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["outcomes"][0]["status"], "completed")
        self.assertFalse(result["public_write"])
        inspected = self.pipeline.queue_inspect(task_id=task["id"])
        self.assertEqual(inspected["counts"]["completed"], 1)
        self.assertEqual(inspected["attempts"][-1]["event"], "completed")

    def test_policy_failure_is_not_retried_without_manual_action(self) -> None:
        task = self.pipeline.enqueue_task(
            "owner/repo", action="prepare", issue_number=42
        )["task"]
        self.pipeline.prepare = Mock(side_effect=PolicyError("not approved"))

        with patch.dict(os.environ, {"REPOSTEWARD_ENABLE_QUEUE_APPLY": "1"}):
            result = self.pipeline.apply_queue(worker="worker")

        self.assertEqual(result["outcomes"][0]["error_code"], "policy_blocked")
        self.assertTrue(result["outcomes"][0]["manual_required"])
        inspected = self.pipeline.queue_inspect(task_id=task["id"])
        self.assertEqual(inspected["counts"]["failed"], 1)
        self.assertEqual(inspected["manual_required"], 1)

    def test_batch_wait_is_retryable_and_reports_prior_public_progress(self) -> None:
        run_id = self.pipeline.store.start_run("owner/repo", 43, "pull_request")
        self.pipeline.store.update_run(
            run_id,
            status="submitted",
            details={"pr_url": "https://github.com/owner/repo/pull/43"},
        )
        task = self.pipeline.enqueue_task(
            "owner/repo",
            action="batch-advance",
            run_id=run_id,
            pull_number=43,
            max_attempts=2,
            reviewed_by="alice",
            batch_plan_digest="d" * 64,
            batch_head_sha="a" * 40,
            batch_base_sha="b" * 40,
            batch_verified_base_sha="b" * 40,
        )["task"]
        self.pipeline.batch_advance = Mock(
            side_effect=BatchDeferred("wait for CI", public_write=True)
        )

        with patch.dict(os.environ, {"REPOSTEWARD_ENABLE_QUEUE_APPLY": "1"}):
            result = self.pipeline.apply_queue(worker="worker")

        self.assertEqual(result["outcomes"][0]["error_code"], "batch_waiting")
        self.assertTrue(result["outcomes"][0]["retryable"])
        self.assertFalse(result["outcomes"][0]["manual_required"])
        self.assertTrue(result["public_write"])
        inspected = self.pipeline.queue_inspect(task_id=task["id"])
        self.assertEqual(inspected["counts"]["failed"], 1)
        self.assertEqual(
            self.pipeline._queue_failure(BatchConflictError("conflict")),
            ("batch_conflict", False),
        )


if __name__ == "__main__":
    unittest.main()
