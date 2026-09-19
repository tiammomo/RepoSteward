from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import test_external_tasks
from fastapi.testclient import TestClient
from test_projects import git, repository

from reposteward.projects.registry import ProjectError
from reposteward.tasks.local_operations import LocalOperations, OperationError
from reposteward.web.api.app import LocalSession, create_app
from reposteward.web.workbench import Workbench


class WorkspaceScanTests(unittest.TestCase):
    def setUp(self):
        test_external_tasks.ExternalTaskTests.setUp(self)
        (self.repo / "app.py").write_text(
            '"""A small public application."""\ndef answer():\n    return 42\n'
        )
        (self.repo / "README.md").write_text(
            "# Example\nRead app.py to understand the entry.\n"
        )
        git(self.repo, "add", "app.py", "README.md")
        git(self.repo, "commit", "-m", "Add public source fixture")
        self.linked = self.service.registry.inspect(self.repo)
        self.project_id = self.linked["project"]["id"]
        self.binding_id = self.linked["binding"]["id"]
        self.operations = LocalOperations(
            Workbench(self.config),
            client_factory=lambda config: self.fail(
                "scan must never construct an API client"
            ),
        )
        self.scans = self.operations.scans

    def enqueue(self, key="scan", *, binding=None, rebuild=False):
        binding = binding or self.binding_id
        preview = self.scans.preview(self.project_id, binding, rebuild=rebuild)
        return self.scans.enqueue(
            self.project_id,
            binding,
            rebuild=rebuild,
            expected_revision=preview["revision"],
            key=key,
        )

    def scan(self, key="scan", **kwargs):
        task = self.enqueue(key, **kwargs)
        self.assertTrue(self.operations.process_once())
        result = self.operations.operation(task["id"])
        self.assertEqual(result["state"], "completed", result)
        return result

    def test_preview_and_get_never_create_index_ledger_or_authenticate(self):
        preview = self.scans.preview(self.project_id, self.binding_id)
        self.assertEqual(preview["coverage"]["indexed_files"], 3)
        self.assertFalse((self.config.state_dir / "understanding").exists())
        self.assertFalse(self.operations.path.exists())
        self.assertFalse(self.operations.process_once())

    def test_scan_persists_stages_and_dirty_content_invalidates_same_head(self):
        result = self.scan()
        self.assertEqual(result["binding_id"], self.binding_id)
        self.assertEqual(
            [s["stage"] for s in result["stages"]],
            ["scan_started", "index_published", "summary"],
        )
        guide = self.operations.workbench.workspace(self.project_id, self.binding_id)[
            "guide"
        ]
        self.assertEqual(guide["status"], "current")
        head = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "app.py").write_text("def answer():\n    return 43\n")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(
            self.operations.workbench.workspace(self.project_id, self.binding_id)[
                "guide"
            ]["status"],
            "stale",
        )
        self.scan("updated")
        self.assertEqual(
            self.operations.workbench.workspace(self.project_id, self.binding_id)[
                "guide"
            ]["status"],
            "current",
        )

    def test_worktrees_have_separate_indices_and_evidence(self):
        other = self.root / "second"
        git(self.repo, "worktree", "add", "-b", "owner/second", str(other))
        linked = self.service.registry.link(other)
        self.assertEqual(linked["project"]["id"], self.project_id)
        self.scan("first")
        self.scan("second", binding=linked["binding"]["id"])
        first = self.operations.workbench.workspace(self.project_id, self.binding_id)[
            "guide"
        ]
        evidence = first["reading_path"][0]["source"]["evidence_id"]
        with self.assertRaises(ProjectError):
            self.operations.workbench.code(
                self.project_id, linked["binding"]["id"], evidence
            )
        (other / "app.py").write_text("def second():\n    return 99\n")
        self.assertEqual(
            self.operations.workbench.workspace(self.project_id, self.binding_id)[
                "guide"
            ]["status"],
            "current",
        )
        self.assertEqual(
            self.operations.workbench.workspace(
                self.project_id, linked["binding"]["id"]
            )["guide"]["status"],
            "stale",
        )
        self.assertEqual(
            len(list((self.config.state_dir / "understanding").glob("*.json"))), 2
        )

    def test_edit_after_preview_is_rejected_without_enqueuing(self):
        preview = self.scans.preview(self.project_id, self.binding_id)
        (self.repo / "app.py").write_text("changed")
        with self.assertRaisesRegex(OperationError, "变化"):
            self.scans.enqueue(
                self.project_id,
                self.binding_id,
                rebuild=False,
                expected_revision=preview["revision"],
                key="changed",
            )
        self.assertFalse(self.operations.path.exists())

    def test_edit_after_enqueue_retains_previous_index(self):
        self.scan()
        index = next((self.config.state_dir / "understanding").glob("*.json"))
        before = index.read_bytes()
        task = self.enqueue("new")
        (self.repo / "app.py").write_text("changed after enqueue")
        self.operations.process_once()
        self.assertEqual(
            self.operations.operation(task["id"])["last_error_code"],
            "workspace_changed",
        )
        self.assertEqual(index.read_bytes(), before)

    def test_lost_response_replays_original_task_after_source_changes(self):
        preview = self.scans.preview(self.project_id, self.binding_id)
        task = self.enqueue()
        (self.repo / "app.py").write_text("changed after acceptance")
        repeated = self.scans.enqueue(
            self.project_id,
            self.binding_id,
            rebuild=False,
            expected_revision=preview["revision"],
            key="scan",
        )
        self.assertEqual(task["id"], repeated["id"])
        with self.assertRaises(OperationError):
            self.scans.enqueue(
                self.project_id,
                self.binding_id,
                rebuild=True,
                expected_revision=preview["revision"],
                key="scan",
            )

    def test_api_rate_backoff_does_not_block_offline_scan_or_claim_native(self):
        store = self.operations.store(write=True)
        native = store.enqueue_queue_task(
            "owner/repo", action="prepare", enqueued_by="owner", issue_number=7
        )
        network = self.operations.sync(self.project_id, "network")
        with store._connection() as db:
            db.execute(
                "INSERT INTO github_account_limits VALUES (?,?,?)",
                (
                    self.operations.account,
                    (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    "rate_limited",
                ),
            )
        result = self.scan()
        self.assertEqual(result["action"], "workspace.scan")
        self.assertEqual(self.operations.operation(network["id"])["state"], "pending")
        self.assertEqual(store.queue_tasks(task_id=native["id"])[0]["state"], "pending")
        with self.assertRaises(ValueError):
            store.claim_queue_tasks(worker="native", actions=("workspace.scan",))

    def test_stale_worker_cannot_replace_index_after_lease_takeover(self):
        self.scan()
        index = next((self.config.state_dir / "understanding").glob("*.json"))
        before = index.read_bytes()
        task = self.enqueue("new", rebuild=True)
        from reposteward.projects.code_facts import parse_code

        changed = False

        def take_over(*args):
            nonlocal changed
            if not changed:
                changed = True
                with self.operations.store(write=True)._connection() as db:
                    db.execute(
                        "UPDATE queue_tasks SET lease_owner='replacement',lease_generation=lease_generation+1 WHERE id=?",
                        (task["id"],),
                    )
            return parse_code(*args)

        with patch("reposteward.projects.code_index.parse_code", side_effect=take_over):
            self.operations.process_once()
        self.assertEqual(self.operations.operation(task["id"])["state"], "running")
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse(list(index.parent.glob(".scan-*")))

    def test_restart_recovers_pending_scan_without_auth(self):
        task = self.enqueue()
        restarted = LocalOperations(
            Workbench(self.config), client_factory=lambda config: self.fail("no auth")
        )
        self.assertTrue(restarted.process_once())
        self.assertEqual(restarted.operation(task["id"])["state"], "completed")

    def test_replaced_workspace_cannot_publish_into_previous_index(self):
        self.scan()
        index = next((self.config.state_dir / "understanding").glob("*.json"))
        before = index.read_bytes()
        task = self.enqueue("replacement")
        self.repo.rename(self.root / "original-checkout")
        repository(self.repo, remote="git@github.com:owner/repo.git")
        self.operations.process_once()
        self.assertEqual(self.operations.operation(task["id"])["state"], "failed")
        self.assertEqual(index.read_bytes(), before)

    def test_edit_during_parsing_keeps_previous_complete_generation(self):
        self.scan()
        index = next((self.config.state_dir / "understanding").glob("*.json"))
        before = index.read_bytes()
        task = self.enqueue("during-parse", rebuild=True)
        from reposteward.projects.code_facts import parse_code

        def edit(*args):
            (self.repo / "app.py").write_text("def changed():\n    return 99\n")
            return parse_code(*args)

        with patch("reposteward.projects.code_index.parse_code", side_effect=edit):
            self.operations.process_once()
        self.assertEqual(self.operations.operation(task["id"])["state"], "failed")
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse(list(index.parent.glob(".scan-*")))

    def test_cancelled_pending_scan_can_be_explicitly_retried(self):
        task = self.enqueue()
        cancelled = self.operations.control(
            task["id"], "cancel", task["revision"], "cancel-scan"
        )
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertFalse(self.operations.process_once())
        self.assertFalse((self.config.state_dir / "understanding").exists())
        self.operations.control(
            task["id"], "retry", cancelled["revision"], "retry-scan"
        )
        self.assertTrue(self.operations.process_once())
        self.assertEqual(self.operations.operation(task["id"])["state"], "completed")

    def test_cross_project_and_account_ids_are_rejected(self):
        other = self.service.registry.link(
            repository(self.root / "other", remote="git@github.com:other/repo.git")
        )
        with self.assertRaises(KeyError):
            self.scans.preview(other["project"]["id"], self.binding_id)
        task = self.enqueue()
        changed = LocalOperations(
            Workbench(
                replace(self.config, github=replace(self.config.github, login="other"))
            )
        )
        with self.assertRaises(KeyError):
            changed.operation(task["id"])

    def test_rebuild_recovers_damaged_index_but_plain_scan_preserves_it(self):
        self.scan()
        index = next((self.config.state_dir / "understanding").glob("*.json"))
        index.write_text("invalid json")
        task = self.enqueue("normal")
        self.operations.process_once()
        self.assertEqual(self.operations.operation(task["id"])["state"], "failed")
        self.assertEqual(index.read_text(), "invalid json")
        self.scan("rebuild", rebuild=True)
        self.assertEqual(
            self.operations.workbench.workspace(self.project_id, self.binding_id)[
                "guide"
            ]["status"],
            "current",
        )

    def test_http_preview_command_boundaries_and_persistent_operation(self):
        session = LocalSession("127.0.0.1:8123")
        with TestClient(
            create_app(
                self.operations.workbench,
                session=session,
                operations=self.operations,
                manage_local=True,
            ),
            base_url=session.origin,
            client=("127.0.0.1", 1234),
        ) as client:
            headers = {
                "Authorization": "Bearer " + session.token,
                "Origin": session.origin,
                "Idempotency-Key": "http",
            }
            url = (
                "/api/v1/scan-plan?project_id="
                + self.project_id
                + "&binding_id="
                + self.binding_id
            )
            self.assertEqual(client.get(url).status_code, 401)
            response = client.get(url, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(self.operations.path.exists())
            preview = response.json()["data"]
            body = {
                "project_id": self.project_id,
                "binding_id": self.binding_id,
                "expected_revision": preview["revision"],
                "rebuild": False,
            }
            response = client.post(
                "/api/v1/commands/workspaces/scan",
                json={**body, "shell": "untrusted"},
                headers=headers,
            )
            self.assertEqual(response.status_code, 400)
            response = client.post(
                "/api/v1/commands/workspaces/scan", json=body, headers=headers
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertIn("operation_id=", response.headers["Location"])
