from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from reposteward.merge import MergeCheck, MergeSnapshot, evaluate_merge
from reposteward.models import Candidate, Issue, RepositoryInfo
from reposteward.store import SCHEMA_VERSION, Store, StoreError


def _context_source_digest(marker: str) -> str:
    sources = [
        {
            "kind": "repository_policy",
            "locator": "test-policy",
            "digest": marker,
            "trust": "operator_trusted",
            "updated_at": "",
        }
    ]
    encoded = json.dumps(
        sources,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _context_payload(
    *,
    pack_id: str,
    work_item_id: str,
    run_id: str,
    source_digest: str,
    base_commit: str,
) -> dict:
    return {
        "id": pack_id,
        "schema_version": 1,
        "work_item_id": work_item_id,
        "project": {
            "repository": "owner/repo",
            "default_branch": "main",
            "base_commit": base_commit,
            "policy_digest": "e" * 64,
            "verification_prefixes": [],
            "required_verification_markers": [],
            "instruction_sources": [],
        },
        "task": {
            "kind": "github_issue",
            "external_id": "7",
            "title": "Example issue",
            "description": "Example",
            "description_omitted_chars": 0,
            "url": "https://github.com/owner/repo/issues/7",
            "updated_at": "2026-01-01T00:00:00Z",
            "acceptance_criteria": [],
        },
        "constraints": [],
        "sources": [
            {
                "kind": "repository_policy",
                "locator": "test-policy",
                "digest": source_digest,
                "trust": "operator_trusted",
                "updated_at": "",
            }
        ],
        "handoff": None,
        "source_digest": _context_source_digest(source_digest),
        "provenance": {
            "run_id": run_id,
            "harness": "codex-cli",
            "model": "",
            "created_at": "2026-01-01T00:00:00Z",
            "generator": "reposteward",
        },
    }


def _checkpoint_payload(
    *, work_item_id: str, run_id: str, pack_id: str, status: str
) -> dict:
    return {
        "schema_version": 1,
        "work_item_id": work_item_id,
        "run_id": run_id,
        "context_pack_id": pack_id,
        "status": status,
        "head_commit": "b" * 40,
        "completed": [],
        "remaining": ["review"],
        "next_action": "human_review" if status == "ready" else "verify",
        "blockers": [],
        "decisions": [],
        "evidence": [],
    }


def _owner_review_facts(run_id: str) -> dict:
    return {
        "repository": "owner/repo",
        "pull_number": 12,
        "run_id": run_id,
        "actor": "alice",
        "pull_author": "alice",
        "head_owner": "owner",
        "head_repository": "owner/repo",
        "head_branch": "alice/feat/example",
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "policy_digest": "c" * 64,
        "review_decision": "",
        "diff_digest": "d" * 64,
        "checks_digest": "e" * 64,
        "conversation_digest": "f" * 64,
        "dependency_digest": "1" * 64,
        "activity_digest": "2" * 64,
        "rules_digest": "3" * 64,
    }


def _usage_run(store: Store, *, details: dict | None = None) -> tuple[str, str]:
    work_item = store.ensure_work_item(
        "owner/repo",
        kind="github_issue",
        external_id="7",
        title="Example issue",
    )
    run_id = store.start_run("owner/repo", 7, "agent")
    pack_id = f"pack-{run_id}"
    payload = _context_payload(
        pack_id=pack_id,
        work_item_id=str(work_item["id"]),
        run_id=run_id,
        source_digest="d" * 64,
        base_commit="b" * 40,
    )
    store.save_context_run(
        pack_id=pack_id,
        work_item_id=str(work_item["id"]),
        run_id=run_id,
        schema_version=1,
        source_digest=str(payload["source_digest"]),
        base_commit="b" * 40,
        payload=payload,
        harness="codex-sdk",
        model="model-a",
    )
    if details is not None:
        store.update_run(run_id, status="submitted", details=details)
    return run_id, str(work_item["id"])


class StoreTests(unittest.TestCase):
    def test_inbox_queries_return_only_staged_proposals_and_latest_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            store.record_issue_proposal(
                project_item_id="staged",
                project_id="project",
                project_url="https://example.test/project",
                draft_id="draft-1",
                repository="owner/repo",
                creator="alice",
                content_digest="a" * 64,
            )
            store.record_issue_proposal(
                project_item_id="published",
                project_id="project",
                project_url="https://example.test/project",
                draft_id="draft-2",
                repository="owner/repo",
                creator="alice",
                content_digest="b" * 64,
            )
            store.mark_issue_proposal_published(
                "published", issue_number=7, issue_url="https://example.test/issues/7"
            )
            old_run = store.start_run("owner/repo", 7, "agent")
            store.update_run(old_run, status="failed")
            latest_run = store.start_run("owner/repo", 7, "verification")
            store.update_run(latest_run, status="ready")
            other_run = store.start_run("owner/repo", 8, "agent")
            store.record_submission(
                "owner/repo", 8, "https://github.com/owner/repo/pull/9"
            )

            proposals = store.staged_issue_proposals("OWNER/REPO")
            runs = store.latest_runs_for_repository("OWNER/REPO")

        self.assertEqual([value["project_item_id"] for value in proposals], ["staged"])
        self.assertEqual({value["id"] for value in runs}, {latest_run, other_run})
        submitted = next(value for value in runs if value["id"] == other_run)
        self.assertEqual(
            submitted["submission_pr_url"],
            "https://github.com/owner/repo/pull/9",
        )

    def test_legacy_unversioned_database_is_migrated_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE runs (
                        id TEXT PRIMARY KEY,
                        repository TEXT NOT NULL,
                        issue_number INTEGER NOT NULL,
                        stage TEXT NOT NULL,
                        status TEXT NOT NULL,
                        worktree TEXT NOT NULL DEFAULT '',
                        details TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO runs(
                        id, repository, issue_number, stage, status,
                        created_at, updated_at
                    ) VALUES ('legacy-run', 'owner/repo', 7, 'review', 'ready',
                              '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                    """
                )

            store = Store(path)

            self.assertEqual(store.schema_version(), SCHEMA_VERSION)
            self.assertEqual(store.run("legacy-run")["status"], "ready")
            with closing(sqlite3.connect(path)) as connection, connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }

        self.assertIn("context_packs", tables)
        self.assertIn("checkpoints", tables)

    def test_newer_database_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")

            with self.assertRaisesRegex(StoreError, "newer than supported"):
                Store(path)

    def test_version_two_database_receives_context_import_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DROP TABLE context_imports")
                connection.execute("PRAGMA user_version=2")

            migrated = Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                table = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='context_imports'
                    """
                ).fetchone()

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertIsNotNone(table)

    def test_version_four_database_receives_github_event_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DROP TABLE github_pr_watermarks")
                connection.execute("DROP TABLE github_pr_events")
                connection.execute("PRAGMA user_version=4")

            migrated = Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertIn("github_pr_events", tables)
        self.assertIn("github_pr_watermarks", tables)

    def test_version_five_database_receives_merge_decision_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DROP TABLE merge_decisions")
                connection.execute("PRAGMA user_version=5")

            migrated = Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='merge_decisions'"
                ).fetchone()

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertIsNotNone(table)

    def test_version_nine_database_receives_merge_execution_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DROP TABLE merge_executions")
                connection.execute("PRAGMA user_version=9")

            migrated = Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='merge_executions'"
                ).fetchone()
                unique_stage = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='index' AND name='merge_executions_stage_once'
                    """
                ).fetchone()

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertIsNotNone(table)
            self.assertIsNotNone(unique_stage)

    def test_version_fourteen_database_receives_publication_attempt_audit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DROP TABLE publication_attempts")
                connection.execute("PRAGMA user_version=14")

            migrated = Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='publication_attempts'"
                ).fetchone()
                unique_stage = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='index'
                      AND name='publication_attempts_stage_once'
                    """
                ).fetchone()

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertIsNotNone(table)
            self.assertIsNotNone(unique_stage)

    def test_version_ten_database_receives_portfolio_dependency_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DROP TABLE portfolio_dependency_events")
                connection.execute("PRAGMA user_version=10")

            migrated = Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE name='portfolio_dependency_events'"
                ).fetchone()
                index = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE name='portfolio_dependency_events_for_pull'"
                ).fetchone()
                digest_index = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE name='portfolio_dependency_events_digest'"
                ).fetchone()

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertIsNotNone(table)
            self.assertIsNotNone(index)
            self.assertIsNotNone(digest_index)

    def test_version_eleven_database_receives_owner_review_attestation_audit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DROP TABLE owner_review_attestations")
                connection.execute("PRAGMA user_version=11")

            migrated = Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE name='owner_review_attestations'"
                ).fetchone()
                index = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE name='owner_review_attestations_for_pull'"
                ).fetchone()

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertIsNotNone(table)
            self.assertIsNotNone(index)

    def test_version_twelve_database_receives_harness_usage_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DROP TABLE harness_usage_events")
                connection.execute("PRAGMA user_version=12")

            migrated = Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='harness_usage_events'"
                ).fetchone()
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='index' AND tbl_name='harness_usage_events'"
                    )
                }

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertIsNotNone(table)
            self.assertIn("harness_usage_events_report", indexes)
            self.assertIn("harness_usage_events_for_work_item", indexes)

    def test_harness_usage_is_idempotent_bounded_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            run_id, work_item_id = _usage_run(
                store,
                details={
                    "pr_url": "https://github.com/owner/repo/pull/12",
                    "agent_metrics": {"input_tokens": 999},
                },
            )
            metrics = {
                "input_tokens": 100,
                "cached_input_tokens": 90,
                "output_tokens": 10,
                "reasoning_output_tokens": 4,
                "prompt_chars": 200,
                "event_bytes": 300,
                "stderr_bytes": 0,
                "event_count": 4,
                "tool_call_count": 2,
                "duration_seconds": 1.5,
            }
            budget = {
                "budget_tokens": 24_000,
                "estimated_tokens": 20_000,
                "trim_reasons": {"events": 2},
            }

            first = store.record_harness_usage(
                run_id=run_id,
                run_stage="repair",
                harness="codex-sdk",
                model="model-a",
                session_resume="resumed",
                portable_context_fallback=False,
                metrics=metrics,
                budget=budget,
            )
            repeated = store.record_harness_usage(
                run_id=run_id,
                run_stage="repair",
                harness="codex-sdk",
                model="model-a",
                session_resume="resumed",
                portable_context_fallback=False,
                metrics=metrics,
                budget=budget,
            )
            rows = store.harness_usage_rows("OWNER/REPO", pull_number=12)
            statistics = store.storage_statistics(repository="owner/repo")

            self.assertEqual(first["id"], repeated["id"])
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(repeated["work_item_id"], work_item_id)
            self.assertNotIn("payload", repeated)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source"], "ledger")
            self.assertEqual(rows[0]["metrics"], metrics)
            self.assertNotIn("agent_metrics", rows[0])
            self.assertEqual(
                next(
                    value["records"]
                    for value in statistics
                    if value["category"] == "harness_usage_ledger"
                ),
                1,
            )

            with self.assertRaisesRegex(StoreError, "already recorded differently"):
                store.record_harness_usage(
                    run_id=run_id,
                    run_stage="repair",
                    harness="codex-sdk",
                    model="model-a",
                    session_resume="resumed",
                    portable_context_fallback=False,
                    metrics={**metrics, "input_tokens": 101},
                    budget=budget,
                )

            second_run, _ = _usage_run(store)
            with self.assertRaisesRegex(ValueError, "duration_seconds"):
                store.record_harness_usage(
                    run_id=second_run,
                    run_stage="prepare",
                    harness="codex-sdk",
                    model="model-a",
                    session_resume="not_requested",
                    portable_context_fallback=False,
                    metrics={**metrics, "duration_seconds": float("nan")},
                    budget=budget,
                )

            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE harness_usage_events SET payload='{}' WHERE run_id=?",
                    (run_id,),
                )
            with self.assertRaisesRegex(StoreError, "ledger was modified"):
                store.harness_usage_rows("owner/repo")

    def test_legacy_harness_usage_keeps_missing_metrics_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            _usage_run(
                store,
                details={
                    "pr_url": "https://github.com/owner/repo/pull/12",
                    "agent_metrics": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                    },
                },
            )

            rows = store.harness_usage_rows("owner/repo")

        self.assertEqual(rows[0]["source"], "legacy")
        self.assertEqual(rows[0]["metrics"]["input_tokens"], 100)
        self.assertIsNone(rows[0]["metrics"]["cached_input_tokens"])
        self.assertEqual(rows[0]["session_resume"], "unknown")
        self.assertIsNone(rows[0]["portable_context_fallback"])

    def test_owner_review_attestations_are_idempotent_and_tamper_evident(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            run_id = store.start_run("owner/repo", 7, "merge")
            facts = _owner_review_facts(run_id)

            first = store.append_owner_review_attestation(facts=facts)
            repeated = store.append_owner_review_attestation(facts=facts)
            history = store.owner_review_attestations("OWNER/REPO", 12)
            latest = store.latest_owner_review_attestation("owner/repo", 12)
            statistics = store.storage_statistics(repository="owner/repo")

            self.assertEqual(first["id"], repeated["id"])
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(len(history), 1)
            assert latest is not None
            self.assertEqual(latest["facts"], facts)
            self.assertEqual(
                next(
                    value["records"]
                    for value in statistics
                    if value["category"] == "owner_review_attestation_audit"
                ),
                1,
            )

            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE owner_review_attestations SET payload='{}' WHERE id=?",
                    (first["id"],),
                )
            with self.assertRaisesRegex(StoreError, "attestation was modified"):
                store.latest_owner_review_attestation("owner/repo", 12)

    def test_dependency_attestations_are_append_only_idempotent_and_verified(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = Store(path)
            confirmed = store.append_portfolio_dependency_event(
                repository="Owner/Repo",
                pull_number=12,
                dependency_number=11,
                head_sha="a" * 40,
                action="confirm",
                actor="alice",
            )
            repeated = store.append_portfolio_dependency_event(
                repository="owner/repo",
                pull_number=12,
                dependency_number=11,
                head_sha="a" * 40,
                action="confirm",
                actor="alice",
            )
            revoked = store.append_portfolio_dependency_event(
                repository="owner/repo",
                pull_number=12,
                dependency_number=11,
                head_sha="b" * 40,
                action="revoke",
                actor="alice",
            )
            repeated_revoke = store.append_portfolio_dependency_event(
                repository="owner/repo",
                pull_number=12,
                dependency_number=11,
                head_sha="b" * 40,
                action="revoke",
                actor="alice",
            )
            reconfirmed = store.append_portfolio_dependency_event(
                repository="owner/repo",
                pull_number=12,
                dependency_number=11,
                head_sha="a" * 40,
                action="confirm",
                actor="alice",
            )
            latest = store.latest_portfolio_dependency_events("OWNER/REPO")
            history = store.portfolio_dependency_events("owner/repo", pull_number=12)
            statistics = store.storage_statistics(repository="owner/repo")

            self.assertEqual(confirmed["id"], repeated["id"])
            self.assertTrue(repeated["idempotent"])
            self.assertNotEqual(confirmed["id"], revoked["id"])
            self.assertEqual(revoked["id"], repeated_revoke["id"])
            self.assertTrue(repeated_revoke["idempotent"])
            self.assertNotEqual(confirmed["id"], reconfirmed["id"])
            self.assertEqual(latest[0]["action"], "confirm")
            self.assertEqual(
                [value["action"] for value in history],
                ["confirm", "revoke", "confirm"],
            )
            self.assertEqual(
                next(
                    value["records"]
                    for value in statistics
                    if value["category"] == "portfolio_dependency_audit"
                ),
                3,
            )

            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "UPDATE portfolio_dependency_events SET payload='{}' WHERE id=?",
                    (reconfirmed["id"],),
                )
            with self.assertRaisesRegex(StoreError, "dependency audit was modified"):
                store.latest_portfolio_dependency_events("owner/repo")

    def test_version_six_database_moves_event_payloads_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            payload = json.dumps(
                {"id": 21, "author": "reviewer", "body": "keep me"},
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(payload.encode()).hexdigest()
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE github_pr_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        repository TEXT NOT NULL,
                        pull_number INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        external_id TEXT NOT NULL,
                        version_digest TEXT NOT NULL,
                        head_sha TEXT NOT NULL DEFAULT '',
                        source_trust TEXT NOT NULL DEFAULT 'github_untrusted',
                        source_created_at TEXT NOT NULL DEFAULT '',
                        source_updated_at TEXT NOT NULL DEFAULT '',
                        payload TEXT NOT NULL,
                        ingested_at TEXT NOT NULL,
                        UNIQUE(repository, pull_number, event_type, external_id,
                               version_digest)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE github_pr_watermarks (
                        run_id TEXT PRIMARY KEY,
                        repository TEXT NOT NULL,
                        pull_number INTEGER NOT NULL,
                        sequence INTEGER NOT NULL DEFAULT 0,
                        batch_digest TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO github_pr_events(
                        repository, pull_number, event_type, external_id,
                        version_digest, payload, ingested_at
                    ) VALUES ('owner/repo', 12, 'issue_comment', '21', ?, ?,
                              '2026-08-20T00:00:00Z')
                    """,
                    (digest, payload),
                )
                connection.execute("PRAGMA user_version=6")

            migrated = Store(path)
            events = migrated.github_pr_events("owner/repo", 12)
            with closing(sqlite3.connect(path)) as connection, connection:
                stored = connection.execute(
                    """
                    SELECT e.payload, e.payload_digest, e.source_actor,
                           b.size_bytes
                    FROM github_pr_events e
                    JOIN content_blobs b ON b.digest=e.payload_digest
                    """
                ).fetchone()

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertEqual(events[0]["payload"]["body"], "keep me")
            self.assertTrue(events[0]["payload_available"])
            self.assertEqual(stored, ("", digest, "reviewer", len(payload.encode())))

    def test_version_seven_database_receives_gc_tombstones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DROP TABLE content_blob_tombstones")
                connection.execute("DROP INDEX github_pr_watermarks_for_pull")
                connection.execute("PRAGMA user_version=7")

            migrated = Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                table = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='content_blob_tombstones'
                    """
                ).fetchone()
                index = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='index' AND name='github_pr_watermarks_for_pull'
                    """
                ).fetchone()

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertIsNotNone(table)
            self.assertIsNotNone(index)

    def test_version_eight_database_receives_append_only_gc_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            Store(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DROP TABLE storage_gc_runs")
                connection.execute("PRAGMA user_version=8")

            store = Store(path)
            first = store.record_storage_gc(
                repository="owner/repo",
                actor="operator",
                stage="applying",
                plan_digest="a" * 64,
                payload={"candidates": ["blob"]},
            )
            second = store.record_storage_gc(
                repository="owner/repo",
                actor="operator",
                stage="completed",
                plan_digest="a" * 64,
                payload={"deleted": ["blob"]},
            )
            records = store.storage_gc_runs()
            schema_version = store.schema_version()

        self.assertEqual(schema_version, SCHEMA_VERSION)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(
            [value["stage"] for value in records], ["completed", "applying"]
        )

    def test_run_gc_safety_includes_workspace_recovery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            work_item = store.ensure_work_item(
                "owner/repo",
                kind="github_issue",
                external_id="7",
                title="Workspace lifecycle",
            )
            run_id = store.start_run("owner/repo", 7, "agent")
            context = _context_payload(
                pack_id="pack-workspace",
                work_item_id=work_item["id"],
                run_id=run_id,
                source_digest="a" * 64,
                base_commit="b" * 40,
            )
            store.save_context_pack(
                pack_id="pack-workspace",
                work_item_id=work_item["id"],
                run_id=run_id,
                schema_version=1,
                source_digest=_context_source_digest("a" * 64),
                base_commit="b" * 40,
                payload=context,
            )
            store.update_run(
                run_id,
                status="submitted",
                worktree="/tmp/reposteward-workspace",
                details={"commit_sha": "c" * 40},
            )
            store.save_checkpoint(
                work_item_id=work_item["id"],
                run_id=run_id,
                context_pack_id="pack-workspace",
                status="submitted",
                payload=_checkpoint_payload(
                    work_item_id=work_item["id"],
                    run_id=run_id,
                    pack_id="pack-workspace",
                    status="submitted",
                ),
            )

            safety = store.run_gc_safety()[run_id]

        self.assertEqual(safety["repository"], "owner/repo")
        self.assertEqual(safety["status"], "submitted")
        self.assertEqual(safety["worktree"], "/tmp/reposteward-workspace")
        self.assertEqual(safety["head_commit"], "c" * 40)
        self.assertTrue(safety["terminal_checkpoint"])

    def test_event_payload_gc_requires_retention_and_every_run_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            activity = {
                "pull_request": {
                    "number": 12,
                    "head_sha": "a" * 40,
                    "state": "open",
                },
                "comments": [],
                "reviews": [],
                "review_comments": [],
                "checks": [],
            }

            def new_run() -> tuple[str, dict]:
                run_id = store.start_run("owner/repo", 7, "pull_request")
                store.update_run(
                    run_id,
                    status="submitted",
                    details={"pr_url": "https://github.com/owner/repo/pull/12"},
                )
                batch = store.ingest_github_pr_activity(
                    run_id=run_id,
                    repository="owner/repo",
                    pull_number=12,
                    activity=activity,
                )
                return run_id, batch

            first_run, first = new_run()
            store.commit_github_follow_up(
                run_id=first_run,
                repository="owner/repo",
                pull_number=12,
                previous_sequence=first["previous_sequence"],
                through_sequence=first["through_sequence"],
                batch_digest=first["batch_digest"],
            )
            no_policy = store.event_payload_gc_inventory({})
            second_run, second = new_run()
            blocked = store.event_payload_gc_inventory(
                {"owner/repo": "2099-01-01T00:00:00Z"}
            )
            store.commit_github_follow_up(
                run_id=second_run,
                repository="owner/repo",
                pull_number=12,
                previous_sequence=second["previous_sequence"],
                through_sequence=second["through_sequence"],
                batch_digest=second["batch_digest"],
            )
            eligible = store.event_payload_gc_inventory(
                {"owner/repo": "2099-01-01T00:00:00Z"}
            )
            digest = eligible["candidates"][0]["digest"]
            deleted = store.delete_event_payloads(
                (digest,),
                retention_cutoffs={"owner/repo": "2099-01-01T00:00:00Z"},
            )
            repeated = store.delete_event_payloads(
                (digest,),
                retention_cutoffs={"owner/repo": "2099-01-01T00:00:00Z"},
            )
            _third_run, third = new_run()
            events = store.github_pr_events("owner/repo", 12)
            blob = store.content_blob(digest)

        self.assertEqual(
            no_policy["retained"][0]["reasons"],
            ["no_explicit_event_retention"],
        )
        self.assertIn(
            "not_checkpointed_by_every_run", blocked["retained"][0]["reasons"]
        )
        self.assertEqual(len(eligible["candidates"]), 1)
        self.assertEqual(len(deleted["deleted"]), 1)
        self.assertEqual(repeated["skipped"], [digest])
        self.assertFalse(third["events"][0]["payload_available"])
        self.assertTrue(third["events"][0]["payload"]["payload_unavailable"])
        self.assertFalse(events[0]["payload_available"])
        self.assertIsNone(blob)

    def test_successor_run_starts_at_committed_event_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            activity = {
                "pull_request": {"number": 12, "head_sha": "a" * 40},
                "comments": [{"id": 1, "body": "fix this"}],
                "reviews": [],
                "review_comments": [],
                "checks": [],
            }
            source = store.start_run("owner/repo", 7, "pull_request")
            batch = store.ingest_github_pr_activity(
                run_id=source,
                repository="owner/repo",
                pull_number=12,
                activity=activity,
            )
            successor = store.start_run("owner/repo", 7, "repair")
            store.seed_github_pr_watermark(
                run_id=successor,
                repository="owner/repo",
                pull_number=12,
                sequence=batch["through_sequence"],
                batch_digest=batch["batch_digest"],
            )
            repeated = store.ingest_github_pr_activity(
                run_id=successor,
                repository="owner/repo",
                pull_number=12,
                activity=activity,
            )

        self.assertEqual(repeated["events"], [])

    def test_merge_decision_audit_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            snapshot = MergeSnapshot(
                repository="owner/repo",
                pull_number=12,
                head_sha="c" * 40,
                base_sha="d" * 40,
                policy_digest="e" * 64,
                state="OPEN",
                draft=False,
                mergeable="MERGEABLE",
                review_decision="APPROVED",
                unresolved_conversations=0,
                files=("src/example.py",),
                additions=1,
                deletions=0,
                checks=(MergeCheck("quality", "COMPLETED", "SUCCESS"),),
                activity_digest="f" * 64,
            )
            evaluated = evaluate_merge(
                snapshot,
                expected_head_sha="c" * 40,
                expected_base_sha="d" * 40,
                expected_policy_digest="e" * 64,
                max_files_changed=18,
                max_diff_lines=700,
            )
            decision = {**evaluated.to_dict(), "snapshot": asdict(snapshot)}

            first = store.append_merge_decision(
                repository="Owner/Repo",
                pull_number=12,
                head_sha="c" * 40,
                base_sha="d" * 40,
                policy_digest="e" * 64,
                decision=decision,
            )
            second = store.append_merge_decision(
                repository="Owner/Repo",
                pull_number=12,
                head_sha="c" * 40,
                base_sha="d" * 40,
                policy_digest="e" * 64,
                decision=decision,
            )
            audit = store.merge_decisions("owner/repo", 12)
            loaded = store.merge_decision(first["id"])
            run_id = store.start_run("owner/repo", 7, "merge")
            intent = store.append_merge_execution(
                attempt_id="attempt-1",
                run_id=run_id,
                decision_id=first["id"],
                repository="owner/repo",
                pull_number=12,
                actor="alice",
                merge_method="squash",
                stage="applying",
                outcome="pending",
                reason="fresh decision",
                decision_digest=evaluated.decision_digest,
                head_sha="c" * 40,
                payload={"phase": "before_write"},
            )
            completed = store.append_merge_execution(
                attempt_id="attempt-1",
                run_id=run_id,
                decision_id=first["id"],
                repository="owner/repo",
                pull_number=12,
                actor="alice",
                merge_method="squash",
                stage="completed",
                outcome="merged",
                reason="merged",
                decision_digest=evaluated.decision_digest,
                head_sha="c" * 40,
                payload={"sha": "f" * 40},
            )
            executions = store.merge_executions("owner/repo", 12)
            statistics = store.storage_statistics(repository="owner/repo")
            with self.assertRaisesRegex(ValueError, "stage and outcome"):
                store.append_merge_execution(
                    attempt_id="attempt-invalid-stage",
                    run_id=run_id,
                    decision_id=first["id"],
                    repository="owner/repo",
                    pull_number=12,
                    actor="alice",
                    merge_method="squash",
                    stage="applying",
                    outcome="merged",
                    reason="invalid",
                    decision_digest=evaluated.decision_digest,
                    head_sha="c" * 40,
                    payload={},
                )
            with self.assertRaisesRegex(StoreError, "run and decision"):
                store.append_merge_execution(
                    attempt_id="attempt-mismatched-decision",
                    run_id=run_id,
                    decision_id=first["id"],
                    repository="owner/repo",
                    pull_number=12,
                    actor="alice",
                    merge_method="squash",
                    stage="completed",
                    outcome="blocked",
                    reason="invalid",
                    decision_digest="0" * 64,
                    head_sha="c" * 40,
                    payload={},
                )
            with self.assertRaisesRegex(StoreError, "already contains"):
                store.append_merge_execution(
                    attempt_id="attempt-1",
                    run_id=run_id,
                    decision_id=first["id"],
                    repository="owner/repo",
                    pull_number=12,
                    actor="alice",
                    merge_method="squash",
                    stage="applying",
                    outcome="pending",
                    reason="duplicate intent",
                    decision_digest=evaluated.decision_digest,
                    head_sha="c" * 40,
                    payload={},
                )

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(audit), 2)
        self.assertTrue(all(value["eligible"] for value in audit))
        assert loaded is not None
        self.assertEqual(loaded["decision_digest"], evaluated.decision_digest)
        self.assertNotEqual(intent["id"], completed["id"])
        self.assertEqual(len(executions), 2)
        self.assertEqual(
            {value["stage"] for value in executions}, {"applying", "completed"}
        )
        by_category = {value["category"]: value for value in statistics}
        self.assertEqual(by_category["merge_execution_audit"]["records"], 2)

    def test_publication_attempts_are_bounded_append_only_and_recoverable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            run_id = store.start_run("owner/repo", 7, "publication")
            lease_one = store.acquire_run_lease("issue:owner/repo#7", owner="worker-1")
            identity = {
                "attempt_id": "attempt-1",
                "step_id": "step-1",
                "run_id": run_id,
                "repository": "Owner/Repo",
                "issue_number": 7,
                "actor": "alice",
                "action": "push",
                "destination": "Owner/Repo",
                "branch": "alice/feat/example",
                "head_sha": "a" * 40,
                "base_branch": "main",
                "expected_remote_sha": "b" * 40,
                "target_pull_number": 12,
            }
            applying = store.append_publication_attempt(
                **identity,
                stage="applying",
                outcome="pending",
                lease=lease_one,
                payload={},
            )
            pending = store.incomplete_publication_attempts(run_id)
            store.release_run_lease(lease_one)
            lease_two = store.acquire_run_lease("issue:owner/repo#7", owner="worker-2")
            completed = store.append_publication_attempt(
                **identity,
                stage="completed",
                outcome="reconciled",
                lease=lease_two,
                payload={
                    "public_write": False,
                    "remote_head_sha": "a" * 40,
                    "reconciliation": "remote_branch_read",
                },
            )
            records = store.publication_attempts(run_id)
            remaining = store.incomplete_publication_attempts(run_id)
            action_completed = store.publication_action_completed(
                run_id, action="push", target_pull_number=12
            )
            other_pull_completed = store.publication_action_completed(
                run_id, action="push", target_pull_number=13
            )
            statistics = {
                value["category"]: value
                for value in store.storage_statistics(repository="owner/repo")
            }

            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                store.append_publication_attempt(
                    **{**identity, "step_id": "step-secret"},
                    stage="applying",
                    outcome="pending",
                    lease=lease_two,
                    payload={"token": "must-not-be-stored"},
                )
            with self.assertRaisesRegex(StoreError, "already contains"):
                store.append_publication_attempt(
                    **identity,
                    stage="completed",
                    outcome="succeeded",
                    lease=lease_two,
                    payload={"public_write": True},
                )
            with self.assertRaisesRegex(StoreError, "lease is stale"):
                store.append_publication_attempt(
                    **{**identity, "step_id": "step-stale"},
                    stage="applying",
                    outcome="pending",
                    lease=lease_one,
                    payload={},
                )

        self.assertEqual(len(pending), 1)
        self.assertEqual(remaining, [])
        self.assertEqual(applying["lease_generation"], 1)
        self.assertEqual(completed["lease_generation"], 2)
        self.assertEqual(
            [value["stage"] for value in records], ["applying", "completed"]
        )
        self.assertTrue(action_completed)
        self.assertFalse(other_pull_completed)
        self.assertEqual(statistics["publication_attempt_audit"]["records"], 2)

    def test_merge_decision_audit_rejects_tampered_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            decision = {
                "eligible": True,
                "snapshot_digest": "a" * 64,
                "decision_digest": "b" * 64,
                "reasons": [],
                "risk_categories": [],
                "risk_files": [],
                "snapshot": {},
            }

            with self.assertRaisesRegex(StoreError, "snapshot.*digest"):
                store.append_merge_decision(
                    repository="owner/repo",
                    pull_number=12,
                    head_sha="c" * 40,
                    base_sha="d" * 40,
                    policy_digest="e" * 64,
                    decision=decision,
                )

    def test_merge_decision_reader_rejects_database_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            snapshot = MergeSnapshot(
                repository="owner/repo",
                pull_number=12,
                head_sha="c" * 40,
                base_sha="d" * 40,
                policy_digest="e" * 64,
                state="OPEN",
                draft=False,
                mergeable="MERGEABLE",
                review_decision="APPROVED",
                unresolved_conversations=0,
                files=("src/example.py",),
                additions=1,
                deletions=0,
                checks=(MergeCheck("quality", "COMPLETED", "SUCCESS"),),
                activity_digest="f" * 64,
            )
            evaluated = evaluate_merge(
                snapshot,
                expected_head_sha="c" * 40,
                expected_base_sha="d" * 40,
                expected_policy_digest="e" * 64,
                max_files_changed=18,
                max_diff_lines=700,
            )
            audit = store.append_merge_decision(
                repository="owner/repo",
                pull_number=12,
                head_sha="c" * 40,
                base_sha="d" * 40,
                policy_digest="e" * 64,
                decision={**evaluated.to_dict(), "snapshot": asdict(snapshot)},
            )
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE merge_decisions SET decision_digest=? WHERE id=?",
                    ("0" * 64, audit["id"]),
                )

            with self.assertRaisesRegex(StoreError, "result was modified"):
                store.merge_decision(audit["id"])

    def test_candidate_round_trip_and_status_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            candidate = Candidate(
                issue=Issue(
                    repository="owner/repo",
                    number=1,
                    node_id=2,
                    title="A bug",
                    body="Reproduction details",
                    url="https://github.com/owner/repo/issues/1",
                    labels=("bug",),
                    comments=0,
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    author_login="user",
                    author_association="NONE",
                ),
                repository=RepositoryInfo(
                    full_name="owner/repo",
                    default_branch="main",
                    stars=1000,
                    forks=20,
                    open_issues=5,
                    pushed_at="2026-01-01T00:00:00Z",
                    archived=False,
                    is_fork=False,
                ),
                score=42,
            )
            store.upsert_candidate(candidate)
            store.set_candidate_status("owner/repo", 1, "ready")
            store.upsert_candidate(candidate)

            restored = store.candidate("owner/repo", 1)
            ready = store.candidates(status="ready")

        self.assertIsNotNone(restored)
        if restored is None:
            self.fail("candidate was not restored")
        self.assertEqual(restored.issue.labels, ("bug",))
        self.assertEqual(len(ready), 1)

    def test_issue_draft_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")

            created = store.create_issue_draft(
                "Owner/Repo", "Example issue", "## Summary\n\nDetails\n"
            )
            restored = store.issue_draft(created["id"])
            listing = store.issue_drafts()

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored["repository"], "owner/repo")
        self.assertEqual(restored["title"], "Example issue")
        self.assertEqual(listing[0]["id"], created["id"])

    def test_issue_proposal_round_trip_and_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            created = store.record_issue_proposal(
                project_item_id="PVTI_example",
                project_id="PVT_example",
                project_url="https://example.test/project/1",
                draft_id="a" * 32,
                repository="Owner/Repo",
                creator="alice",
                content_digest="b" * 64,
            )
            restored = store.issue_proposal_for_draft("PVT_example", "a" * 32)
            store.mark_issue_proposal_published(
                "PVTI_example",
                issue_number=42,
                issue_url="https://example.test/owner/repo/issues/42",
            )
            published = store.issue_proposal_for_draft("PVT_example", "a" * 32)

        self.assertEqual(created["repository"], "owner/repo")
        self.assertIsNotNone(restored)
        assert published is not None
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["issue_number"], 42)

    def test_context_bundle_round_trip_uses_latest_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            work_item = store.ensure_work_item(
                "Owner/Repo",
                kind="github_issue",
                external_id="7",
                title="Example issue",
                payload={"url": "https://github.com/owner/repo/issues/7"},
            )
            run_id = store.start_run("owner/repo", 7, "agent")
            context = _context_payload(
                pack_id="pack-1",
                work_item_id=work_item["id"],
                run_id=run_id,
                source_digest="a" * 64,
                base_commit="b" * 40,
            )
            store.save_context_pack(
                pack_id="pack-1",
                work_item_id=work_item["id"],
                run_id=run_id,
                schema_version=1,
                source_digest=_context_source_digest("a" * 64),
                base_commit="b" * 40,
                payload=context,
            )
            store.bind_harness_run(
                run_id,
                work_item_id=work_item["id"],
                context_pack_id="pack-1",
                harness="codex-cli",
                model="gpt-example",
            )
            first = store.save_checkpoint(
                work_item_id=work_item["id"],
                run_id=run_id,
                context_pack_id="pack-1",
                status="running",
                payload=_checkpoint_payload(
                    work_item_id=work_item["id"],
                    run_id=run_id,
                    pack_id="pack-1",
                    status="running",
                ),
            )
            second = store.save_checkpoint(
                work_item_id=work_item["id"],
                run_id=run_id,
                context_pack_id="pack-1",
                status="ready",
                payload=_checkpoint_payload(
                    work_item_id=work_item["id"],
                    run_id=run_id,
                    pack_id="pack-1",
                    status="ready",
                ),
            )

            bundle = store.context_bundle(run_id)

        self.assertEqual(first["sequence"], 1)
        self.assertEqual(second["sequence"], 2)
        self.assertIsNotNone(bundle)
        assert bundle is not None
        self.assertEqual(bundle["work_item"]["repository"], "owner/repo")
        self.assertEqual(bundle["harness_run"]["harness"], "codex-cli")
        self.assertEqual(bundle["checkpoint"]["status"], "ready")
        self.assertEqual(json.dumps(bundle["context_pack"]), json.dumps(context))

    def test_github_event_versions_and_checkpoint_watermark_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            work_item = store.ensure_work_item(
                "owner/repo",
                kind="github_issue",
                external_id="7",
                title="Example issue",
            )
            run_id = store.start_run("owner/repo", 7, "pull_request")
            store.update_run(
                run_id,
                status="submitted",
                details={
                    "commit_sha": "a" * 40,
                    "pr_url": "https://github.com/owner/repo/pull/12",
                },
            )
            context = _context_payload(
                pack_id="pack-1",
                work_item_id=work_item["id"],
                run_id=run_id,
                source_digest="a" * 64,
                base_commit="b" * 40,
            )
            store.save_context_run(
                pack_id="pack-1",
                work_item_id=work_item["id"],
                run_id=run_id,
                schema_version=1,
                source_digest=_context_source_digest("a" * 64),
                base_commit="b" * 40,
                payload=context,
                harness="codex-cli",
            )
            activity = {
                "pull_request": {
                    "number": 12,
                    "url": "https://github.com/owner/repo/pull/12",
                    "head_sha": "a" * 40,
                },
                "comments": [
                    {
                        "id": 21,
                        "author": "reviewer",
                        "body": "first",
                        "created_at": "2026-08-20T00:00:00Z",
                        "updated_at": "2026-08-20T00:00:00Z",
                    }
                ],
                "reviews": [],
                "review_comments": [],
                "checks": [],
            }

            first = store.ingest_github_pr_activity(
                run_id=run_id,
                repository="owner/repo",
                pull_number=12,
                activity=activity,
            )
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("DELETE FROM content_blobs")
            repeated = store.ingest_github_pr_activity(
                run_id=run_id,
                repository="owner/repo",
                pull_number=12,
                activity=activity,
            )
            checkpoint = _checkpoint_payload(
                work_item_id=work_item["id"],
                run_id=run_id,
                pack_id="pack-1",
                status="submitted",
            )
            committed = store.commit_github_follow_up(
                run_id=run_id,
                repository="owner/repo",
                pull_number=12,
                previous_sequence=first["previous_sequence"],
                through_sequence=first["through_sequence"],
                batch_digest=first["batch_digest"],
                checkpoint=checkpoint,
            )
            after_commit = store.ingest_github_pr_activity(
                run_id=run_id,
                repository="owner/repo",
                pull_number=12,
                activity=activity,
            )
            activity["comments"][0]["body"] = "edited"
            activity["comments"][0]["updated_at"] = "2026-08-21T00:00:00Z"
            edited = store.ingest_github_pr_activity(
                run_id=run_id,
                repository="owner/repo",
                pull_number=12,
                activity=activity,
            )
            events = store.github_pr_events("owner/repo", 12)
            statistics = store.storage_statistics(repository="owner/repo")
            watermark = store.github_pr_watermark(run_id)
            bundle = store.context_bundle(run_id)
            with closing(sqlite3.connect(store.path)) as connection, connection:
                blob_count = connection.execute(
                    "SELECT COUNT(*) FROM content_blobs"
                ).fetchone()[0]
                inline_bytes = connection.execute(
                    "SELECT SUM(length(payload)) FROM github_pr_events"
                ).fetchone()[0]

        self.assertEqual(len(first["events"]), 2)
        self.assertEqual(repeated["batch_digest"], first["batch_digest"])
        self.assertEqual(len(repeated["events"]), 2)
        self.assertTrue(all(event["payload_available"] for event in repeated["events"]))
        self.assertFalse(committed["idempotent"])
        self.assertEqual(committed["checkpoint"]["sequence"], 1)
        self.assertEqual(after_commit["events"], [])
        self.assertEqual(len(edited["events"]), 1)
        self.assertEqual(edited["events"][0]["event_type"], "issue_comment")
        self.assertEqual(len(events), 3)
        self.assertEqual(blob_count, 3)
        self.assertEqual(inline_bytes, 0)
        self.assertTrue(all(event["payload_available"] for event in events))
        by_category = {value["category"]: value for value in statistics}
        self.assertEqual(by_category["github_event_index"]["records"], 3)
        self.assertEqual(by_category["github_event_payload"]["records"], 3)
        self.assertGreater(by_category["checkpoint"]["bytes"], 0)
        self.assertTrue(
            all(event["source_trust"] == "github_untrusted" for event in events)
        )
        self.assertEqual(events[1]["source_actor"], "reviewer")
        self.assertEqual(
            [event["payload"]["body"] for event in events[1:]],
            ["first", "edited"],
        )
        assert watermark is not None
        self.assertEqual(watermark["sequence"], first["through_sequence"])
        assert bundle is not None
        self.assertEqual(bundle["checkpoint"]["status"], "submitted")

    def test_context_pack_and_harness_binding_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = Store(path)
            work_item = store.ensure_work_item(
                "owner/repo",
                kind="github_issue",
                external_id="7",
                title="Example issue",
            )
            run_id = store.start_run("owner/repo", 7, "agent")
            store.save_context_pack(
                pack_id="existing-pack",
                work_item_id=work_item["id"],
                run_id=run_id,
                schema_version=1,
                source_digest=_context_source_digest("a" * 64),
                base_commit="b" * 40,
                payload=_context_payload(
                    pack_id="existing-pack",
                    work_item_id=work_item["id"],
                    run_id=run_id,
                    source_digest="a" * 64,
                    base_commit="b" * 40,
                ),
            )
            store.bind_harness_run(
                run_id,
                work_item_id=work_item["id"],
                context_pack_id="existing-pack",
                harness="codex-cli",
            )
            payload = _context_payload(
                pack_id="rolled-back-pack",
                work_item_id=work_item["id"],
                run_id=run_id,
                source_digest="c" * 64,
                base_commit="d" * 40,
            )

            with self.assertRaises(sqlite3.IntegrityError):
                store.save_context_run(
                    pack_id="rolled-back-pack",
                    work_item_id=work_item["id"],
                    run_id=run_id,
                    schema_version=1,
                    source_digest=_context_source_digest("c" * 64),
                    base_commit="d" * 40,
                    payload=payload,
                    harness="codex-cli",
                )
            with closing(sqlite3.connect(path)) as connection, connection:
                retained = connection.execute(
                    "SELECT COUNT(*) FROM context_packs WHERE id='rolled-back-pack'"
                ).fetchone()[0]

        self.assertEqual(retained, 0)

    def test_context_run_rejects_mismatched_materialized_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            work_item = store.ensure_work_item(
                "owner/repo",
                kind="github_issue",
                external_id="7",
                title="Example issue",
            )
            run_id = store.start_run("owner/repo", 7, "agent")
            payload = _context_payload(
                pack_id="pack-1",
                work_item_id=work_item["id"],
                run_id=run_id,
                source_digest="c" * 64,
                base_commit="b" * 40,
            )

            with self.assertRaisesRegex(StoreError, "source digest"):
                store.save_context_run(
                    pack_id="pack-1",
                    work_item_id=work_item["id"],
                    run_id=run_id,
                    schema_version=1,
                    source_digest="a" * 64,
                    base_commit="b" * 40,
                    payload=payload,
                    harness="codex-cli",
                )

    def test_checkpoint_record_rejects_mismatched_payload_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")

            with self.assertRaisesRegex(StoreError, "checkpoint payload"):
                store.save_checkpoint(
                    work_item_id="work-1",
                    run_id="run-1",
                    context_pack_id="pack-1",
                    status="ready",
                    payload={
                        "work_item_id": "other-work",
                        "run_id": "run-1",
                        "context_pack_id": "pack-1",
                        "status": "ready",
                    },
                )

    def test_checkpoint_cannot_cross_context_run_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            first_work = store.ensure_work_item(
                "owner/repo",
                kind="github_issue",
                external_id="7",
                title="First issue",
            )
            second_work = store.ensure_work_item(
                "owner/repo",
                kind="github_issue",
                external_id="8",
                title="Second issue",
            )
            first_run = store.start_run("owner/repo", 7, "agent")
            second_run = store.start_run("owner/repo", 8, "agent")
            store.save_context_pack(
                pack_id="pack-1",
                work_item_id=first_work["id"],
                run_id=first_run,
                schema_version=1,
                source_digest=_context_source_digest("a" * 64),
                base_commit="b" * 40,
                payload=_context_payload(
                    pack_id="pack-1",
                    work_item_id=first_work["id"],
                    run_id=first_run,
                    source_digest="a" * 64,
                    base_commit="b" * 40,
                ),
            )
            payload = _checkpoint_payload(
                work_item_id=second_work["id"],
                run_id=second_run,
                pack_id="pack-1",
                status="ready",
            )

            with self.assertRaisesRegex(StoreError, "different work item or run"):
                store.save_checkpoint(
                    work_item_id=second_work["id"],
                    run_id=second_run,
                    context_pack_id="pack-1",
                    status="ready",
                    payload=payload,
                )


if __name__ == "__main__":
    unittest.main()
