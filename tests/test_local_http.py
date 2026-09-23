from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import test_local_operations
from fastapi.testclient import TestClient

from reposteward.web.api.app import LocalSession, create_app


class LocalHTTPTests(unittest.TestCase):
    setUp = test_local_operations.LocalOperationTests.setUp
    pull = test_local_operations.LocalOperationTests.pull
    read = test_local_operations.LocalOperationTests.read

    def client(self, *, writable=True):
        session = LocalSession("127.0.0.1:8080")
        client = TestClient(
            create_app(
                self.operations.workbench,
                session=session,
                manage_local=writable,
                operations=self.operations,
            ),
            base_url=session.origin,
            client=("127.0.0.1", 1234),
            headers={
                "Authorization": "Bearer " + session.token,
                "Origin": session.origin,
                "Idempotency-Key": "http-request",
            },
        )
        self.addCleanup(client.close)
        return client

    def test_acceptance_is_durable_and_refresh_does_not_enqueue(self):
        client = self.client()
        response = client.post(
            "/api/v1/commands/github/sync", json={"project_id": self.project["id"]}
        )
        self.assertEqual(response.status_code, 202, response.text)
        identifier = response.json()["data"]["id"]
        replay = client.post(
            "/api/v1/commands/github/sync", json={"project_id": self.project["id"]}
        )
        self.assertEqual(replay.json()["data"]["id"], identifier)
        self.assertEqual(
            client.get(
                "/api/v1/operation", params={"operation_id": identifier}
            ).status_code,
            200,
        )
        self.assertEqual(len(self.operations.listing()["items"]), 1)
        self.assertFalse(self.calls)
        self.operations.process_once()
        response = client.get(
            "/api/v1/github", params={"project_id": self.project["id"]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["items"][0]["state"], "open")
        for route in (
            "/operations",
            "/operations/" + identifier,
            "/projects/" + self.project["id"] + "/github",
        ):
            self.assertEqual(client.get(route).status_code, 200, route)

    def test_read_only_and_invalid_commands_leave_no_database(self):
        reader = self.client(writable=False)
        self.assertEqual(
            reader.post(
                "/api/v1/commands/github/sync", json={"project_id": self.project["id"]}
            ).status_code,
            405,
        )
        client = self.client()
        for headers, status in [
            ({"Origin": "https://foreign.test"}, 403),
            ({"Authorization": "Bearer wrong"}, 401),
            ({"Idempotency-Key": "invalid key"}, 400),
            ({"Content-Type": "text/plain"}, 415),
        ]:
            response = client.post(
                "/api/v1/commands/github/sync",
                content=json.dumps({"project_id": self.project["id"]}),
                headers={"Content-Type": "application/json", **headers},
            )
            self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(
            client.post(
                "/api/v1/commands/github/sync",
                json={"project_id": self.project["id"], "execute": "anything"},
            ).status_code,
            400,
        )
        self.assertEqual(
            client.post("/api/v1/commands/execute", json={}).status_code, 405
        )
        self.assertEqual(
            client.post(
                "/api/v1/commands/github/sync",
                content=b" " * 64001,
                headers={"Content-Type": "application/json"},
            ).status_code,
            413,
        )
        self.assertFalse(self.operations.path.exists())

    def test_get_and_schema_have_no_database_auth_or_worker_side_effect(self):
        client = self.client()
        with patch.object(
            self.operations,
            "client_factory",
            side_effect=AssertionError("authentication"),
        ):
            for route in (
                "/api/v1/session",
                "/api/v1/openapi.json",
                "/api/v1/operations",
                "/api/v1/github?project_id=" + self.project["id"],
            ):
                self.assertEqual(client.get(route).status_code, 200, route)
        self.assertFalse(self.operations.path.exists())
        self.assertEqual(
            client.get(
                "/api/v1/github?project_id="
                + self.project["id"]
                + "&kind=pulls&kind=issues"
            ).status_code,
            400,
        )
        self.assertIn(
            "manage_local", client.get("/api/v1/session").json()["data"]["capabilities"]
        )
