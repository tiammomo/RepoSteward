"""Storage extension for external attempts; work items, runs and checkpoints stay canonical."""

from __future__ import annotations

EXTERNAL_TASK_MIGRATION = (
    """CREATE TABLE IF NOT EXISTS external_task_runs (
        run_id TEXT PRIMARY KEY REFERENCES runs(id),
        project_id TEXT NOT NULL, binding_id TEXT NOT NULL, workspace_root TEXT NOT NULL,
        binding_fingerprint TEXT NOT NULL, checkpoint_revision INTEGER NOT NULL DEFAULT 0,
        snapshot TEXT NOT NULL, policy_digest TEXT NOT NULL, base_commit TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS external_tasks_by_project ON external_task_runs(project_id,updated_at)""",
    """CREATE TABLE IF NOT EXISTS external_task_checkpoints (
        run_id TEXT NOT NULL REFERENCES runs(id), revision INTEGER NOT NULL,
        checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
        idempotency_key TEXT NOT NULL, request_digest TEXT NOT NULL,
        snapshot TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(run_id,revision), UNIQUE(run_id,idempotency_key))""",
    """CREATE TABLE IF NOT EXISTS external_task_sources (
        run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL,
        digest TEXT NOT NULL REFERENCES content_blobs(digest),
        PRIMARY KEY(run_id,kind))""",
)
