"""Append-only verification reconciliation receipt migration."""

VERIFICATION_RECOVERY_MIGRATION = (
    """CREATE TABLE IF NOT EXISTS verification_reconciliations (
    verification_id TEXT PRIMARY KEY REFERENCES external_verifications(id),
    idempotency_key TEXT NOT NULL, request_digest TEXT NOT NULL,
    payload TEXT NOT NULL, created_at TEXT NOT NULL)""",
)
