from __future__ import annotations

import io
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from dataclasses import asdict, replace
from unittest.mock import Mock, patch

import test_external_verification
from test_projects import repository

from reposteward.cli import main
from reposteward.github import GitHubError
from reposteward.merge import MergeSnapshot, evaluate_merge
from reposteward.overview import ProjectOverview, render_overview
from reposteward.store import utc_now


class OverviewTests(unittest.TestCase):
    container_run = test_external_verification.ExternalVerificationTests.container_run
    request = test_external_verification.ExternalVerificationTests.request

    def setUp(self) -> None:
        test_external_verification.ExternalVerificationTests.setUp(self)
        self.remote = Mock()
        self.remote.open_pull_requests.return_value = ()
        self.overview = ProjectOverview(self.config, github=self.remote)
        self.store = self.service._store(write=True)

    def items(self, result: dict) -> list[dict]:
        return [item for project in result["projects"] for item in project["items"]]

    def add_second_project(self) -> dict:
        second = repository(
            self.root / "second", remote="git@github.com:owner/second.git"
        )
        linked = self.service.registry.link(second)
        policy = replace(self.config.repositories["owner/repo"], name="owner/second")
        self.config = replace(
            self.config,
            repositories={**self.config.repositories, "owner/second": policy},
        )
        self.overview = ProjectOverview(self.config, github=self.remote)
        return linked

    def submitted(self, *, issue: int = 7, pull: int = 12) -> str:
        run_id = self.store.start_run("owner/repo", issue, "pull_request")
        self.store.update_run(
            run_id,
            status="submitted",
            stage="pull_request",
            details={"pr_url": f"https://github.com/owner/repo/pull/{pull}"},
        )
        return run_id

    def test_default_local_view_and_cli_do_not_authenticate_launch_or_consume_feedback(
        self,
    ) -> None:
        (self.repo / "source.txt").write_text("dirty work\n")
        with (
            patch(
                "reposteward.overview.GitHubClient",
                side_effect=AssertionError("authentication"),
            ),
            patch("reposteward.cli.Pipeline", side_effect=AssertionError("Pipeline")),
        ):
            result = self.overview.show()
            self.assertEqual(
                result["projects"][0]["sources"]["status"], "not_refreshed"
            )
            self.assertTrue(self.items(result)[0]["dirty"])
            self.assertEqual(
                self.items(result)[0]["reason_code"], "external_verification_required"
            )
            output = io.StringIO()
            with (
                patch("reposteward.cli.load_config", return_value=self.config),
                redirect_stdout(output),
            ):
                self.assertEqual(main(["overview", "show", "--format", "text"]), 0)
            self.assertIn("owner/repo", output.getvalue())
        self.remote.open_pull_requests.assert_not_called()
        self.assertFalse(result["public_write"])

    def test_explicit_refresh_caches_facts_and_failed_refresh_preserves_them(
        self,
    ) -> None:
        first = self.overview.refresh()
        self.assertTrue(first["complete"])
        fetched = first["projects"][0]["sources"]["fetched_at"]
        self.remote.open_pull_requests.side_effect = GitHubError(
            "temporarily unavailable"
        )
        failed = self.overview.refresh()
        self.assertFalse(failed["complete"])
        self.assertEqual(failed["projects"][0]["sources"]["fetched_at"], fetched)
        self.assertEqual(failed["projects"][0]["sources"]["status"], "refresh_failed")
        self.assertIn(
            "github_refresh_failed",
            [item["reason_code"] for item in self.items(failed)],
        )
        self.assertIn(
            "external_verification_required",
            [item["reason_code"] for item in self.items(failed)],
        )

    def test_multiple_projects_partial_failure_and_limits_are_explicit(self) -> None:
        second = self.add_second_project()
        self.overview.refresh()
        with self.store._connection() as db:
            db.execute(
                "UPDATE project_inbox_cache SET portfolio='invalid JSON' WHERE project_id=?",
                (second["project"]["id"],),
            )
        result = self.overview.show()
        self.assertEqual(len(result["projects"]), 2)
        self.assertFalse(result["complete"])
        first, second_result = result["projects"]
        self.assertTrue(first["complete"])
        self.assertEqual(
            second_result["items"][0]["reason_code"], "project_state_unknown"
        )
        limited = self.overview.show(project_limit=1, item_limit=1)
        self.assertEqual(limited["omitted_projects"], 1)
        self.assertEqual(len(self.items(limited)), 1)
        self.assertFalse(limited["complete"])

    def test_show_does_not_create_or_migrate_a_ledger(self) -> None:
        config = replace(self.config, state_dir=self.root / "new-state")
        overview = ProjectOverview(config)
        overview.registry.link(self.repo)
        ledger = config.state_dir / "reposteward.sqlite3"
        result = overview.show()
        self.assertFalse(result["complete"])
        self.assertFalse(ledger.exists())
        with closing(sqlite3.connect(self.service.path)) as db:
            db.execute("PRAGMA user_version=21")
        before = self.service.path.read_bytes()
        result = self.overview.show()
        self.assertEqual(self.items(result)[0]["reason_code"], "project_state_unknown")
        self.assertEqual(self.service.path.read_bytes(), before)

    def test_duplicate_attempts_coalesce_and_fact_digest_is_stable_between_reads(
        self,
    ) -> None:
        self.service.start(self.repo, issue_number=7, reviewed_by="owner")
        first = self.overview.refresh()
        self.assertEqual(first["duplicates_collapsed"], 1)
        self.assertEqual(len(self.items(first)), 1)
        again = self.overview.show(previous_digest=first["overview_digest"])
        self.assertTrue(again["unchanged"])
        current = self.service.current(self.repo)
        self.service.checkpoint(
            current["run_id"],
            expected_revision=0,
            expected_snapshot=current["current_snapshot"]["digest"],
            idempotency_key="blocker",
            payload={
                "blockers": ["Need a design decision"],
                "next_action": "review design",
            },
        )
        changed = self.overview.show(previous_digest=again["overview_digest"])
        self.assertFalse(changed["unchanged"])
        self.assertEqual(self.items(changed)[0]["reason_code"], "external_blocked")
        self.assertIn("owner/repo", render_overview(changed))

    def test_existing_pr_is_retained_when_new_external_attempt_starts_for_same_issue(
        self,
    ) -> None:
        submitted = self.submitted()
        self.service.start(self.repo, issue_number=7, reviewed_by="owner")
        result = self.overview.show()
        self.assertIn(submitted, [item.get("run_id") for item in self.items(result)])
        self.assertIn(
            "refresh_required", [item["reason_code"] for item in self.items(result)]
        )

    def test_audited_merge_suppresses_only_with_complete_cached_portfolio(self) -> None:
        run_id = self.submitted()
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
            files=("source.txt",),
            additions=1,
            deletions=0,
            checks=(),
        )
        decision = evaluate_merge(
            snapshot,
            expected_head_sha="c" * 40,
            expected_base_sha="d" * 40,
            expected_policy_digest="e" * 64,
            max_files_changed=40,
            max_diff_lines=2000,
        )
        audit = self.store.append_merge_decision(
            repository="owner/repo",
            pull_number=12,
            head_sha="c" * 40,
            base_sha="d" * 40,
            policy_digest="e" * 64,
            decision={**decision.to_dict(), "snapshot": asdict(snapshot)},
        )
        self.store.append_merge_execution(
            attempt_id="merged",
            run_id=run_id,
            decision_id=audit["id"],
            repository="owner/repo",
            pull_number=12,
            actor="owner",
            merge_method="squash",
            stage="completed",
            outcome="merged",
            reason="observed merged",
            decision_digest=decision.decision_digest,
            head_sha="c" * 40,
            payload={},
        )
        before = self.overview.show()
        self.assertIn(run_id, [item.get("run_id") for item in self.items(before)])
        after = self.overview.refresh()
        self.assertNotIn(run_id, [item.get("run_id") for item in self.items(after)])

    def test_pending_feedback_stays_visible_without_advancing_processing_or_watermark(
        self,
    ) -> None:
        run_id = self.submitted()
        self.store.ingest_github_pr_activity(
            run_id=run_id,
            repository="owner/repo",
            pull_number=12,
            activity={
                "pull_request": {"number": 12, "head_sha": "a" * 40},
                "comments": [],
                "reviews": [],
                "checks": [],
                "review_comments": [
                    {
                        "id": 1,
                        "author": "reviewer",
                        "body": "Please handle empty input",
                        "created_at": utc_now(),
                        "updated_at": utc_now(),
                    }
                ],
            },
        )
        before = self.store.pending_feedback("owner/repo", 12)
        self.overview.refresh()
        result = self.overview.show()
        self.assertIn(
            "review_feedback_required",
            [item["reason_code"] for item in self.items(result)],
        )
        self.assertEqual(self.store.pending_feedback("owner/repo", 12), before)
        with self.store._connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT sequence FROM github_pr_watermarks WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0],
                0,
            )

    def test_verification_success_becomes_stale_after_code_changes(self) -> None:
        self.request()
        result = self.overview.show()
        self.assertEqual(
            self.items(result)[0]["reason_code"], "external_review_required"
        )
        (self.repo / "source.txt").write_text("new edit")
        stale = self.overview.show()
        self.assertEqual(
            self.items(stale)[0]["reason_code"], "external_verification_stale"
        )
        self.assertNotEqual(result["overview_digest"], stale["overview_digest"])

    def test_invalid_refresh_limits_do_not_make_remote_requests(self) -> None:
        with self.assertRaises(ValueError):
            self.overview.refresh(item_limit=0)
        with self.assertRaises(ValueError):
            self.overview.refresh(project_limit=51)
        self.remote.open_pull_requests.assert_not_called()
