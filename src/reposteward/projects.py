"""User-owned local project identities and workspace bindings.

The small registry is separate from the task ledger: machine paths are never
portable task facts. Constructors and queries do not create or migrate files.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .workspace import sanitized_environment

PROJECT_SCHEMA_VERSION = 1
MAX_GIT_OUTPUT = 1_000_000


class ProjectError(RuntimeError):
    """A project binding or its current local identity is not usable."""


def canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def local_git(path: Path, *args: str, optional: bool = False) -> str:
    """Run a bounded local Git query without hooks, network or credentials."""
    env = sanitized_environment(keep_codex_credentials=False)
    # Ambient repository routing must not redirect an explicitly supplied path.
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(name, None)
    try:
        result = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                "credential.helper=",
                "-C",
                str(path),
                *args,
            ],
            capture_output=True,
            check=False,
            timeout=15,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProjectError("local Git query unavailable") from exc
    if len(result.stdout) > MAX_GIT_OUTPUT:
        raise ProjectError("local Git result exceeds the metadata limit")
    if result.returncode:
        if optional and result.returncode == 1:
            return ""
        # Git stderr may include an original credential-bearing remote URL.
        raise ProjectError("local Git query failed; inspect the selected workspace")
    return result.stdout.decode("utf-8", errors="strict").rstrip("\n")


def normalize_remote(raw: str) -> dict[str, str]:
    if not raw or len(raw) > 4096 or any(ord(c) < 32 for c in raw):
        raise ProjectError("invalid remote identity")
    if "://" not in raw:
        match = re.fullmatch(r"(?:[^/@:\s]+@)?([a-zA-Z0-9.-]+):([^\s]+)", raw)
        if not match:
            raise ProjectError("origin must identify a hosted repository")
        raw = f"ssh://{match[1]}/{match[2]}"
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError as exc:
        raise ProjectError("invalid remote identity") from exc
    if parsed.scheme not in {"https", "ssh", "git"} or not re.fullmatch(
        r"[a-z0-9.-]+", host
    ):
        raise ProjectError("unsupported remote identity")
    if parsed.query or parsed.fragment or port not in {None, 22, 443, 9418}:
        raise ProjectError("remote identity contains unsupported URL components")
    path = parsed.path.removeprefix("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2 or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or p in {".", ".."} for p in parts
    ):
        raise ProjectError("origin must identify owner/repository")
    repository = path.casefold() if host == "github.com" else path
    return {"host": host, "repository": repository, "identity": f"{host}/{repository}"}


def workspace_metadata(path: Path) -> dict[str, Any]:
    start = path.expanduser().resolve(strict=True)
    if not start.is_dir():
        raise ProjectError("workspace must be a directory")
    root = Path(local_git(start, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if not start.is_relative_to(root):
        raise ProjectError("Git root is outside the selected workspace")
    git_dir = Path(local_git(root, "rev-parse", "--absolute-git-dir")).resolve(
        strict=True
    )
    common = Path(
        local_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve(strict=True)
    if any(ord(c) < 32 for c in str(root)):
        raise ProjectError("workspace path contains control characters")
    remote = normalize_remote(
        local_git(root, "config", "--no-includes", "--get", "remote.origin.url")
    )
    root_stat, git_stat = root.stat(), git_dir.stat()
    fingerprint = canonical_digest(
        {
            "root_device": root_stat.st_dev,
            "root_inode": root_stat.st_ino,
            "git_directory": str(git_dir),
            "git_device": git_stat.st_dev,
            "git_inode": git_stat.st_ino,
            "common_directory": str(common),
        }
    )
    return {
        "root": str(root),
        "git_directory": str(git_dir),
        "common_directory": str(common),
        "fingerprint": fingerprint,
        **remote,
    }


def workspace_state(root: Path) -> dict[str, Any]:
    head = local_git(root, "rev-parse", "--verify", "HEAD")
    branch = local_git(root, "symbolic-ref", "--short", "-q", "HEAD", optional=True)
    status = local_git(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=normal"
    )
    return {"head": head, "branch": branch, "dirty": bool(status)}


class ProjectRegistry:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection | None]:
        if not write and not self.path.exists():
            yield None
            return
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(
            str(self.path) if write else self.path.as_uri() + "?mode=ro",
            uri=not write,
            timeout=10,
            isolation_level=None,
        )
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if write and version == 0:
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
                ).fetchone():
                    raise ProjectError("unknown project registry schema")
                db.execute("""CREATE TABLE projects (
                    id TEXT PRIMARY KEY, identity TEXT NOT NULL UNIQUE,
                    host TEXT NOT NULL, repository TEXT NOT NULL,
                    name TEXT NOT NULL, created_at TEXT NOT NULL)""")
                db.execute("""CREATE TABLE workspace_bindings (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    root TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL)""")
                db.execute(
                    "CREATE INDEX project_workspaces ON workspace_bindings(project_id,active)"
                )
                db.execute("""CREATE TABLE project_events (
                    sequence INTEGER PRIMARY KEY, binding_id TEXT NOT NULL,
                    kind TEXT NOT NULL, created_at TEXT NOT NULL)""")
                db.execute(f"PRAGMA user_version={PROJECT_SCHEMA_VERSION}")
                version = PROJECT_SCHEMA_VERSION
            if version != PROJECT_SCHEMA_VERSION:
                raise ProjectError(f"unsupported project registry schema {version}")
            yield db
            if write:
                db.commit()
            else:
                db.rollback()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def link(self, path: Path, *, name: str = "") -> dict[str, Any]:
        meta = workspace_metadata(path)
        label = name.strip() or meta["repository"].split("/")[-1]
        if len(label) > 120 or any(ord(c) < 32 for c in label):
            raise ProjectError("project name must be at most 120 printable characters")
        project_id = uuid.uuid5(uuid.NAMESPACE_URL, meta["identity"]).hex
        binding_id = uuid.uuid5(uuid.NAMESPACE_URL, "workspace:" + meta["root"]).hex
        now = datetime.now(UTC).isoformat()
        with self.connection(write=True) as db:
            assert db is not None
            existing = db.execute(
                "SELECT * FROM workspace_bindings WHERE root=?", (meta["root"],)
            ).fetchone()
            if (
                existing
                and existing["active"]
                and (
                    existing["fingerprint"] != meta["fingerprint"]
                    or existing["project_id"] != project_id
                )
            ):
                raise ProjectError(
                    "workspace identity changed; unlink before explicitly rebinding"
                )
            db.execute(
                "INSERT OR IGNORE INTO projects VALUES (?,?,?,?,?,?)",
                (
                    project_id,
                    meta["identity"],
                    meta["host"],
                    meta["repository"],
                    label,
                    now,
                ),
            )
            if not existing or not existing["active"]:
                db.execute(
                    """INSERT INTO workspace_bindings VALUES (?,?,?,?,1,?,?)
                    ON CONFLICT(root) DO UPDATE SET project_id=excluded.project_id,
                    fingerprint=excluded.fingerprint,active=1,updated_at=excluded.updated_at""",
                    (
                        binding_id,
                        project_id,
                        meta["root"],
                        meta["fingerprint"],
                        now,
                        now,
                    ),
                )
                db.execute(
                    "INSERT INTO project_events(binding_id,kind,created_at) VALUES (?,?,?)",
                    (binding_id, "linked", now),
                )
        return self.inspect(path)

    def inspect(self, path: Path) -> dict[str, Any]:
        meta = workspace_metadata(path)
        with self.connection() as db:
            row = (
                db.execute(
                    "SELECT * FROM workspace_bindings WHERE root=? AND active=1",
                    (meta["root"],),
                ).fetchone()
                if db
                else None
            )
            if row is None:
                raise ProjectError(
                    "workspace is not linked; run reposteward project link"
                )
            project = db.execute(
                "SELECT * FROM projects WHERE id=?", (row["project_id"],)
            ).fetchone()
            if (
                project is None
                or row["fingerprint"] != meta["fingerprint"]
                or project["identity"] != meta["identity"]
            ):
                raise ProjectError("workspace identity changed; explicitly rebind it")
            binding = dict(row)
            state = workspace_state(Path(meta["root"]))
            if workspace_metadata(Path(meta["root"])) != meta:
                raise ProjectError("workspace identity changed during inspection")
            result = {
                "schema_version": 1,
                "project": dict(project),
                "binding": binding,
                "workspace": state,
                "public_write": False,
            }
        return result

    def list(self, *, limit: int = 50) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ProjectError("project limit must be between 1 and 500")
        with self.connection() as db:
            if db is None:
                return {
                    "schema_version": 1,
                    "projects": [],
                    "omitted": 0,
                    "public_write": False,
                }
            total = db.execute("SELECT count(*) FROM projects").fetchone()[0]
            rows = db.execute(
                "SELECT * FROM projects ORDER BY identity LIMIT ?", (limit,)
            ).fetchall()
            projects = []
            for row in rows:
                count = db.execute(
                    "SELECT count(*) FROM workspace_bindings WHERE project_id=? AND active=1",
                    (row["id"],),
                ).fetchone()[0]
                bindings = db.execute(
                    "SELECT id,root FROM workspace_bindings WHERE project_id=? AND active=1 ORDER BY root LIMIT 100",
                    (row["id"],),
                ).fetchall()
                missing = sum(not Path(b["root"]).is_dir() for b in bindings)
                projects.append(
                    {
                        **dict(row),
                        "workspace_count": count,
                        "missing_workspaces": missing,
                        "workspace_check_omitted": max(0, count - 100),
                        "workspaces": [dict(b) for b in bindings],
                    }
                )
        return {
            "schema_version": 1,
            "projects": projects,
            "omitted": max(0, total - limit),
            "public_write": False,
        }

    def unlink(self, binding_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{32}", binding_id):
            raise ProjectError("invalid workspace binding ID")
        with self.connection(write=True) as db:
            assert db is not None
            row = db.execute(
                "SELECT active FROM workspace_bindings WHERE id=?", (binding_id,)
            ).fetchone()
            if row is None:
                raise ProjectError("workspace binding does not exist")
            if row["active"]:
                now = datetime.now(UTC).isoformat()
                db.execute(
                    "UPDATE workspace_bindings SET active=0,updated_at=? WHERE id=?",
                    (now, binding_id),
                )
                db.execute(
                    "INSERT INTO project_events(binding_id,kind,created_at) VALUES (?,?,?)",
                    (binding_id, "unlinked", now),
                )
        return {
            "binding_id": binding_id,
            "active": False,
            "files_deleted": False,
            "public_write": False,
        }
