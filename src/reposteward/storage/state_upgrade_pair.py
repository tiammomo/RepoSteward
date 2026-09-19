"""Explicit two-database upgrades with backups and honest partial-commit recovery."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path

from reposteward.core.runtime import inspect_database, installation_info
from reposteward.projects.identity import PROJECT_MIGRATION, migrate_registry
from reposteward.projects.registry import PROJECT_SCHEMA_VERSION
from reposteward.storage.store import SCHEMA_VERSION, apply_migration

REGISTRY_TABLES = ("projects", "workspace_bindings", "project_events")


def add_registry_plan(config, result):
    from reposteward.storage.state_upgrade import (
        StateUpgradeError,
        _digest,
        _fingerprint,
    )

    path = config.state_dir / "projects.sqlite3"
    database = inspect_database(
        path, supported=PROJECT_SCHEMA_VERSION, required_tables=REGISTRY_TABLES
    )
    registry = {
        "database": database,
        "target_schema": PROJECT_SCHEMA_VERSION,
        "migration_digest": _digest(PROJECT_MIGRATION),
    }
    if database["status"] in {"compatible", "migration_required"}:
        try:
            registry["fingerprint"] = _fingerprint(path)
            if (
                inspect_database(
                    path,
                    supported=PROJECT_SCHEMA_VERSION,
                    required_tables=REGISTRY_TABLES,
                )
                != database
            ):
                raise StateUpgradeError("registry changed while planning")
        except (OSError, sqlite3.Error, StateUpgradeError):
            registry["database"] = {**database, "status": "snapshot_required"}
    result["registry"] = registry
    statuses = [result["database"]["status"], registry["database"]["status"]]
    result["eligible"] = (
        all(
            status in {"compatible", "migration_required", "missing"}
            for status in statuses
        )
        and "migration_required" in statuses
        and not result["active_leases"]
    )


def inspect_pair(directory: Path, record: dict) -> dict:
    from reposteward.storage.state_upgrade import (
        StateUpgradeError,
        _fingerprint,
        _integrity,
    )

    records = record.get("databases")
    if not isinstance(records, list) or not 1 <= len(records) <= 2:
        raise StateUpgradeError("invalid backup database manifest")
    result, names = [], set()
    for item in records:
        name = item["name"]
        if name not in {"reposteward.sqlite3", "projects.sqlite3"} or name in names:
            raise StateUpgradeError("invalid backup database name")
        names.add(name)
        path = directory / name
        original = _fingerprint(path)
        if original["sha256"] != item.get("backup_sha256"):
            raise StateUpgradeError("backup checksum does not match its manifest")
        registry = name == "projects.sqlite3"
        database = inspect_database(
            path,
            supported=PROJECT_SCHEMA_VERSION if registry else SCHEMA_VERSION,
            required_tables=REGISTRY_TABLES
            if registry
            else ("runs", "candidates", "issue_drafts"),
        )
        if (
            database["status"] not in {"compatible", "migration_required"}
            or database["schema"] != item["original_schema"]
        ):
            raise StateUpgradeError("backup schema or snapshot is not valid")
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as db:
            _integrity(db)
        if _fingerprint(path) != original:
            raise StateUpgradeError("backup changed while checking")
        result.append(
            {
                "name": name,
                "schema": database["schema"],
                "sha256": original["sha256"],
                "integrity": "ok",
            }
        )
    main = next(
        (item for item in result if item["name"] == "reposteward.sqlite3"), result[0]
    )
    return {
        "backup_directory": str(directory),
        "backup_verified": True,
        "schema": main["schema"],
        "sha256": main["sha256"],
        "databases": result,
        "phase": record["phase"],
        "integrity": "ok",
        "public_write": False,
    }


def upgrade_pair(config, plan: dict) -> dict:
    from reposteward.storage.state_upgrade import (
        StateUpgradeError,
        _active_leases,
        _backup,
        _fingerprint,
        _integrity,
        _sync_directory,
        _write_manifest,
        inspect_backup,
        upgrade_plan,
    )

    root = Path(plan["state_dir"])
    directory, record = None, {}
    committed, unknown = [], []
    try:
        with ExitStack() as stack:
            databases = []
            for name, entry in (
                ("reposteward.sqlite3", plan),
                ("projects.sqlite3", plan["registry"]),
            ):
                if entry["database"]["status"] == "missing":
                    continue
                path = root / name
                db = stack.enter_context(
                    closing(
                        sqlite3.connect(
                            path.as_uri() + "?mode=rw",
                            uri=True,
                            timeout=2,
                            isolation_level=None,
                        )
                    )
                )
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                stack.callback(
                    lambda connection=db: (
                        connection.rollback() if connection.in_transaction else None
                    )
                )
                databases.append((name, path, entry, db))
            locked = upgrade_plan(config, expected_state_dir=root)
            if locked["plan_digest"] != plan["plan_digest"] or not locked["eligible"]:
                raise StateUpgradeError("database changed before the write locks")
            for name, _, _, db in databases:
                if name == "reposteward.sqlite3" and _active_leases(db):
                    raise StateUpgradeError("active workers prevent upgrade")
                _integrity(db)
            backups = root / "backups"
            if backups.is_symlink():
                raise StateUpgradeError("backup root must not be a symlink")
            backups.mkdir(mode=0o700, exist_ok=True)
            directory = backups / uuid.uuid4().hex
            directory.mkdir(mode=0o700)
            _sync_directory(backups)
            record = {
                "format_version": 2,
                "operation_id": directory.name,
                "created_at": datetime.now(UTC).isoformat(),
                "plan_digest": plan["plan_digest"],
                "installation": installation_info(),
                "phase": "preparing_backup",
                "databases": [],
            }
            _write_manifest(directory, record)
            # Back up every existing database before either schema changes.
            for name, path, entry, _ in databases:
                _backup(path, directory / name)
                record["databases"].append(
                    {
                        "name": name,
                        "original_schema": entry["database"]["schema"],
                        "target_schema": entry["target_schema"],
                        "backup_sha256": _fingerprint(directory / name)["sha256"],
                        "migration_digest": entry["migration_digest"],
                        "phase": "backed_up",
                    }
                )
            record["phase"] = "backup_verified"
            _write_manifest(directory, record)
            inspect_backup(directory)
            for name, _, entry, db in databases:
                if name == "projects.sqlite3" and entry["database"]["schema"] == 1:
                    migrate_registry(db)
                elif name == "reposteward.sqlite3":
                    for version in entry["migrations"]:
                        apply_migration(db, version)
                _integrity(db)
            for name, path, entry, db in databases:
                identity = path.stat()
                if path.is_symlink() or any(
                    getattr(identity, key) != entry["fingerprint"][key]
                    for key in ("st_dev", "st_ino")
                ):
                    raise StateUpgradeError("database identity changed during upgrade")
                item = next(row for row in record["databases"] if row["name"] == name)
                item["phase"] = "committing"
                record["phase"] = "committing"
                _write_manifest(directory, record)
                try:
                    db.commit()
                except BaseException:
                    if not db.in_transaction:
                        unknown.append(name)
                    raise
                committed.append(name)
                item["phase"] = "committed"
                _write_manifest(directory, record)
        record["phase"] = "upgraded"
        _write_manifest(directory, record)
        return {
            "upgraded": True,
            "schema": SCHEMA_VERSION,
            "registry_schema": PROJECT_SCHEMA_VERSION,
            "backup": inspect_backup(directory),
            "public_write": False,
        }
    except (OSError, sqlite3.Error, StateUpgradeError) as exc:
        if directory:
            record["phase"] = (
                "commit_outcome_unknown"
                if unknown
                else "partially_upgraded"
                if committed
                else "rolled_back"
            )
            record["committed_databases"], record["unknown_databases"] = (
                committed,
                unknown,
            )
            try:
                _write_manifest(directory, record)
            except OSError:
                pass
        message = (
            "upgrade commit outcome is unknown"
            if unknown
            else "upgrade partially committed"
            if committed
            else "upgrade did not commit"
        )
        raise StateUpgradeError(
            f"{message}; inspect {directory}; a fresh state plan upgrades only remaining schemas"
        ) from exc
