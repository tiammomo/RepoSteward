from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from reposteward.lifecycle import (
    MAX_TEXT_CHARS,
    SOURCE_ORDER,
    LifecycleTraceError,
    build_lifecycle_trace,
    render_lifecycle_text,
)
from reposteward.store import Store


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _dump(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return "\n".join(connection.iterdump())


def _seed_lifecycle(state_dir: Path) -> tuple[Path, str, str]:
    database = state_dir / "reposteward.sqlite3"
    store = Store(database)
    work_item = store.ensure_work_item(
        "owner/repo",
        kind="github_issue",
        external_id="7",
        title="A secret title that must not be rendered",
        payload={"prompt": "ghp_secret_token"},
    )
    run_one = store.start_run("owner/repo", 7, "agent")
    run_two = store.start_run("owner/repo", 7, "repair")
    store.update_run(
        run_one,
        status="ready",
        worktree="/tmp/private-workspace",
        details={
            "commit_sha": "1" * 40,
            "worktree": "/tmp/private-workspace",
            "prompt": "ghp_secret_token",
            "verification": {
                "passed": True,
                "commands": [
                    {
                        "command": "print ghp_secret_token",
                        "exit_code": 0,
                        "output": "ghp_secret_token",
                    }
                ],
            },
        },
    )
    store.update_run(
        run_two,
        status="failed",
        details={
            "source_run_id": run_one,
            "verification": {
                "passed": False,
                "commands": [{"command": "pytest", "exit_code": 1}],
            },
        },
    )

    policy_digest = "a" * 64
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE work_items SET created_at='2026-01-01T00:00:00Z',
                                  updated_at='2026-01-01T00:00:30Z'
            WHERE id=?
            """,
            (work_item["id"],),
        )
        for index, run_id in enumerate((run_one, run_two), start=1):
            created_at = f"2026-01-01T00:0{index}:00Z"
            context_at = f"2026-01-01T00:0{index}:10Z"
            checkpoint_at = f"2026-01-01T00:0{index}:20Z"
            usage_at = f"2026-01-01T00:0{index}:30Z"
            pack_id = f"pack-{index}"
            checkpoint_id = f"checkpoint-{index}"
            context_payload = {
                "sources": [{"kind": "repository_policy", "digest": policy_digest}],
                "prompt": "ghp_secret_token",
            }
            checkpoint_payload = {
                "head_commit": str(index) * 40,
                "next_action": "human_review" if index == 1 else "diagnose_failure",
                "completed": ["implementation"],
                "remaining": ["review"],
                "blockers": [] if index == 1 else ["tests"],
                "evidence": ["verification"],
                "prompt": "ghp_secret_token",
            }
            usage_payload = {
                "run_id": run_id,
                "work_item_id": str(work_item["id"]),
                "repository": "owner/repo",
                "issue_number": 7,
                "run_stage": "prepare" if index == 1 else "repair",
                "harness": "codex-cli",
                "model": "model-a",
                "metrics": {
                    "input_tokens": 999,
                    "prompt_chars": 999,
                    "event_count": index,
                    "tool_call_count": index + 1,
                    "duration_seconds": 2.5 * index,
                },
            }
            connection.execute(
                "UPDATE runs SET created_at=?, updated_at=? WHERE id=?",
                (created_at, usage_at, run_id),
            )
            connection.execute(
                """
                INSERT INTO context_packs(
                    id, work_item_id, run_id, schema_version, source_digest,
                    base_commit, payload, created_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    pack_id,
                    work_item["id"],
                    run_id,
                    "b" * 64,
                    "0" * 40,
                    json.dumps(context_payload),
                    context_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO harness_runs(
                    run_id, work_item_id, context_pack_id, harness, model,
                    native_session_id, created_at
                ) VALUES (?, ?, ?, 'codex-cli', 'model-a', ?, ?)
                """,
                (run_id, work_item["id"], pack_id, "private-session", context_at),
            )
            connection.execute(
                """
                INSERT INTO checkpoints(
                    id, work_item_id, run_id, context_pack_id, sequence, status,
                    payload, created_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    work_item["id"],
                    run_id,
                    pack_id,
                    "ready" if index == 1 else "failed",
                    json.dumps(checkpoint_payload),
                    checkpoint_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO harness_usage_events(
                    id, run_id, work_item_id, repository, issue_number, run_stage,
                    harness, model, session_resume, portable_context_fallback,
                    event_digest, payload, created_at
                ) VALUES (?, ?, ?, 'owner/repo', 7, ?, 'codex-cli', 'model-a',
                          'not_requested', 0, ?, ?, ?)
                """,
                (
                    f"usage-{index}",
                    run_id,
                    work_item["id"],
                    "prepare" if index == 1 else "repair",
                    _digest(usage_payload),
                    json.dumps(usage_payload),
                    usage_at,
                ),
            )

        connection.execute(
            """
            INSERT INTO run_lease_events(
                scope, owner, generation, action, expires_at, created_at
            ) VALUES ('issue:owner/repo#7', '123:private-owner', 1, 'acquired',
                      '2026-01-01T00:05:00Z', '2026-01-01T00:00:40Z')
            """
        )
        connection.execute(
            """
            INSERT INTO context_imports(
                id, bundle_digest, work_item_id, source_run_id, payload, imported_at
            ) VALUES (
                'import-1', ?, ?, ?, ?, '2026-01-01T00:02:40Z'
            )
            """,
            (
                "9" * 64,
                work_item["id"],
                run_one,
                json.dumps({"prompt": "ghp_secret_token"}),
            ),
        )
        connection.execute(
            """
            INSERT INTO queue_tasks(
                id, dedupe_key, repository, action, work_item_id, run_id,
                issue_number, pull_number, parameters, parameters_digest,
                idempotency_digest, priority, state, depends_on_task_id,
                max_attempts, attempt_count, manual_required, last_error_code,
                lease_owner, lease_generation, lease_expires_at, available_at,
                created_at, updated_at
            ) VALUES (
                'task-1', 'dedupe-1', 'owner/repo', 'submit', ?, ?, 7, 12,
                '{}', ?, ?, 10, 'completed', NULL, 3, 1, 0, '', '', 0, '',
                '2026-01-01T00:02:45Z', '2026-01-01T00:02:45Z',
                '2026-01-01T00:02:50Z'
            )
            """,
            (work_item["id"], run_one, "4" * 64, "5" * 64),
        )
        connection.execute(
            """
            INSERT INTO queue_attempts(
                id, task_id, generation, worker, event, outcome, payload,
                payload_digest, created_at
            ) VALUES (
                'queue-attempt-1', 'task-1', 1, 'private-worker', 'completed',
                'completed', ?, ?, '2026-01-01T00:02:50Z'
            )
            """,
            (json.dumps({"prompt": "ghp_secret_token"}), "6" * 64),
        )
        connection.execute(
            """
            INSERT INTO submissions(repository, issue_number, pr_url, created_at)
            VALUES ('owner/repo', 7, 'https://github.com/owner/repo/pull/12',
                    '2026-01-01T00:03:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO publication_attempts(
                id, attempt_id, step_id, run_id, repository, issue_number, actor,
                action, stage, outcome, destination, branch, head_sha, base_branch,
                expected_remote_sha, target_pull_number, lease_owner,
                lease_generation, payload, created_at
            ) VALUES (
                'publication-1', 'attempt-1', 'step-1', ?, 'owner/repo', 7,
                'alice', 'create', 'completed', 'succeeded', 'owner/repo',
                'alice/feat/example', ?, 'main', '', 12, 'owner', 1, '{}',
                '2026-01-01T00:03:00Z'
            )
            """,
            (run_one, "1" * 40),
        )
        connection.execute(
            """
            INSERT INTO github_pr_watermarks(
                run_id, repository, pull_number, sequence, batch_digest, updated_at
            ) VALUES (
                ?, 'owner/repo', 12, 1, ?, '2026-01-01T00:03:10Z'
            )
            """,
            (run_one, "7" * 64),
        )
        connection.execute(
            """
            INSERT INTO github_pr_events(
                repository, pull_number, event_type, external_id, version_digest,
                head_sha, source_trust, source_created_at, source_updated_at,
                payload, ingested_at, payload_digest, source_actor, source_state
            ) VALUES (
                'owner/repo', 12, 'pull_request', '12', ?, ?,
                'github_untrusted', '2026-01-01T00:03:15Z',
                '2026-01-01T00:03:20Z', '', '2026-01-01T00:03:21Z', ?,
                'external-user', 'open'
            )
            """,
            ("8" * 64, "1" * 40, "8" * 64),
        )
        connection.execute(
            """
            INSERT INTO portfolio_dependency_events(
                id, repository, pull_number, dependency_number, head_sha, action,
                actor, source, event_digest, payload, created_at
            ) VALUES (
                'dependency-1', 'owner/repo', 12, 11, ?, 'confirm', 'alice',
                'maintainer_attestation', ?, '{}', '2026-01-01T00:03:30Z'
            )
            """,
            ("1" * 40, "3" * 64),
        )
        connection.execute(
            """
            INSERT INTO merge_decisions(
                id, repository, pull_number, head_sha, base_sha, policy_digest,
                snapshot_digest, eligible, decision_digest, payload, created_at
            ) VALUES (
                'decision-1', 'owner/repo', 12, ?, ?, ?, ?, 1, ?, '{}',
                '2026-01-01T00:04:00Z'
            )
            """,
            ("1" * 40, "0" * 40, policy_digest, "c" * 64, "d" * 64),
        )
        connection.execute(
            """
            INSERT INTO owner_review_attestations(
                id, repository, pull_number, run_id, actor, head_sha, base_sha,
                policy_digest, review_facts_digest, attestation_digest, payload,
                created_at
            ) VALUES (
                'review-1', 'owner/repo', 12, ?, 'alice', ?, ?, ?, ?, ?, '{}',
                '2026-01-01T00:04:10Z'
            )
            """,
            (
                run_one,
                "1" * 40,
                "0" * 40,
                policy_digest,
                "e" * 64,
                "f" * 64,
            ),
        )
        connection.execute(
            """
            INSERT INTO merge_executions(
                id, attempt_id, run_id, decision_id, repository, pull_number,
                actor, merge_method, stage, outcome, reason, decision_digest,
                head_sha, payload, created_at
            ) VALUES (
                'merge-1', 'merge-attempt-1', ?, 'decision-1', 'owner/repo', 12,
                'alice', 'squash', 'completed', 'merged', 'merged', ?, ?, '{}',
                '2026-01-01T00:04:20Z'
            )
            """,
            (run_one, "d" * 64, "1" * 40),
        )
        connection.commit()
    return database, run_one, run_two


class LifecycleTraceTests(unittest.TestCase):
    def test_trace_is_stable_bounded_sanitized_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database, run_one, run_two = _seed_lifecycle(state_dir)
            before = _dump(database)

            first = build_lifecycle_trace(state_dir, "OWNER/REPO", 7)
            second = build_lifecycle_trace(state_dir, "owner/repo", 7)
            text = render_lifecycle_text(first)
            after = _dump(database)

        self.assertEqual(first, second)
        self.assertEqual(before, after)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(len(first["trace_digest"]), 64)
        self.assertEqual(first["next_action"], "diagnose_failure")
        self.assertFalse(first["network_access"])
        self.assertFalse(first["harness_invoked"])
        self.assertFalse(first["workspace_modified"])
        self.assertFalse(first["store_modified"])
        self.assertFalse(first["public_write"])
        run_events = [value for value in first["events"] if value["source"] == "run"]
        self.assertEqual(
            {value["facts"]["run_id"] for value in run_events}, {run_one, run_two}
        )
        self.assertTrue(
            {
                "context_import",
                "queue_task",
                "queue_attempt",
                "github_watermark",
                "github_pr",
                "dependency",
                "owner_review",
                "merge_decision",
                "merge_execution",
            }.issubset({value["source"] for value in first["events"]})
        )
        self.assertEqual(
            [
                (
                    value["occurred_at"],
                    SOURCE_ORDER[value["source"]],
                    value["sequence"],
                    value["id"],
                )
                for value in first["events"]
            ],
            sorted(
                (
                    value["occurred_at"],
                    SOURCE_ORDER[value["source"]],
                    value["sequence"],
                    value["id"],
                )
                for value in first["events"]
            ),
        )
        serialized = json.dumps(first, ensure_ascii=False).casefold()
        self.assertNotIn("ghp_secret", serialized)
        self.assertNotIn("private-workspace", serialized)
        self.assertNotIn("private-session", serialized)
        self.assertNotIn("private-worker", serialized)
        self.assertNotIn("input_tokens", serialized)
        self.assertNotIn("prompt_chars", serialized)
        self.assertLessEqual(len(serialized), 1_000_000)
        self.assertLessEqual(len(text), MAX_TEXT_CHARS)
        self.assertIn("Lifecycle: owner/repo#7", text)

    def test_missing_relationships_and_global_limit_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            store = Store(state_dir / "reposteward.sqlite3")
            store.ensure_work_item(
                "owner/repo",
                kind="github_issue",
                external_id="8",
                title="Missing relationships",
            )
            with (
                patch("reposteward.store.utc_now", return_value="2026-01-01T00:00:00Z"),
                patch(
                    "reposteward.store.uuid.uuid4",
                    side_effect=[UUID(int=n) for n in (3, 2, 1)],
                ),
            ):
                run_ids = [store.start_run("owner/repo", 8, "agent") for _ in range(3)]
            store.update_run(run_ids[-1], status="failed")

            trace = build_lifecycle_trace(state_dir, "owner/repo", 8, event_limit=2)

        sources = {value["name"]: value for value in trace["sources"]}
        self.assertFalse(trace["complete"])
        self.assertEqual(trace["next_action"], "diagnose_failure")
        self.assertEqual(trace["stats"]["available_runs"], 3)
        self.assertGreater(trace["stats"]["events_omitted"], 0)
        self.assertEqual(sources["run"]["status"], "incomplete")
        self.assertEqual(sources["context_pack"]["status"], "incomplete")
        self.assertEqual(
            sources["context_pack"]["reason"], "some_runs_have_no_context_pack"
        )
        self.assertEqual(sources["publication"]["status"], "unknown")
        self.assertEqual(sources["publication"]["reason"], "no_recorded_facts")
        self.assertEqual(sources["github_pr"]["trust"], "github_untrusted")
        self.assertEqual(sources["owner_review"]["trust"], "operator_attested")

    def test_legacy_run_does_not_include_other_issues_queue_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            store = Store(state_dir / "reposteward.sqlite3")
            store.start_run("owner/repo", 7, "agent")
            store.enqueue_queue_task(
                "owner/repo", action="prepare", enqueued_by="alice", issue_number=8
            )
            own_task = store.enqueue_queue_task(
                "owner/repo", action="prepare", enqueued_by="alice", issue_number=7
            )
            with sqlite3.connect(state_dir / "reposteward.sqlite3") as connection:
                connection.execute("UPDATE queue_tasks SET work_item_id=''")

            trace = build_lifecycle_trace(state_dir, "owner/repo", 7)

        tasks = [
            e["facts"]["task_id"]
            for e in trace["events"]
            if e["source"] == "queue_task"
        ]
        attempts = [
            e["facts"]["task_id"]
            for e in trace["events"]
            if e["source"] == "queue_attempt"
        ]
        self.assertEqual(tasks, [own_task["id"]])
        self.assertEqual(attempts, [own_task["id"]])

    def test_text_preserves_false_verification_and_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            _seed_lifecycle(state_dir)
            text = render_lifecycle_text(
                build_lifecycle_trace(state_dir, "owner/repo", 7)
            )

        self.assertIn("passed=False", text)
        self.assertIn("failed_command_count=0", text)

    def test_latest_run_summary_survives_history_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            _, _, latest = _seed_lifecycle(state_dir)
            trace = build_lifecycle_trace(state_dir, "owner/repo", 7, event_limit=1)

        self.assertEqual(trace["current"]["run_id"], latest)
        self.assertEqual(trace["current"]["status"], "failed")
        self.assertEqual(trace["current"]["head_sha"], "2" * 40)
        self.assertEqual(trace["next_action"], "diagnose_failure")

    def test_checkpoint_free_text_is_not_exposed_as_an_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database, _, _ = _seed_lifecycle(state_dir)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE checkpoints SET payload=json_set(payload, '$.next_action', ?)",
                    ("Read /home/private/workspace with ghp_secret_token",),
                )
            trace = build_lifecycle_trace(state_dir, "owner/repo", 7)

        serialized = json.dumps(trace)
        self.assertNotIn("ghp_secret", serialized)
        self.assertNotIn("/home/private", serialized)
        checkpoints = [
            e["facts"] for e in trace["events"] if e["source"] == "checkpoint"
        ]
        self.assertTrue(all(c["next_action"] == "unknown" for c in checkpoints))

    def test_current_merge_outcome_is_bound_to_latest_run_head(self) -> None:
        for stage, outcome, head, expected in (
            ("completed", "merged", "2" * 40, "complete"),
            ("completed", "already_merged", "2" * 40, "complete"),
            ("completed", "merged", "1" * 40, "monitor_pull_request"),
            ("completed", "outcome_unknown", "2" * 40, "monitor_pull_request"),
            ("applying", "", "2" * 40, "reconcile_merge"),
        ):
            with self.subTest(stage=stage, outcome=outcome, head=head):
                with tempfile.TemporaryDirectory() as directory:
                    state_dir = Path(directory)
                    database, _, latest = _seed_lifecycle(state_dir)
                    with sqlite3.connect(database) as connection:
                        connection.execute(
                            "UPDATE runs SET status='submitted' WHERE id=?", (latest,)
                        )
                        connection.execute(
                            "UPDATE merge_executions SET run_id=?, head_sha=?, stage=?, outcome=?",
                            (latest, head, stage, outcome),
                        )
                    trace = build_lifecycle_trace(
                        state_dir, "owner/repo", 7, event_limit=1
                    )

                self.assertEqual(trace["next_action"], expected)

    def test_unsupported_database_schema_is_never_migrated(self) -> None:
        for version in (0, 999):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as directory,
            ):
                state_dir = Path(directory)
                database, _, _ = _seed_lifecycle(state_dir)
                with sqlite3.connect(database) as connection:
                    connection.execute(f"PRAGMA user_version={version}")
                before = database.read_bytes()
                with self.assertRaises(LifecycleTraceError):
                    build_lifecycle_trace(state_dir, "owner/repo", 7)
                self.assertEqual(database.read_bytes(), before)

    def test_no_local_work_item_or_run_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            Store(state_dir / "reposteward.sqlite3")

            with self.assertRaisesRegex(KeyError, "no local lifecycle facts"):
                build_lifecycle_trace(state_dir, "owner/repo", 404)

    def test_usage_digest_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database, _, _ = _seed_lifecycle(state_dir)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE harness_usage_events SET event_digest=? WHERE id='usage-1'",
                    ("0" * 64,),
                )
                connection.commit()

            with self.assertRaisesRegex(LifecycleTraceError, "digest check"):
                build_lifecycle_trace(state_dir, "owner/repo", 7)


if __name__ == "__main__":
    unittest.main()
