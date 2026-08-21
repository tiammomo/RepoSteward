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


class StoreTests(unittest.TestCase):
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

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(audit), 2)
        self.assertTrue(all(value["eligible"] for value in audit))

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
