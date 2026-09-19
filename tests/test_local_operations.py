from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import test_external_tasks

from reposteward.github.client import ConditionalRead, GitHubReadError
from reposteward.github.sync import observations
from reposteward.storage.store import Store
from reposteward.tasks.local_operations import LocalOperations, OperationError
from reposteward.web.overview import ProjectOverview
from reposteward.web.workbench import Workbench


class LocalOperationTests(unittest.TestCase):
    def setUp(self):
        test_external_tasks.ExternalTaskTests.setUp(self)
        self.project = self.service.registry.list()["projects"][0]
        self.remote = Mock(config=self.config.github)
        self.remote.conditional_get.side_effect = self.read
        self.operations = LocalOperations(
            Workbench(self.config), client_factory=lambda config: self.remote
        )
        self.closed = False
        self.fail = ""
        self.not_modified = False
        self.calls = []

    def pull(self):
        return {
            "number": 12,
            "state": "closed" if self.closed else "open",
            "merged": self.closed,
            "title": "<img src=x onerror=alert(1)>",
            "head": {"sha": "a" * 40},
            "user": {"login": "contributor"},
            "updated_at": "2026-09-06T01:00:00Z",
        }

    def read(self, path, *, query=None, etag=""):
        self.calls.append((path, query, etag))
        if path == self.fail:
            raise GitHubReadError("network_unavailable")
        if self.not_modified and etag:
            return ConditionalRead(None, etag, False, True)
        if path == "/user":
            body = {"login": "owner"}
        elif path.endswith("/pulls"):
            body = [] if self.closed and query["state"] == "open" else [self.pull()]
        elif path.endswith("/pulls/12"):
            body = self.pull()
        elif path.endswith("/check-runs"):
            body = {
                "check_runs": [
                    {"name": "test", "status": "completed", "conclusion": "success"}
                ]
            }
        elif path.endswith("/status"):
            body = {"state": "success"}
        else:
            body = []
        return ConditionalRead(body, '"etag"', False)

    def sync(self, key="one"):
        task = self.operations.sync(self.project["id"], key)
        self.assertTrue(self.operations.process_once())
        return self.operations.operation(task["id"])

    def test_get_and_startup_do_not_create_or_authenticate(self):
        self.assertTrue(self.operations.github(self.project["id"])["never_synced"])
        self.assertEqual(self.operations.listing()["items"], [])
        self.assertFalse(self.operations.process_once())
        self.assertFalse(self.operations.path.exists())
        self.assertFalse(self.calls)

    def test_terminal_observation_updates_inbox_without_fabricated_merge(self):
        task = self.sync()
        self.assertEqual(task["state"], "completed")
        store = Store(self.operations.path)
        run = store.start_run("owner/repo", 7, "pull_request")
        store.update_run(
            run,
            status="submitted",
            details={"pr_url": "https://github.com/Owner/Repo/pull/12"},
        )
        self.closed = True
        self.sync("two")
        view = self.operations.github(self.project["id"])
        self.assertEqual(view["items"][0]["state"], "merged")
        overview = ProjectOverview(self.config).show()["projects"][0]
        self.assertFalse(
            any(
                item["reason_code"]
                in {"merge_check_required", "refresh_required", "pull_in_progress"}
                for item in overview["items"]
            )
        )
        self.assertEqual(overview["terminal_observed"], 1)
        self.assertEqual(store.latest_merge_outcomes("owner/repo"), {})
        self.assertEqual(store.run(run)["status"], "submitted")
        detail = self.operations.github(self.project["id"], number=12)
        self.assertEqual(detail["items"][0]["native_runs"][0]["id"], run)
        self.assertIn(
            "trace owner/repo 7", detail["items"][0]["native_runs"][0]["trace_command"]
        )

    def test_new_open_list_does_not_erase_a_positive_terminal_observation(self):
        self.closed = True
        self.sync()
        store = Store(self.operations.path)
        run = store.start_run("owner/repo", 7, "pull_request")
        store.update_run(
            run,
            status="submitted",
            details={"pr_url": "https://github.com/owner/repo/pull/12"},
        )
        future = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
        with store._connection() as db:
            db.execute(
                "INSERT INTO project_inbox_cache VALUES (?,?,?,?,?,'')",
                (
                    self.project["id"],
                    "owner/repo",
                    json.dumps({"snapshot": {"complete": True, "pull_requests": []}}),
                    future,
                    future,
                ),
            )
        report = ProjectOverview(self.config).show()["projects"][0]
        self.assertEqual(report["terminal_observed"], 1)
        self.assertFalse(
            any(row["reason_code"] == "refresh_required" for row in report["items"])
        )

    def test_newer_approval_supersedes_old_change_request(self):
        original = self.read

        def remote(path, **kwargs):
            if path.endswith("/reviews"):
                return ConditionalRead(
                    [
                        {
                            "user": {"login": "reviewer"},
                            "state": "CHANGES_REQUESTED",
                            "submitted_at": "2026-09-05",
                            "commit_id": "a" * 40,
                        },
                        {
                            "user": {"login": "reviewer"},
                            "state": "APPROVED",
                            "submitted_at": "2026-09-06",
                            "commit_id": "a" * 40,
                        },
                    ],
                    '"reviews"',
                    False,
                )
            return original(path, **kwargs)

        self.remote.conditional_get.side_effect = remote
        self.sync()
        report = ProjectOverview(self.config).show()["projects"][0]
        self.assertFalse(
            any(row["reason_code"] == "review_requested" for row in report["items"])
        )

    def test_failure_preserves_previous_success_and_retry_is_explicit(self):
        self.sync()
        prior = observations(
            self.operations.store(), self.operations.account, self.project["id"]
        )["issues"]
        self.fail = "/repos/owner/repo/issues"
        failed = self.sync("failure")
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["manual_required"])
        after = observations(
            self.operations.store(), self.operations.account, self.project["id"]
        )["issues"]
        self.assertEqual(after["observation_id"], prior["observation_id"])
        self.assertEqual(after["last_success"], prior["last_success"])
        self.assertEqual(after["error_code"], "network_unavailable")
        self.assertFalse(self.operations.process_once())
        self.fail = ""
        retried = self.operations.control(
            failed["id"], "retry", failed["revision"], "retry"
        )
        self.assertEqual(retried["state"], "pending")
        self.assertTrue(self.operations.process_once())
        self.assertEqual(self.operations.operation(failed["id"])["state"], "completed")

    def test_304_confirms_prior_content_and_stale_cursor_is_rejected(self):
        self.sync()
        first = self.operations.github(self.project["id"])
        self.not_modified = True
        self.sync("conditional")
        second = self.operations.github(self.project["id"])
        self.assertNotEqual(first["snapshot"], second["snapshot"])
        self.assertEqual(second["items"][0]["title"], first["items"][0]["title"])
        self.assertTrue(any(etag for _, _, etag in self.calls))
        with self.assertRaisesRegex(OperationError, "快照"):
            self.operations.github(
                self.project["id"], cursor=first["snapshot"] + ":50:pulls"
            )

    def test_rate_limit_prevents_other_tasks_and_survives_service_restart(self):
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat(
            timespec="microseconds"
        )
        self.remote.conditional_get.side_effect = GitHubReadError(
            "rate_limited", retry_at=future
        )
        task = self.sync()
        self.assertEqual(task["available_at"], future)
        self.assertFalse(task["manual_required"])
        restarted = LocalOperations(
            Workbench(self.config), client_factory=lambda config: self.remote
        )
        self.assertFalse(restarted.process_once())
        self.assertEqual(self.remote.conditional_get.call_count, 1)

    def test_controls_bind_revision_family_account_and_idempotency(self):
        task = self.operations.sync(self.project["id"], "one")
        cancelled = self.operations.control(
            task["id"], "cancel", task["revision"], "cancel"
        )
        replay = self.operations.control(
            task["id"], "cancel", task["revision"], "cancel"
        )
        self.assertEqual(cancelled["state"], replay["state"])
        with self.assertRaises(OperationError):
            self.operations.control(task["id"], "retry", task["revision"], "new")
        other = LocalOperations(
            Workbench(
                replace(self.config, github=replace(self.config.github, login="other"))
            )
        )
        with self.assertRaises(KeyError):
            other.operation(task["id"])
        with self.assertRaises(KeyError):
            Store(self.operations.path).requeue_queue_task(
                task["id"], requeued_by="native"
            )

    def test_commands_refuse_implicit_daily_migration(self):
        Store(self.operations.path)
        with closing(sqlite3.connect(self.operations.path)) as db:
            db.execute("PRAGMA user_version=22")
        with self.assertRaisesRegex(OperationError, "升级"):
            self.operations.sync(self.project["id"], "one")
        with closing(sqlite3.connect(self.operations.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 22)
            self.assertEqual(
                db.execute("SELECT count(*) FROM queue_tasks").fetchone()[0], 0
            )

    def test_watch_project_without_execution_policy_can_sync(self):
        config = replace(self.config, repositories={})
        self.operations = LocalOperations(
            Workbench(config), client_factory=lambda config: self.remote
        )
        self.assertEqual(self.sync()["state"], "completed")
        self.assertEqual(len(ProjectOverview(config).show()["projects"]), 1)

    def test_old_worker_cannot_publish_after_lease_takeover(self):
        def lose_lease(path, **kwargs):
            with Store(self.operations.path)._connection() as db:
                db.execute(
                    "UPDATE queue_tasks SET lease_owner='new-worker',lease_generation=lease_generation+1 WHERE state='running'"
                )
            return self.read(path, **kwargs)

        self.remote.conditional_get.side_effect = lose_lease
        task = self.sync()
        self.assertEqual(task["state"], "running")
        self.assertEqual(task["stages"], [])
        self.assertEqual(
            observations(
                self.operations.store(), self.operations.account, self.project["id"]
            ),
            {},
        )

    def test_tampered_plan_never_reaches_remote(self):
        self.operations.sync(self.project["id"], "one")
        with Store(self.operations.path)._connection() as db:
            db.execute(
                "UPDATE local_operation_plans SET payload=?",
                (json.dumps({"redirect": "other/repo"}),),
            )
        with patch.object(
            self.operations,
            "client_factory",
            side_effect=AssertionError("must not authenticate"),
        ):
            self.assertFalse(self.operations.process_once())
        self.assertFalse(self.calls)
