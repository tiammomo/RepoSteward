"""Bounded cached remote facts for the explicit multi-project refresh."""

OVERVIEW_MIGRATION = (
    """CREATE TABLE IF NOT EXISTS project_inbox_cache (
        project_id TEXT PRIMARY KEY, repository TEXT NOT NULL, portfolio TEXT NOT NULL DEFAULT '{}',
        fetched_at TEXT NOT NULL DEFAULT '', attempted_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')""",
)
