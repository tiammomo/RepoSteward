from __future__ import annotations

import http.client
import io
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from threading import Thread
from unittest.mock import Mock

from reposteward.external_tasks import TaskConflict
from reposteward.web_server import LocalServer


class WorkbenchHTTPTests(unittest.TestCase):
    def setUp(self):
        self.app = Mock()
        self.app.settings.return_value = {"status": "local", "public_write": False}
        self.server = LocalServer(self.app)
        self.thread_errors = []

        def serve():
            try:
                self.server.serve_forever(poll_interval=0.01)
            except BaseException as exc:  # noqa: BLE001 - asserted by close_server
                self.thread_errors.append(exc)

        self.thread = Thread(
            target=serve,
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.thread_errors, [])

    def request(self, path="/api/settings", *, method="GET", headers=None, token=True):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        default = {"Authorization": "Bearer " + self.server.session} if token else {}
        try:
            connection.request(method, path, headers={**default, **(headers or {})})
            response = connection.getresponse()
            return (
                response.status,
                {k.title(): v for k, v in response.getheaders()},
                response.read(),
            )
        finally:
            connection.close()

    def test_loopback_session_and_fixed_assets(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertFalse(self.server.verify_request(None, ("192.0.2.1", 1234)))
        self.assertIn("/#session=", self.server.url)
        status, headers, body = self.request("/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"RepoSteward", body)
        self.assertNotIn(self.server.session.encode(), body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        for path in ("/app.js", "/app.css"):
            self.assertEqual(self.request(path, token=False)[0], 200)
        self.assertEqual(self.request(token=False)[0], 401)
        self.assertEqual(self.request()[0], 200)
        self.app.check_configuration.assert_called()

    def test_host_origin_and_fetch_metadata_are_checked_even_with_session(self):
        for headers in (
            {"Host": "evil.example"},
            {"Host": "localhost:" + str(self.server.server_port)},
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)
                self.assertEqual(self.request("/", headers=headers)[0], 403)
        self.assertEqual(
            self.request(
                headers={"Origin": self.server.origin, "Sec-Fetch-Site": "same-origin"}
            )[0],
            200,
        )

    def test_stale_wrong_and_duplicate_sessions_are_rejected(self):
        self.assertEqual(
            self.request(headers={"Authorization": "Bearer wrong"})[0], 401
        )
        self.assertEqual(self.request(headers={"Authorization": "Bearer café"})[0], 401)
        self.server.expires = time.monotonic() - 1
        self.assertEqual(self.request()[0], 401)
        self.app.settings.assert_not_called()

    def test_duplicate_host_or_authorization_header_is_rejected(self):
        for header, value, expected in (
            ("Host", self.server.authority, 403),
            ("Authorization", "Bearer " + self.server.session, 401),
        ):
            connection = http.client.HTTPConnection(
                "127.0.0.1", self.server.server_port, timeout=5
            )
            try:
                connection.putrequest("GET", "/api/settings")
                connection.putheader("Authorization", "Bearer " + self.server.session)
                connection.putheader(header, value)
                connection.endheaders()
                response = connection.getresponse()
                # h11 may reject duplicate Host before the ASGI boundary.
                if header == "Host":
                    self.assertIn(response.status, {400, expected})
                else:
                    self.assertEqual(response.status, expected)
                response.read()
            finally:
                connection.close()

    def test_write_methods_and_request_bodies_never_reach_services(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            self.assertEqual(self.request(method=method)[0], 405)
        for headers in ({"Content-Length": "8"}, {"Transfer-Encoding": "chunked"}):
            self.assertEqual(self.request(headers=headers)[0], 400)
        self.app.settings.assert_not_called()

    def test_only_fixed_routes_and_declared_bounded_parameters_are_accepted(self):
        self.assertEqual(self.request("http://evil.example/api/settings")[0], 403)
        for path in ("/../.env", "/api/refresh", "/favicon.ico"):
            self.assertEqual(self.request(path)[0], 404)
        for path in (
            "/%2e%2e/.env",
            "/api/settings?path=/etc/passwd",
            "/api/settings?x=1&x=2",
            "/api/workspace?project_id=abc",
            "/api/settings?" + "x" * 5000,
        ):
            self.assertEqual(self.request(path)[0], 400)
        self.app.settings.assert_not_called()

    def test_untrusted_errors_requests_and_tokens_do_not_enter_logs_or_html(self):
        secret = "private-test-value"
        self.app.settings.side_effect = RuntimeError(secret)
        logs = io.StringIO()
        with redirect_stderr(logs):
            status, headers, body = self.request("/api/settings")
            self.request("/not-found?secret=" + secret)
        self.assertEqual(status, 503)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertNotIn(secret, body.decode())
        self.assertNotIn(secret, logs.getvalue())
        self.assertNotIn(self.server.session, logs.getvalue())
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_changed_configuration_and_large_results_fail_without_partial_data(self):
        self.app.check_configuration.side_effect = TaskConflict("changed")
        self.assertEqual(self.request()[0], 409)
        self.app.check_configuration.side_effect = [
            None,
            TaskConflict("changed during read"),
        ]
        self.assertEqual(self.request()[0], 409)
        self.app.check_configuration.side_effect = None
        self.app.settings.return_value = {"data": "x" * 400001}
        status, _, body = self.request()
        self.assertEqual(status, 400)
        self.assertNotIn("data", json.loads(body))

    @unittest.skipUnless(
        shutil.which("node"), "Node is available in the verifier image"
    )
    def test_packaged_javascript_parses(self):
        data = self.server.assets["/app.js"][0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.js"
            path.write_bytes(data)
            result = subprocess.run(
                ["node", "--check", str(path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
