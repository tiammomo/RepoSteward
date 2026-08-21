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
from .protocol import validate_checkpoint, validate_context_pack

SCHEMA_VERSION = 3

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
}


class StoreError(RuntimeError):
    """The local state database cannot be opened or migrated safely."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


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

    def save_checkpoint(
        self,
        *,
        work_item_id: str,
        run_id: str,
        context_pack_id: str,
        status: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
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
        checkpoint_id = uuid.uuid4().hex
        now = utc_now()
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
