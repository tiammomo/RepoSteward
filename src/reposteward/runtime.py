"""Offline installation and state diagnostics shared by local user interfaces."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .config import AppConfig, default_user_config_path
from .projects import PROJECT_SCHEMA_VERSION
from .store import SCHEMA_VERSION


def installation_info() -> dict:
    result = {
        "version": __version__,
        "metadata_available": False,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "module_path": str(Path(__file__).resolve().parent),
        "distribution_path": None,
        "editable": None,
        "source_revision": None,
    }
    try:
        distribution = metadata.distribution("reposteward")
        result.update(
            version=distribution.version,
            metadata_available=True,
            distribution_path=str(distribution.locate_file("")),
        )
        direct = distribution.read_text("direct_url.json")
        if direct and len(direct) <= 64_000:
            value = json.loads(direct)
            if isinstance(value, dict):
                directory = value.get("dir_info")
                if isinstance(directory, dict):
                    result["editable"] = directory.get("editable") is True
                vcs = value.get("vcs_info")
                revision = vcs.get("commit_id") if isinstance(vcs, dict) else None
                if isinstance(revision, str) and re.fullmatch(
                    "(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", revision
                ):
                    result["source_revision"] = revision
        # Deliberately never return the installation URL or unparsed metadata.
    except (metadata.PackageNotFoundError, OSError, ValueError):
        pass
    return result


def _signature(path: Path) -> tuple[int, int, int, int]:
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _pending_journal(path: Path) -> bool:
    return any(
        sidecar.exists() and sidecar.stat().st_size > 0
        for sidecar in (Path(str(path) + "-wal"), Path(str(path) + "-journal"))
    )


def inspect_database(path: Path, *, supported: int, required_tables: tuple) -> dict:
    """Inspect a settled SQLite snapshot without creating a DB, WAL or SHM file.

    A live journal is intentionally reported as unknown. Immutable SQLite reads
    cannot include its latest state; a read/write connection could create sidecars.
    This report is diagnostic evidence, never a migration or mutation permit.
    """
    result = {"path": str(path), "schema": None, "supported_schema": supported}
    try:
        if path.is_symlink():
            return {**result, "status": "unsupported_path"}
        if not path.exists():
            return {**result, "status": "missing"}
        if not path.is_file():
            return {**result, "status": "unreadable"}
        before = _signature(path)
        if _pending_journal(path):
            return {**result, "status": "snapshot_required"}
        with closing(
            sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0.2
            )
        ) as db:
            db.execute("PRAGMA query_only=ON")
            steps = 0

            def limit() -> int:
                nonlocal steps
                steps += 1
                return int(steps > 1000)

            db.set_progress_handler(limit, 1000)
            schema = int(db.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        if _pending_journal(path) or _signature(path) != before:
            return {**result, "status": "snapshot_required"}
        result["schema"] = schema
        if schema > supported:
            status = "newer_than_supported"
        elif schema == 0:
            status = "uninitialized" if not tables else "unrecognized"
        elif not set(required_tables).issubset(tables):
            status = "unrecognized"
        elif schema < supported:
            status = "migration_required"
        else:
            status = "compatible"
        return {**result, "status": status}
    except sqlite3.OperationalError:
        return {**result, "status": "unreadable"}
    except sqlite3.DatabaseError:
        return {**result, "status": "invalid_database"}
    except OSError:
        return {**result, "status": "unreadable"}


def local_diagnostics(
    config: AppConfig | None, *, expected_state_dir: Path | None = None
) -> tuple[dict, bool]:
    report = {
        "observed_at": datetime.now(UTC).isoformat(),
        "mode": "local",
        "installation": installation_info(),
        "configuration": {"status": "unavailable"},
        "databases": {},
        "next_actions": [],
        "public_write": False,
    }
    if config is None:
        report["configuration"]["user_config_path"] = str(default_user_config_path())
        report["next_actions"].append(
            "Create missing configuration with reposteward init, or correct the selected TOML file."
        )
        return report, False
    api = urlsplit(config.github.api_url)
    if api.username or api.password or api.query or api.fragment:
        # Other effective values, such as a namespaced path, may have been
        # derived from this URL. Avoid echoing them as well as the URL itself.
        report["configuration"]["status"] = "unsupported_api_url"
        report["next_actions"].append(
            "Use a GitHub API URL without userinfo, query or fragment; configure authentication separately."
        )
        return report, False
    sources = dict(config.setting_sources)
    values = {
        "project.state_dir": str(config.state_dir),
        "project.workspace_dir": str(config.workspace_dir),
        "project.namespace_state": config.state_namespace,
        "github.login": config.github.login,
        "agent.harness": config.agent.harness,
        "runner.image": config.runner.image,
    }
    # Only a public origin is useful here; URLs may contain userinfo or query tokens.
    host = api.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    try:
        port = ":" + str(api.port) if api.port else ""
    except ValueError:
        port = ""
    values["github.api_url"] = api.scheme + "://" + host + port
    expected = expected_state_dir.expanduser().resolve() if expected_state_dir else None
    matches = expected is None or config.state_dir.resolve() == expected
    report["configuration"] = {
        "status": "loaded",
        "selected_path": str(config.path),
        "files": [
            {"layer": source, "path": path} for source, path in config.config_files
        ],
        "settings": {
            key: {"effective_value": value, "source": sources.get(key, "unknown")}
            for key, value in values.items()
        },
        "expected_state_dir": str(expected) if expected is not None else None,
        "state_dir_matches": matches,
        "workspace_fallback": (
            "state_dir/workspaces"
            if config.workspace_dir == config.state_dir / "workspaces"
            else None
        ),
    }
    report["configuration"]["settings"]["github.api_url"]["display_scope"] = (
        "origin_only"
    )
    if not matches:
        report["next_actions"].append(
            "The effective state directory differs from the expected directory; correct configuration before any write."
        )
        # Do not inspect an unexpectedly selected database.
        return report, False
    report["databases"] = {
        "tasks": inspect_database(
            config.state_dir / "reposteward.sqlite3",
            supported=SCHEMA_VERSION,
            required_tables=("runs", "candidates", "issue_drafts"),
        ),
        "projects": inspect_database(
            config.state_dir / "projects.sqlite3",
            supported=PROJECT_SCHEMA_VERSION,
            required_tables=("projects", "workspace_bindings", "project_events"),
        ),
    }
    ok = True
    actions = {
        "missing": "No local ledger yet; explicit project/task setup will create it when needed.",
        "migration_required": "Stop writers and back up this database before upgrading with the newer installation.",
        "newer_than_supported": "Use an installation supporting this schema; do not open it for writes with this version.",
        "snapshot_required": "An active or pending journal prevents offline inspection; stop writers cleanly and inspect again.",
    }
    for name, database in report["databases"].items():
        status = database["status"]
        if status != "compatible":
            report["next_actions"].append(
                name
                + ": "
                + actions.get(status, "Inspect or restore this ledger before using it.")
            )
        ok = ok and status in {"compatible", "missing"}
    report["scope"] = (
        "Installation and schema diagnostics only; authentication, integrity of all rows, verification and merge eligibility are not evaluated."
    )
    return report, ok
