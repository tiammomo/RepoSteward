from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from reposteward.pipeline import Pipeline
from reposteward.store import SCHEMA_VERSION, Store, StoreError


class RunLeaseStoreTests(unittest.TestCase):
    def test_schema_thirteen_database_receives_lease_tables_without_data_loss(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = Store(path)
            run_id = store.start_run("owner/repo", 7, "agent")
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TABLE run_lease_events")
                connection.execute("DROP TABLE run_leases")
                connection.execute("PRAGMA user_version=13")

            migrated = Store(path)

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertIsNotNone(migrated.run(run_id))
            lease = migrated.acquire_run_lease("issue:owner/repo#7", owner="worker-1")
            migrated.release_run_lease(lease)

    def test_only_one_concurrent_owner_acquires_a_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            now = datetime(2026, 8, 22, tzinfo=UTC)

            def acquire(owner: str) -> str:
                try:
                    return store.acquire_run_lease(
                        "issue:owner/repo#7", owner=owner, now=now
                    ).owner
                except StoreError:
                    return "blocked"

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(acquire, ("worker-1", "worker-2")))

        self.assertEqual(results.count("blocked"), 1)
        self.assertEqual(len(set(results) - {"blocked"}), 1)

    def test_unrelated_issue_scopes_can_both_make_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")

            def acquire(issue_number: int) -> str:
                return store.acquire_run_lease(
                    f"issue:owner/repo#{issue_number}", owner=f"worker-{issue_number}"
                ).scope

            with ThreadPoolExecutor(max_workers=2) as executor:
                scopes = set(executor.map(acquire, (7, 8)))

        self.assertEqual(scopes, {"issue:owner/repo#7", "issue:owner/repo#8"})

    def test_expired_takeover_rejects_old_generation_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            run_id = store.start_run("owner/repo", 7, "agent")
            started = datetime.now(UTC)
            first = store.acquire_run_lease(
                "issue:owner/repo#7", owner="worker-1", ttl_seconds=30, now=started
            )
            renewed = store.renew_run_lease(
                first,
                ttl_seconds=30,
                now=started + timedelta(seconds=10),
            )
            second = store.acquire_run_lease(
                "issue:owner/repo#7",
                owner="worker-2",
                ttl_seconds=30,
                now=started + timedelta(seconds=41),
            )

            self.assertGreater(second.generation, renewed.generation)
            store.update_run(run_id, status="ready", lease=second)
            with (
                self.assertRaisesRegex(StoreError, "stale"),
                store.bind_run_lease(renewed),
            ):
                store.update_run(run_id, status="failed")
            with self.assertRaisesRegex(StoreError, "stale"):
                store.release_run_lease(renewed)
            current = store.run(run_id)
            assert current is not None
            status = current["status"]
            store.release_run_lease(second)
            actions = [
                value["action"]
                for value in reversed(store.run_lease_events("issue:owner/repo#7"))
            ]

        self.assertEqual(status, "ready")
        self.assertEqual(actions, ["acquired", "renewed", "taken_over", "released"])

    def test_bound_lease_fences_all_store_writes_after_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            started = datetime.now(UTC)
            first = store.acquire_run_lease(
                "issue:owner/repo#7", owner="worker-1", ttl_seconds=30, now=started
            )
            store.acquire_run_lease(
                "issue:owner/repo#7",
                owner="worker-2",
                ttl_seconds=30,
                now=started + timedelta(seconds=31),
            )

            with (
                self.assertRaisesRegex(StoreError, "stale"),
                store.bind_run_lease(first),
            ):
                store.start_run("owner/repo", 8, "agent")

            self.assertIsNone(store.latest_run("owner/repo", 8))


class PipelineRunLeaseTests(unittest.TestCase):
    def test_prepare_holds_and_releases_one_issue_scoped_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline = object.__new__(Pipeline)
            pipeline.store = Store(Path(directory) / "state.sqlite3")
            pipeline._prepare_leased = Mock(return_value={"run_id": "run-1"})

            result = pipeline.prepare("Owner/Repo", 7)
            events = list(
                reversed(pipeline.store.run_lease_events("issue:owner/repo#7"))
            )

        self.assertEqual(result, {"run_id": "run-1"})
        pipeline._prepare_leased.assert_called_once_with("Owner/Repo", 7)
        self.assertEqual(
            [value["action"] for value in events], ["acquired", "released"]
        )

    def test_nested_mutation_of_the_same_issue_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline = object.__new__(Pipeline)
            pipeline.store = Store(Path(directory) / "state.sqlite3")

            with (
                pipeline._mutation_lease("owner/repo", 7),
                self.assertRaisesRegex(StoreError, "already held"),
                pipeline._mutation_lease("owner/repo", 7),
            ):
                pass


if __name__ == "__main__":
    unittest.main()
