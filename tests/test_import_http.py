from __future__ import annotations

import unittest
from unittest.mock import patch

import test_project_import
from fastapi.testclient import TestClient

from reposteward.web.api.app import LocalSession, create_app


class ImportHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_project_import.ProjectImportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.session = LocalSession("127.0.0.1:8123")
        with patch(
            "reposteward.web.api.app.LocalOperations",
            return_value=self.fixture.operations,
        ):
            self.app = create_app(
                self.fixture.operations.workbench,
                session=self.session,
                manage_local=True,
            )
        self.client = TestClient(
            self.app, base_url=self.session.origin, client=("127.0.0.1", 1234)
        )
        self.addCleanup(self.client.close)
        self.headers = {
            "Authorization": "Bearer " + self.session.token,
            "Origin": self.session.origin,
            "Idempotency-Key": "request",
        }

    def test_import_commands_require_origin_session_and_bound_queries(self):
        body = {"kind": "github_url", "value": "https://github.com/owner/repo"}
        self.assertEqual(
            self.client.post(
                "/api/v1/commands/projects/inspect", json=body
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/commands/projects/inspect",
                json=body,
                headers={k: v for k, v in self.headers.items() if k != "Origin"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/import?import_id=" + "a" * 32 + "&unknown=1",
                headers=self.headers,
            ).status_code,
            400,
        )
        self.assertFalse(self.fixture.operations.path.exists())

    def test_url_credentials_rejected_with_useful_error_and_no_state(self):
        response = self.client.post(
            "/api/v1/commands/projects/inspect",
            json={
                "kind": "github_url",
                "value": "https://secret@github.com/owner/repo",
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "invalid_source")
        self.assertNotIn("secret", response.text)
        self.assertFalse(self.fixture.operations.path.exists())

    def test_import_dto_roundtrip_preview_apply_and_persistent_read(self):
        response = self.client.post(
            "/api/v1/commands/projects/inspect",
            json={"kind": "github_url", "value": "https://github.com/owner/repo"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertIn("operation_id=", response.headers["Location"])
        operation = response.json()["data"]
        self.fixture.operations.process_once()
        current = self.client.get(
            "/api/v1/import?import_id=" + operation["import_id"], headers=self.headers
        ).json()["data"]
        response = self.client.post(
            "/api/v1/commands/projects/plan",
            json={
                "import_id": current["id"],
                "inspection_id": current["inspection_id"],
                "method": "watch",
                "purpose": "watch",
                "target": "",
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()["data"]
        response = self.client.post(
            "/api/v1/commands/projects/apply",
            json={
                "import_id": current["id"],
                "preview_id": preview["id"],
                "expected_digest": preview["digest"],
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.fixture.operations.process_once()
        response = self.client.get(
            "/api/v1/import?import_id=" + current["id"], headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["operations"][0]["state"], "completed")
