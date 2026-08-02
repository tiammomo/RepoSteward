from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Candidate


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS candidates (
                    repository TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    score REAL NOT NULL,
                    blocked INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'candidate',
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (repository, issue_number)
                );
                CREATE INDEX IF NOT EXISTS candidates_rank
                    ON candidates(status, blocked, score DESC);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worktree TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    repository TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    pr_url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (repository, issue_number)
                );
                """
            )

    def upsert_candidate(self, candidate: Candidate) -> None:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO candidates(
                    repository, issue_number, payload, score, blocked,
                    status, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?)
                ON CONFLICT(repository, issue_number) DO UPDATE SET
                    payload=excluded.payload,
                    score=excluded.score,
                    blocked=excluded.blocked,
                    discovered_at=excluded.discovered_at,
                    updated_at=excluded.updated_at
                """,
                (
                    candidate.issue.repository.lower(),
                    candidate.issue.number,
                    json.dumps(candidate.to_dict(), ensure_ascii=False),
                    candidate.score,
                    int(bool(candidate.blockers)),
                    candidate.discovered_at or now,
                    now,
                ),
            )

    def candidate(self, repository: str, issue_number: int) -> Candidate | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM candidates WHERE repository=? AND issue_number=?",
                (repository.lower(), issue_number),
            ).fetchone()
        if row is None:
            return None
        return Candidate.from_dict(json.loads(row["payload"]))

    def candidates(
        self,
        *,
        include_blocked: bool = False,
        status: str = "candidate",
        limit: int = 50,
        auto_prepare_repositories: tuple[str, ...] = (),
        min_score: float | None = None,
    ) -> list[tuple[Candidate, str]]:
        clauses = ["status=?"]
        parameters: list[Any] = [status]
        if not include_blocked:
            clauses.append("blocked=0")
        if auto_prepare_repositories:
            placeholders = ",".join("?" for _ in auto_prepare_repositories)
            clauses.append(f"repository IN ({placeholders})")
            parameters.extend(value.lower() for value in auto_prepare_repositories)
        if min_score is not None:
            clauses.append("score>=?")
            parameters.append(min_score)
        parameters.append(limit)
        sql = (
            "SELECT payload, status FROM candidates WHERE "
            + " AND ".join(clauses)
            + " ORDER BY score DESC, updated_at DESC LIMIT ?"
        )
        with self._connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [
            (Candidate.from_dict(json.loads(row["payload"])), str(row["status"]))
            for row in rows
        ]

    def set_candidate_status(
        self, repository: str, issue_number: int, status: str
    ) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE candidates SET status=?, updated_at=?
                WHERE repository=? AND issue_number=?
                """,
                (status, utc_now(), repository.lower(), issue_number),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"candidate not found: {repository}#{issue_number}")

    def start_run(self, repository: str, issue_number: int, stage: str) -> str:
        run_id = uuid.uuid4().hex
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO runs(
                    id, repository, issue_number, stage, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?)
                """,
                (run_id, repository.lower(), issue_number, stage, now, now),
            )
        return run_id

    def update_run(
        self,
        run_id: str,
        *,
        status: str,
        stage: str | None = None,
        worktree: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        assignments = ["status=?", "updated_at=?"]
        values: list[Any] = [status, utc_now()]
        if stage is not None:
            assignments.append("stage=?")
            values.append(stage)
        if worktree is not None:
            assignments.append("worktree=?")
            values.append(worktree)
        if details is not None:
            assignments.append("details=?")
            values.append(json.dumps(details, ensure_ascii=False))
        values.append(run_id)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE runs SET {', '.join(assignments)} WHERE id=?", values
            )

    def latest_run(self, repository: str, issue_number: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE repository=? AND issue_number=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (repository.lower(), issue_number),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["details"] = json.loads(result["details"])
        return result

    def record_submission(
        self, repository: str, issue_number: int, pr_url: str
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO submissions(
                    repository, issue_number, pr_url, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (repository.lower(), issue_number, pr_url, utc_now()),
            )

    def recent_submission_count(self, *, hours: int = 24) -> int:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM submissions WHERE created_at>=?",
                (cutoff,),
            ).fetchone()
        return int(row["count"])
