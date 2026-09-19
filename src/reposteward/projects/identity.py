"""Stable remote identities and purpose metadata, separate from execution policy."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

PROJECT_MIGRATION = (
    """CREATE TABLE IF NOT EXISTS project_import_receipts (
        plan_id TEXT PRIMARY KEY, payload_digest TEXT NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(id))""",
    """CREATE TABLE IF NOT EXISTS project_aliases (
        identity TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id))""",
    """CREATE TABLE IF NOT EXISTS project_remotes (
        project_id TEXT PRIMARY KEY REFERENCES projects(id), host TEXT NOT NULL,
        remote_id INTEGER NOT NULL, canonical_identity TEXT NOT NULL,
        fork INTEGER NOT NULL, upstream TEXT NOT NULL, observed_at TEXT NOT NULL,
        UNIQUE(host,remote_id))""",
    """CREATE TABLE IF NOT EXISTS project_profiles (
        project_id TEXT PRIMARY KEY REFERENCES projects(id), purpose TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    "INSERT OR IGNORE INTO project_aliases SELECT identity,id FROM projects",
)
PURPOSES = frozenset({"maintain", "contribute", "watch"})


def migrate_registry(db):
    for statement in PROJECT_MIGRATION:
        db.execute(statement)
    db.execute("PRAGMA user_version=2")


def lookup(db, identity: str):
    return db.execute(
        "SELECT p.* FROM projects p JOIN project_aliases a ON a.project_id=p.id WHERE a.identity=?",
        (identity,),
    ).fetchone()


def profile(db, project_id: str) -> dict:
    remote = db.execute(
        "SELECT * FROM project_remotes WHERE project_id=?", (project_id,)
    ).fetchone()
    purpose = db.execute(
        "SELECT purpose FROM project_profiles WHERE project_id=?", (project_id,)
    ).fetchone()
    aliases = db.execute(
        "SELECT identity FROM project_aliases WHERE project_id=? ORDER BY identity LIMIT 50",
        (project_id,),
    ).fetchall()
    return {
        "purpose": purpose[0] if purpose else "unspecified",
        "remote": {**dict(remote), "remote_id": str(remote["remote_id"])}
        if remote
        else None,
        "aliases": [row[0] for row in aliases],
    }


def register(db, remote: dict, purpose: str) -> dict:
    from reposteward.projects.registry import ProjectError, normalize_remote

    if (
        purpose not in PURPOSES
        or not str(remote.get("remote_id", "")).isdigit()
        or not 0 < int(remote["remote_id"]) < 2**63
    ):
        raise ProjectError("invalid project identity or purpose")
    canonical = normalize_remote(
        "https://" + remote["host"] + "/" + remote["repository"]
    )
    aliases = sorted({canonical["identity"], *remote.get("aliases", [])})
    if len(aliases) > 20 or any(
        normalize_remote("https://" + value)["host"] != canonical["host"]
        for value in aliases
    ):
        raise ProjectError("invalid repository aliases")
    stable = db.execute(
        "SELECT p.* FROM projects p JOIN project_remotes r ON r.project_id=p.id WHERE r.host=? AND r.remote_id=?",
        (canonical["host"], remote["remote_id"]),
    ).fetchone()
    matches = [row for alias in aliases if (row := lookup(db, alias))]
    ids = {row["id"] for row in matches}
    if stable:
        ids.add(stable["id"])
    if len(ids) > 1:
        raise ProjectError(
            "repository aliases belong to different projects; reconcile explicitly"
        )
    project_id = next(
        iter(ids), uuid.uuid5(uuid.NAMESPACE_URL, canonical["identity"]).hex
    )
    known = db.execute(
        "SELECT * FROM project_remotes WHERE project_id=?", (project_id,)
    ).fetchone()
    if known and (known["host"], known["remote_id"]) != (
        canonical["host"],
        int(remote["remote_id"]),
    ):
        raise ProjectError(
            "repository name now identifies a different remote; reconcile explicitly"
        )
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT OR IGNORE INTO projects VALUES (?,?,?,?,?,?)",
        (
            project_id,
            canonical["identity"],
            canonical["host"],
            canonical["repository"],
            canonical["repository"].split("/")[-1],
            now,
        ),
    )
    for alias in aliases:
        db.execute(
            "INSERT OR IGNORE INTO project_aliases VALUES (?,?)", (alias, project_id)
        )
    db.execute(
        "UPDATE projects SET identity=?,host=?,repository=? WHERE id=?",
        (canonical["identity"], canonical["host"], canonical["repository"], project_id),
    )
    db.execute(
        "INSERT INTO project_remotes VALUES (?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET canonical_identity=excluded.canonical_identity,fork=excluded.fork,upstream=excluded.upstream,observed_at=excluded.observed_at",
        (
            project_id,
            canonical["host"],
            remote["remote_id"],
            canonical["identity"],
            int(bool(remote.get("fork"))),
            json.dumps(remote.get("upstream")),
            now,
        ),
    )
    db.execute(
        "INSERT INTO project_profiles VALUES (?,?,?) ON CONFLICT(project_id) DO UPDATE SET purpose=excluded.purpose,updated_at=excluded.updated_at",
        (project_id, purpose, now),
    )
    return {
        **dict(
            db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        ),
        **profile(db, project_id),
    }


def registry_revision(db) -> str:
    from reposteward.projects.registry import ProjectError, canonical_digest

    if db is None:
        return canonical_digest({})
    result = {}
    for table in (
        "projects",
        "workspace_bindings",
        "project_aliases",
        "project_remotes",
        "project_profiles",
        "project_import_receipts",
    ):
        rows = db.execute(f"SELECT * FROM {table} ORDER BY 1 LIMIT 10001").fetchall()
        if len(rows) > 10000:
            raise ProjectError("registry exceeds import planning limit")
        result[table] = [dict(row) for row in rows]
    # A newly initialized empty registry has the same identity as absent state.
    return canonical_digest(result if any(result.values()) else {})
