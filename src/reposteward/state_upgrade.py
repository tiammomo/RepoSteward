"""Explicit, content-bound local upgrades with independently verifiable backups."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .config import AppConfig
from .runtime import inspect_database, installation_info
from .store import MIGRATIONS, SCHEMA_VERSION, apply_migration

FORMAT_VERSION = 1
MAX_DATABASE_BYTES = 2_000_000_000
REQUIRED_TABLES = ("runs", "candidates", "issue_drafts")


class StateUpgradeError(ValueError):
    """The reviewed state upgrade cannot be safely completed."""


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _fingerprint(path: Path) -> dict:
    before = path.stat()
    if not path.is_file() or path.is_symlink() or before.st_size > MAX_DATABASE_BYTES:
        raise StateUpgradeError("database must be a regular file of at most 2 GB")
    digest = hashlib.sha256()
    deadline = time.monotonic() + 30
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            if time.monotonic() > deadline:
                raise StateUpgradeError("database fingerprint exceeded its time limit")
    after = path.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, key) != getattr(after, key) for key in fields):
        raise StateUpgradeError("database changed while planning")
    return {
        **{key: getattr(after, key) for key in fields},
        "sha256": digest.hexdigest(),
    }


def _scope(config: AppConfig, expected_state_dir: Path | None) -> Path:
    api = urlsplit(config.github.api_url)
    if api.username or api.password or api.query or api.fragment:
        raise StateUpgradeError("correct the API URL using doctor --local first")
    root = config.state_dir.expanduser().resolve()
    if (
        expected_state_dir is not None
        and root != expected_state_dir.expanduser().resolve()
    ):
        raise StateUpgradeError(
            "effective state directory differs from the expected directory"
        )
    return root


def _active_leases(db: sqlite3.Connection) -> int:
    tables = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    active = 0
    now = datetime.now(UTC)
    for table, column, where in (
        ("run_leases", "expires_at", ""),
        ("queue_tasks", "lease_expires_at", " WHERE state='running'"),
    ):
        if table not in tables:
            continue
        rows = db.execute(f"SELECT {column} FROM {table}{where} LIMIT 1001").fetchall()
        if len(rows) > 1000:
            raise StateUpgradeError("lease inspection exceeds its limit")
        for row in rows:
            try:
                expires = datetime.fromisoformat(str(row[0]))
                if expires.tzinfo is None:
                    raise ValueError("missing timezone")
            except ValueError as exc:
                raise StateUpgradeError(
                    "unrecognized lease expiry; inspect running work"
                ) from exc
            active += int(expires > now)
    return active


def upgrade_plan(config: AppConfig, *, expected_state_dir: Path | None = None) -> dict:
    root = _scope(config, expected_state_dir)
    path = root / "reposteward.sqlite3"
    database = inspect_database(
        path, supported=SCHEMA_VERSION, required_tables=REQUIRED_TABLES
    )
    result = {
        "format_version": FORMAT_VERSION,
        "state_dir": str(root),
        "database": database,
        "target_schema": SCHEMA_VERSION,
        "eligible": False,
        "active_leases": 0,
        "migrations": [],
        "public_write": False,
    }
    if database["status"] in {"compatible", "migration_required"}:
        try:
            fingerprint = _fingerprint(path)
            with closing(
                sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
            ) as db:
                db.execute("PRAGMA query_only=ON")
                result["active_leases"] = _active_leases(db)
            if (
                _fingerprint(path) != fingerprint
                or inspect_database(
                    path, supported=SCHEMA_VERSION, required_tables=REQUIRED_TABLES
                )
                != database
            ):
                raise StateUpgradeError("database changed while planning")
            result["fingerprint"] = fingerprint
            current = database["schema"]
            result["migrations"] = list(range(current + 1, SCHEMA_VERSION + 1))
            statements = {
                version: MIGRATIONS[version] for version in result["migrations"]
            }
            result["migration_digest"] = _digest(statements)
            result["eligible"] = (
                database["status"] == "migration_required"
                and not result["active_leases"]
            )
        except (OSError, sqlite3.Error, StateUpgradeError, KeyError):
            result["database"] = {**database, "status": "snapshot_required"}
            result["eligible"] = False
    result["plan_digest"] = _digest(result)
    return result


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_manifest(directory: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(directory / "manifest.json")
        _sync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _integrity(db: sqlite3.Connection) -> None:
    deadline = time.monotonic() + 60
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        if db.execute("PRAGMA integrity_check(1)").fetchone()[0] != "ok":
            raise StateUpgradeError("database integrity check failed")
    finally:
        db.set_progress_handler(None, 0)


def _backup(source: Path, destination: Path) -> None:
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    deadline = time.monotonic() + 120

    def progress(status: int, _remaining: int, _total: int) -> None:
        if (
            status in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            or time.monotonic() > deadline
        ):
            raise StateUpgradeError("backup is busy or exceeded its time limit")

    # The caller holds a reserved write lock; use a separate read connection for
    # backup to avoid backing up a connection with its own pending write transaction.
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader,
        closing(sqlite3.connect(destination)) as backup,
    ):
        reader.backup(backup, pages=256, progress=progress, sleep=0.05)
        backup.execute("PRAGMA journal_mode=DELETE")
        _integrity(backup)
    with destination.open("rb") as stream:
        os.fsync(stream.fileno())
    _sync_directory(destination.parent)


def inspect_backup(directory: Path) -> dict:
    if directory.is_symlink() or not directory.is_dir():
        raise StateUpgradeError("backup directory is unavailable")
    manifest = directory / "manifest.json"
    backup = directory / "reposteward.sqlite3"
    if manifest.is_symlink() or manifest.stat().st_size > 100_000:
        raise StateUpgradeError("backup manifest is unsupported")
    record = json.loads(manifest.read_text())
    if not isinstance(record, dict) or record.get("format_version") != FORMAT_VERSION:
        raise StateUpgradeError("unsupported backup format")
    fingerprint = _fingerprint(backup)
    if fingerprint["sha256"] != record.get("backup_sha256"):
        raise StateUpgradeError("backup checksum does not match its manifest")
    database = inspect_database(
        backup, supported=SCHEMA_VERSION, required_tables=REQUIRED_TABLES
    )
    if database["status"] not in {"compatible", "migration_required"} or database[
        "schema"
    ] != record.get("original_schema"):
        raise StateUpgradeError("backup schema or snapshot is not valid")
    with closing(
        sqlite3.connect(backup.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as db:
        _integrity(db)
    if _fingerprint(backup) != fingerprint:
        raise StateUpgradeError("backup changed while checking")
    return {
        "backup_directory": str(directory),
        "backup_verified": True,
        "schema": database["schema"],
        "sha256": fingerprint["sha256"],
        "phase": record.get("phase"),
        "integrity": "ok",
        "public_write": False,
    }


def upgrade_state(
    config: AppConfig, *, expected_state_dir: Path, plan_digest: str
) -> dict:
    root = _scope(config, expected_state_dir)
    plan = upgrade_plan(config, expected_state_dir=expected_state_dir)
    if plan["plan_digest"] != plan_digest or not plan["eligible"]:
        raise StateUpgradeError(
            "upgrade plan is stale or ineligible; inspect a fresh plan"
        )
    path = root / "reposteward.sqlite3"
    directory = None
    record = {}
    committed = False
    commit_started = False
    commit_unknown = False
    try:
        with closing(
            sqlite3.connect(
                path.as_uri() + "?mode=rw", uri=True, timeout=2, isolation_level=None
            )
        ) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                locked = upgrade_plan(config, expected_state_dir=expected_state_dir)
                if (
                    locked["plan_digest"] != plan_digest
                    or not locked["eligible"]
                    or _active_leases(db)
                ):
                    raise StateUpgradeError(
                        "database or leases changed before the write lock"
                    )
                _integrity(db)
                backups = root / "backups"
                if backups.is_symlink():
                    raise StateUpgradeError("backup root must not be a symlink")
                backups.mkdir(mode=0o700, exist_ok=True)
                directory = backups / uuid.uuid4().hex
                directory.mkdir(mode=0o700)
                _sync_directory(backups)
                record = {
                    "format_version": FORMAT_VERSION,
                    "operation_id": directory.name,
                    "created_at": datetime.now(UTC).isoformat(),
                    "source_database": str(path),
                    "original_schema": plan["database"]["schema"],
                    "target_schema": SCHEMA_VERSION,
                    "plan_digest": plan_digest,
                    "migration_digest": plan["migration_digest"],
                    "installation": installation_info(),
                    "phase": "preparing_backup",
                }
                _write_manifest(directory, record)
                _backup(path, directory / "reposteward.sqlite3")
                record["backup_sha256"] = _fingerprint(
                    directory / "reposteward.sqlite3"
                )["sha256"]
                record["phase"] = "backup_verified"
                _write_manifest(directory, record)
                inspect_backup(directory)
                for version in plan["migrations"]:
                    apply_migration(db, version)
                _integrity(db)
                identity = path.stat()
                if path.is_symlink() or any(
                    getattr(identity, field) != plan["fingerprint"][field]
                    for field in ("st_dev", "st_ino")
                ):
                    raise StateUpgradeError("database identity changed during upgrade")
                record["phase"] = "committing"
                _write_manifest(directory, record)
                commit_started = True
                db.commit()
                committed = True
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                elif commit_started and not committed:
                    commit_unknown = True
                raise
        record["phase"] = "upgraded"
        _write_manifest(directory, record)
        return {
            "upgraded": True,
            "schema": SCHEMA_VERSION,
            "backup": inspect_backup(directory),
            "public_write": False,
        }
    except (OSError, sqlite3.Error, StateUpgradeError) as exc:
        if directory is not None:
            record["phase"] = (
                "upgraded_record_incomplete"
                if committed
                else "commit_outcome_unknown"
                if commit_unknown
                else "rolled_back"
            )
            try:
                _write_manifest(directory, record)
            except OSError:
                pass
        location = f"; inspect backup directory {directory}" if directory else ""
        message = (
            "upgrade committed; outcome recording failed"
            if committed
            else "upgrade commit outcome is unknown; inspect state before retrying"
            if commit_unknown
            else "upgrade did not commit"
        )
        raise StateUpgradeError(message + location) from exc
