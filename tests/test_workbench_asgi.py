from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_workbench
from fastapi.testclient import TestClient

from reposteward.web_api.app import LocalSession, create_app


class WorkbenchASGITests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.service.projects.return_value = {
            "schema_version": 1,
            "projects": [],
            "omitted": 0,
            "public_write": False,
        }
        self.session = LocalSession("127.0.0.1:8765")
        self.app = create_app(self.service, session=self.session)
        self.client = TestClient(
            self.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 40000)
        )
        self.headers = {"Authorization": "Bearer " + self.session.token}

    def test_typed_response_preserves_readonly_facts(self):
        result = self.client.get("/api/v1/projects", headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["data"], self.service.projects.return_value)
        self.assertEqual(result.json()["meta"]["api_version"], "1")
        self.service.check_configuration.assert_called()

    def test_unknown_duplicate_and_unbound_parameters_do_not_reach_service(self):
        for path in (
            "/api/v1/projects?path=/etc/passwd",
            "/api/v1/projects?x=1&x=2",
            "/api/v1/workspace?project_id=bad&binding_id=bad",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.client.get(path, headers=self.headers).status_code, 400
                )
        self.service.projects.assert_not_called()

        self.service.workspace.assert_not_called()

    def test_authentication_precedes_query_validation(self):
        response = self.client.get(
            "/api/v1/projects?path=/etc/passwd",
            headers={**self.headers, "Origin": "http://foreign.example"},
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.get("/api/v1/projects?path=/etc/passwd")
        self.assertEqual(response.status_code, 401)

    def test_schema_can_be_exported_without_runtime_or_authentication(self):
        with (
            patch(
                "reposteward.github.resolve_token", side_effect=AssertionError("auth")
            ),
            patch("reposteward.store.Store", side_effect=AssertionError("store")),
        ):
            schema = create_app().openapi()
        self.assertEqual(schema["openapi"], "3.1.0")
        self.assertIn("Project", schema["components"]["schemas"])
        self.assertEqual(
            schema["paths"]["/api/v1/projects"]["get"]["operationId"], "projects"
        )
        saved = Path(__file__).resolve().parents[1] / "frontend/openapi.json"
        self.assertEqual(schema, json.loads(saved.read_text()))
        self.assertEqual(self.client.get("/api/v1/openapi.json").status_code, 401)

    def test_response_validation_does_not_leak_uncurated_objects(self):
        self.service.projects.return_value = {"projects": "private-invalid-object"}
        response = self.client.get("/api/v1/projects", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-invalid-object", response.text)

    def test_deep_link_and_missing_asset_have_distinct_results(self):
        for route in ("/projects", "/projects/" + "a" * 32 + "?focus=cli", "/settings"):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200)
            self.assertIn('id="root"', response.text)
        for route in ("/assets/missing.js", "/api/v1/not-found", "/projects/invalid"):
            response = self.client.get(route, headers=self.headers)
            self.assertEqual(response.status_code, 404)
            self.assertIn("application/json", response.headers["content-type"])

    def test_no_transport_method_can_trigger_a_write(self):
        for method in ("POST", "PATCH", "DELETE", "PUT"):
            response = self.client.request(
                method,
                "/api/v1/projects",
                headers=self.headers,
                json={"repository": "owner/repo"},
            )
            self.assertEqual(response.status_code, 405)
        self.service.projects.assert_not_called()


class WorkbenchResponseIntegrationTests(unittest.TestCase):
    def test_real_project_guide_and_task_records_match_the_public_contract(self):
        fixture = test_workbench.WorkbenchTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        session = LocalSession("127.0.0.1:8765")
        project = fixture.project_id
        binding = fixture.binding_id
        run = fixture.task["run_id"]
        source = fixture.app.workspace(project, binding)["guide"]["reading_path"][0][
            "source"
        ]
        routes = {
            "projects": {},
            "overview": {},
            "settings": {},
            "workspace": {"project_id": project, "binding_id": binding},
            "code": {
                "project_id": project,
                "binding_id": binding,
                "evidence_id": source["evidence_id"],
            },
            "tasks": {"project_id": project},
            "task": {"project_id": project, "run_id": run},
            "review": {"project_id": project, "run_id": run},
        }
        with TestClient(
            create_app(fixture.app, session=session),
            base_url=session.origin,
            client=("127.0.0.1", 40000),
        ) as client:
            for route, params in routes.items():
                with self.subTest(route=route):
                    response = client.get(
                        "/api/v1/" + route,
                        params=params,
                        headers={"Authorization": "Bearer " + session.token},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn("data", response.json())
