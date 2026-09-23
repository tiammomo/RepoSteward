from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_external_tasks
from fastapi.testclient import TestClient

from reposteward.context.budget import ContextBudgetError
from reposteward.core.config import VerificationProfile
from reposteward.projects.registry import ProjectError, canonical_digest
from reposteward.storage.store import Store
from reposteward.tasks.external import TaskConflict
from reposteward.tasks.handoffs import TaskHandoffs
from reposteward.tasks.local_operations import LocalOperations
from reposteward.verification.external import ExternalVerification
from reposteward.web.api.app import LocalSession, create_app
from reposteward.web.workbench import Workbench


class TaskHandoffTests(unittest.TestCase):
    def setUp(self):
        test_external_tasks.ExternalTaskTests.setUp(self)
        self.config = replace(
            self.config,
            verification_profiles=(
                VerificationProfile("owner/repo", "unit", ("python -m unittest",)),
            ),
        )
        self.service.config = self.config
        self.task = self.service.start(self.repo, issue_number=7, reviewed_by="owner")
        self.run_id = self.task["run_id"]
        self.project_id = self.task["project_id"]
        self.app = Workbench(self.config)
        self.local = LocalOperations(self.app)
        self.handoffs = TaskHandoffs(self.local)
        self.scope = (self.project_id, self.run_id)

    def preview(self, budget=24000):
        return self.handoffs.preview(*self.scope, budget)

    def export(self, *, key="one", plan=None):
        return self.handoffs.export(
            *self.scope,
            budget=24000,
            client="codex",
            expected_plan=plan or self.preview()["plan_digest"],
            key=key,
        )

    def client(self, *, writable=False):
        session = LocalSession("127.0.0.1:8765")
        client = TestClient(
            create_app(
                self.app, session=session, manage_local=writable, operations=self.local
            ),
            base_url="http://127.0.0.1:8765",
            client=("127.0.0.1", 40000),
        )
        self.addCleanup(client.close)
        client.headers.update(
            {
                "Authorization": "Bearer " + session.token,
                "Origin": session.origin,
                "Idempotency-Key": "request-one",
            }
        )
        return client

    def test_preview_is_read_only_and_preserves_mandatory_pending_work(self):
        test_external_tasks.ExternalTaskTests.checkpoint(
            self,
            self.run_id,
            payload={
                "remaining": ["retain this obligation"],
                "blockers": ["await source"],
                "decisions": [
                    {
                        "statement": "keep scope",
                        "rationale": "reviewed issue",
                        "evidence": [],
                    }
                ],
                "next_action": "read evidence",
            },
        )
        path = self.config.state_dir / "reposteward.sqlite3"
        before = path.read_bytes()
        with patch(
            "reposteward.github.client.resolve_token",
            side_effect=AssertionError("auth"),
        ):
            preview = self.preview()
            self.handoffs.listing(*self.scope)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(preview["context"]["open_work"], ["retain this obligation"])
        self.assertEqual(preview["context"]["blockers"], ["await source"])
        with self.assertRaises(ContextBudgetError):
            self.preview(512)
        client = self.client()
        response = client.get(
            "/api/v1/task-preview",
            params={
                "project_id": self.project_id,
                "run_id": self.run_id,
                "budget": 512,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "context_budget")

    def test_export_is_immutable_idempotent_and_receipt_never_proves_execution(self):
        plan = self.preview()["plan_digest"]
        first = self.export(plan=plan)
        self.assertEqual(first["id"], self.export(plan=plan)["id"])
        self.assertIsNone(first["acknowledgement"])
        fetched = self.handoffs.get(*self.scope, first["id"], content=True)
        self.assertEqual(canonical_digest(fetched["content"]), first["digest"])
        self.assertIsNone(fetched["acknowledgement"])
        received = self.handoffs.acknowledge(
            *self.scope, first["id"], expected_digest=first["digest"]
        )
        self.assertEqual(received["acknowledgement"]["trust"], "user_reported")
        self.assertFalse(received["execution_observed"])
        self.assertFalse(received["verification_granted"])
        self.assertEqual(
            received,
            self.handoffs.acknowledge(
                *self.scope, first["id"], expected_digest=first["digest"]
            ),
        )
        self.assertEqual(self.service.inspect(self.run_id)["revision"], 0)
        (self.repo / "source.txt").write_text("new work")
        stale = self.handoffs.get(*self.scope, first["id"], content=True)
        self.assertEqual(stale["current_applicability"], "stale")
        self.assertEqual(stale["content"], fetched["content"])
        self.assertEqual(self.export(plan=plan)["id"], first["id"])
        with self.assertRaises(TaskConflict):
            self.export(key="new", plan=plan)
        with self.assertRaises(TaskConflict):
            self.export()

    def test_snapshot_checkpoint_and_profile_changes_invalidate_preview(self):
        plan = self.preview()["plan_digest"]
        test_external_tasks.ExternalTaskTests.checkpoint(self, self.run_id)
        with self.assertRaises(TaskConflict):
            self.export(plan=plan)
        current = self.preview()["plan_digest"]
        config = replace(
            self.config,
            verification_profiles=(
                VerificationProfile("owner/repo", "unit", ("python changed.py",)),
            ),
        )
        other = TaskHandoffs(LocalOperations(Workbench(config)))
        with self.assertRaises(TaskConflict):
            other.verify(
                *self.scope,
                budget=24000,
                expected_plan=current,
                profile="unit",
                key="new",
            )

    def test_cross_project_account_and_tampering_cannot_download_or_confirm(self):
        saved = self.export()
        with self.assertRaises((KeyError, ProjectError)):
            self.handoffs.get("f" * 32, self.run_id, saved["id"], content=True)
        other = TaskHandoffs(
            LocalOperations(
                Workbench(
                    replace(
                        self.config,
                        github=replace(self.config.github, login="different"),
                    )
                )
            )
        )
        self.assertEqual(other.listing(*self.scope)["items"], [])
        with self.assertRaises(KeyError):
            other.get(*self.scope, saved["id"], content=True)
        with self.assertRaises(KeyError):
            other.acknowledge(*self.scope, saved["id"], expected_digest=saved["digest"])
        with self.service._store(write=True)._connection() as db:
            db.execute(
                "UPDATE task_handoffs SET payload=? WHERE id=?",
                (json.dumps({"altered": True}), saved["id"]),
            )
        with self.assertRaises(TaskConflict):
            self.handoffs.get(*self.scope, saved["id"], content=True)

    def test_grouping_retains_attempts_without_inventing_delivery_state(self):
        store = self.service._store(write=True)
        store.update_run(
            self.run_id, status="submitted", stage="pull_request", details={}
        )
        second = self.service.start(self.repo, issue_number=7, reviewed_by="owner")
        store.update_run(
            second["run_id"], status="failed", stage="external", details={}
        )
        groups = self.app.tasks(self.project_id)["work_items"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(
            {a["id"] for a in groups[0]["attempts"]}, {self.run_id, second["run_id"]}
        )
        self.assertEqual(store.run(self.run_id)["status"], "submitted")
        self.assertNotIn("delivery_status", groups[0])
        preview = self.preview()
        self.assertEqual(preview["kind"], "managed")
        self.assertFalse(preview["verification_available"])

    def test_api_gates_and_download_ownership(self):
        preview = self.preview()
        body = {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "budget": 24000,
            "expected_plan": preview["plan_digest"],
            "client": "codex",
        }
        self.assertEqual(
            self.client().post("/api/v1/commands/tasks/handoff", json=body).status_code,
            405,
        )
        client = self.client(writable=True)
        self.assertEqual(
            client.post(
                "/api/v1/commands/tasks/handoff", json={**body, "path": "/etc/passwd"}
            ).status_code,
            400,
        )
        result = client.post("/api/v1/commands/tasks/handoff", json=body)
        self.assertEqual(result.status_code, 200, result.text)
        saved = result.json()["data"]
        query = {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "handoff_id": saved["id"],
        }
        response = client.get("/api/v1/handoff", params=query)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["data"]["acknowledgement"])
        self.assertEqual(
            client.get(
                "/api/v1/handoff", params={**query, "path": "/etc/passwd"}
            ).status_code,
            400,
        )
        self.assertEqual(
            client.post(
                "/api/v1/commands/tasks/handoff",
                json=body,
                headers={"Origin": "http://elsewhere"},
            ).status_code,
            403,
        )

    def enqueue(self):
        preview = self.preview()
        return self.handoffs.verify(
            *self.scope,
            budget=24000,
            expected_plan=preview["plan_digest"],
            profile="unit",
            key="verify",
        )

    def test_verification_uses_durable_scoped_worker_and_preserves_task(self):
        queued = self.enqueue()
        self.assertEqual(queued["state"], "pending")
        self.assertEqual(self.enqueue()["id"], queued["id"])
        with patch.object(
            ExternalVerification,
            "request",
            return_value={
                "outcome": "passed",
                "current_applicability": "matches",
                "public_write": False,
            },
        ) as execute:
            self.assertTrue(self.local.process_once())
            self.assertFalse(self.local.process_once())
        self.assertEqual(execute.call_args.kwargs["run_id"], self.run_id)
        self.assertEqual(execute.call_args.kwargs["profile"], "unit")
        result = self.local.operation(queued["id"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(
            result["stages"][-1]["result"]["current_applicability"], "not_checked"
        )
        self.assertEqual(self.service.inspect(self.run_id)["status"], "running")

    def test_cancellation_stale_snapshot_and_unknown_profile_do_not_execute(self):
        queued = self.enqueue()
        self.local.control(queued["id"], "cancel", queued["revision"], "cancel")
        with patch.object(
            ExternalVerification, "request", side_effect=AssertionError("execution")
        ):
            self.assertFalse(self.local.process_once())
        preview = self.preview()
        with self.assertRaises(RuntimeError):
            self.handoffs.verify(
                *self.scope,
                budget=24000,
                expected_plan=preview["plan_digest"],
                profile="arbitrary shell",
                key="other",
            )
        (self.repo / "source.txt").write_text("changed")
        with self.assertRaises(TaskConflict):
            self.handoffs.verify(
                *self.scope,
                budget=24000,
                expected_plan=preview["plan_digest"],
                profile="unit",
                key="other",
            )

    def test_native_operations_are_projected_but_not_controllable(self):
        store = self.service._store(write=True)
        native = store.enqueue_queue_task(
            "owner/repo",
            action="prepare",
            enqueued_by="owner",
            issue_number=7,
            parameters={},
        )
        listed = self.local.listing(self.project_id)
        self.assertEqual(listed["native_items"][0]["id"], native["id"])
        self.assertFalse(listed["native_items"][0]["can_retry"])
        self.assertEqual(
            listed["native_items"][0]["account_scope"], "legacy_unrecorded"
        )
        with self.assertRaises(KeyError):
            self.local.control(native["id"], "retry", "0" * 64, "retry")
        with patch.object(
            ExternalVerification, "request", side_effect=AssertionError("execution")
        ):
            self.assertFalse(self.local.process_once())
        self.assertEqual(store.queue_tasks(task_id=native["id"])[0]["state"], "pending")

    def test_verification_retry_recovers_original_operation_after_edits(self):
        plan = self.preview()["plan_digest"]
        queued = self.enqueue()
        (self.repo / "source.txt").write_text("later edit")
        retry = self.handoffs.verify(
            *self.scope, budget=24000, expected_plan=plan, profile="unit", key="verify"
        )
        self.assertEqual(retry["id"], queued["id"])
        with self.assertRaises(TaskConflict):
            self.handoffs.verify(
                *self.scope,
                budget=48000,
                expected_plan=plan,
                profile="unit",
                key="verify",
            )
        with self.assertRaises(TaskConflict):
            self.handoffs.verify(
                *self.scope,
                budget=24000,
                expected_plan=plan,
                profile="other",
                key="verify",
            )

    def test_unlinked_workspace_fails_without_starving_the_queue(self):
        queued = self.enqueue()
        with self.app.registry.connection(write=True) as db:
            db.execute(
                "UPDATE workspace_bindings SET active=0 WHERE id=?",
                (self.task["binding_id"],),
            )
        with patch.object(
            ExternalVerification, "request", side_effect=AssertionError("execution")
        ):
            self.assertTrue(self.local.process_once())
            self.assertFalse(self.local.process_once())
        result = self.local.operation(queued["id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["last_error_code"], "workspace_scope_changed")

    def test_reading_does_not_open_writable_store(self):
        original = Store.__init__

        def guarded(instance, path, *, read_only=False):
            self.assertTrue(read_only)
            original(instance, path, read_only=read_only)

        with patch.object(Store, "__init__", guarded):
            self.preview()
            self.handoffs.listing(*self.scope)
            self.local.listing(self.project_id)
