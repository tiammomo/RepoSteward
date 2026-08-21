from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reposteward.config import RepositoryPolicy, StorageConfig
from reposteward.pipeline import Pipeline
from reposteward.policy import PolicyError


class StubStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.write_bytes(b"database")

    def storage_statistics(self, *, repository: str, cutoff: str) -> list[dict]:
        return [
            {
                "repository": repository or "owner/repo",
                "category": "checkpoint",
                "records": 2,
                "bytes": 120,
                "oldest_at": "2026-08-20T00:00:00Z",
                "newest_at": "2026-08-21T00:00:00Z",
            }
        ]

    def run_repositories(self) -> dict[str, str]:
        return {"run-1": "owner/repo"}


class StorageStatisticsTests(unittest.TestCase):
    def test_statistics_include_safe_log_and_database_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verification = root / "runs" / "run-1" / "verification"
            verification.mkdir(parents=True)
            (verification / "command.log").write_bytes(b"12345")
            outside = root / "outside.log"
            outside.write_bytes(b"do not follow")
            os.symlink(outside, verification / "linked.log")
            policy = RepositoryPolicy(name="owner/repo")
            pipeline = object.__new__(Pipeline)
            pipeline.config = SimpleNamespace(
                state_dir=root,
                repositories={"owner/repo": policy},
            )
            pipeline.store = StubStore(root / "state.sqlite3")

            result = pipeline.storage_statistics(repository="Owner/Repo")

        categories = {value["category"]: value for value in result["categories"]}
        self.assertEqual(categories["verification_log_cache"]["records"], 1)
        self.assertEqual(categories["verification_log_cache"]["bytes"], 5)
        self.assertEqual(result["database_physical_bytes"], 8)
        self.assertFalse(result["public_write"])

    def test_invalid_time_window_is_rejected(self) -> None:
        pipeline = object.__new__(Pipeline)
        with self.assertRaisesRegex(ValueError, "between 0 and 36500"):
            pipeline.storage_statistics(since_days=-1)


class GcStore:
    def __init__(self) -> None:
        self.audit: list[str] = []
        self.payload_deleted = False

    def run_gc_safety(self) -> dict[str, dict]:
        return {"run-1": {"repository": "owner/repo", "terminal_checkpoint": True}}

    def event_payload_gc_inventory(self, _cutoffs: dict[str, str]) -> dict:
        if self.payload_deleted:
            return {"candidates": [], "retained": []}
        return {
            "candidates": [
                {
                    "kind": "github_event_payload",
                    "digest": "a" * 64,
                    "bytes": 7,
                    "created_at": "2020-01-01T00:00:00+00:00",
                    "repositories": ["owner/repo"],
                    "reference_count": 1,
                    "reason": "explicit_retention_elapsed_and_checkpointed",
                }
            ],
            "retained": [],
        }

    def delete_event_payloads(
        self, digests: tuple[str, ...], *, retention_cutoffs: dict[str, str]
    ) -> dict:
        self.payload_deleted = True
        return {
            "deleted": [{"digest": digests[0], "bytes": 7}],
            "skipped": [],
        }

    def record_storage_gc(self, *, stage: str, **_kwargs) -> dict:
        self.audit.append(stage)
        return {"id": f"audit-{len(self.audit)}", "stage": stage}


class StorageGcTests(unittest.TestCase):
    def pipeline(self, root: Path) -> tuple[Pipeline, GcStore, Path]:
        verification = root / "runs" / "run-1" / "verification"
        verification.mkdir(parents=True)
        log = verification / "command.log"
        log.write_bytes(b"12345")
        os.utime(log, (1, 1))
        policy = RepositoryPolicy(name="owner/repo", event_payload_retention_days=30)
        pipeline = object.__new__(Pipeline)
        pipeline.config = SimpleNamespace(
            state_dir=root,
            storage=StorageConfig(cache_retention_days=30, max_gc_items=10),
            repositories={"owner/repo": policy},
            github=SimpleNamespace(login="operator"),
        )
        store = GcStore()
        pipeline.store = store
        return pipeline, store, log

    def test_gc_defaults_to_exact_read_only_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline, store, log = self.pipeline(Path(directory))

            result = pipeline.storage_gc(repository="Owner/Repo")

            self.assertTrue(log.exists())
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["estimated_reclaimable_bytes"], 12)
        self.assertEqual(store.audit, [])
        self.assertIn("merge_decision_audit", result["protected_categories"])
        self.assertIn("merge_execution_audit", result["protected_categories"])

    def test_gc_apply_requires_switch_then_audits_and_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline, store, log = self.pipeline(Path(directory))
            with self.assertRaisesRegex(PolicyError, "ENABLE_GC"):
                pipeline.storage_gc(repository="owner/repo", apply=True)
            self.assertTrue(log.exists())

            with patch.dict("os.environ", {"REPOSTEWARD_ENABLE_GC": "1"}):
                result = pipeline.storage_gc(repository="owner/repo", apply=True)

            self.assertFalse(log.exists())
        self.assertEqual(store.audit, ["applying", "completed"])
        self.assertEqual(len(result["applied"]["deleted_logs"]), 1)
        self.assertEqual(len(result["applied"]["deleted_event_payloads"]), 1)
