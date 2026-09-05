from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_context import _candidate

from reposteward.config import ContextConfig, RepositoryPolicy
from reposteward.context import build_context_pack, running_checkpoint
from reposteward.models import (
    AgentExecution,
    AgentMetrics,
    AgentResult,
    VerificationResult,
)
from reposteward.pipeline import Pipeline
from reposteward.policy import DiffSummary
from reposteward.store import Store
from reposteward.task_contract import issue_digest, review_contract


class FeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / ".git").mkdir()
        self.store = Store(self.root / "state.sqlite3")
        self.policy = RepositoryPolicy(name="owner/repo")
        candidate = _candidate()
        self.store.upsert_candidate(candidate)
        self.run_id = self.store.start_run("owner/repo", 7, "pull_request")
        work = self.store.ensure_work_item(
            "owner/repo", kind="github_issue", external_id="7", title="Fix"
        )
        context = build_context_pack(
            candidate,
            self.policy,
            work_item_id=work["id"],
            run_id=self.run_id,
            worktree=self.root,
            base_commit="b" * 40,
            harness="fake",
            model="test",
        )
        self.store.save_context_run(
            pack_id=context.id,
            work_item_id=work["id"],
            run_id=self.run_id,
            schema_version=context.schema_version,
            source_digest=context.source_digest,
            base_commit="b" * 40,
            payload=context.to_dict(),
            harness="fake",
        )
        self.store.save_checkpoint(
            work_item_id=work["id"],
            run_id=self.run_id,
            context_pack_id=context.id,
            status="running",
            payload=running_checkpoint(
                context, head_commit="a" * 40, completed=(), next_action="follow_up"
            ),
        )
        self.details = {
            "commit_sha": "a" * 40,
            "base_commit": "b" * 40,
            "base_branch": "main",
            "branch": "fix",
            "worktree": str(self.root),
            "changed_files": ["src/example.py"],
            "pr_url": "https://github.com/owner/repo/pull/12",
        }
        self.store.update_run(
            self.run_id, status="submitted", stage="pull_request", details=self.details
        )
        self.activity = {
            "pull_request": {
                "number": 12,
                "url": self.details["pr_url"],
                "state": "open",
                "draft": True,
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "base_branch": "main",
                "merged": False,
            },
            "comments": [],
            "reviews": [],
            "checks": [],
            "review_comments": [
                {
                    "id": i,
                    "author": "reviewer",
                    "association": "MEMBER",
                    "path": "src/example.py",
                    "line": i,
                    "body": f"feedback {i}: " + "x" * 700 + " preserve compatibility",
                    "created_at": "2026-09-05T00:00:00Z",
                    "updated_at": "2026-09-05T00:00:00Z",
                }
                for i in range(1, 21)
            ],
        }
        self.pipeline = object.__new__(Pipeline)
        self.pipeline.store = self.store
        self.pipeline.config = SimpleNamespace(
            state_dir=self.root / "runs",
            context=ContextConfig(follow_up_max_tokens=12000),
            agent=SimpleNamespace(model="test"),
            repositories={"owner/repo": self.policy},
        )
        self.pipeline.github = Mock()
        self.pipeline.github.pull_request_activity.return_value = self.activity
        self.pipeline.github.pull_request_merge_snapshot.return_value = {
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "state": "OPEN",
        }
        self.pipeline.harness = Mock()
        self.pipeline.harness.name = "fake"
        self.pipeline.harness.run.return_value = AgentExecution(
            AgentResult("Fixed", "fix: feedback", "notes", ()),
            AgentMetrics(),
            "fake",
            "test",
        )
        self.pipeline.verifier = Mock()
        self.pipeline.verifier.verify.return_value = VerificationResult(True, ())
        self.pipeline.workspaces = Mock()
        self.pipeline.workspaces.commit.return_value = "c" * 40
        self.pipeline._follow_up_diff_snippets = Mock(return_value={})
        self.pipeline.inspect_run = lambda run_id: self.store.run(run_id)

    def ingest(self) -> dict:
        return self.store.ingest_github_pr_activity(
            run_id=self.run_id,
            repository="owner/repo",
            pull_number=12,
            activity=self.activity,
        )

    def prepare(self) -> dict:
        with (
            patch.object(Pipeline, "_revision", return_value="a" * 40),
            patch(
                "reposteward.pipeline.subprocess.run",
                return_value=SimpleNamespace(stdout=""),
            ),
            patch(
                "reposteward.pipeline.enforce_change_policy",
                return_value=DiffSummary(("src/example.py",), 1, 1),
            ),
        ):
            return self.pipeline.prepare_repair(self.run_id)

    def test_follow_up_does_not_consume_feedback_and_full_bodies_survive(self) -> None:
        self.pipeline.config.context = replace(
            self.pipeline.config.context, follow_up_max_tokens=5000
        )
        first = self.pipeline.follow_up(self.run_id)
        preview = self.pipeline._follow_up(self.run_id, commit=False)
        self.assertGreater(first["new_review_comments_omitted"], 0)
        self.assertFalse(preview["changed"])
        self.assertTrue(preview["context_plan"]["actionable"])
        self.assertEqual(preview["pending_feedback"]["counts"]["pending"], 20)
        self.assertTrue(
            all(
                value["body"].endswith("preserve compatibility")
                for value in preview["context_plan"]["events"]
            )
        )
        self.assertEqual(
            len(self.store.pending_feedback("owner/repo", 12)["events"]), 20
        )

    def test_verified_batch_consumes_only_final_prompt_selection_and_successor_sees_rest(
        self,
    ) -> None:
        self.pipeline.follow_up(self.run_id)
        ready = self.prepare()
        self.assertEqual(ready["status"], "ready")
        selected = ready["details"]["feedback_sequences"]
        self.assertGreater(len(selected), 0)
        self.assertLess(len(selected), 20)
        report = self.store.pending_feedback("owner/repo", 12)
        self.assertEqual(report["counts"]["verified"], len(selected))
        self.assertEqual(len(report["events"]), 20 - len(selected))
        self.assertTrue(
            set(selected).isdisjoint(event["sequence"] for event in report["events"])
        )
        self.store.update_run(ready["id"], status="submitted")
        self.activity["pull_request"]["head_sha"] = "c" * 40
        next_preview = self.pipeline._follow_up(ready["id"], commit=False)
        self.assertTrue(next_preview["context_plan"]["actionable"])
        self.assertTrue(
            set(selected).isdisjoint(
                event["sequence"] for event in next_preview["context_plan"]["events"]
            )
        )
        with closing(sqlite3.connect(self.store.path)) as db:
            evidence = db.execute(
                "SELECT handled_run_id,commit_sha,verification_digest FROM feedback_items WHERE status='verified'"
            ).fetchall()
        self.assertTrue(
            all(
                row[0] == ready["id"] and row[1] == "c" * 40 and len(row[2]) == 64
                for row in evidence
            )
        )

    def test_repair_reuses_the_reviewed_source_contract(self) -> None:
        issue = _candidate().issue
        contract = review_contract(
            issue,
            {
                "goal": issue.title,
                "source_digest": issue_digest(issue),
                "acceptance_criteria": ["Preserve the reviewed condition"],
            },
            reviewed_by="operator",
        )
        original = self.store.context_bundle

        def bundle(run_id):
            value = original(run_id)
            if run_id == self.run_id:
                value = {
                    **value,
                    "context_pack": {
                        **value["context_pack"],
                        "task_contract": contract.to_dict(),
                    },
                }
            return value

        self.store.context_bundle = bundle
        self.pipeline.follow_up(self.run_id)
        self.prepare()
        request = self.pipeline.harness.run.call_args.args[0]
        self.assertEqual(request.context.task_contract.digest, contract.digest)

    def test_failure_preserves_every_item_for_retry(self) -> None:
        self.pipeline.follow_up(self.run_id)
        self.pipeline.verifier.verify.side_effect = RuntimeError("verification failed")
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            self.prepare()
        report = self.store.pending_feedback("owner/repo", 12)
        self.assertEqual(len(report["events"]), 20)
        self.assertNotIn("verified", report["counts"])
        self.pipeline.verifier.verify.side_effect = None
        self.assertEqual(self.prepare()["status"], "ready")

    def test_outside_scope_does_not_starve_valid_feedback(self) -> None:
        for value in self.activity["review_comments"][1:]:
            value["path"] = "docs/outside.md"
        self.pipeline.follow_up(self.run_id)
        ready = self.prepare()
        self.assertEqual(len(ready["details"]["feedback_sequences"]), 1)
        report = self.store.pending_feedback("owner/repo", 12)
        self.assertEqual(report["counts"]["deferred"], 19)
        self.assertTrue(
            all(
                event["processing_reason"] == "path_outside_existing_pull_request_scope"
                for event in report["events"]
            )
        )

    def test_edited_versions_supersede_once_and_stale_poll_does_not_revert(
        self,
    ) -> None:
        self.ingest()
        old = dict(self.activity["review_comments"][0])
        self.activity["review_comments"][0].update(
            body="replacement", updated_at="2026-09-06T00:00:00Z"
        )
        self.ingest()
        self.ingest()
        self.activity["review_comments"][0] = old
        self.ingest()
        report = self.store.pending_feedback("owner/repo", 12)
        self.assertEqual(len(report["events"]), 20)
        self.assertEqual(report["counts"]["superseded"], 1)
        self.assertEqual(
            next(e for e in report["events"] if e["external_id"] == "1")["payload"][
                "body"
            ],
            "replacement",
        )

    def test_unresolved_payloads_are_gc_protected_and_missing_is_unknown(self) -> None:
        self.pipeline.follow_up(self.run_id)
        report = self.store.pending_feedback("owner/repo", 12)
        digest = report["events"][0]["payload_digest"]
        inventory = self.store.event_payload_gc_inventory(
            {"owner/repo": "2099-01-01T00:00:00Z"}
        )
        retained = next(
            value for value in inventory["retained"] if value["digest"] == digest
        )
        self.assertIn("unresolved_feedback_reference", retained["reasons"])
        self.assertEqual(
            self.store.delete_event_payloads(
                (digest,), retention_cutoffs={"owner/repo": "2099-01-01T00:00:00Z"}
            )["deleted"],
            [],
        )
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("DELETE FROM content_blobs WHERE digest=?", (digest,))
        missing = self.store.pending_feedback("owner/repo", 12)
        self.assertEqual(missing["unknown"][0]["status"], "unknown")
        self.assertEqual(missing["unknown"][0]["digest"], digest)

    def test_ready_and_completion_are_atomic_and_require_evidence(self) -> None:
        self.ingest()
        sequence = self.store.pending_feedback("owner/repo", 12)["events"][0][
            "sequence"
        ]
        run_id = self.store.start_run("owner/repo", 7, "repair")
        details = {
            "feedback_sequences": [sequence],
            "commit_sha": "c" * 40,
            "repair_guard": {
                "source_run_id": self.run_id,
                "parent_commit": "a" * 40,
                "pull_number": 12,
            },
            "agent_result": {"summary": "all feedback fixed"},
        }
        with self.assertRaisesRegex(ValueError, "verified repair"):
            self.store.update_run(run_id, status="ready", details=details)
        self.assertNotEqual(self.store.run(run_id)["status"], "ready")
        details["verification"] = asdict(VerificationResult(True, ()))
        details["repair_guard"]["pull_number"] = 99
        with self.assertRaisesRegex(ValueError, "different pull"):
            self.store.update_run(run_id, status="ready", details=details)
        self.assertEqual(
            len(self.store.pending_feedback("owner/repo", 12)["events"]), 20
        )

    def test_migration_replays_old_watermarks_and_missing_payloads_conservatively(
        self,
    ) -> None:
        self.pipeline.follow_up(self.run_id)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("DROP TABLE feedback_items")
            db.execute("PRAGMA user_version=17")
        migrated = Store(self.store.path)
        report = migrated.pending_feedback("owner/repo", 12)
        self.assertEqual(len(report["events"]), 20)
        self.assertTrue(
            all(
                event["processing_reason"] == "historical_processing_unknown"
                for event in report["events"]
            )
        )
        self.assertEqual(migrated.github_pr_watermark(self.run_id)["sequence"], 21)


if __name__ == "__main__":
    unittest.main()
