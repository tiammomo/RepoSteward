from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Candidate
from .protocol import validate_checkpoint, validate_context_pack

SCHEMA_VERSION = 16

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
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
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS candidates_rank
        ON candidates(status, blocked, score DESC)
        """,
        """
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
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS submissions (
            repository TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            pr_url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (repository, issue_number)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS issue_drafts (
            id TEXT PRIMARY KEY,
            repository TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS issue_drafts_recent
        ON issue_drafts(status, updated_at DESC)
        """,
    ),
    2: (
        """
        CREATE INDEX IF NOT EXISTS runs_for_work_item
        ON runs(repository, issue_number, created_at DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS work_items (
            id TEXT PRIMARY KEY,
            repository TEXT NOT NULL,
            kind TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(repository, kind, external_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS work_items_recent
        ON work_items(repository, status, updated_at DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS context_packs (
            id TEXT PRIMARY KEY,
            work_item_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            source_digest TEXT NOT NULL,
            base_commit TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(work_item_id) REFERENCES work_items(id),
            FOREIGN KEY(run_id) REFERENCES runs(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS context_packs_for_work_item
        ON context_packs(work_item_id, created_at DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS harness_runs (
            run_id TEXT PRIMARY KEY,
            work_item_id TEXT NOT NULL,
            context_pack_id TEXT NOT NULL,
            harness TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            native_session_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id),
            FOREIGN KEY(work_item_id) REFERENCES work_items(id),
            FOREIGN KEY(context_pack_id) REFERENCES context_packs(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS harness_runs_for_work_item
        ON harness_runs(work_item_id, created_at DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS checkpoints (
            id TEXT PRIMARY KEY,
            work_item_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            context_pack_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            status TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, sequence),
            FOREIGN KEY(work_item_id) REFERENCES work_items(id),
            FOREIGN KEY(run_id) REFERENCES runs(id),
            FOREIGN KEY(context_pack_id) REFERENCES context_packs(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS checkpoints_for_run
        ON checkpoints(run_id, sequence DESC)
        """,
    ),
    3: (
        """
        CREATE TABLE IF NOT EXISTS context_imports (
            id TEXT PRIMARY KEY,
            bundle_digest TEXT NOT NULL UNIQUE,
            work_item_id TEXT NOT NULL,
            source_run_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            FOREIGN KEY(work_item_id) REFERENCES work_items(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS context_imports_for_work_item
        ON context_imports(work_item_id, imported_at DESC)
        """,
    ),
    4: (
        """
        CREATE TABLE IF NOT EXISTS issue_proposals (
            project_item_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            project_url TEXT NOT NULL,
            draft_id TEXT NOT NULL DEFAULT '',
            repository TEXT NOT NULL,
            creator TEXT NOT NULL,
            content_digest TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'staged',
            issue_number INTEGER NOT NULL DEFAULT 0,
            issue_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS issue_proposals_for_local_draft
        ON issue_proposals(project_id, draft_id)
        WHERE draft_id <> ''
        """,
        """
        CREATE INDEX IF NOT EXISTS issue_proposals_recent
        ON issue_proposals(status, updated_at DESC)
        """,
    ),
    5: (
        """
        CREATE TABLE IF NOT EXISTS github_pr_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            repository TEXT NOT NULL,
            pull_number INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            version_digest TEXT NOT NULL,
            head_sha TEXT NOT NULL DEFAULT '',
            source_trust TEXT NOT NULL DEFAULT 'github_untrusted',
            source_created_at TEXT NOT NULL DEFAULT '',
            source_updated_at TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            UNIQUE(
                repository, pull_number, event_type, external_id, version_digest
            )
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS github_pr_events_for_pull
        ON github_pr_events(repository, pull_number, sequence)
        """,
        """
        CREATE TABLE IF NOT EXISTS github_pr_watermarks (
            run_id TEXT PRIMARY KEY,
            repository TEXT NOT NULL,
            pull_number INTEGER NOT NULL,
            sequence INTEGER NOT NULL DEFAULT 0,
            batch_digest TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        )
        """,
    ),
    6: (
        """
        CREATE TABLE IF NOT EXISTS merge_decisions (
            id TEXT PRIMARY KEY,
            repository TEXT NOT NULL,
            pull_number INTEGER NOT NULL,
            head_sha TEXT NOT NULL,
            base_sha TEXT NOT NULL,
            policy_digest TEXT NOT NULL,
            snapshot_digest TEXT NOT NULL,
            eligible INTEGER NOT NULL,
            decision_digest TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS merge_decisions_for_pull
        ON merge_decisions(repository, pull_number, created_at DESC)
        """,
    ),
    7: (
        """
        CREATE TABLE IF NOT EXISTS content_blobs (
            digest TEXT PRIMARY KEY,
            payload BLOB NOT NULL,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        "ALTER TABLE github_pr_events ADD COLUMN payload_digest TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE github_pr_events ADD COLUMN source_actor TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE github_pr_events ADD COLUMN source_state TEXT NOT NULL DEFAULT ''",
        """
        INSERT OR IGNORE INTO content_blobs(digest, payload, size_bytes, created_at)
        SELECT version_digest, CAST(payload AS BLOB),
               length(CAST(payload AS BLOB)), ingested_at
        FROM github_pr_events
        """,
        """
        UPDATE github_pr_events
        SET payload_digest=version_digest,
            source_actor=COALESCE(json_extract(payload, '$.author'), ''),
            source_state=COALESCE(
                json_extract(payload, '$.state'),
                json_extract(payload, '$.status'),
                json_extract(payload, '$.conclusion'),
                ''
            ),
            payload=''
        """,
        """
        CREATE INDEX IF NOT EXISTS github_pr_events_for_payload
        ON github_pr_events(payload_digest)
        """,
    ),
    8: (
        """
        CREATE TABLE IF NOT EXISTS content_blob_tombstones (
            digest TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            reason TEXT NOT NULL,
            deleted_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS github_pr_watermarks_for_pull
        ON github_pr_watermarks(repository, pull_number, sequence)
        """,
    ),
    9: (
        """
        CREATE TABLE IF NOT EXISTS storage_gc_runs (
            id TEXT PRIMARY KEY,
            repository TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL,
            stage TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS storage_gc_runs_recent
        ON storage_gc_runs(created_at DESC)
        """,
    ),
    10: (
        """
        CREATE TABLE IF NOT EXISTS merge_executions (
            id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            repository TEXT NOT NULL,
            pull_number INTEGER NOT NULL,
            actor TEXT NOT NULL,
            merge_method TEXT NOT NULL,
            stage TEXT NOT NULL,
            outcome TEXT NOT NULL,
            reason TEXT NOT NULL,
            decision_digest TEXT NOT NULL,
            head_sha TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id),
            FOREIGN KEY(decision_id) REFERENCES merge_decisions(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS merge_executions_for_pull
        ON merge_executions(repository, pull_number, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS merge_executions_for_attempt
        ON merge_executions(attempt_id, created_at)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS merge_executions_stage_once
        ON merge_executions(attempt_id, stage)
        """,
    ),
    11: (
        """
        CREATE TABLE IF NOT EXISTS portfolio_dependency_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            repository TEXT NOT NULL,
            pull_number INTEGER NOT NULL,
            dependency_number INTEGER NOT NULL,
            head_sha TEXT NOT NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            source TEXT NOT NULL,
            event_digest TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS portfolio_dependency_events_for_pull
        ON portfolio_dependency_events(
            repository, pull_number, dependency_number, sequence DESC
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS portfolio_dependency_events_digest
        ON portfolio_dependency_events(event_digest)
        """,
    ),
    12: (
        """
        CREATE TABLE IF NOT EXISTS owner_review_attestations (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            repository TEXT NOT NULL,
            pull_number INTEGER NOT NULL,
            run_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            head_sha TEXT NOT NULL,
            base_sha TEXT NOT NULL,
            policy_digest TEXT NOT NULL,
            review_facts_digest TEXT NOT NULL UNIQUE,
            attestation_digest TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS owner_review_attestations_for_pull
        ON owner_review_attestations(repository, pull_number, sequence DESC)
        """,
    ),
    13: (
        """
        CREATE TABLE IF NOT EXISTS harness_usage_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL UNIQUE,
            work_item_id TEXT NOT NULL,
            repository TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            run_stage TEXT NOT NULL,
            harness TEXT NOT NULL,
            model TEXT NOT NULL,
            session_resume TEXT NOT NULL,
            portable_context_fallback INTEGER NOT NULL,
            event_digest TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id),
            FOREIGN KEY(work_item_id) REFERENCES work_items(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS harness_usage_events_report
        ON harness_usage_events(repository, created_at, sequence)
        """,
        """
        CREATE INDEX IF NOT EXISTS harness_usage_events_for_work_item
        ON harness_usage_events(work_item_id, sequence)
        """,
    ),
    14: (
        """
        CREATE TABLE IF NOT EXISTS run_leases (
            scope TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation >= 1),
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS run_lease_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            owner TEXT NOT NULL,
            generation INTEGER NOT NULL,
            action TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS run_lease_events_for_scope
        ON run_lease_events(scope, sequence DESC)
        """,
    ),
    15: (
        """
        CREATE TABLE IF NOT EXISTS publication_attempts (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            attempt_id TEXT NOT NULL,
            step_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            repository TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            stage TEXT NOT NULL,
            outcome TEXT NOT NULL,
            destination TEXT NOT NULL,
            branch TEXT NOT NULL,
            head_sha TEXT NOT NULL,
            base_branch TEXT NOT NULL,
            expected_remote_sha TEXT NOT NULL DEFAULT '',
            target_pull_number INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT NOT NULL,
            lease_generation INTEGER NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS publication_attempts_for_run
        ON publication_attempts(run_id, sequence)
        """,
        """
        CREATE INDEX IF NOT EXISTS publication_attempts_for_repository
        ON publication_attempts(repository, issue_number, sequence DESC)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS publication_attempts_stage_once
        ON publication_attempts(step_id, stage)
        """,
    ),
    16: (
        """
        CREATE TABLE IF NOT EXISTS queue_tasks (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            dedupe_key TEXT NOT NULL UNIQUE,
            repository TEXT NOT NULL,
            action TEXT NOT NULL,
            work_item_id TEXT,
            run_id TEXT,
            issue_number INTEGER NOT NULL DEFAULT 0,
            pull_number INTEGER NOT NULL DEFAULT 0,
            parameters TEXT NOT NULL,
            parameters_digest TEXT NOT NULL,
            idempotency_digest TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            depends_on_task_id TEXT,
            max_attempts INTEGER NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            manual_required INTEGER NOT NULL DEFAULT 0,
            last_error_code TEXT NOT NULL DEFAULT '',
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_generation INTEGER NOT NULL DEFAULT 0,
            lease_expires_at TEXT NOT NULL DEFAULT '',
            available_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(work_item_id) REFERENCES work_items(id),
            FOREIGN KEY(run_id) REFERENCES runs(id),
            FOREIGN KEY(depends_on_task_id) REFERENCES queue_tasks(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS queue_tasks_claim
        ON queue_tasks(
            state, manual_required, available_at, priority DESC, sequence
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS queue_tasks_for_repository
        ON queue_tasks(
            repository, state, manual_required, available_at,
            priority DESC, sequence
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS queue_tasks_expired_leases
        ON queue_tasks(state, lease_expires_at, attempt_count)
        """,
        """
        CREATE INDEX IF NOT EXISTS queue_tasks_for_dependency
        ON queue_tasks(depends_on_task_id, state)
        """,
        """
        CREATE TABLE IF NOT EXISTS queue_attempts (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            task_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            worker TEXT NOT NULL,
            event TEXT NOT NULL,
            outcome TEXT NOT NULL,
            payload TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES queue_tasks(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS queue_attempts_for_task
        ON queue_attempts(task_id, sequence)
        """,
    ),
}


class StoreError(RuntimeError):
    """The local state database cannot be opened or migrated safely."""


@dataclass(frozen=True, slots=True)
class RunLease:
    scope: str
    owner: str
    generation: int
    expires_at: str


@dataclass(frozen=True, slots=True)
class QueueLease:
    task_id: str
    worker: str
    generation: int
    expires_at: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _is_lower_hex(value: str, length: int) -> bool:
    return len(value) == length and all(
        character in "0123456789abcdef" for character in value
    )


OWNER_REVIEW_FACT_KEYS = frozenset(
    {
        "repository",
        "pull_number",
        "run_id",
        "actor",
        "pull_author",
        "head_owner",
        "head_repository",
        "head_branch",
        "head_sha",
        "base_sha",
        "policy_digest",
        "review_decision",
        "diff_digest",
        "checks_digest",
        "conversation_digest",
        "dependency_digest",
        "activity_digest",
        "rules_digest",
    }
)

PUBLICATION_ACTIONS = frozenset({"push", "create", "reopen", "update", "close"})
PUBLICATION_COMPLETED_OUTCOMES = frozenset(
    {"succeeded", "reconciled", "already_current", "not_applied", "blocked", "failed"}
)
PUBLICATION_PAYLOAD_KEYS = frozenset(
    {
        "public_write",
        "remote_head_sha",
        "pull_number",
        "pull_url",
        "pull_state",
        "pull_draft",
        "reconciliation",
    }
)

QUEUE_ACTIONS = frozenset(
    {
        "prepare",
        "follow-up",
        "repair",
        "submit",
        "merge-decision",
        "merge-attest",
        "merge",
        "batch-advance",
    }
)
QUEUE_STATES = frozenset({"pending", "running", "completed", "failed", "cancelled"})
QUEUE_PARAMETER_KEYS = frozenset(
    {
        "reviewed_by",
        "decision_id",
        "reopen_pull_request",
        "batch_plan_digest",
        "batch_head_sha",
        "batch_base_sha",
        "batch_verified_base_sha",
    }
)
QUEUE_RESULT_KEYS = frozenset(
    {
        "run_id",
        "status",
        "stage",
        "pr_number",
        "pr_url",
        "decision_id",
        "merge_commit_sha",
        "merged",
        "idempotent",
        "public_write",
        "next_action",
        "error_code",
        "retryable",
        "manual_required",
    }
)
QUEUE_EVENTS = frozenset(
    {
        "enqueued",
        "claimed",
        "taken_over",
        "completed",
        "failed",
        "cancelled",
        "requeued",
        "attempts_exhausted",
    }
)

USAGE_METRIC_KEYS = frozenset(
    {
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "prompt_chars",
        "event_bytes",
        "stderr_bytes",
        "event_count",
        "tool_call_count",
        "duration_seconds",
    }
)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._run_lease_context: ContextVar[RunLease | None] = ContextVar(
            f"reposteward_run_lease_{id(self)}", default=None
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _begin_immediate(connection: sqlite3.Connection) -> None:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")

    @contextmanager
    def _connection(
        self, *, guard_bound_lease: bool = True
    ) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            lease = self._run_lease_context.get() if guard_bound_lease else None
            if lease is not None:
                # Keep the fencing check and any following write in one short
                # transaction. The lock is released when this Store call returns;
                # it is never held across Harness, Docker, or GitHub operations.
                self._begin_immediate(connection)
                self._assert_run_lease_row(connection, lease)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise StoreError(
                    f"database schema {current} is newer than supported "
                    f"schema {SCHEMA_VERSION}"
                )
            for version in range(current + 1, SCHEMA_VERSION + 1):
                statements = MIGRATIONS.get(version)
                if statements is None:
                    raise StoreError(f"missing database migration {version}")
                self._begin_immediate(connection)
                try:
                    for statement in statements:
                        if version == 7 and statement.startswith(
                            "ALTER TABLE github_pr_events ADD COLUMN"
                        ):
                            column = statement.split("ADD COLUMN", 1)[1].split()[0]
                            existing = {
                                str(row[1])
                                for row in connection.execute(
                                    "PRAGMA table_info(github_pr_events)"
                                )
                            }
                            if column in existing:
                                continue
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version={version}")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        finally:
            connection.close()

    def schema_version(self) -> int:
        with self._connection() as connection:
            row = connection.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def create_issue_draft(
        self, repository: str, title: str, body: str
    ) -> dict[str, Any]:
        draft_id = uuid.uuid4().hex
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO issue_drafts(
                    id, repository, title, body, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'draft', ?, ?)
                """,
                (draft_id, repository.lower(), title, body, now, now),
            )
        return self.issue_draft(draft_id) or {}

    def issue_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM issue_drafts WHERE id=?", (draft_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def issue_drafts(self, *, limit: int = 30) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 100)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM issue_drafts
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def issue_proposal_for_draft(
        self, project_id: str, draft_id: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM issue_proposals
                WHERE project_id=? AND draft_id=?
                """,
                (project_id, draft_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def staged_issue_proposals(
        self, repository: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM issue_proposals
                WHERE repository=? AND status='staged'
                ORDER BY updated_at, project_item_id LIMIT ?
                """,
                (repository.casefold(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_issue_proposal(
        self,
        *,
        project_item_id: str,
        project_id: str,
        project_url: str,
        draft_id: str,
        repository: str,
        creator: str,
        content_digest: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO issue_proposals(
                    project_item_id, project_id, project_url, draft_id,
                    repository, creator, content_digest, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?)
                ON CONFLICT(project_item_id) DO UPDATE SET
                    project_url=excluded.project_url,
                    repository=excluded.repository,
                    creator=excluded.creator,
                    content_digest=excluded.content_digest,
                    updated_at=excluded.updated_at
                """,
                (
                    project_item_id,
                    project_id,
                    project_url,
                    draft_id,
                    repository.casefold(),
                    creator,
                    content_digest,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM issue_proposals WHERE project_item_id=?",
                (project_item_id,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def mark_issue_proposal_published(
        self, project_item_id: str, *, issue_number: int, issue_url: str
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE issue_proposals
                SET status='published', issue_number=?, issue_url=?, updated_at=?
                WHERE project_item_id=?
                """,
                (issue_number, issue_url, utc_now(), project_item_id),
            )

    def ensure_work_item(
        self,
        repository: str,
        *,
        kind: str,
        external_id: str,
        title: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        work_item_id = uuid.uuid4().hex
        now = utc_now()
        serialized = json.dumps(payload or {}, ensure_ascii=False)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO work_items(
                    id, repository, kind, external_id, title, status,
                    payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                ON CONFLICT(repository, kind, external_id) DO UPDATE SET
                    title=excluded.title,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (
                    work_item_id,
                    repository.lower(),
                    kind,
                    external_id,
                    title,
                    serialized,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM work_items
                WHERE repository=? AND kind=? AND external_id=?
                """,
                (repository.lower(), kind, external_id),
            ).fetchone()
        assert row is not None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def save_context_pack(
        self,
        *,
        pack_id: str,
        work_item_id: str,
        run_id: str,
        schema_version: int,
        source_digest: str,
        base_commit: str,
        payload: dict[str, Any],
    ) -> None:
        self._validate_context_record(
            pack_id=pack_id,
            work_item_id=work_item_id,
            run_id=run_id,
            schema_version=schema_version,
            source_digest=source_digest,
            base_commit=base_commit,
            payload=payload,
        )
        with self._connection() as connection:
            self._validate_run_work_item(
                connection, run_id, work_item_id, context_payload=payload
            )
            connection.execute(
                """
                INSERT INTO context_packs(
                    id, work_item_id, run_id, schema_version, source_digest,
                    base_commit, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pack_id,
                    work_item_id,
                    run_id,
                    schema_version,
                    source_digest,
                    base_commit,
                    json.dumps(payload, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def save_context_run(
        self,
        *,
        pack_id: str,
        work_item_id: str,
        run_id: str,
        schema_version: int,
        source_digest: str,
        base_commit: str,
        payload: dict[str, Any],
        harness: str,
        model: str = "",
        native_session_id: str = "",
    ) -> None:
        self._validate_context_record(
            pack_id=pack_id,
            work_item_id=work_item_id,
            run_id=run_id,
            schema_version=schema_version,
            source_digest=source_digest,
            base_commit=base_commit,
            payload=payload,
        )
        now = utc_now()
        with self._connection() as connection:
            self._begin_immediate(connection)
            self._validate_run_work_item(
                connection, run_id, work_item_id, context_payload=payload
            )
            connection.execute(
                """
                INSERT INTO context_packs(
                    id, work_item_id, run_id, schema_version, source_digest,
                    base_commit, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pack_id,
                    work_item_id,
                    run_id,
                    schema_version,
                    source_digest,
                    base_commit,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO harness_runs(
                    run_id, work_item_id, context_pack_id, harness, model,
                    native_session_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    work_item_id,
                    pack_id,
                    harness,
                    model,
                    native_session_id,
                    now,
                ),
            )

    @staticmethod
    def _validate_run_work_item(
        connection: sqlite3.Connection,
        run_id: str,
        work_item_id: str,
        *,
        context_payload: dict[str, Any] | None = None,
    ) -> None:
        row = connection.execute(
            """
            SELECT
                r.repository AS run_repository,
                r.issue_number,
                w.repository AS work_repository,
                w.kind,
                w.external_id
            FROM runs r JOIN work_items w ON w.id=?
            WHERE r.id=?
            """,
            (work_item_id, run_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"run or work item not found: {run_id}, {work_item_id}")
        if (
            str(row["run_repository"]).casefold()
            != str(row["work_repository"]).casefold()
            or str(row["kind"]) != "github_issue"
            or str(row["issue_number"]) != str(row["external_id"])
        ):
            raise StoreError("run and work item refer to different GitHub issues")
        if context_payload is None:
            return
        project = context_payload.get("project")
        task = context_payload.get("task")
        if not isinstance(project, dict) or not isinstance(task, dict):
            raise StoreError("context pack does not contain project and task scopes")
        if (
            str(project.get("repository", "")).casefold()
            != str(row["work_repository"]).casefold()
            or str(task.get("kind", "")) != str(row["kind"])
            or str(task.get("external_id", "")) != str(row["external_id"])
        ):
            raise StoreError("context pack scope does not match its run and work item")

    @staticmethod
    def _validate_context_record(
        *,
        pack_id: str,
        work_item_id: str,
        run_id: str,
        schema_version: int,
        source_digest: str,
        base_commit: str,
        payload: dict[str, Any],
    ) -> None:
        validate_context_pack(payload)
        if payload.get("id") != pack_id:
            raise StoreError("context pack payload id does not match its record id")
        if payload.get("work_item_id") != work_item_id:
            raise StoreError("context pack payload references a different work item")
        if payload.get("schema_version") != schema_version:
            raise StoreError("context pack payload has a different schema version")
        if payload.get("source_digest") != source_digest:
            raise StoreError("context pack payload has a different source digest")
        project = payload.get("project")
        if not isinstance(project, dict) or project.get("base_commit") != base_commit:
            raise StoreError("context pack payload has a different base commit")
        provenance = payload.get("provenance")
        if not isinstance(provenance, dict) or provenance.get("run_id") != run_id:
            raise StoreError("context pack payload references a different run")

    def bind_harness_run(
        self,
        run_id: str,
        *,
        work_item_id: str,
        context_pack_id: str,
        harness: str,
        model: str = "",
        native_session_id: str = "",
    ) -> None:
        with self._connection() as connection:
            self._begin_immediate(connection)
            context = connection.execute(
                """
                SELECT work_item_id, run_id FROM context_packs WHERE id=?
                """,
                (context_pack_id,),
            ).fetchone()
            if context is None:
                raise KeyError(f"context pack not found: {context_pack_id}")
            if (
                str(context["work_item_id"]) != work_item_id
                or str(context["run_id"]) != run_id
            ):
                raise StoreError(
                    "harness context pack belongs to a different work item or run"
                )
            connection.execute(
                """
                INSERT INTO harness_runs(
                    run_id, work_item_id, context_pack_id, harness, model,
                    native_session_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    work_item_id,
                    context_pack_id,
                    harness,
                    model,
                    native_session_id,
                    utc_now(),
                ),
            )

    def update_harness_session(self, run_id: str, native_session_id: str) -> None:
        self.update_harness_run(run_id, native_session_id=native_session_id)

    def update_harness_run(
        self,
        run_id: str,
        *,
        harness: str | None = None,
        model: str | None = None,
        native_session_id: str | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[str] = []
        for column, value in (
            ("harness", harness),
            ("model", model),
            ("native_session_id", native_session_id),
        ):
            if value is not None:
                assignments.append(f"{column}=?")
                values.append(value)
        if not assignments:
            return
        values.append(run_id)
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE harness_runs SET {', '.join(assignments)} WHERE run_id=?",
                values,
            )
            if cursor.rowcount != 1:
                raise KeyError(f"harness run not found: {run_id}")

    def record_harness_usage(
        self,
        *,
        run_id: str,
        run_stage: str,
        harness: str,
        model: str,
        session_resume: str,
        portable_context_fallback: bool,
        metrics: dict[str, Any],
        budget: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one bounded, prompt-free usage event for a Harness run."""
        if set(metrics) != USAGE_METRIC_KEYS:
            raise StoreError("Harness usage metrics have an unexpected shape")
        integer_metrics = USAGE_METRIC_KEYS - {"duration_seconds"}
        for key in integer_metrics:
            value = metrics[key]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"Harness usage metric {key} must be non-negative")
        duration = metrics["duration_seconds"]
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration < 0
        ):
            raise ValueError("Harness usage duration_seconds must be non-negative")
        if run_stage not in {"prepare", "repair"}:
            raise ValueError("unsupported Harness usage run stage")
        if not isinstance(portable_context_fallback, bool):
            raise TypeError("portable_context_fallback must be a boolean")
        if session_resume not in {
            "not_requested",
            "resumed",
            "fallback_new_session",
            "unavailable",
        }:
            raise ValueError("unsupported Harness session resume outcome")
        if not harness or not isinstance(budget, dict):
            raise ValueError("Harness usage harness and budget are required")
        expected_budget_keys = {
            "budget_tokens",
            "estimated_tokens",
            "trim_reasons",
        }
        if set(budget) != expected_budget_keys:
            raise StoreError("Harness usage budget has an unexpected shape")
        for key in ("budget_tokens", "estimated_tokens"):
            value = budget[key]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"Harness usage {key} must be non-negative")
        trim_reasons = budget["trim_reasons"]
        if not isinstance(trim_reasons, dict) or any(
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in trim_reasons.items()
        ):
            raise ValueError("Harness usage trim_reasons must be non-negative counts")

        with self._connection() as connection:
            self._begin_immediate(connection)
            scope = connection.execute(
                """
                SELECT h.work_item_id, h.harness, h.model,
                       r.repository, r.issue_number
                FROM harness_runs h JOIN runs r ON r.id=h.run_id
                WHERE h.run_id=?
                """,
                (run_id,),
            ).fetchone()
            if scope is None:
                raise StoreError("Harness usage references an unknown run")
            expected = {
                "harness": harness,
                "model": model,
            }
            if any(str(scope[key]) != value for key, value in expected.items()):
                raise StoreError("Harness usage identity differs from its run")
            material = {
                "schema_version": 1,
                "run_id": run_id,
                "work_item_id": str(scope["work_item_id"]),
                "repository": str(scope["repository"]),
                "issue_number": int(scope["issue_number"]),
                "run_stage": run_stage,
                "harness": harness,
                "model": model,
                "session_resume": session_resume,
                "portable_context_fallback": portable_context_fallback,
                "metrics": metrics,
                "budget": budget,
            }
            event_digest = _json_digest(material)
            previous = connection.execute(
                "SELECT * FROM harness_usage_events WHERE run_id=?", (run_id,)
            ).fetchone()
            if previous is not None:
                value = self._harness_usage_event(previous)
                if str(value["event_digest"]) != event_digest:
                    raise StoreError("Harness usage was already recorded differently")
                return {**value, "idempotent": True}
            event_id = uuid.uuid4().hex
            now = utc_now()
            connection.execute(
                """
                INSERT INTO harness_usage_events(
                    id, run_id, work_item_id, repository, issue_number,
                    run_stage, harness, model, session_resume,
                    portable_context_fallback, event_digest, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    material["work_item_id"],
                    material["repository"],
                    material["issue_number"],
                    run_stage,
                    harness,
                    model,
                    session_resume,
                    int(portable_context_fallback),
                    event_digest,
                    _canonical_json(material),
                    now,
                ),
            )
        return {
            "id": event_id,
            **material,
            "event_digest": event_digest,
            "created_at": now,
            "idempotent": False,
        }

    @staticmethod
    def _harness_usage_event(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        try:
            payload = json.loads(str(value["payload"]))
        except json.JSONDecodeError as exc:
            raise StoreError("Harness usage ledger was modified") from exc
        if not isinstance(payload, dict):
            raise StoreError("Harness usage ledger was modified")
        scope = {
            "run_id": str(value["run_id"]),
            "work_item_id": str(value["work_item_id"]),
            "repository": str(value["repository"]),
            "issue_number": int(value["issue_number"]),
            "run_stage": str(value["run_stage"]),
            "harness": str(value["harness"]),
            "model": str(value["model"]),
            "session_resume": str(value["session_resume"]),
            "portable_context_fallback": bool(value["portable_context_fallback"]),
        }
        if (
            any(payload.get(key) != expected for key, expected in scope.items())
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("metrics"), dict)
            or set(payload["metrics"]) != USAGE_METRIC_KEYS
            or not isinstance(payload.get("budget"), dict)
            or _json_digest(payload) != str(value["event_digest"])
        ):
            raise StoreError("Harness usage ledger was modified")
        return {
            "id": str(value["id"]),
            **payload,
            "event_digest": str(value["event_digest"]),
            "created_at": str(value["created_at"]),
        }

    @staticmethod
    def _legacy_usage_metrics(details: dict[str, Any]) -> dict[str, Any]:
        raw = details.get("agent_metrics")
        if not isinstance(raw, dict):
            raw = {}
        result: dict[str, Any] = {}
        for key in USAGE_METRIC_KEYS:
            value = raw.get(key)
            if key == "duration_seconds":
                valid = (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and value >= 0
                )
            else:
                valid = (
                    not isinstance(value, bool)
                    and isinstance(value, int)
                    and value >= 0
                )
            result[key] = value if valid else None
        return result

    @staticmethod
    def _legacy_usage_budget(details: dict[str, Any]) -> dict[str, Any]:
        raw = details.get("context_budget")
        if not isinstance(raw, dict):
            raw = {}
        trim_reasons: dict[str, int] = {}
        for key, initial_key, retained_key in (
            ("events", "initial_events", "retained_events"),
            ("diff_snippets", "initial_diff_snippets", "retained_diff_snippets"),
        ):
            initial = raw.get(initial_key)
            retained = raw.get(retained_key)
            if (
                isinstance(initial, int)
                and not isinstance(initial, bool)
                and isinstance(retained, int)
                and not isinstance(retained, bool)
                and initial > retained
            ):
                trim_reasons[key] = initial - retained
        omitted = raw.get("issue_description_omitted_chars")
        if isinstance(omitted, int) and not isinstance(omitted, bool) and omitted > 0:
            trim_reasons["issue_description_chars"] = omitted

        def non_negative_int(name: str) -> int | None:
            raw_value = raw.get(name)
            return (
                raw_value
                if isinstance(raw_value, int)
                and not isinstance(raw_value, bool)
                and raw_value >= 0
                else None
            )

        return {
            "budget_tokens": non_negative_int("budget_tokens"),
            "estimated_tokens": non_negative_int("estimated_tokens"),
            "trim_reasons": trim_reasons,
        }

    def harness_usage_rows(
        self,
        repository: str,
        *,
        issue_number: int = 0,
        pull_number: int = 0,
        run_stage: str = "",
        harness: str = "",
        model: str = "",
        since: str = "",
        until: str = "",
        limit: int = 10_001,
    ) -> list[dict[str, Any]]:
        """Read compact lifecycle usage facts, including compatible legacy runs."""
        limit = min(max(limit, 1), 10_001)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT r.id AS run_id, r.repository, r.issue_number,
                       r.stage AS current_stage, r.status, r.details,
                       r.created_at AS run_created_at,
                       r.updated_at AS run_updated_at,
                       h.work_item_id, h.harness, h.model,
                       h.native_session_id, h.created_at AS harness_created_at,
                       s.pr_url AS submission_pr_url,
                       e.sequence AS usage_sequence, e.payload AS usage_payload,
                       e.event_digest AS usage_event_digest,
                       e.created_at AS usage_created_at
                FROM harness_runs h
                JOIN runs r ON r.id=h.run_id
                LEFT JOIN submissions s
                  ON s.repository=r.repository AND s.issue_number=r.issue_number
                LEFT JOIN harness_usage_events e ON e.run_id=h.run_id
                WHERE r.repository=?
                  AND (?=0 OR r.issue_number=?)
                  AND (
                      ?=0
                      OR CAST(
                          json_extract(r.details, '$.repair_guard.pull_number')
                          AS INTEGER
                      )=?
                      OR rtrim(
                          COALESCE(json_extract(r.details, '$.pr_url'), ''), '/'
                      ) LIKE ?
                      OR rtrim(COALESCE(s.pr_url, ''), '/') LIKE ?
                  )
                  AND (?='' OR h.harness=?)
                  AND (?='' OR h.model=?)
                  AND (?='' OR h.created_at>=?)
                  AND (?='' OR h.created_at<?)
                ORDER BY h.created_at, r.id LIMIT ?
                """,
                (
                    repository.casefold(),
                    issue_number,
                    issue_number,
                    pull_number,
                    pull_number,
                    f"%/pull/{pull_number}",
                    f"%/pull/{pull_number}",
                    harness,
                    harness,
                    model,
                    model,
                    since,
                    since,
                    until,
                    until,
                    limit,
                ),
            ).fetchall()
        if len(rows) >= limit:
            raise StoreError(
                "Harness usage query exceeded 10000 runs; narrow its filters"
            )
        result: list[dict[str, Any]] = []
        for row in rows:
            raw = dict(row)
            try:
                details = json.loads(str(raw["details"]))
            except json.JSONDecodeError as exc:
                raise StoreError("run details were modified") from exc
            if not isinstance(details, dict):
                raise StoreError("run details were modified")
            pr_url = str(details.get("pr_url") or raw["submission_pr_url"] or "")
            parsed_pull = 0
            if "/pull/" in pr_url:
                tail = pr_url.rsplit("/pull/", 1)[1].rstrip("/")
                parsed_pull = int(tail) if tail.isdigit() else 0
            repair_guard = details.get("repair_guard")
            if not parsed_pull and isinstance(repair_guard, dict):
                guard_pull = repair_guard.get("pull_number")
                if isinstance(guard_pull, int) and not isinstance(guard_pull, bool):
                    parsed_pull = guard_pull
            if pull_number and parsed_pull != pull_number:
                continue
            event: dict[str, Any] | None = None
            if raw["usage_sequence"] is not None:
                try:
                    event_payload = json.loads(str(raw["usage_payload"]))
                except json.JSONDecodeError as exc:
                    raise StoreError("Harness usage ledger was modified") from exc
                event_scope = {
                    "run_id": str(raw["run_id"]),
                    "work_item_id": str(raw["work_item_id"]),
                    "repository": str(raw["repository"]),
                    "issue_number": int(raw["issue_number"]),
                    "harness": str(raw["harness"]),
                    "model": str(raw["model"]),
                }
                if (
                    not isinstance(event_payload, dict)
                    or any(
                        event_payload.get(key) != expected
                        for key, expected in event_scope.items()
                    )
                    or _json_digest(event_payload) != str(raw["usage_event_digest"])
                ):
                    raise StoreError("Harness usage ledger was modified")
                event = {"payload": event_payload}
            if event is not None:
                payload = event["payload"]
                stage = str(payload["run_stage"])
                metrics = dict(payload["metrics"])
                budget = dict(payload["budget"])
                session_resume = str(payload["session_resume"])
                portable_fallback: bool | None = bool(
                    payload["portable_context_fallback"]
                )
                source = "ledger"
            else:
                stage = (
                    "repair"
                    if details.get("source_run_id") or isinstance(repair_guard, dict)
                    else (
                        "adopt"
                        if str(raw["harness"]) == "external-workspace"
                        else "prepare"
                    )
                )
                metrics = self._legacy_usage_metrics(details)
                budget = self._legacy_usage_budget(details)
                session_resume = "unknown"
                portable_fallback = None
                source = "legacy"
            if run_stage and stage != run_stage:
                continue
            result.append(
                {
                    "run_id": str(raw["run_id"]),
                    "work_item_id": str(raw["work_item_id"]),
                    "repository": str(raw["repository"]),
                    "issue_number": int(raw["issue_number"]),
                    "pull_number": parsed_pull or None,
                    "run_stage": stage,
                    "current_stage": str(raw["current_stage"]),
                    "status": str(raw["status"]),
                    "harness": str(raw["harness"]),
                    "model": str(raw["model"]),
                    "created_at": str(raw["harness_created_at"]),
                    "metrics": metrics,
                    "budget": budget,
                    "session_resume": session_resume,
                    "portable_context_fallback": portable_fallback,
                    "source": source,
                }
            )
        return result

    @staticmethod
    def _validate_checkpoint_record(
        *,
        work_item_id: str,
        run_id: str,
        context_pack_id: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        expected = {
            "work_item_id": work_item_id,
            "run_id": run_id,
            "context_pack_id": context_pack_id,
            "status": status,
        }
        mismatched = [
            key for key, value in expected.items() if payload.get(key) != value
        ]
        if mismatched:
            raise StoreError(
                "checkpoint payload does not match its record: " + ", ".join(mismatched)
            )
        validate_checkpoint(payload)

    @staticmethod
    def _insert_checkpoint(
        connection: sqlite3.Connection,
        *,
        work_item_id: str,
        run_id: str,
        context_pack_id: str,
        status: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        context = connection.execute(
            """
            SELECT work_item_id, run_id FROM context_packs WHERE id=?
            """,
            (context_pack_id,),
        ).fetchone()
        if context is None:
            raise KeyError(f"context pack not found: {context_pack_id}")
        if (
            str(context["work_item_id"]) != work_item_id
            or str(context["run_id"]) != run_id
        ):
            raise StoreError(
                "checkpoint context pack belongs to a different work item or run"
            )
        row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
            FROM checkpoints WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        assert row is not None
        sequence = int(row["next_sequence"])
        checkpoint_id = uuid.uuid4().hex
        now = utc_now()
        materialized = {
            **payload,
            "id": checkpoint_id,
            "sequence": sequence,
            "created_at": now,
        }
        connection.execute(
            """
            INSERT INTO checkpoints(
                id, work_item_id, run_id, context_pack_id, sequence,
                status, payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint_id,
                work_item_id,
                run_id,
                context_pack_id,
                sequence,
                status,
                json.dumps(materialized, ensure_ascii=False),
                now,
            ),
        )
        return materialized

    def save_checkpoint(
        self,
        *,
        work_item_id: str,
        run_id: str,
        context_pack_id: str,
        status: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._validate_checkpoint_record(
            work_item_id=work_item_id,
            run_id=run_id,
            context_pack_id=context_pack_id,
            status=status,
            payload=payload,
        )
        with self._connection() as connection:
            self._begin_immediate(connection)
            return self._insert_checkpoint(
                connection,
                work_item_id=work_item_id,
                run_id=run_id,
                context_pack_id=context_pack_id,
                status=status,
                payload=payload,
            )

    @staticmethod
    def _github_event_values(
        activity: dict[str, Any],
    ) -> list[dict[str, str]]:
        pull = activity.get("pull_request")
        if not isinstance(pull, dict):
            raise StoreError("GitHub activity is missing its pull request")
        head_sha = str(pull.get("head_sha", ""))
        collections: tuple[tuple[str, object], ...] = (
            ("pull_request", (pull,)),
            ("issue_comment", activity.get("comments")),
            ("review", activity.get("reviews")),
            ("review_comment", activity.get("review_comments")),
            ("check", activity.get("checks")),
        )
        result: list[dict[str, str]] = []
        for event_type, raw_values in collections:
            if not isinstance(raw_values, (list, tuple)):
                raise StoreError(f"GitHub {event_type} activity is not a list")
            for value in raw_values:
                if not isinstance(value, dict):
                    raise StoreError(f"GitHub {event_type} event is not an object")
                external_value = (
                    value.get("number")
                    if event_type == "pull_request"
                    else value.get("id")
                )
                external_id = str(external_value or "").strip()
                if not external_id:
                    raise StoreError(f"GitHub {event_type} event has no stable ID")
                result.append(
                    {
                        "event_type": event_type,
                        "external_id": external_id,
                        "version_digest": _json_digest(value),
                        "head_sha": head_sha,
                        "source_created_at": str(
                            value.get("created_at") or value.get("submitted_at") or ""
                        ),
                        "source_updated_at": str(
                            value.get("updated_at") or value.get("submitted_at") or ""
                        ),
                        "source_actor": str(value.get("author") or ""),
                        "source_state": str(
                            value.get("state")
                            or value.get("status")
                            or value.get("conclusion")
                            or ""
                        ),
                        "payload": _canonical_json(value),
                    }
                )
        return result

    def ingest_github_pr_activity(
        self,
        *,
        run_id: str,
        repository: str,
        pull_number: int,
        activity: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist immutable event versions and return those after the run watermark."""
        repository = repository.casefold()
        if pull_number < 1:
            raise ValueError("pull_number must be positive")
        event_values = self._github_event_values(activity)
        now = utc_now()
        with self._connection() as connection:
            self._begin_immediate(connection)
            run = connection.execute(
                "SELECT repository, details FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"run not found: {run_id}")
            if str(run["repository"]).casefold() != repository:
                raise StoreError("GitHub activity belongs to a different repository")
            details = json.loads(str(run["details"]))
            pr_url = str(details.get("pr_url", "")).rstrip("/")
            if pr_url and not pr_url.endswith(f"/pull/{pull_number}"):
                raise StoreError("GitHub activity belongs to a different pull request")
            connection.execute(
                """
                INSERT INTO github_pr_watermarks(
                    run_id, repository, pull_number, sequence,
                    batch_digest, updated_at
                ) VALUES (?, ?, ?, 0, '', ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (run_id, repository, pull_number, now),
            )
            watermark = connection.execute(
                """
                SELECT repository, pull_number, sequence
                FROM github_pr_watermarks WHERE run_id=?
                """,
                (run_id,),
            ).fetchone()
            assert watermark is not None
            if (
                str(watermark["repository"]).casefold() != repository
                or int(watermark["pull_number"]) != pull_number
            ):
                raise StoreError("GitHub watermark belongs to a different pull request")
            for event in event_values:
                payload_bytes = event["payload"].encode()
                tombstone = connection.execute(
                    "SELECT digest FROM content_blob_tombstones WHERE digest=?",
                    (event["version_digest"],),
                ).fetchone()
                if tombstone is None:
                    connection.execute(
                        """
                        INSERT INTO content_blobs(
                            digest, payload, size_bytes, created_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(digest) DO NOTHING
                        """,
                        (
                            event["version_digest"],
                            payload_bytes,
                            len(payload_bytes),
                            now,
                        ),
                    )
                    stored_blob = connection.execute(
                        "SELECT payload FROM content_blobs WHERE digest=?",
                        (event["version_digest"],),
                    ).fetchone()
                    if (
                        stored_blob is None
                        or bytes(stored_blob["payload"]) != payload_bytes
                    ):
                        raise StoreError("content blob digest collision or corruption")
                connection.execute(
                    """
                    INSERT INTO github_pr_events(
                        repository, pull_number, event_type, external_id,
                        version_digest, head_sha, source_trust, source_created_at,
                        source_updated_at, payload, ingested_at, payload_digest,
                        source_actor, source_state
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, 'github_untrusted', ?, ?, '', ?, ?, ?, ?
                    )
                    ON CONFLICT(
                        repository, pull_number, event_type,
                        external_id, version_digest
                    ) DO NOTHING
                    """,
                    (
                        repository,
                        pull_number,
                        event["event_type"],
                        event["external_id"],
                        event["version_digest"],
                        event["head_sha"],
                        event["source_created_at"],
                        event["source_updated_at"],
                        now,
                        event["version_digest"],
                        event["source_actor"],
                        event["source_state"],
                    ),
                )
            previous_sequence = int(watermark["sequence"])
            rows = connection.execute(
                """
                SELECT e.*, b.payload AS blob_payload
                FROM github_pr_events e
                LEFT JOIN content_blobs b ON b.digest=e.payload_digest
                WHERE e.repository=? AND e.pull_number=? AND e.sequence>?
                ORDER BY sequence
                """,
                (repository, pull_number, previous_sequence),
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            blob_payload = event.pop("blob_payload")
            event["payload_available"] = blob_payload is not None
            event["payload"] = (
                json.loads(bytes(blob_payload))
                if blob_payload is not None
                else self._expired_event_payload(event)
            )
            events.append(event)
        through_sequence = int(events[-1]["sequence"]) if events else previous_sequence
        batch_digest = _json_digest(
            [
                {
                    "sequence": value["sequence"],
                    "event_type": value["event_type"],
                    "external_id": value["external_id"],
                    "version_digest": value["version_digest"],
                }
                for value in events
            ]
        )
        return {
            "previous_sequence": previous_sequence,
            "through_sequence": through_sequence,
            "batch_digest": batch_digest,
            "events": events,
        }

    def seed_github_pr_watermark(
        self,
        *,
        run_id: str,
        repository: str,
        pull_number: int,
        sequence: int,
        batch_digest: str,
    ) -> dict[str, Any]:
        """Start a successor run at an already committed event boundary."""
        repository = repository.casefold()
        if pull_number < 1 or sequence < 0:
            raise ValueError("pull number must be positive and sequence non-negative")
        with self._connection() as connection:
            self._begin_immediate(connection)
            run = connection.execute(
                "SELECT repository FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"run not found: {run_id}")
            if str(run["repository"]).casefold() != repository:
                raise StoreError("GitHub watermark belongs to a different repository")
            if sequence:
                event = connection.execute(
                    """
                    SELECT sequence FROM github_pr_events
                    WHERE repository=? AND pull_number=? AND sequence=?
                    """,
                    (repository, pull_number, sequence),
                ).fetchone()
                if event is None:
                    raise StoreError(
                        "GitHub watermark does not reference a stored event"
                    )
            connection.execute(
                """
                INSERT INTO github_pr_watermarks(
                    run_id, repository, pull_number, sequence,
                    batch_digest, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (run_id, repository, pull_number, sequence, batch_digest, utc_now()),
            )
            row = connection.execute(
                "SELECT * FROM github_pr_watermarks WHERE run_id=?", (run_id,)
            ).fetchone()
            assert row is not None
            if (
                str(row["repository"]).casefold() != repository
                or int(row["pull_number"]) != pull_number
                or int(row["sequence"]) != sequence
                or str(row["batch_digest"]) != batch_digest
            ):
                raise StoreError("successor GitHub watermark already has another scope")
        return dict(row)

    def commit_github_follow_up(
        self,
        *,
        run_id: str,
        repository: str,
        pull_number: int,
        previous_sequence: int,
        through_sequence: int,
        batch_digest: str,
        checkpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Advance a PR watermark and append its Review Checkpoint atomically."""
        repository = repository.casefold()
        checkpoint_args: dict[str, str] | None = None
        if checkpoint is not None:
            checkpoint_args = {
                "work_item_id": str(checkpoint.get("work_item_id", "")),
                "run_id": str(checkpoint.get("run_id", "")),
                "context_pack_id": str(checkpoint.get("context_pack_id", "")),
                "status": str(checkpoint.get("status", "")),
            }
            self._validate_checkpoint_record(payload=checkpoint, **checkpoint_args)
        with self._connection() as connection:
            self._begin_immediate(connection)
            watermark = connection.execute(
                "SELECT * FROM github_pr_watermarks WHERE run_id=?", (run_id,)
            ).fetchone()
            if watermark is None:
                raise KeyError(f"GitHub watermark not found for run: {run_id}")
            if (
                str(watermark["repository"]).casefold() != repository
                or int(watermark["pull_number"]) != pull_number
            ):
                raise StoreError("GitHub watermark belongs to a different pull request")
            current_sequence = int(watermark["sequence"])
            if current_sequence != previous_sequence:
                if current_sequence >= through_sequence:
                    return {
                        "idempotent": True,
                        "sequence": current_sequence,
                        "checkpoint": None,
                    }
                raise StoreError("GitHub watermark changed during follow-up")
            if through_sequence < previous_sequence:
                raise StoreError("GitHub watermark cannot move backwards")
            if through_sequence > previous_sequence:
                event = connection.execute(
                    """
                    SELECT sequence FROM github_pr_events
                    WHERE repository=? AND pull_number=? AND sequence=?
                    """,
                    (repository, pull_number, through_sequence),
                ).fetchone()
                if event is None:
                    raise StoreError(
                        "GitHub watermark does not reference a stored event"
                    )
            materialized = None
            if checkpoint is not None:
                assert checkpoint_args is not None
                materialized = self._insert_checkpoint(
                    connection, payload=checkpoint, **checkpoint_args
                )
            connection.execute(
                """
                UPDATE github_pr_watermarks
                SET sequence=?, batch_digest=?, updated_at=? WHERE run_id=?
                """,
                (through_sequence, batch_digest, utc_now(), run_id),
            )
        return {
            "idempotent": False,
            "sequence": through_sequence,
            "checkpoint": materialized,
        }

    def github_pr_events(
        self, repository: str, pull_number: int
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT e.*, b.payload AS blob_payload
                FROM github_pr_events e
                LEFT JOIN content_blobs b ON b.digest=e.payload_digest
                WHERE e.repository=? AND e.pull_number=? ORDER BY e.sequence
                """,
                (repository.casefold(), pull_number),
            ).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            blob_payload = event.pop("blob_payload")
            event["payload_available"] = blob_payload is not None
            event["payload"] = (
                json.loads(bytes(blob_payload))
                if blob_payload is not None
                else self._expired_event_payload(event)
            )
            result.append(event)
        return result

    @staticmethod
    def _expired_event_payload(event: dict[str, Any]) -> dict[str, Any]:
        external_id = str(event.get("external_id", ""))
        return {
            "id": int(external_id) if external_id.isdecimal() else external_id,
            "author": str(event.get("source_actor", "")),
            "state": str(event.get("source_state", "")),
            "created_at": str(event.get("source_created_at", "")),
            "updated_at": str(event.get("source_updated_at", "")),
            "payload_unavailable": True,
        }

    def content_blob(self, digest: str) -> bytes | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM content_blobs WHERE digest=?", (digest,)
            ).fetchone()
        return bytes(row["payload"]) if row is not None else None

    def storage_statistics(
        self, *, repository: str = "", cutoff: str = ""
    ) -> list[dict[str, Any]]:
        """Return logical payload usage grouped by repository and data category."""
        repository = repository.casefold()
        definitions = (
            (
                "github_event_index",
                """
                SELECT repository, COUNT(*) AS records,
                       COALESCE(SUM(
                           length(CAST(repository AS BLOB)) +
                           length(CAST(event_type AS BLOB)) +
                           length(CAST(external_id AS BLOB)) +
                           length(CAST(version_digest AS BLOB)) +
                           length(CAST(head_sha AS BLOB)) +
                           length(CAST(source_actor AS BLOB)) +
                           length(CAST(source_state AS BLOB))
                       ), 0) AS bytes,
                       MIN(ingested_at) AS oldest_at, MAX(ingested_at) AS newest_at
                FROM github_pr_events
                WHERE (?='' OR repository=?) AND (?='' OR ingested_at>=?)
                GROUP BY repository
                """,
            ),
            (
                "github_event_payload",
                """
                SELECT refs.repository, COUNT(*) AS records,
                       COALESCE(SUM(b.size_bytes), 0) AS bytes,
                       MIN(refs.oldest_at) AS oldest_at,
                       MAX(refs.newest_at) AS newest_at
                FROM (
                    SELECT repository, payload_digest,
                           MIN(ingested_at) AS oldest_at,
                           MAX(ingested_at) AS newest_at
                    FROM github_pr_events
                    WHERE payload_digest<>'' AND (?='' OR repository=?)
                      AND (?='' OR ingested_at>=?)
                    GROUP BY repository, payload_digest
                ) refs JOIN content_blobs b ON b.digest=refs.payload_digest
                GROUP BY refs.repository
                """,
            ),
            (
                "run_metadata",
                """
                SELECT repository, COUNT(*) AS records,
                       COALESCE(SUM(length(CAST(details AS BLOB))), 0) AS bytes,
                       MIN(created_at) AS oldest_at, MAX(updated_at) AS newest_at
                FROM runs
                WHERE (?='' OR repository=?) AND (?='' OR created_at>=?)
                GROUP BY repository
                """,
            ),
            (
                "context_pack",
                """
                SELECT r.repository, COUNT(*) AS records,
                       COALESCE(SUM(length(CAST(p.payload AS BLOB))), 0) AS bytes,
                       MIN(p.created_at) AS oldest_at, MAX(p.created_at) AS newest_at
                FROM context_packs p JOIN runs r ON r.id=p.run_id
                WHERE (?='' OR r.repository=?) AND (?='' OR p.created_at>=?)
                GROUP BY r.repository
                """,
            ),
            (
                "checkpoint",
                """
                SELECT r.repository, COUNT(*) AS records,
                       COALESCE(SUM(length(CAST(c.payload AS BLOB))), 0) AS bytes,
                       MIN(c.created_at) AS oldest_at, MAX(c.created_at) AS newest_at
                FROM checkpoints c JOIN runs r ON r.id=c.run_id
                WHERE (?='' OR r.repository=?) AND (?='' OR c.created_at>=?)
                GROUP BY r.repository
                """,
            ),
            (
                "merge_decision_audit",
                """
                SELECT repository, COUNT(*) AS records,
                       COALESCE(SUM(length(CAST(payload AS BLOB))), 0) AS bytes,
                       MIN(created_at) AS oldest_at, MAX(created_at) AS newest_at
                FROM merge_decisions
                WHERE (?='' OR repository=?) AND (?='' OR created_at>=?)
                GROUP BY repository
                """,
            ),
            (
                "merge_execution_audit",
                """
                SELECT repository, COUNT(*) AS records,
                       COALESCE(SUM(length(CAST(payload AS BLOB))), 0) AS bytes,
                       MIN(created_at) AS oldest_at, MAX(created_at) AS newest_at
                FROM merge_executions
                WHERE (?='' OR repository=?) AND (?='' OR created_at>=?)
                GROUP BY repository
                """,
            ),
            (
                "publication_attempt_audit",
                """
                SELECT repository, COUNT(*) AS records,
                       COALESCE(SUM(length(CAST(payload AS BLOB))), 0) AS bytes,
                       MIN(created_at) AS oldest_at, MAX(created_at) AS newest_at
                FROM publication_attempts
                WHERE (?='' OR repository=?) AND (?='' OR created_at>=?)
                GROUP BY repository
                """,
            ),
            (
                "task_queue_control",
                """
                SELECT repository, COUNT(*) AS records,
                       COALESCE(SUM(length(CAST(parameters AS BLOB))), 0) AS bytes,
                       MIN(created_at) AS oldest_at, MAX(updated_at) AS newest_at
                FROM queue_tasks
                WHERE (?='' OR repository=?) AND (?='' OR created_at>=?)
                GROUP BY repository
                """,
            ),
            (
                "task_queue_attempt_audit",
                """
                SELECT tasks.repository, COUNT(*) AS records,
                       COALESCE(SUM(length(CAST(attempts.payload AS BLOB))), 0) AS bytes,
                       MIN(attempts.created_at) AS oldest_at,
                       MAX(attempts.created_at) AS newest_at
                FROM queue_attempts attempts
                JOIN queue_tasks tasks ON tasks.id=attempts.task_id
                WHERE (?='' OR tasks.repository=?)
                  AND (?='' OR attempts.created_at>=?)
                GROUP BY tasks.repository
                """,
            ),
            (
                "portfolio_dependency_audit",
                """
                SELECT repository, COUNT(*) AS records,
                       COALESCE(SUM(length(CAST(payload AS BLOB))), 0) AS bytes,
                       MIN(created_at) AS oldest_at, MAX(created_at) AS newest_at
                FROM portfolio_dependency_events
                WHERE (?='' OR repository=?) AND (?='' OR created_at>=?)
                GROUP BY repository
                """,
            ),
            (
                "owner_review_attestation_audit",
                """
                SELECT repository, COUNT(*) AS records,
                       COALESCE(SUM(length(CAST(payload AS BLOB))), 0) AS bytes,
                       MIN(created_at) AS oldest_at, MAX(created_at) AS newest_at
                FROM owner_review_attestations
                WHERE (?='' OR repository=?) AND (?='' OR created_at>=?)
                GROUP BY repository
                """,
            ),
            (
                "harness_usage_ledger",
                """
                SELECT repository, COUNT(*) AS records,
                       COALESCE(SUM(length(CAST(payload AS BLOB))), 0) AS bytes,
                       MIN(created_at) AS oldest_at, MAX(created_at) AS newest_at
                FROM harness_usage_events
                WHERE (?='' OR repository=?) AND (?='' OR created_at>=?)
                GROUP BY repository
                """,
            ),
            (
                "storage_gc_audit",
                """
                SELECT CASE WHEN repository='' THEN '*' ELSE repository END AS repository,
                       COUNT(*) AS records,
                       COALESCE(SUM(length(CAST(payload AS BLOB))), 0) AS bytes,
                       MIN(created_at) AS oldest_at, MAX(created_at) AS newest_at
                FROM storage_gc_runs
                WHERE (?='' OR repository=?) AND (?='' OR created_at>=?)
                GROUP BY repository
                """,
            ),
        )
        result: list[dict[str, Any]] = []
        parameters = (repository, repository, cutoff, cutoff)
        with self._connection() as connection:
            for category, query in definitions:
                for row in connection.execute(query, parameters).fetchall():
                    result.append(
                        {
                            "repository": str(row["repository"]),
                            "category": category,
                            "records": int(row["records"]),
                            "bytes": int(row["bytes"]),
                            "oldest_at": str(row["oldest_at"] or ""),
                            "newest_at": str(row["newest_at"] or ""),
                        }
                    )
        return sorted(
            result, key=lambda value: (value["repository"], value["category"])
        )

    def run_repositories(self) -> dict[str, str]:
        with self._connection() as connection:
            rows = connection.execute("SELECT id, repository FROM runs").fetchall()
        return {str(row["id"]): str(row["repository"]) for row in rows}

    def run_gc_safety(self) -> dict[str, dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT r.id, r.repository, r.status, r.worktree, r.details,
                       r.updated_at,
                       EXISTS(
                           SELECT 1 FROM checkpoints c
                           WHERE c.run_id=r.id
                             AND c.status IN ('ready', 'submitted', 'failed')
                       ) AS terminal_checkpoint
                FROM runs r
                """
            ).fetchall()
        result = {}
        for row in rows:
            try:
                details = json.loads(str(row["details"]))
            except json.JSONDecodeError as exc:
                raise StoreError("run details were modified") from exc
            if not isinstance(details, dict):
                raise StoreError("run details were modified")
            result[str(row["id"])] = {
                "repository": str(row["repository"]),
                "status": str(row["status"]),
                "worktree": str(row["worktree"]),
                "updated_at": str(row["updated_at"]),
                "head_commit": str(details.get("commit_sha") or ""),
                "terminal_checkpoint": bool(row["terminal_checkpoint"]),
            }
        return result

    def record_storage_gc(
        self,
        *,
        repository: str,
        actor: str,
        stage: str,
        plan_digest: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if stage not in {"applying", "completed"}:
            raise ValueError("storage GC stage must be applying or completed")
        record_id = uuid.uuid4().hex
        created_at = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO storage_gc_runs(
                    id, repository, actor, stage, plan_digest, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    repository.casefold(),
                    actor,
                    stage,
                    plan_digest,
                    _canonical_json(payload),
                    created_at,
                ),
            )
        return {"id": record_id, "stage": stage, "created_at": created_at}

    def storage_gc_runs(self, *, limit: int = 30) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 100)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM storage_gc_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            result.append(value)
        return result

    @staticmethod
    def _event_payload_gc_inventory(
        connection: sqlite3.Connection, retention_cutoffs: dict[str, str]
    ) -> dict[str, list[dict[str, Any]]]:
        rows = connection.execute(
            """
            SELECT b.digest, b.size_bytes, b.created_at,
                   e.repository, e.pull_number, e.sequence, e.ingested_at,
                   COALESCE(w.watermark_count, 0) AS watermark_count,
                   COALESCE(w.minimum_watermark, 0) AS minimum_watermark
            FROM content_blobs b
            LEFT JOIN github_pr_events e ON e.payload_digest=b.digest
            LEFT JOIN (
                SELECT repository, pull_number,
                       COUNT(*) AS watermark_count,
                       MIN(sequence) AS minimum_watermark
                FROM github_pr_watermarks
                GROUP BY repository, pull_number
            ) w ON w.repository=e.repository AND w.pull_number=e.pull_number
            ORDER BY b.created_at, b.digest, e.sequence
            """
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            digest = str(row["digest"])
            value = grouped.setdefault(
                digest,
                {
                    "kind": "github_event_payload",
                    "digest": digest,
                    "bytes": int(row["size_bytes"]),
                    "created_at": str(row["created_at"]),
                    "repositories": set(),
                    "reference_count": 0,
                    "reasons": set(),
                },
            )
            if row["repository"] is None:
                value["reasons"].add("unreferenced_content_blob")
                continue
            repository = str(row["repository"])
            value["repositories"].add(repository)
            value["reference_count"] += 1
            cutoff = retention_cutoffs.get(repository)
            if not cutoff:
                value["reasons"].add("no_explicit_event_retention")
            elif str(row["ingested_at"]) >= cutoff:
                value["reasons"].add("within_event_retention")
            if int(row["watermark_count"] or 0) < 1 or int(
                row["minimum_watermark"] or 0
            ) < int(row["sequence"]):
                value["reasons"].add("not_checkpointed_by_every_run")
        candidates = []
        retained = []
        for value in grouped.values():
            reasons = set(value.pop("reasons"))
            value["repositories"] = sorted(value["repositories"])
            if reasons == {"unreferenced_content_blob"}:
                value["reason"] = "unreferenced_content_blob"
                candidates.append(value)
            elif not reasons:
                value["reason"] = "explicit_retention_elapsed_and_checkpointed"
                candidates.append(value)
            else:
                value["reasons"] = sorted(reasons)
                retained.append(value)
        key = lambda item: (item["created_at"], item["digest"])
        return {
            "candidates": sorted(candidates, key=key),
            "retained": sorted(retained, key=key),
        }

    def event_payload_gc_inventory(
        self, retention_cutoffs: dict[str, str]
    ) -> dict[str, list[dict[str, Any]]]:
        normalized = {
            repository.casefold(): cutoff
            for repository, cutoff in retention_cutoffs.items()
        }
        with self._connection() as connection:
            return self._event_payload_gc_inventory(connection, normalized)

    def delete_event_payloads(
        self, digests: tuple[str, ...], *, retention_cutoffs: dict[str, str]
    ) -> dict[str, Any]:
        requested = set(digests)
        if not requested:
            return {"deleted": [], "skipped": []}
        normalized = {
            repository.casefold(): cutoff
            for repository, cutoff in retention_cutoffs.items()
        }
        deleted = []
        with self._connection() as connection:
            self._begin_immediate(connection)
            inventory = self._event_payload_gc_inventory(connection, normalized)
            eligible = {value["digest"]: value for value in inventory["candidates"]}
            for digest in sorted(requested & eligible.keys()):
                value = eligible[digest]
                connection.execute(
                    """
                    INSERT INTO content_blob_tombstones(
                        digest, category, size_bytes, reason, deleted_at
                    ) VALUES (?, 'github_event_payload', ?, ?, ?)
                    ON CONFLICT(digest) DO NOTHING
                    """,
                    (digest, value["bytes"], value["reason"], utc_now()),
                )
                cursor = connection.execute(
                    "DELETE FROM content_blobs WHERE digest=?", (digest,)
                )
                if cursor.rowcount:
                    deleted.append(value)
        return {
            "deleted": deleted,
            "skipped": sorted(requested - {value["digest"] for value in deleted}),
        }

    def github_pr_watermark(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM github_pr_watermarks WHERE run_id=?", (run_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def update_work_item_status(self, work_item_id: str, status: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE work_items SET status=?, updated_at=? WHERE id=?
                """,
                (status, utc_now(), work_item_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"work item not found: {work_item_id}")

    def update_work_item_status_for_run(self, run_id: str, status: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE work_items SET status=?, updated_at=?
                WHERE id=(SELECT work_item_id FROM harness_runs WHERE run_id=?)
                """,
                (status, utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"work item for run not found: {run_id}")

    def context_bundle(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    h.harness,
                    h.model,
                    h.native_session_id,
                    h.created_at AS harness_created_at,
                    w.id AS work_item_id,
                    w.repository,
                    w.kind,
                    w.external_id,
                    w.title,
                    w.status AS work_item_status,
                    w.payload AS work_item_payload,
                    p.id AS context_pack_id,
                    p.schema_version,
                    p.source_digest,
                    p.base_commit,
                    p.payload AS context_payload,
                    p.created_at AS context_created_at
                FROM harness_runs h
                JOIN work_items w ON w.id=h.work_item_id
                JOIN context_packs p ON p.id=h.context_pack_id
                WHERE h.run_id=?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            checkpoint = connection.execute(
                """
                SELECT payload FROM checkpoints
                WHERE run_id=? ORDER BY sequence DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        result = dict(row)
        return {
            "work_item": {
                "id": result["work_item_id"],
                "repository": result["repository"],
                "kind": result["kind"],
                "external_id": result["external_id"],
                "title": result["title"],
                "status": result["work_item_status"],
                "payload": json.loads(result["work_item_payload"]),
            },
            "harness_run": {
                "run_id": run_id,
                "harness": result["harness"],
                "model": result["model"],
                "native_session_id": result["native_session_id"],
                "created_at": result["harness_created_at"],
            },
            "context_pack": json.loads(result["context_payload"]),
            "context_metadata": {
                "id": result["context_pack_id"],
                "schema_version": result["schema_version"],
                "source_digest": result["source_digest"],
                "base_commit": result["base_commit"],
                "created_at": result["context_created_at"],
            },
            "checkpoint": (
                json.loads(checkpoint["payload"]) if checkpoint is not None else None
            ),
        }

    def latest_checkpoint_for_work_item(
        self, work_item_id: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            local = connection.execute(
                """
                SELECT payload, created_at FROM checkpoints
                WHERE work_item_id=?
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (work_item_id,),
            ).fetchone()
            imported = connection.execute(
                """
                SELECT payload, imported_at FROM context_imports
                WHERE work_item_id=?
                ORDER BY imported_at DESC, rowid DESC LIMIT 1
                """,
                (work_item_id,),
            ).fetchone()
        if local is None and imported is None:
            return None
        if imported is not None and (
            local is None or str(imported["imported_at"]) >= str(local["created_at"])
        ):
            bundle = json.loads(imported["payload"])
            checkpoint = bundle.get("checkpoint")
            return checkpoint if isinstance(checkpoint, dict) else None
        assert local is not None
        return json.loads(local["payload"])

    def latest_harness_session(self, work_item_id: str, harness: str) -> str:
        with self._connection() as connection:
            local = connection.execute(
                """
                SELECT native_session_id, created_at FROM harness_runs
                WHERE work_item_id=? AND harness=? AND native_session_id<>''
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (work_item_id, harness),
            ).fetchone()
            imported_rows = connection.execute(
                """
                SELECT payload, imported_at FROM context_imports
                WHERE work_item_id=? ORDER BY imported_at DESC, rowid DESC LIMIT 20
                """,
                (work_item_id,),
            ).fetchall()
        imported_session = ""
        imported_at = ""
        for row in imported_rows:
            bundle = json.loads(row["payload"])
            harness_run = bundle.get("harness_run")
            if not isinstance(harness_run, dict):
                continue
            if harness_run.get("harness") != harness:
                continue
            value = str(harness_run.get("native_session_id", ""))
            if value:
                imported_session = value
                imported_at = str(row["imported_at"])
                break
        if imported_session and (
            local is None or imported_at >= str(local["created_at"])
        ):
            return imported_session
        return str(local["native_session_id"]) if local is not None else ""

    def import_context_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        from .protocol import validate_context_bundle

        validate_context_bundle(bundle, require_checkpoint=True)
        source_work_item = bundle["work_item"]
        harness_run = bundle["harness_run"]
        bundle_digest = str(bundle["bundle_digest"])
        import_id = uuid.uuid4().hex
        work_item_id = uuid.uuid4().hex
        now = utc_now()
        repository = str(source_work_item["repository"]).lower()
        kind = str(source_work_item["kind"])
        external_id = str(source_work_item["external_id"])
        title = str(source_work_item["title"])
        work_item_payload = json.dumps(
            source_work_item.get("payload", {}), ensure_ascii=False
        )
        serialized = json.dumps(bundle, ensure_ascii=False)
        with self._connection() as connection:
            self._begin_immediate(connection)
            existing = connection.execute(
                """
                SELECT id, work_item_id, imported_at FROM context_imports
                WHERE bundle_digest=?
                """,
                (bundle_digest,),
            ).fetchone()
            if existing is not None:
                return {
                    "id": str(existing["id"]),
                    "imported": False,
                    "bundle_digest": bundle_digest,
                    "work_item_id": str(existing["work_item_id"]),
                    "repository": repository,
                    "kind": kind,
                    "external_id": external_id,
                    "source_run_id": str(harness_run["run_id"]),
                    "checkpoint_status": str(bundle["checkpoint"]["status"]),
                    "imported_at": str(existing["imported_at"]),
                }
            connection.execute(
                """
                INSERT INTO work_items(
                    id, repository, kind, external_id, title, status,
                    payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'imported', ?, ?, ?)
                ON CONFLICT(repository, kind, external_id) DO NOTHING
                """,
                (
                    work_item_id,
                    repository,
                    kind,
                    external_id,
                    title,
                    work_item_payload,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM work_items
                WHERE repository=? AND kind=? AND external_id=?
                """,
                (repository, kind, external_id),
            ).fetchone()
            assert row is not None
            work_item_id = str(row["id"])
            connection.execute(
                """
                INSERT INTO context_imports(
                    id, bundle_digest, work_item_id, source_run_id,
                    payload, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(bundle_digest) DO NOTHING
                """,
                (
                    import_id,
                    bundle_digest,
                    work_item_id,
                    str(harness_run["run_id"]),
                    serialized,
                    now,
                ),
            )
        return {
            "id": import_id,
            "imported": True,
            "bundle_digest": bundle_digest,
            "work_item_id": work_item_id,
            "repository": repository,
            "kind": kind,
            "external_id": external_id,
            "source_run_id": str(harness_run["run_id"]),
            "checkpoint_status": str(bundle["checkpoint"]["status"]),
            "imported_at": now,
        }

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

    @staticmethod
    def _lease_inputs(
        scope: str,
        owner: str,
        ttl_seconds: int,
        now: datetime | None,
    ) -> tuple[str, str, datetime, str, str]:
        normalized_scope = scope.strip().casefold()
        normalized_owner = owner.strip()
        if not normalized_scope or len(normalized_scope) > 500:
            raise ValueError("run lease scope must contain at most 500 characters")
        if not normalized_owner or len(normalized_owner) > 128:
            raise ValueError("run lease owner must contain at most 128 characters")
        if isinstance(ttl_seconds, bool) or not 5 <= ttl_seconds <= 86_400:
            raise ValueError("run lease ttl_seconds must be between 5 and 86400")
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("run lease time must include a timezone")
        current = current.astimezone(UTC)
        current_text = current.isoformat(timespec="microseconds")
        expires_at = (current + timedelta(seconds=ttl_seconds)).isoformat(
            timespec="microseconds"
        )
        return (
            normalized_scope,
            normalized_owner,
            current,
            current_text,
            expires_at,
        )

    @staticmethod
    def _append_run_lease_event(
        connection: sqlite3.Connection,
        lease: RunLease,
        action: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO run_lease_events(
                scope, owner, generation, action, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                lease.scope,
                lease.owner,
                lease.generation,
                action,
                lease.expires_at,
                created_at,
            ),
        )

    def acquire_run_lease(
        self,
        scope: str,
        *,
        owner: str,
        ttl_seconds: int = 90,
        now: datetime | None = None,
    ) -> RunLease:
        normalized_scope, normalized_owner, _, current_text, expires_at = (
            self._lease_inputs(scope, owner, ttl_seconds, now)
        )
        with self._connection(guard_bound_lease=False) as connection:
            self._begin_immediate(connection)
            row = connection.execute(
                "SELECT * FROM run_leases WHERE scope=?", (normalized_scope,)
            ).fetchone()
            if row is None:
                generation = 1
                action = "acquired"
                connection.execute(
                    """
                    INSERT INTO run_leases(
                        scope, owner, generation, expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_scope,
                        normalized_owner,
                        generation,
                        expires_at,
                        current_text,
                    ),
                )
            else:
                current_owner = str(row["owner"])
                active = str(row["expires_at"]) > current_text
                if active and current_owner != normalized_owner:
                    raise StoreError(
                        f"run lease is already held for {normalized_scope}"
                    )
                generation = int(row["generation"])
                if not active:
                    generation += 1
                    action = (
                        "taken_over"
                        if current_owner != normalized_owner
                        else "reacquired"
                    )
                else:
                    action = "renewed"
                connection.execute(
                    """
                    UPDATE run_leases
                    SET owner=?, generation=?, expires_at=?, updated_at=?
                    WHERE scope=?
                    """,
                    (
                        normalized_owner,
                        generation,
                        expires_at,
                        current_text,
                        normalized_scope,
                    ),
                )
            lease = RunLease(normalized_scope, normalized_owner, generation, expires_at)
            self._append_run_lease_event(connection, lease, action, current_text)
        return lease

    def renew_run_lease(
        self,
        lease: RunLease,
        *,
        ttl_seconds: int = 90,
        now: datetime | None = None,
    ) -> RunLease:
        scope, owner, _, current_text, expires_at = self._lease_inputs(
            lease.scope, lease.owner, ttl_seconds, now
        )
        renewed = RunLease(scope, owner, lease.generation, expires_at)
        with self._connection(guard_bound_lease=False) as connection:
            self._begin_immediate(connection)
            cursor = connection.execute(
                """
                UPDATE run_leases SET expires_at=?, updated_at=?
                WHERE scope=? AND owner=? AND generation=? AND expires_at>?
                """,
                (
                    expires_at,
                    current_text,
                    scope,
                    owner,
                    lease.generation,
                    current_text,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError(f"run lease is stale for {scope}")
            self._append_run_lease_event(connection, renewed, "renewed", current_text)
        return renewed

    @staticmethod
    def _assert_run_lease_row(
        connection: sqlite3.Connection,
        lease: RunLease,
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("run lease time must include a timezone")
        current_text = current.astimezone(UTC).isoformat(timespec="microseconds")
        row = connection.execute(
            """
            SELECT 1 FROM run_leases
            WHERE scope=? AND owner=? AND generation=? AND expires_at>?
            """,
            (lease.scope, lease.owner, lease.generation, current_text),
        ).fetchone()
        if row is None:
            raise StoreError(f"run lease is stale for {lease.scope}")

    def validate_run_lease(
        self, lease: RunLease, *, now: datetime | None = None
    ) -> None:
        with self._connection(guard_bound_lease=False) as connection:
            self._assert_run_lease_row(connection, lease, now=now)

    def release_run_lease(self, lease: RunLease) -> None:
        created_at = utc_now()
        released = RunLease(lease.scope, lease.owner, lease.generation, created_at)
        with self._connection(guard_bound_lease=False) as connection:
            self._begin_immediate(connection)
            cursor = connection.execute(
                """
                UPDATE run_leases SET expires_at=?, updated_at=?
                WHERE scope=? AND owner=? AND generation=?
                """,
                (
                    created_at,
                    created_at,
                    lease.scope,
                    lease.owner,
                    lease.generation,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError(f"run lease is stale for {lease.scope}")
            self._append_run_lease_event(connection, released, "released", created_at)

    @contextmanager
    def bind_run_lease(self, lease: RunLease) -> Iterator[None]:
        """Require guarded run updates in the current execution context."""
        token = self._run_lease_context.set(lease)
        try:
            yield
        finally:
            self._run_lease_context.reset(token)

    def run_lease_events(self, scope: str, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM run_lease_events
                WHERE scope=? ORDER BY sequence DESC LIMIT ?
                """,
                (scope.strip().casefold(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _queue_timestamp(now: datetime | None = None) -> tuple[datetime, str]:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("queue time must include a timezone")
        normalized = current.astimezone(UTC)
        return normalized, normalized.isoformat(timespec="microseconds")

    @staticmethod
    def _queue_worker(worker: str) -> str:
        normalized = worker.strip()
        if (
            not normalized
            or len(normalized) > 128
            or any(character in normalized for character in "\r\n")
            or normalized.startswith(("/", "~"))
            or re.match(r"^[A-Za-z]:[\\/]", normalized)
        ):
            raise ValueError("queue worker must contain 1 to 128 characters")
        return normalized

    @staticmethod
    def _queue_payload(payload: dict[str, Any], *, allowed_keys: frozenset[str]) -> str:
        if not isinstance(payload, dict):
            raise TypeError("queue payload must be an object")
        unknown = set(payload) - allowed_keys
        if unknown:
            raise ValueError(
                "queue payload contains unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        for value in payload.values():
            if not isinstance(value, (str, int, bool)):
                raise TypeError("queue payload values must be bounded scalars")
            if isinstance(value, str):
                if len(value) > 512:
                    raise ValueError("queue payload string exceeds 512 characters")
                if value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", value):
                    raise ValueError("queue payload must not contain absolute paths")
        encoded = _canonical_json(payload)
        if len(encoded.encode()) > 4_096:
            raise ValueError("queue payload exceeds 4096 bytes")
        return encoded

    @staticmethod
    def _validate_queue_parameters(action: str, parameters: dict[str, Any]) -> None:
        required: dict[str, frozenset[str]] = {
            "prepare": frozenset(),
            "follow-up": frozenset(),
            "repair": frozenset(),
            "merge-decision": frozenset(),
            "submit": frozenset({"reviewed_by"}),
            "merge-attest": frozenset({"reviewed_by"}),
            "merge": frozenset({"reviewed_by", "decision_id"}),
            "batch-advance": frozenset(
                {
                    "reviewed_by",
                    "batch_plan_digest",
                    "batch_head_sha",
                    "batch_base_sha",
                    "batch_verified_base_sha",
                }
            ),
        }
        required_keys = required[action]
        missing = required_keys - set(parameters)
        if missing:
            raise ValueError(
                "queue action is missing parameters: " + ", ".join(sorted(missing))
            )
        allowed = required_keys | (
            frozenset({"reopen_pull_request"}) if action == "submit" else frozenset()
        )
        unexpected = set(parameters) - allowed
        if unexpected:
            raise ValueError(
                "queue action has unexpected parameters: "
                + ", ".join(sorted(unexpected))
            )
        reviewed_by = parameters.get("reviewed_by")
        if reviewed_by is not None and (
            not isinstance(reviewed_by, str)
            or not reviewed_by.strip()
            or len(reviewed_by) > 128
        ):
            raise ValueError("queue reviewed_by must contain 1 to 128 characters")
        decision_id = parameters.get("decision_id")
        if decision_id is not None and (
            not isinstance(decision_id, str) or not _is_lower_hex(decision_id, 32)
        ):
            raise ValueError("queue decision_id must be 32 lowercase hex characters")
        for key, length in (
            ("batch_plan_digest", 64),
            ("batch_head_sha", 40),
            ("batch_base_sha", 40),
            ("batch_verified_base_sha", 40),
        ):
            value = parameters.get(key)
            if value is not None and (
                not isinstance(value, str) or not _is_lower_hex(value, length)
            ):
                raise ValueError(
                    f"queue {key} must be {length} lowercase hex characters"
                )
        reopen = parameters.get("reopen_pull_request")
        if reopen is not None and (
            isinstance(reopen, bool) or not isinstance(reopen, int) or reopen < 0
        ):
            raise ValueError("queue reopen_pull_request must not be negative")

    @staticmethod
    def _queue_task(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        for key in ("work_item_id", "run_id", "depends_on_task_id"):
            value[key] = str(value.get(key) or "")
        action = str(value["action"])
        if action not in QUEUE_ACTIONS or str(value["state"]) not in QUEUE_STATES:
            raise StoreError("queue task contains an unsupported action or state")
        try:
            parameters = json.loads(str(value["parameters"]))
            if not isinstance(parameters, dict):
                raise TypeError("queue parameters are not an object")
            Store._validate_queue_parameters(action, parameters)
            encoded_parameters = Store._queue_payload(
                parameters, allowed_keys=QUEUE_PARAMETER_KEYS
            )
        except (TypeError, ValueError) as exc:
            raise StoreError("queue task parameters were modified") from exc
        if hashlib.sha256(encoded_parameters.encode()).hexdigest() != str(
            value["parameters_digest"]
        ):
            raise StoreError("queue task parameters were modified")
        material = {
            "repository": str(value["repository"]),
            "action": action,
            "work_item_id": value["work_item_id"],
            "run_id": value["run_id"],
            "issue_number": int(value["issue_number"]),
            "pull_number": int(value["pull_number"]),
            "parameters_digest": str(value["parameters_digest"]),
            "depends_on_task_id": value["depends_on_task_id"],
            "idempotency_digest": str(value["idempotency_digest"]),
        }
        if _json_digest(material) != str(value["dedupe_key"]):
            raise StoreError("queue task identity was modified")
        value["parameters"] = parameters
        value["manual_required"] = bool(value["manual_required"])
        return value

    def _append_queue_attempt(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        generation: int,
        worker: str,
        event: str,
        outcome: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> dict[str, Any]:
        if event not in QUEUE_EVENTS:
            raise ValueError(f"unsupported queue event: {event!r}")
        if outcome not in QUEUE_STATES:
            raise ValueError(f"unsupported queue outcome: {outcome!r}")
        encoded = self._queue_payload(payload, allowed_keys=QUEUE_RESULT_KEYS)
        payload_digest = hashlib.sha256(encoded.encode()).hexdigest()
        event_id = uuid.uuid4().hex
        try:
            connection.execute(
                """
                INSERT INTO queue_attempts(
                    id, task_id, generation, worker, event, outcome, payload,
                    payload_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    task_id,
                    generation,
                    worker,
                    event,
                    outcome,
                    encoded,
                    payload_digest,
                    created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise StoreError("queue attempt event already exists") from exc
        return {
            "id": event_id,
            "task_id": task_id,
            "generation": generation,
            "worker": worker,
            "event": event,
            "outcome": outcome,
            "payload": payload,
            "created_at": created_at,
        }

    def enqueue_queue_task(
        self,
        repository: str,
        *,
        action: str,
        enqueued_by: str,
        work_item_id: str = "",
        run_id: str = "",
        issue_number: int = 0,
        pull_number: int = 0,
        parameters: dict[str, Any] | None = None,
        priority: int = 0,
        depends_on_task_id: str = "",
        max_attempts: int = 3,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        normalized_repository = repository.strip().casefold()
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", normalized_repository):
            raise ValueError("queue repository must use owner/name")
        if action not in QUEUE_ACTIONS:
            raise ValueError(f"unsupported queue action: {action!r}")
        worker = self._queue_worker(enqueued_by)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (issue_number, pull_number)
        ):
            raise ValueError("queue issue and pull numbers must not be negative")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise TypeError("queue priority must be an integer")
        if not -1_000 <= priority <= 1_000:
            raise ValueError("queue priority must be between -1000 and 1000")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("queue max_attempts must be an integer")
        if not 1 <= max_attempts <= 20:
            raise ValueError("queue max_attempts must be between 1 and 20")
        if len(idempotency_key) > 128:
            raise ValueError("queue idempotency key exceeds 128 characters")
        for label, reference in (
            ("work_item_id", work_item_id),
            ("run_id", run_id),
            ("depends_on_task_id", depends_on_task_id),
        ):
            if len(reference) > 128 or any(
                character.isspace() for character in reference
            ):
                raise ValueError(f"queue {label} must be a bounded identifier")
        values = dict(parameters or {})
        self._validate_queue_parameters(action, values)
        encoded_parameters = self._queue_payload(
            values, allowed_keys=QUEUE_PARAMETER_KEYS
        )
        if action in {"prepare", "submit"}:
            if issue_number < 1 or run_id or work_item_id or pull_number:
                raise ValueError(
                    f"queue action {action!r} requires only an issue reference"
                )
        elif not run_id:
            raise ValueError(f"queue action {action!r} requires run_id")
        now = utc_now()
        task_id = uuid.uuid4().hex
        with self._connection() as connection:
            self._begin_immediate(connection)
            if run_id:
                run = connection.execute(
                    "SELECT repository, issue_number, details FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise StoreError("queue task references a missing run")
                if str(run["repository"]) != normalized_repository:
                    raise StoreError("queue run belongs to another repository")
                run_issue = int(run["issue_number"])
                if issue_number and issue_number != run_issue:
                    raise StoreError("queue issue does not match its run")
                issue_number = run_issue
                if pull_number:
                    details = json.loads(str(run["details"]))
                    match = re.search(
                        r"/pull/(\d+)/?$", str(details.get("pr_url") or "")
                    )
                    if match is None or int(match.group(1)) != pull_number:
                        raise StoreError("queue pull number does not match its run")
            normalized_work_item_id = work_item_id or None
            if normalized_work_item_id:
                item = connection.execute(
                    "SELECT repository FROM work_items WHERE id=?",
                    (normalized_work_item_id,),
                ).fetchone()
                if item is None:
                    raise StoreError("queue task references a missing work item")
                if str(item["repository"]) != normalized_repository:
                    raise StoreError("queue work item belongs to another repository")
                if run_id:
                    binding = connection.execute(
                        """
                        SELECT 1 FROM harness_runs
                        WHERE run_id=? AND work_item_id=?
                        """,
                        (run_id, normalized_work_item_id),
                    ).fetchone()
                    if binding is None:
                        raise StoreError(
                            "queue run and work item do not share a context binding"
                        )
            normalized_dependency = depends_on_task_id or None
            if normalized_dependency:
                dependency = connection.execute(
                    "SELECT repository FROM queue_tasks WHERE id=?",
                    (normalized_dependency,),
                ).fetchone()
                if dependency is None:
                    raise StoreError("queue dependency task does not exist")
                if str(dependency["repository"]) != normalized_repository:
                    raise StoreError("queue dependency belongs to another repository")
            idempotency_digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
            material = {
                "repository": normalized_repository,
                "action": action,
                "work_item_id": normalized_work_item_id or "",
                "run_id": run_id,
                "issue_number": issue_number,
                "pull_number": pull_number,
                "parameters_digest": hashlib.sha256(
                    encoded_parameters.encode()
                ).hexdigest(),
                "depends_on_task_id": normalized_dependency or "",
                "idempotency_digest": idempotency_digest,
            }
            dedupe_key = _json_digest(material)
            cursor = connection.execute(
                """
                INSERT INTO queue_tasks(
                    id, dedupe_key, repository, action, work_item_id, run_id,
                    issue_number, pull_number, parameters, parameters_digest,
                    idempotency_digest, priority, state, depends_on_task_id, max_attempts,
                    available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                ON CONFLICT(dedupe_key) DO NOTHING
                """,
                (
                    task_id,
                    dedupe_key,
                    normalized_repository,
                    action,
                    normalized_work_item_id,
                    run_id or None,
                    issue_number,
                    pull_number,
                    encoded_parameters,
                    material["parameters_digest"],
                    idempotency_digest,
                    priority,
                    normalized_dependency,
                    max_attempts,
                    now,
                    now,
                    now,
                ),
            )
            idempotent = cursor.rowcount == 0
            row = connection.execute(
                "SELECT * FROM queue_tasks WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            if row is None:
                raise StoreError("queue task was not persisted")
            if not idempotent:
                self._append_queue_attempt(
                    connection,
                    task_id=task_id,
                    generation=0,
                    worker=worker,
                    event="enqueued",
                    outcome="pending",
                    payload={},
                    created_at=now,
                )
        return {**self._queue_task(row), "idempotent": idempotent}

    def queue_tasks(
        self,
        *,
        repository: str = "",
        task_id: str = "",
        states: tuple[str, ...] = (),
        limit: int = 100,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        normalized_repository = repository.casefold()
        unknown_states = set(states) - QUEUE_STATES
        if unknown_states:
            raise ValueError(
                "unsupported queue states: " + ", ".join(sorted(unknown_states))
            )
        limit = min(max(limit, 1), 500)
        _current, current_text = self._queue_timestamp(now)
        state_filter = ""
        parameters: list[object] = [
            normalized_repository,
            normalized_repository,
            task_id,
            task_id,
        ]
        if states:
            placeholders = ",".join("?" for _ in states)
            state_filter = f"AND tasks.state IN ({placeholders})"
            parameters.extend(states)
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT tasks.*, dependency.state AS dependency_state
                FROM queue_tasks tasks
                LEFT JOIN queue_tasks dependency
                  ON dependency.id=tasks.depends_on_task_id
                WHERE (?='' OR tasks.repository=?)
                  AND (?='' OR tasks.id=?) {state_filter}
                ORDER BY tasks.priority DESC, tasks.sequence ASC LIMIT ?
                """,
                parameters,
            ).fetchall()
        result = []
        for row in rows:
            value = self._queue_task(row)
            dependency_state = str(value.pop("dependency_state") or "")
            value["dependency_state"] = dependency_state
            value["blocked_by_dependency"] = bool(
                value["depends_on_task_id"] and dependency_state != "completed"
            )
            value["lease_expired"] = bool(
                value["state"] == "running"
                and value["lease_expires_at"]
                and str(value["lease_expires_at"]) <= current_text
            )
            value["attention"] = (
                "manual"
                if value["manual_required"]
                else "dependency"
                if value["blocked_by_dependency"]
                else "lease_expired"
                if value["lease_expired"]
                else ""
            )
            result.append(value)
        return result

    def queue_task_summary(self, *, repository: str = "") -> dict[str, Any]:
        normalized_repository = repository.casefold()
        counts = {state: 0 for state in QUEUE_STATES}
        manual_required = 0
        dependency_blocked = 0
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT tasks.state, COUNT(*) AS records,
                       SUM(tasks.manual_required) AS manual_required,
                       SUM(CASE
                           WHEN tasks.depends_on_task_id IS NOT NULL
                            AND dependency.state<>'completed' THEN 1 ELSE 0
                       END) AS dependency_blocked
                FROM queue_tasks tasks
                LEFT JOIN queue_tasks dependency
                  ON dependency.id=tasks.depends_on_task_id
                WHERE (?='' OR tasks.repository=?)
                GROUP BY tasks.state
                """,
                (normalized_repository, normalized_repository),
            ).fetchall()
        for row in rows:
            counts[str(row["state"])] = int(row["records"])
            manual_required += int(row["manual_required"] or 0)
            dependency_blocked += int(row["dependency_blocked"] or 0)
        return {
            "counts": counts,
            "manual_required": manual_required,
            "dependency_blocked": dependency_blocked,
        }

    def queue_attempts(self, task_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM queue_attempts
                    WHERE task_id=? ORDER BY sequence DESC LIMIT ?
                ) recent ORDER BY sequence ASC
                """,
                (task_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            try:
                payload = json.loads(str(value["payload"]))
                if not isinstance(payload, dict):
                    raise TypeError("queue attempt payload is not an object")
                encoded = self._queue_payload(payload, allowed_keys=QUEUE_RESULT_KEYS)
            except (TypeError, ValueError) as exc:
                raise StoreError("queue attempt payload was modified") from exc
            if hashlib.sha256(encoded.encode()).hexdigest() != str(
                value["payload_digest"]
            ):
                raise StoreError("queue attempt payload was modified")
            value["payload"] = payload
            result.append(value)
        return result

    def claim_queue_tasks(
        self,
        *,
        worker: str,
        limit: int = 1,
        lease_seconds: int = 900,
        repository: str = "",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        normalized_worker = self._queue_worker(worker)
        if not 1 <= limit <= 100:
            raise ValueError("queue claim limit must be between 1 and 100")
        if not 5 <= lease_seconds <= 86_400:
            raise ValueError("queue lease_seconds must be between 5 and 86400")
        current, current_text = self._queue_timestamp(now)
        expires_at = (current + timedelta(seconds=lease_seconds)).isoformat(
            timespec="microseconds"
        )
        normalized_repository = repository.casefold()
        claimed: list[dict[str, Any]] = []
        with self._connection() as connection:
            self._begin_immediate(connection)
            exhausted = connection.execute(
                """
                SELECT * FROM queue_tasks
                WHERE state='running' AND lease_expires_at<=?
                  AND attempt_count>=max_attempts
                  AND (?='' OR repository=?)
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (
                    current_text,
                    normalized_repository,
                    normalized_repository,
                    limit,
                ),
            ).fetchall()
            for row in exhausted:
                connection.execute(
                    """
                    UPDATE queue_tasks
                    SET state='failed', manual_required=1,
                        last_error_code='attempt_limit_exhausted',
                        lease_owner='', lease_expires_at='', updated_at=?
                    WHERE id=? AND state='running' AND lease_generation=?
                    """,
                    (current_text, row["id"], row["lease_generation"]),
                )
                self._append_queue_attempt(
                    connection,
                    task_id=str(row["id"]),
                    generation=int(row["lease_generation"]),
                    worker=normalized_worker,
                    event="attempts_exhausted",
                    outcome="failed",
                    payload={
                        "error_code": "attempt_limit_exhausted",
                        "retryable": False,
                        "manual_required": True,
                    },
                    created_at=current_text,
                )
            rows = connection.execute(
                """
                SELECT tasks.* FROM queue_tasks tasks
                LEFT JOIN queue_tasks dependency
                  ON dependency.id=tasks.depends_on_task_id
                WHERE tasks.manual_required=0
                  AND tasks.attempt_count<tasks.max_attempts
                  AND (?='' OR tasks.repository=?)
                  AND (
                    (tasks.state IN ('pending', 'failed') AND tasks.available_at<=?)
                    OR (tasks.state='running' AND tasks.lease_expires_at<=?)
                  )
                  AND (
                    tasks.depends_on_task_id IS NULL
                    OR dependency.state='completed'
                  )
                ORDER BY tasks.priority DESC, tasks.sequence ASC LIMIT ?
                """,
                (
                    normalized_repository,
                    normalized_repository,
                    current_text,
                    current_text,
                    limit,
                ),
            ).fetchall()
            for row in rows:
                generation = int(row["lease_generation"]) + 1
                event = "taken_over" if row["state"] == "running" else "claimed"
                cursor = connection.execute(
                    """
                    UPDATE queue_tasks
                    SET state='running', attempt_count=attempt_count+1,
                        lease_owner=?, lease_generation=?, lease_expires_at=?,
                        last_error_code='', updated_at=?
                    WHERE id=? AND state=? AND lease_generation=?
                    """,
                    (
                        normalized_worker,
                        generation,
                        expires_at,
                        current_text,
                        row["id"],
                        row["state"],
                        row["lease_generation"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise StoreError("queue task changed during atomic claim")
                self._append_queue_attempt(
                    connection,
                    task_id=str(row["id"]),
                    generation=generation,
                    worker=normalized_worker,
                    event=event,
                    outcome="running",
                    payload={},
                    created_at=current_text,
                )
                updated = connection.execute(
                    "SELECT * FROM queue_tasks WHERE id=?", (row["id"],)
                ).fetchone()
                if updated is None:
                    raise StoreError("claimed queue task disappeared")
                claimed.append(
                    {
                        **self._queue_task(updated),
                        "lease": QueueLease(
                            task_id=str(row["id"]),
                            worker=normalized_worker,
                            generation=generation,
                            expires_at=expires_at,
                        ),
                    }
                )
        return claimed

    def renew_queue_lease(
        self,
        lease: QueueLease,
        *,
        lease_seconds: int = 900,
        now: datetime | None = None,
    ) -> QueueLease:
        if not 5 <= lease_seconds <= 86_400:
            raise ValueError("queue lease_seconds must be between 5 and 86400")
        current, current_text = self._queue_timestamp(now)
        expires_at = (current + timedelta(seconds=lease_seconds)).isoformat(
            timespec="microseconds"
        )
        with self._connection() as connection:
            self._begin_immediate(connection)
            cursor = connection.execute(
                """
                UPDATE queue_tasks SET lease_expires_at=?, updated_at=?
                WHERE id=? AND state='running' AND lease_owner=?
                  AND lease_generation=? AND lease_expires_at>?
                """,
                (
                    expires_at,
                    current_text,
                    lease.task_id,
                    lease.worker,
                    lease.generation,
                    current_text,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("queue lease is stale")
        return QueueLease(lease.task_id, lease.worker, lease.generation, expires_at)

    def _finish_queue_task(
        self,
        lease: QueueLease,
        *,
        state: str,
        event: str,
        payload: dict[str, Any],
        manual_required: bool | None,
        error_code: str,
        now: datetime | None,
    ) -> dict[str, Any]:
        if state not in {"completed", "failed"}:
            raise ValueError("queue finish state must be completed or failed")
        if error_code and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
            raise ValueError("queue error_code has an invalid format")
        current, current_text = self._queue_timestamp(now)
        with self._connection() as connection:
            self._begin_immediate(connection)
            row = connection.execute(
                "SELECT * FROM queue_tasks WHERE id=?", (lease.task_id,)
            ).fetchone()
            if (
                row is None
                or str(row["state"]) != "running"
                or str(row["lease_owner"]) != lease.worker
                or int(row["lease_generation"]) != lease.generation
                or str(row["lease_expires_at"]) <= current_text
            ):
                raise StoreError("queue lease is stale")
            if manual_required is None:
                manual_required = not bool(payload.get("retryable")) or int(
                    row["attempt_count"]
                ) >= int(row["max_attempts"])
                payload = {**payload, "manual_required": manual_required}
            self._queue_payload(payload, allowed_keys=QUEUE_RESULT_KEYS)
            available_at = current_text
            if state == "failed" and not manual_required:
                delay_seconds = min(
                    300, 5 * (2 ** max(int(row["attempt_count"]) - 1, 0))
                )
                available_at = (current + timedelta(seconds=delay_seconds)).isoformat(
                    timespec="microseconds"
                )
            connection.execute(
                """
                UPDATE queue_tasks
                SET state=?, manual_required=?, last_error_code=?,
                    lease_owner='', lease_expires_at='', available_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    state,
                    int(manual_required),
                    error_code,
                    available_at,
                    current_text,
                    lease.task_id,
                ),
            )
            audit = self._append_queue_attempt(
                connection,
                task_id=lease.task_id,
                generation=lease.generation,
                worker=lease.worker,
                event=event,
                outcome=state,
                payload=payload,
                created_at=current_text,
            )
            updated = connection.execute(
                "SELECT * FROM queue_tasks WHERE id=?", (lease.task_id,)
            ).fetchone()
        if updated is None:
            raise StoreError("finished queue task disappeared")
        return {**self._queue_task(updated), "audit": audit}

    def complete_queue_task(
        self,
        lease: QueueLease,
        *,
        result: dict[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._finish_queue_task(
            lease,
            state="completed",
            event="completed",
            payload=result,
            manual_required=False,
            error_code="",
            now=now,
        )

    def fail_queue_task(
        self,
        lease: QueueLease,
        *,
        error_code: str,
        retryable: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._finish_queue_task(
            lease,
            state="failed",
            event="failed",
            payload={
                "error_code": error_code,
                "retryable": retryable,
            },
            manual_required=None,
            error_code=error_code,
            now=now,
        )

    def cancel_queue_task(
        self,
        task_id: str,
        *,
        cancelled_by: str,
        reason_code: str = "operator_cancelled",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        worker = self._queue_worker(cancelled_by)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason_code):
            raise ValueError("queue cancellation reason has an invalid format")
        _current, current_text = self._queue_timestamp(now)
        with self._connection() as connection:
            self._begin_immediate(connection)
            row = connection.execute(
                "SELECT * FROM queue_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"queue task not found: {task_id}")
            if str(row["state"]) == "cancelled":
                return {**self._queue_task(row), "idempotent": True}
            if str(row["state"]) == "completed":
                raise StoreError("completed queue task cannot be cancelled")
            if (
                str(row["state"]) == "running"
                and str(row["lease_expires_at"]) > current_text
            ):
                raise StoreError("active queue task cannot be cancelled")
            connection.execute(
                """
                UPDATE queue_tasks
                SET state='cancelled', manual_required=0,
                    last_error_code=?, lease_owner='', lease_expires_at='',
                    updated_at=? WHERE id=?
                """,
                (reason_code, current_text, task_id),
            )
            audit = self._append_queue_attempt(
                connection,
                task_id=task_id,
                generation=int(row["lease_generation"]),
                worker=worker,
                event="cancelled",
                outcome="cancelled",
                payload={"error_code": reason_code},
                created_at=current_text,
            )
            updated = connection.execute(
                "SELECT * FROM queue_tasks WHERE id=?", (task_id,)
            ).fetchone()
        if updated is None:
            raise StoreError("cancelled queue task disappeared")
        return {**self._queue_task(updated), "audit": audit, "idempotent": False}

    def requeue_queue_task(
        self,
        task_id: str,
        *,
        requeued_by: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        worker = self._queue_worker(requeued_by)
        _current, current_text = self._queue_timestamp(now)
        with self._connection() as connection:
            self._begin_immediate(connection)
            row = connection.execute(
                "SELECT * FROM queue_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"queue task not found: {task_id}")
            if str(row["state"]) == "pending" and not bool(row["manual_required"]):
                return {**self._queue_task(row), "idempotent": True}
            if str(row["state"]) not in {"failed", "cancelled"}:
                raise StoreError("only failed or cancelled queue tasks can be requeued")
            max_attempts = max(int(row["max_attempts"]), int(row["attempt_count"]) + 1)
            connection.execute(
                """
                UPDATE queue_tasks
                SET state='pending', manual_required=0, last_error_code='',
                    max_attempts=?, available_at=?, lease_owner='',
                    lease_expires_at='', updated_at=? WHERE id=?
                """,
                (max_attempts, current_text, current_text, task_id),
            )
            audit = self._append_queue_attempt(
                connection,
                task_id=task_id,
                generation=int(row["lease_generation"]),
                worker=worker,
                event="requeued",
                outcome="pending",
                payload={},
                created_at=current_text,
            )
            updated = connection.execute(
                "SELECT * FROM queue_tasks WHERE id=?", (task_id,)
            ).fetchone()
        if updated is None:
            raise StoreError("requeued task disappeared")
        return {**self._queue_task(updated), "audit": audit, "idempotent": False}

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
        lease: RunLease | None = None,
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
            effective_lease = lease
            if effective_lease is not None:
                if self._run_lease_context.get() is None:
                    self._begin_immediate(connection)
                self._assert_run_lease_row(connection, effective_lease)
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

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["details"] = json.loads(result["details"])
        return result

    def latest_runs_for_repository(
        self, repository: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Return one latest run per Issue without N+1 queries."""
        limit = min(max(limit, 1), 1_000)
        with self._connection() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT r.*, r.rowid AS run_rowid,
                           ROW_NUMBER() OVER (
                               PARTITION BY repository, issue_number
                               ORDER BY created_at DESC, r.rowid DESC
                           ) AS run_rank
                    FROM runs r WHERE repository=?
                )
                SELECT ranked.*, submissions.pr_url AS submission_pr_url
                FROM ranked
                LEFT JOIN submissions
                  ON submissions.repository=ranked.repository
                 AND submissions.issue_number=ranked.issue_number
                WHERE run_rank=1
                ORDER BY updated_at, id LIMIT ?
                """,
                (repository.casefold(), limit),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value.pop("run_rank", None)
            value.pop("run_rowid", None)
            value["details"] = json.loads(str(value["details"]))
            result.append(value)
        return result

    def append_owner_review_attestation(
        self, *, facts: dict[str, Any]
    ) -> dict[str, Any]:
        """Append one immutable owner review bound to exact merge facts."""
        if set(facts) != OWNER_REVIEW_FACT_KEYS:
            raise StoreError("owner review facts have an unexpected shape")
        normalized = dict(facts)
        if isinstance(facts["pull_number"], bool) or not isinstance(
            facts["pull_number"], int
        ):
            raise TypeError("owner review pull_number must be an integer")
        string_keys = OWNER_REVIEW_FACT_KEYS - {"pull_number"}
        if any(not isinstance(facts[key], str) for key in string_keys):
            raise TypeError("owner review fact values must be strings")
        normalized["repository"] = str(facts["repository"]).casefold()
        normalized["actor"] = str(facts["actor"]).strip()
        if int(normalized["pull_number"]) < 1:
            raise ValueError("owner review pull_number must be positive")
        if not normalized["run_id"] or not normalized["actor"]:
            raise ValueError("owner review run and actor must not be empty")
        for key in ("head_sha", "base_sha"):
            value = str(normalized[key])
            if not _is_lower_hex(value, 40):
                raise ValueError(f"owner review {key} must be 40 lowercase hex chars")
        for key in (
            "policy_digest",
            "diff_digest",
            "checks_digest",
            "conversation_digest",
            "dependency_digest",
            "activity_digest",
            "rules_digest",
        ):
            value = str(normalized[key])
            if not _is_lower_hex(value, 64):
                raise ValueError(f"owner review {key} must be 64 lowercase hex chars")
        review_facts_digest = _json_digest(normalized)
        material = {
            "schema_version": 1,
            "source": "owner_attestation",
            "facts": normalized,
            "review_facts_digest": review_facts_digest,
        }
        attestation_digest = _json_digest(material)
        now = utc_now()
        attestation_id = uuid.uuid4().hex
        with self._connection() as connection:
            self._begin_immediate(connection)
            run = connection.execute(
                "SELECT repository FROM runs WHERE id=?", (normalized["run_id"],)
            ).fetchone()
            if run is None or str(run["repository"]) != normalized["repository"]:
                raise StoreError("owner review does not match its tracked run")
            previous = connection.execute(
                """
                SELECT * FROM owner_review_attestations
                WHERE review_facts_digest=?
                """,
                (review_facts_digest,),
            ).fetchone()
            if previous is not None:
                value = self._owner_review_attestation(previous)
                return {**value, "idempotent": True}
            connection.execute(
                """
                INSERT INTO owner_review_attestations(
                    id, repository, pull_number, run_id, actor, head_sha,
                    base_sha, policy_digest, review_facts_digest,
                    attestation_digest, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attestation_id,
                    normalized["repository"],
                    int(normalized["pull_number"]),
                    normalized["run_id"],
                    normalized["actor"],
                    normalized["head_sha"],
                    normalized["base_sha"],
                    normalized["policy_digest"],
                    review_facts_digest,
                    attestation_digest,
                    _canonical_json(material),
                    now,
                ),
            )
        return {
            "id": attestation_id,
            **material,
            "attestation_digest": attestation_digest,
            "created_at": now,
            "idempotent": False,
        }

    @staticmethod
    def _owner_review_attestation(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        payload = json.loads(str(value["payload"]))
        if not isinstance(payload, dict):
            raise StoreError("owner review attestation was modified")
        facts = payload.get("facts")
        if not isinstance(facts, dict) or set(facts) != OWNER_REVIEW_FACT_KEYS:
            raise StoreError("owner review attestation was modified")
        review_facts_digest = _json_digest(facts)
        material = {
            "schema_version": 1,
            "source": "owner_attestation",
            "facts": facts,
            "review_facts_digest": review_facts_digest,
        }
        scope = {
            "repository": str(value["repository"]),
            "pull_number": int(value["pull_number"]),
            "run_id": str(value["run_id"]),
            "actor": str(value["actor"]),
            "head_sha": str(value["head_sha"]),
            "base_sha": str(value["base_sha"]),
            "policy_digest": str(value["policy_digest"]),
        }
        if (
            payload != material
            or any(facts.get(key) != expected for key, expected in scope.items())
            or str(value["review_facts_digest"]) != review_facts_digest
            or str(value["attestation_digest"]) != _json_digest(material)
        ):
            raise StoreError("owner review attestation was modified")
        value["payload"] = payload
        value["facts"] = facts
        return value

    def latest_owner_review_attestation(
        self, repository: str, pull_number: int
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM owner_review_attestations
                WHERE repository=? AND pull_number=?
                ORDER BY sequence DESC LIMIT 1
                """,
                (repository.casefold(), pull_number),
            ).fetchone()
        return self._owner_review_attestation(row) if row is not None else None

    def owner_review_attestations(
        self, repository: str, pull_number: int, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM owner_review_attestations
                WHERE repository=? AND pull_number=?
                ORDER BY sequence DESC LIMIT ?
                """,
                (repository.casefold(), pull_number, limit),
            ).fetchall()
        return [self._owner_review_attestation(row) for row in rows]

    def append_merge_decision(
        self,
        *,
        repository: str,
        pull_number: int,
        head_sha: str,
        base_sha: str,
        policy_digest: str,
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        """Append one immutable merge evaluation, including repeated evaluations."""
        if pull_number < 1:
            raise ValueError("pull_number must be positive")
        decision_id = uuid.uuid4().hex
        now = utc_now()
        payload = _canonical_json(decision)
        snapshot_digest = str(decision.get("snapshot_digest", ""))
        decision_digest = str(decision.get("decision_digest", ""))
        if not snapshot_digest or not decision_digest:
            raise StoreError("merge decision is missing its audit digests")
        snapshot = decision.get("snapshot")
        if not isinstance(snapshot, dict) or _json_digest(snapshot) != snapshot_digest:
            raise StoreError("merge decision snapshot does not match its digest")
        expected_scope = {
            "repository": repository.casefold(),
            "pull_number": pull_number,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "policy_digest": policy_digest,
        }
        if any(snapshot.get(key) != value for key, value in expected_scope.items()):
            raise StoreError("merge decision snapshot does not match its audit scope")
        decision_material = {
            key: decision.get(key)
            for key in (
                "eligible",
                "reasons",
                "risk_categories",
                "risk_files",
                "snapshot_digest",
            )
        }
        if _json_digest(decision_material) != decision_digest:
            raise StoreError("merge decision does not match its digest")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO merge_decisions(
                    id, repository, pull_number, head_sha, base_sha,
                    policy_digest, snapshot_digest, eligible,
                    decision_digest, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    repository.casefold(),
                    pull_number,
                    head_sha,
                    base_sha,
                    policy_digest,
                    snapshot_digest,
                    int(bool(decision.get("eligible"))),
                    decision_digest,
                    payload,
                    now,
                ),
            )
        return {
            "id": decision_id,
            "repository": repository.casefold(),
            "pull_number": pull_number,
            "decision_digest": decision_digest,
            "created_at": now,
        }

    def append_portfolio_dependency_event(
        self,
        *,
        repository: str,
        pull_number: int,
        dependency_number: int,
        head_sha: str,
        action: str,
        actor: str,
    ) -> dict[str, Any]:
        """Append one head-bound maintainer dependency attestation."""
        normalized_repository = repository.casefold()
        normalized_action = action.casefold()
        normalized_actor = actor.strip()
        if pull_number < 1 or dependency_number < 1:
            raise ValueError("dependency pull numbers must be positive")
        if pull_number == dependency_number:
            raise ValueError("a pull request cannot depend on itself")
        if normalized_action not in {"confirm", "revoke"}:
            raise ValueError("dependency action must be confirm or revoke")
        if not normalized_actor:
            raise ValueError("dependency actor must not be empty")
        if len(head_sha) != 40 or any(
            value not in "0123456789abcdef" for value in head_sha
        ):
            raise ValueError("dependency head_sha must be 40 lowercase hex chars")
        with self._connection() as connection:
            self._begin_immediate(connection)
            previous = connection.execute(
                """
                SELECT id, head_sha, action, actor, source,
                       event_digest, payload, created_at
                FROM portfolio_dependency_events
                WHERE repository=? AND pull_number=? AND dependency_number=?
                ORDER BY sequence DESC LIMIT 1
                """,
                (normalized_repository, pull_number, dependency_number),
            ).fetchone()
            if previous is not None and (
                str(previous["head_sha"]) == head_sha
                and str(previous["action"]) == normalized_action
                and str(previous["actor"]) == normalized_actor
                and str(previous["source"]) == "maintainer_attestation"
            ):
                previous_payload = json.loads(str(previous["payload"]))
                expected_previous = {
                    "schema_version": 1,
                    "repository": normalized_repository,
                    "pull_number": pull_number,
                    "dependency_number": dependency_number,
                    "head_sha": head_sha,
                    "action": normalized_action,
                    "actor": normalized_actor,
                    "source": "maintainer_attestation",
                    "previous_event_id": (
                        str(previous_payload.get("previous_event_id") or "")
                        if isinstance(previous_payload, dict)
                        else ""
                    ),
                }
                if previous_payload != expected_previous or _json_digest(
                    expected_previous
                ) != str(previous["event_digest"]):
                    raise StoreError("portfolio dependency audit was modified")
                return {
                    "id": str(previous["id"]),
                    **previous_payload,
                    "event_digest": str(previous["event_digest"]),
                    "created_at": str(previous["created_at"]),
                    "idempotent": True,
                }
            material = {
                "schema_version": 1,
                "repository": normalized_repository,
                "pull_number": pull_number,
                "dependency_number": dependency_number,
                "head_sha": head_sha,
                "action": normalized_action,
                "actor": normalized_actor,
                "source": "maintainer_attestation",
                "previous_event_id": str(previous["id"]) if previous else "",
            }
            event_digest = _json_digest(material)
            payload = _canonical_json(material)
            event_id = uuid.uuid4().hex
            now = utc_now()
            connection.execute(
                """
                INSERT OR IGNORE INTO portfolio_dependency_events(
                    id, repository, pull_number, dependency_number, head_sha,
                    action, actor, source, event_digest, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    normalized_repository,
                    pull_number,
                    dependency_number,
                    head_sha,
                    normalized_action,
                    normalized_actor,
                    "maintainer_attestation",
                    event_digest,
                    payload,
                    now,
                ),
            )
            stored = connection.execute(
                """
                SELECT id, created_at FROM portfolio_dependency_events
                WHERE event_digest=?
                """,
                (event_digest,),
            ).fetchone()
            assert stored is not None
            return {
                "id": str(stored["id"]),
                **material,
                "event_digest": event_digest,
                "created_at": str(stored["created_at"]),
                "idempotent": str(stored["id"]) != event_id,
            }

    def latest_portfolio_dependency_events(
        self, repository: str, *, pull_number: int = 0
    ) -> list[dict[str, Any]]:
        """Return the latest append-only action for every dependency pair."""
        normalized_repository = repository.casefold()
        with self._connection() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY repository, pull_number, dependency_number
                        ORDER BY sequence DESC
                    ) AS dependency_rank
                    FROM portfolio_dependency_events
                    WHERE repository=? AND (?=0 OR pull_number=?)
                )
                SELECT * FROM ranked
                WHERE dependency_rank=1
                ORDER BY pull_number, dependency_number
                """,
                (normalized_repository, pull_number, pull_number),
            ).fetchall()
        return [self._portfolio_dependency_event(row) for row in rows]

    @staticmethod
    def _portfolio_dependency_event(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value.pop("dependency_rank", None)
        payload = json.loads(str(value["payload"]))
        if not isinstance(payload, dict):
            raise StoreError("portfolio dependency audit was modified")
        material = {
            "schema_version": 1,
            "repository": str(value["repository"]),
            "pull_number": int(value["pull_number"]),
            "dependency_number": int(value["dependency_number"]),
            "head_sha": str(value["head_sha"]),
            "action": str(value["action"]),
            "actor": str(value["actor"]),
            "source": str(value["source"]),
            "previous_event_id": str(payload.get("previous_event_id") or ""),
        }
        if payload != material or _json_digest(material) != str(value["event_digest"]):
            raise StoreError("portfolio dependency audit was modified")
        value["payload"] = payload
        return value

    def portfolio_dependency_events(
        self, repository: str, *, pull_number: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Read recent dependency audit events without collapsing their history."""
        limit = min(max(limit, 1), 500)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM portfolio_dependency_events
                WHERE repository=? AND (?=0 OR pull_number=?)
                ORDER BY sequence DESC LIMIT ?
                """,
                (repository.casefold(), pull_number, pull_number, limit),
            ).fetchall()
        return [self._portfolio_dependency_event(row) for row in rows]

    def merge_decisions(
        self, repository: str, pull_number: int, *, limit: int = 30
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 100)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM merge_decisions
                WHERE repository=? AND pull_number=?
                ORDER BY created_at DESC LIMIT ?
                """,
                (repository.casefold(), pull_number, limit),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["eligible"] = bool(value["eligible"])
            value["payload"] = json.loads(value["payload"])
            result.append(value)
        return result

    def merge_decision(self, decision_id: str) -> dict[str, Any] | None:
        """Read one merge decision and reject any materialized audit mismatch."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM merge_decisions WHERE id=?", (decision_id,)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        payload = json.loads(str(value["payload"]))
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict):
            raise StoreError("merge decision audit has no snapshot")
        if _json_digest(snapshot) != str(value["snapshot_digest"]):
            raise StoreError("merge decision audit snapshot was modified")
        scope = {
            "repository": str(value["repository"]),
            "pull_number": int(value["pull_number"]),
            "head_sha": str(value["head_sha"]),
            "base_sha": str(value["base_sha"]),
            "policy_digest": str(value["policy_digest"]),
        }
        if any(snapshot.get(key) != expected for key, expected in scope.items()):
            raise StoreError("merge decision audit scope was modified")
        material = {
            key: payload.get(key)
            for key in (
                "eligible",
                "reasons",
                "risk_categories",
                "risk_files",
                "snapshot_digest",
            )
        }
        if _json_digest(material) != str(value["decision_digest"]):
            raise StoreError("merge decision audit result was modified")
        if bool(value["eligible"]) != bool(payload.get("eligible")):
            raise StoreError("merge decision audit eligibility was modified")
        value["eligible"] = bool(value["eligible"])
        value["payload"] = payload
        return value

    def append_publication_attempt(
        self,
        *,
        attempt_id: str,
        step_id: str,
        run_id: str,
        repository: str,
        issue_number: int,
        actor: str,
        action: str,
        stage: str,
        outcome: str,
        destination: str,
        branch: str,
        head_sha: str,
        base_branch: str,
        expected_remote_sha: str = "",
        target_pull_number: int = 0,
        lease: RunLease,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Append one bounded publication intent or reconciliation result."""
        if not attempt_id or not step_id or not run_id:
            raise ValueError("publication attempt identities must not be empty")
        repository = repository.casefold()
        destination = destination.casefold()
        actor = actor.strip()
        if (
            not repository
            or not destination
            or not actor
            or not branch
            or not base_branch
        ):
            raise ValueError("publication attempt fields must not be empty")
        if issue_number < 1 or target_pull_number < 0:
            raise ValueError("publication issue and pull numbers must be valid")
        expected_lease_scope = f"issue:{repository}#{issue_number}"
        if lease.scope != expected_lease_scope:
            raise StoreError(
                "publication lease does not match its repository and issue"
            )
        if action not in PUBLICATION_ACTIONS:
            raise ValueError(f"unsupported publication action: {action!r}")
        if stage not in {"applying", "completed"}:
            raise ValueError(f"unsupported publication stage: {stage!r}")
        allowed_outcomes = (
            {"pending"} if stage == "applying" else PUBLICATION_COMPLETED_OUTCOMES
        )
        if outcome not in allowed_outcomes:
            raise ValueError("publication stage and outcome do not match")
        if not _is_lower_hex(head_sha, 40):
            raise ValueError("publication head_sha must be a lowercase Git SHA")
        if expected_remote_sha and not _is_lower_hex(expected_remote_sha, 40):
            raise ValueError(
                "publication expected_remote_sha must be empty or a lowercase Git SHA"
            )
        if len(branch) > 255 or len(base_branch) > 255:
            raise ValueError("publication branch names are too long")
        if not isinstance(payload, dict):
            raise TypeError("publication payload must be an object")
        unknown_payload = set(payload) - PUBLICATION_PAYLOAD_KEYS
        if unknown_payload:
            raise ValueError(
                "publication payload contains unsupported fields: "
                + ", ".join(sorted(unknown_payload))
            )
        if any(not isinstance(value, (str, int, bool)) for value in payload.values()):
            raise TypeError("publication payload values must be bounded scalars")
        encoded_payload = _canonical_json(payload)
        if len(encoded_payload.encode()) > 4_096:
            raise ValueError("publication payload exceeds 4096 bytes")
        record_id = uuid.uuid4().hex
        now = utc_now()
        with self._connection() as connection:
            self._begin_immediate(connection)
            self._assert_run_lease_row(connection, lease)
            run = connection.execute(
                "SELECT repository, issue_number FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise StoreError("publication attempt references a missing run")
            if (
                str(run["repository"]) != repository
                or int(run["issue_number"]) != issue_number
            ):
                raise StoreError("publication attempt does not match its run")
            previous = connection.execute(
                """
                SELECT attempt_id, run_id, repository, issue_number, actor, action,
                       destination, branch, head_sha, base_branch,
                       expected_remote_sha, target_pull_number
                FROM publication_attempts WHERE step_id=? LIMIT 1
                """,
                (step_id,),
            ).fetchone()
            identity = {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "repository": repository,
                "issue_number": issue_number,
                "actor": actor,
                "action": action,
                "destination": destination,
                "branch": branch,
                "head_sha": head_sha,
                "base_branch": base_branch,
                "expected_remote_sha": expected_remote_sha,
                "target_pull_number": target_pull_number,
            }
            if previous is not None and any(
                previous[key] != value for key, value in identity.items()
            ):
                raise StoreError("publication step identity changed")
            try:
                connection.execute(
                    """
                    INSERT INTO publication_attempts(
                        id, attempt_id, step_id, run_id, repository, issue_number,
                        actor, action, stage, outcome, destination, branch, head_sha,
                        base_branch, expected_remote_sha, target_pull_number,
                        lease_owner, lease_generation, payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        attempt_id,
                        step_id,
                        run_id,
                        repository,
                        issue_number,
                        actor,
                        action,
                        stage,
                        outcome,
                        destination,
                        branch,
                        head_sha,
                        base_branch,
                        expected_remote_sha,
                        target_pull_number,
                        lease.owner,
                        lease.generation,
                        encoded_payload,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(
                    "publication step already contains this stage"
                ) from exc
        return {
            "id": record_id,
            "attempt_id": attempt_id,
            "step_id": step_id,
            "action": action,
            "stage": stage,
            "outcome": outcome,
            "lease_owner": lease.owner,
            "lease_generation": lease.generation,
            "created_at": now,
        }

    @staticmethod
    def _publication_attempt(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["payload"] = json.loads(str(value["payload"]))
        return value

    def publication_attempts(
        self, run_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM publication_attempts
                WHERE run_id=? ORDER BY sequence ASC LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [self._publication_attempt(row) for row in rows]

    def incomplete_publication_attempts(
        self, run_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT applying.*
                FROM publication_attempts applying
                WHERE applying.run_id=? AND applying.stage='applying'
                  AND NOT EXISTS (
                      SELECT 1 FROM publication_attempts completed
                      WHERE completed.step_id=applying.step_id
                        AND completed.stage='completed'
                  )
                ORDER BY applying.sequence ASC LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [self._publication_attempt(row) for row in rows]

    def publication_action_completed(
        self,
        run_id: str,
        *,
        action: str,
        target_pull_number: int = 0,
    ) -> bool:
        if action not in PUBLICATION_ACTIONS:
            raise ValueError(f"unsupported publication action: {action!r}")
        if target_pull_number < 0:
            raise ValueError("publication pull number must not be negative")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM publication_attempts
                WHERE run_id=? AND action=? AND stage='completed'
                  AND outcome IN ('succeeded', 'reconciled', 'already_current')
                  AND (?=0 OR target_pull_number=?)
                LIMIT 1
                """,
                (run_id, action, target_pull_number, target_pull_number),
            ).fetchone()
        return row is not None

    def append_merge_execution(
        self,
        *,
        attempt_id: str,
        run_id: str,
        decision_id: str,
        repository: str,
        pull_number: int,
        actor: str,
        merge_method: str,
        stage: str,
        outcome: str,
        reason: str,
        decision_digest: str,
        head_sha: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Append one intent or result record for a merge execution attempt."""
        if not attempt_id or not run_id or not decision_id:
            raise ValueError("merge execution identities must not be empty")
        repository = repository.casefold()
        actor = actor.strip()
        if not repository or not actor or not decision_digest or not head_sha:
            raise ValueError("merge execution audit fields must not be empty")
        if pull_number < 1:
            raise ValueError("pull_number must be positive")
        if stage not in {"applying", "completed"}:
            raise ValueError(f"unsupported merge execution stage: {stage!r}")
        if outcome not in {"pending", "merged", "already_merged", "blocked", "failed"}:
            raise ValueError(f"unsupported merge execution outcome: {outcome!r}")
        if (stage == "applying") != (outcome == "pending"):
            raise ValueError("merge execution stage and outcome do not match")
        if merge_method not in {"merge", "squash", "rebase"}:
            raise ValueError(f"unsupported merge method: {merge_method!r}")
        if not isinstance(payload, dict):
            raise TypeError("merge execution payload must be an object")
        record_id = uuid.uuid4().hex
        now = utc_now()
        with self._connection() as connection:
            run = connection.execute(
                "SELECT repository FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            decision = connection.execute(
                """
                SELECT repository, pull_number, decision_digest, head_sha
                FROM merge_decisions WHERE id=?
                """,
                (decision_id,),
            ).fetchone()
            if run is None or decision is None:
                raise StoreError("merge execution references missing audit material")
            expected = {
                "repository": repository,
                "pull_number": pull_number,
                "decision_digest": decision_digest,
                "head_sha": head_sha,
            }
            if str(run["repository"]) != repository or any(
                decision[key] != value for key, value in expected.items()
            ):
                raise StoreError("merge execution does not match its run and decision")
            previous = connection.execute(
                """
                SELECT run_id, decision_id, repository, pull_number, actor,
                       merge_method, decision_digest, head_sha
                FROM merge_executions WHERE attempt_id=? LIMIT 1
                """,
                (attempt_id,),
            ).fetchone()
            identity = {
                "run_id": run_id,
                "decision_id": decision_id,
                "repository": repository,
                "pull_number": pull_number,
                "actor": actor,
                "merge_method": merge_method,
                "decision_digest": decision_digest,
                "head_sha": head_sha,
            }
            if previous is not None and any(
                previous[key] != value for key, value in identity.items()
            ):
                raise StoreError("merge execution attempt identity changed")
            try:
                connection.execute(
                    """
                    INSERT INTO merge_executions(
                        id, attempt_id, run_id, decision_id, repository,
                        pull_number, actor, merge_method, stage, outcome,
                        reason, decision_digest, head_sha, payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        attempt_id,
                        run_id,
                        decision_id,
                        repository,
                        pull_number,
                        actor,
                        merge_method,
                        stage,
                        outcome,
                        str(reason)[:2_000],
                        decision_digest,
                        head_sha,
                        _canonical_json(payload),
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(
                    "merge execution attempt already contains this stage"
                ) from exc
        return {
            "id": record_id,
            "attempt_id": attempt_id,
            "stage": stage,
            "outcome": outcome,
            "created_at": now,
        }

    def merge_executions(
        self, repository: str, pull_number: int, *, limit: int = 30
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 100)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM merge_executions
                WHERE repository=? AND pull_number=?
                ORDER BY created_at DESC LIMIT ?
                """,
                (repository.casefold(), pull_number, limit),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(str(value["payload"]))
            result.append(value)
        return result

    def latest_merge_outcomes(self, repository: str) -> dict[int, str]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT pull_number, outcome,
                           ROW_NUMBER() OVER (
                               PARTITION BY repository, pull_number
                               ORDER BY created_at DESC, rowid DESC
                           ) AS outcome_rank
                    FROM merge_executions
                    WHERE repository=? AND stage='completed'
                )
                SELECT pull_number, outcome FROM ranked WHERE outcome_rank=1
                """,
                (repository.casefold(),),
            ).fetchall()
        return {int(row["pull_number"]): str(row["outcome"]) for row in rows}

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
