from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from starfix.models import Candidate, Issue, RepositoryInfo
from starfix.store import Store


class StoreTests(unittest.TestCase):
    def test_candidate_round_trip_and_status_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            candidate = Candidate(
                issue=Issue(
                    repository="owner/repo",
                    number=1,
                    node_id=2,
                    title="A bug",
                    body="Reproduction details",
                    url="https://github.com/owner/repo/issues/1",
                    labels=("bug",),
                    comments=0,
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    author_login="user",
                    author_association="NONE",
                ),
                repository=RepositoryInfo(
                    full_name="owner/repo",
                    default_branch="main",
                    stars=1000,
                    forks=20,
                    open_issues=5,
                    pushed_at="2026-01-01T00:00:00Z",
                    archived=False,
                    is_fork=False,
                ),
                score=42,
            )
            store.upsert_candidate(candidate)
            store.set_candidate_status("owner/repo", 1, "ready")
            store.upsert_candidate(candidate)

            restored = store.candidate("owner/repo", 1)
            ready = store.candidates(status="ready")

        self.assertIsNotNone(restored)
        if restored is None:
            self.fail("candidate was not restored")
        self.assertEqual(restored.issue.labels, ("bug",))
        self.assertEqual(len(ready), 1)


if __name__ == "__main__":
    unittest.main()
