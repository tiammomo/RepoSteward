from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Candidate
from .protocol import validate_checkpoint, validate_context_pack

SCHEMA_VERSION = 9

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
}


class StoreError(RuntimeError):
    """The local state database cannot be opened or migrated safely."""


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


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
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
                connection.execute("BEGIN IMMEDIATE")
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
            connection.execute("BEGIN IMMEDIATE")
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
            connection.execute("BEGIN IMMEDIATE")
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
            connection.execute("BEGIN IMMEDIATE")
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
            connection.execute("BEGIN IMMEDIATE")
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
            connection.execute("BEGIN IMMEDIATE")
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
                SELECT r.id, r.repository,
                       EXISTS(
                           SELECT 1 FROM checkpoints c
                           WHERE c.run_id=r.id
                             AND c.status IN ('ready', 'submitted', 'failed')
                       ) AS terminal_checkpoint
                FROM runs r
                """
            ).fetchall()
        return {
            str(row["id"]): {
                "repository": str(row["repository"]),
                "terminal_checkpoint": bool(row["terminal_checkpoint"]),
            }
            for row in rows
        }

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
            connection.execute("BEGIN IMMEDIATE")
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
            connection.execute("BEGIN IMMEDIATE")
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
