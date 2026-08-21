from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from reposteward.config import RepositoryPolicy
from reposteward.pipeline import Pipeline


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
