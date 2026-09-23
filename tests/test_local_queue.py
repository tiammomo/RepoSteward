from __future__ import annotations

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from reposteward.storage.local_queue import enqueue
from reposteward.storage.store import Store, StoreError, apply_migration


class LocalQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.account = "a" * 64
        self.project = "b" * 32

    def enqueue(self, key="request", **overrides):
        values = {
            "account": self.account,
            "project": self.project,
            "repository": "owner/repo",
            "action": "github.sync",
            "payload": {"scopes": ["pulls"]},
            "idempotency_key": key,
            "actor": "local-user",
        }
        values.update(overrides)
        return enqueue(self.store, **values)

    def test_native_and_local_claims_and_summaries_are_isolated(self):
        native = self.store.enqueue_queue_task(
            "owner/repo", action="prepare", enqueued_by="owner", issue_number=7
        )
        local = self.enqueue()
        self.assertEqual(
            [row["id"] for row in self.store.queue_tasks()], [native["id"]]
        )
        self.assertEqual(self.store.queue_task_summary()["counts"]["pending"], 1)
        self.assertEqual(
            [row["id"] for row in self.store.claim_queue_tasks(worker="native")],
            [native["id"]],
        )
        self.assertEqual(
            self.store.claim_queue_tasks(
                worker="other", operation_family="local", account_digest="c" * 64
            ),
            [],
        )
        claimed = self.store.claim_queue_tasks(
            worker="local", operation_family="local", account_digest=self.account
        )
        self.assertEqual([row["id"] for row in claimed], [local["id"]])
        self.assertEqual(claimed[0]["parameters"], {"plan_id": local["plan_id"]})

    def test_same_request_replays_and_conflicts_are_atomic(self):
        first = self.enqueue()
        second = self.enqueue()
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["idempotent"])
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.enqueue(payload={"scopes": ["issues"]})
        coalesced = self.enqueue("another-tab")
        self.assertEqual(coalesced["id"], first["id"])
        with self.store._connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM local_operation_plans").fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM local_operation_requests").fetchone()[
                    0
                ],
                2,
            )

    def test_concurrent_tabs_persist_one_logical_sync(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda index: self.enqueue(f"tab-{index}"), range(4)))
        self.assertEqual(len({r["id"] for r in rows}), 1)
        self.assertEqual(sum(not r["idempotent"] for r in rows), 1)
        self.assertEqual(len(self.store.queue_attempts(rows[0]["id"])), 1)

    def test_expired_local_lease_is_not_consumed_by_native_worker(self):
        task = self.enqueue()
        now = datetime.now(UTC) + timedelta(seconds=1)
        claimed = self.store.claim_queue_tasks(
            worker="old",
            operation_family="local",
            account_digest=self.account,
            lease_seconds=5,
            now=now,
        )[0]
        self.assertEqual(
            self.store.claim_queue_tasks(
                worker="native", now=now + timedelta(seconds=10)
            ),
            [],
        )
        resumed = self.store.claim_queue_tasks(
            worker="new",
            operation_family="local",
            account_digest=self.account,
            now=now + timedelta(seconds=10),
        )[0]
        self.assertEqual(resumed["id"], task["id"])
        self.assertEqual(resumed["lease"].generation, claimed["lease"].generation + 1)
        with self.assertRaises(StoreError):
            self.store.complete_queue_task(
                claimed["lease"],
                result={"status": "done"},
                now=now + timedelta(seconds=10),
            )

    def test_changed_family_version_or_plan_fails_closed(self):
        for column, value in (
            ("operation_family", "native"),
            ("payload_version", 1),
            ("scope_key", "d" * 32),
            ("plan_id", "e" * 32),
        ):
            with self.subTest(column=column):
                task = self.enqueue(column, payload={"case": column})
                with self.store._connection() as db:
                    # Column names are a fixed test-owned list, never request data.
                    db.execute(
                        f"UPDATE queue_tasks SET {column}=? WHERE id=?",
                        (value, task["id"]),
                    )
                with self.assertRaises(StoreError):
                    self.store.queue_tasks(task_id=task["id"], operation_family="all")

    def test_upgrade_preserves_native_v1_digests_and_ids(self):
        old = self.path.with_name("v22.sqlite3")
        native = self.store.enqueue_queue_task(
            "owner/repo", action="prepare", enqueued_by="owner", issue_number=7
        )
        with self.store._connection() as db:
            current = dict(
                db.execute(
                    "SELECT * FROM queue_tasks WHERE id=?", (native["id"],)
                ).fetchone()
            )
        with closing(sqlite3.connect(old)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for version in range(1, 23):
                apply_migration(db, version)
            columns = [row[1] for row in db.execute("PRAGMA table_info(queue_tasks)")]
            original = {column: current[column] for column in columns}
            db.execute(
                "INSERT INTO queue_tasks("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(original.values()),
            )
        # A native envelope uses the original v1 digest even after adding columns.
        migrated = Store(old)
        self.assertEqual(
            Store._queue_task(original)["dedupe_key"], native["dedupe_key"]
        )
        row = migrated.queue_tasks()[0]
        self.assertEqual(row["id"], native["id"])
        self.assertEqual(row["dedupe_key"], native["dedupe_key"])
        self.assertEqual(row["operation_family"], "native")
        self.assertEqual(row["payload_version"], 1)
