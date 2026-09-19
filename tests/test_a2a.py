from __future__ import annotations

import asyncio
import json
import socket
import time
import unittest
from threading import Thread
from unittest.mock import patch

import httpx
import test_external_tasks
import uvicorn
from a2a.client.transports.rest import RestTransport
from a2a.types import AgentCard, GetTaskRequest, ListTasksRequest, SendMessageRequest
from a2a.utils.errors import TaskNotFoundError
from fastapi.testclient import TestClient
from google.protobuf.json_format import ParseDict
from test_projects import git

from reposteward.integrations.a2a.cli import create_token, read_token
from reposteward.integrations.a2a.server import create_app
from reposteward.integrations.mcp import ScopedBridge
from reposteward.storage.local_queue import enqueue
from reposteward.tasks.assistance_operations import AssistanceOperations


class A2ATests(unittest.TestCase):
    def setUp(self):
        test_external_tasks.ExternalTaskTests.setUp(self)
        self.operations = AssistanceOperations(ScopedBridge(self.config, self.repo))
        self.operations.understanding.scan(self.repo)
        self.token_file = self.root / "a2a-token"
        create_token(self.token_file)
        self.token = read_token(self.token_file, self.repo)
        self.origin = "http://127.0.0.1:8765"
        self.headers = {"Authorization": "Bearer " + self.token, "A2A-Version": "1.0"}

    def client(self, *, worker=False):
        return TestClient(
            create_app(
                self.operations, origin=self.origin, token=self.token, run_worker=worker
            ),
            base_url=self.origin,
            headers=self.headers,
        )

    def message(self, key="one", *, immediate=True, focus="entry"):
        return {
            "message": {
                "messageId": key,
                "role": "ROLE_USER",
                "parts": [{"text": focus}],
            },
            "configuration": {"returnImmediately": immediate},
        }

    def test_card_and_auth_version_scope_boundaries(self):
        with (
            self.client() as client,
            patch(
                "reposteward.github.client.resolve_token",
                side_effect=AssertionError("no GitHub credential"),
            ),
        ):
            card = client.get("/.well-known/agent-card.json").json()
            ParseDict(card, AgentCard())
            self.assertFalse(card["capabilities"]["streaming"])
            self.assertNotIn(str(self.repo), json.dumps(card))
            self.assertEqual(
                client.get("/tasks", headers={"Authorization": "invalid"}).status_code,
                401,
            )
            wrong = client.get("/tasks", headers={"A2A-Version": "0.3"})
            self.assertEqual(wrong.status_code, 400)
            self.assertEqual(
                wrong.json()["error"]["details"][0]["reason"], "VERSION_NOT_SUPPORTED"
            )
            self.assertEqual(
                client.get("/tasks", headers={"Host": "remote.example"}).status_code,
                403,
            )
            self.assertEqual(
                client.get(
                    "/tasks", headers={"Origin": "https://remote.example"}
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post("/message:stream", json=self.message()).status_code, 400
            )
            self.assertFalse(self.operations.local.path.exists())

    def test_report_reconnect_worker_completion_and_idempotent_message(self):
        with self.client() as client:
            initial = client.post("/message:send", json=self.message()).json()["task"]
            self.assertEqual(initial["status"]["state"], "TASK_STATE_SUBMITTED")
            duplicate = client.post("/message:send", json=self.message()).json()["task"]
            self.assertEqual(initial["id"], duplicate["id"])
        with self.client(worker=True) as client:
            for _ in range(80):
                result = client.get("/tasks/" + initial["id"]).json()
                if result["status"]["state"] == "TASK_STATE_COMPLETED":
                    break
                time.sleep(0.05)
            self.assertEqual(result["status"]["state"], "TASK_STATE_COMPLETED")
            self.assertFalse(result["metadata"]["developmentTaskCompleted"])
            report = result["artifacts"][0]["parts"][0]["data"]
            self.assertEqual(report["current_applicability"], "not_checked")
            self.assertTrue(report["index_digest"])
            self.assertEqual(
                self.operations.get(initial["id"])["stages"][-1]["result"], report
            )

    def test_default_blocking_send_waits_for_terminal_report(self):
        with self.client(worker=True) as client:
            request = self.message()
            request.pop("configuration")
            result = client.post("/message:send", json=request)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(
                result.json()["task"]["status"]["state"], "TASK_STATE_COMPLETED"
            )

    def test_cancel_pending_and_reject_terminal_cancellation(self):
        with self.client() as client:
            task = client.post("/message:send", json=self.message()).json()["task"]
            cancelled = client.post("/tasks/" + task["id"] + ":cancel", json={})
            self.assertEqual(cancelled.json()["status"]["state"], "TASK_STATE_CANCELED")
            self.assertFalse(self.operations.process_once())
            again = client.post("/tasks/" + task["id"] + ":cancel", json={})
            self.assertEqual(again.status_code, 400)
            self.assertEqual(
                again.json()["error"]["details"][0]["reason"], "TASK_NOT_CANCELABLE"
            )

    def test_list_filters_bounded_cursor_and_untrusted_input(self):
        with self.client() as client:
            for n in range(3):
                self.assertEqual(
                    client.post(
                        "/message:send", json=self.message(str(n), focus=str(n))
                    ).status_code,
                    200,
                )
            first = client.get("/tasks?pageSize=1").json()
            self.assertEqual(first["totalSize"], 3)
            self.assertEqual(len(first["tasks"]), 1)
            self.assertNotIn("artifacts", first["tasks"][0])
            second = client.get(
                "/tasks", params={"pageSize": "1", "pageToken": first["nextPageToken"]}
            ).json()
            self.assertNotEqual(first["tasks"][0]["id"], second["tasks"][0]["id"])
            self.assertEqual(
                client.get(
                    "/tasks",
                    params={"pageSize": "2", "pageToken": first["nextPageToken"]},
                ).status_code,
                400,
            )
            self.assertEqual(client.get("/tasks?contextId=other").json()["tasks"], [])
            self.assertEqual(
                client.get("/tasks?status=TASK_STATE_COMPLETED").json()["totalSize"], 0
            )
            for path in (
                "/tasks?pageSize=101",
                "/tasks?historyLength=-1",
                "/tasks?includeArtifacts=unexpected",
                "/tasks?status=TASK_STATE_RUNNING",
            ):
                self.assertEqual(client.get(path).status_code, 400)
            invalid = self.message("bad")
            invalid["message"]["parts"] = [
                {"data": {"command": "private-secret-input"}}
            ]
            response = client.post("/message:send", json=invalid)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("private-secret-input", response.text)
            for mode in ({"private-secret-input": True}, [], None):
                invalid["message"]["parts"] = [{"data": {"mode": mode}}]
                response = client.post("/message:send", json=invalid)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("private-secret-input", response.text)
            oversized = client.post(
                "/message:send",
                content="x" * 64001,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(oversized.status_code, 400)

    def test_other_binding_cannot_read_or_cancel_reports(self):
        with self.client() as client:
            task = client.post("/message:send", json=self.message()).json()["task"]
        other = self.root / "other"
        git(self.repo, "worktree", "add", "-b", "owner/other", str(other))
        self.service.registry.link(other)
        other_ops = AssistanceOperations(ScopedBridge(self.config, other))
        with TestClient(
            create_app(
                other_ops, origin=self.origin, token=self.token, run_worker=False
            ),
            base_url=self.origin,
            headers=self.headers,
        ) as client:
            self.assertEqual(client.get("/tasks").json()["totalSize"], 0)
            self.assertEqual(client.get("/tasks/" + task["id"]).status_code, 404)
            self.assertEqual(
                client.post("/tasks/" + task["id"] + ":cancel", json={}).status_code,
                404,
            )

    def test_token_file_is_private_external_and_not_overwritten(self):
        with self.assertRaises(FileExistsError):
            create_token(self.token_file)
        link = self.root / "token-link"
        link.symlink_to(self.token_file)
        with self.assertRaises(OSError):
            read_token(link, self.repo)
        self.token_file.chmod(0o644)
        with self.assertRaises(ValueError):
            read_token(self.token_file, self.repo)
        with self.assertRaises(ValueError):
            read_token(self.repo / "token", self.repo)

    def test_report_worker_does_not_claim_verification_operations(self):
        store = self.operations.local.store(write=True)
        pending = enqueue(
            store,
            account=self.operations.local.account,
            project=self.operations.project,
            repository="owner/repo",
            action="assistance.verification",
            payload={"scope_digest": self.operations.scope},
            idempotency_key="verification-only",
            actor="owner",
        )
        self.assertFalse(
            self.operations.process_once(actions=("assistance.understanding",))
        )
        self.assertEqual(self.operations.get(pending["id"])["state"], "pending")
        with self.client() as client:
            self.assertEqual(client.get("/tasks").json()["totalSize"], 0)
            self.assertEqual(client.get("/tasks/" + pending["id"]).status_code, 404)

    def test_real_http_official_sdk_and_error_mapping(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(16)
            origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
            app = create_app(
                self.operations, origin=origin, token=self.token, run_worker=False
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app, log_level="error", access_log=False, proxy_headers=False
                )
            )
            thread = Thread(target=server.run, kwargs={"sockets": [listener]})
            thread.start()
            try:
                for _ in range(100):
                    if server.started:
                        break
                    time.sleep(0.02)
                self.assertTrue(server.started)

                async def run():
                    async with httpx.AsyncClient(
                        headers=self.headers, trust_env=False
                    ) as client:
                        card = ParseDict(
                            (
                                await client.get(
                                    origin + "/.well-known/agent-card.json"
                                )
                            ).json(),
                            AgentCard(),
                        )
                        sdk = RestTransport(client, card, origin)
                        sent = await sdk.send_message(
                            ParseDict(self.message(), SendMessageRequest())
                        )
                        self.assertTrue(sent.task.id)
                        await asyncio.to_thread(self.operations.process_once)
                        result = await sdk.get_task(GetTaskRequest(id=sent.task.id))
                        self.assertEqual(result.status.state, 3)
                        self.assertTrue(result.artifacts)
                        listing = await sdk.list_tasks(ListTasksRequest(page_size=1))
                        self.assertEqual(listing.tasks[0].id, sent.task.id)
                        with self.assertRaises(TaskNotFoundError):
                            await sdk.get_task(GetTaskRequest(id="f" * 32))

                asyncio.run(run())
            finally:
                server.should_exit = True
                thread.join(10)
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
