"""Versioned local operation plans and GitHub observations in the existing ledger."""

WORKBENCH_MIGRATION = (
    "ALTER TABLE queue_tasks ADD COLUMN operation_family TEXT NOT NULL DEFAULT 'native'",
    "ALTER TABLE queue_tasks ADD COLUMN payload_version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE queue_tasks ADD COLUMN scope_kind TEXT NOT NULL DEFAULT 'repository'",
    "ALTER TABLE queue_tasks ADD COLUMN scope_key TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE queue_tasks ADD COLUMN account_digest TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE queue_tasks ADD COLUMN plan_id TEXT NOT NULL DEFAULT ''",
    """CREATE INDEX IF NOT EXISTS queue_tasks_family_claim ON queue_tasks(
        operation_family,account_digest,state,manual_required,available_at,sequence)""",
    """CREATE TABLE IF NOT EXISTS local_operation_plans (
        id TEXT PRIMARY KEY, action TEXT NOT NULL, account_digest TEXT NOT NULL,
        scope_kind TEXT NOT NULL, scope_key TEXT NOT NULL, repository TEXT NOT NULL,
        payload_version INTEGER NOT NULL, payload TEXT NOT NULL,
        request_digest TEXT NOT NULL, plan_digest TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS local_operation_requests (
        account_digest TEXT NOT NULL, action TEXT NOT NULL,
        scope_kind TEXT NOT NULL, scope_key TEXT NOT NULL, key_digest TEXT NOT NULL,
        request_digest TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES queue_tasks(id),
        PRIMARY KEY(account_digest,action,scope_kind,scope_key,key_digest))""",
    """CREATE TABLE IF NOT EXISTS local_operation_results (
        task_id TEXT NOT NULL REFERENCES queue_tasks(id), generation INTEGER NOT NULL,
        stage TEXT NOT NULL, result TEXT NOT NULL, result_digest TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(task_id,generation,stage))""",
    """CREATE TABLE IF NOT EXISTS github_observations (
        id TEXT PRIMARY KEY, account_digest TEXT NOT NULL, project_id TEXT NOT NULL,
        source TEXT NOT NULL, body TEXT NOT NULL, body_digest TEXT NOT NULL,
        complete INTEGER NOT NULL, fetched_at TEXT NOT NULL,
        operation_id TEXT NOT NULL REFERENCES queue_tasks(id))""",
    """CREATE INDEX IF NOT EXISTS github_observation_history ON github_observations(
        account_digest,project_id,source,fetched_at DESC,id)""",
    """CREATE TABLE IF NOT EXISTS github_sources (
        account_digest TEXT NOT NULL, project_id TEXT NOT NULL, source TEXT NOT NULL,
        observation_id TEXT REFERENCES github_observations(id),
        last_attempt TEXT NOT NULL, last_success TEXT NOT NULL DEFAULT '',
        error_code TEXT NOT NULL DEFAULT '', retry_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(account_digest,project_id,source))""",
    """CREATE TABLE IF NOT EXISTS github_request_cache (
        account_digest TEXT NOT NULL, request_key TEXT NOT NULL, etag TEXT NOT NULL,
        body TEXT NOT NULL, body_digest TEXT NOT NULL, has_more INTEGER NOT NULL,
        checked_at TEXT NOT NULL, PRIMARY KEY(account_digest,request_key))""",
    """CREATE TABLE IF NOT EXISTS github_account_limits (
        account_digest TEXT PRIMARY KEY, retry_at TEXT NOT NULL, reason TEXT NOT NULL)""",
)
