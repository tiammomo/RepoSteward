"""Versioned storage for reviewed project guidance."""

KNOWLEDGE_MIGRATION = (
    """CREATE TABLE IF NOT EXISTS project_knowledge (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, origin_run_id TEXT NOT NULL REFERENCES runs(id),
        status TEXT NOT NULL, statement TEXT NOT NULL, scope_paths TEXT NOT NULL, conditions TEXT NOT NULL,
        evidence TEXT NOT NULL, dependencies_digest TEXT NOT NULL, supersedes TEXT NOT NULL DEFAULT '',
        successor TEXT NOT NULL DEFAULT '', review TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS knowledge_project ON project_knowledge(project_id,status,updated_at)""",
)
