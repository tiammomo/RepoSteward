from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from reposteward.cli import main
from reposteward.projects.registry import ProjectError, local_git


class LocalGitDiagnosticsTests(unittest.TestCase):
    def timeout(self) -> subprocess.TimeoutExpired:
        return subprocess.TimeoutExpired(
            ["git", "https://user:secret@example.invalid/private"],
            15,
            output=b"private-output",
            stderr=b"private-error",
        )

    def assert_sanitized(self, message: str) -> None:
        for value in ("secret", "example.invalid", "private-output", "private-error"):
            self.assertNotIn(value, message)

    def test_timeout_reports_deadline_and_workspace_guidance(self) -> None:
        for optional in (False, True):
            with self.subTest(optional=optional):
                error = self.timeout()
                with patch(
                    "reposteward.projects.registry.subprocess.run", side_effect=error
                ) as run:
                    with self.assertRaises(ProjectError) as raised:
                        local_git(
                            Path("/private-checkout"), "status", optional=optional
                        )
                message = str(raised.exception)
                self.assertIn("timed out", message)
                self.assertIn("15 seconds", message)
                self.assertIn("workspace", message)
                self.assertNotIn("/private-checkout", message)
                self.assert_sanitized(message)
                self.assertIs(raised.exception.__cause__, error)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.kwargs["timeout"], 15)

    def test_os_errors_remain_unavailable_and_sanitized(self) -> None:
        for error in (FileNotFoundError("secret"), PermissionError("private-error")):
            with self.subTest(error=type(error).__name__):
                with patch(
                    "reposteward.projects.registry.subprocess.run", side_effect=error
                ):
                    with self.assertRaises(ProjectError) as raised:
                        local_git(Path("/private-checkout"), "status")
                self.assertEqual(str(raised.exception), "local Git query unavailable")
                self.assertIs(raised.exception.__cause__, error)

    def run_scan(self, *, machine: bool) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = ["--json-envelope"] if machine else []
            arguments += [
                "understand",
                "scan",
                str(root),
                "--cache-dir",
                str(root / "cache"),
            ]
            with (
                patch(
                    "reposteward.projects.registry.subprocess.run",
                    side_effect=self.timeout(),
                ),
                redirect_stdout(io.StringIO()) as output,
                redirect_stderr(io.StringIO()) as errors,
            ):
                code = main(arguments)
            return code, output.getvalue(), errors.getvalue()

    def test_scan_cli_reports_timeout_without_traceback(self) -> None:
        code, output, errors = self.run_scan(machine=False)
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("timed out", errors)
        self.assertIn("15 seconds", errors)
        self.assertIn("workspace", errors)
        self.assertNotIn("Traceback", errors)
        self.assert_sanitized(errors)

    def test_scan_machine_error_keeps_existing_contract(self) -> None:
        code, output, errors = self.run_scan(machine=True)
        self.assertEqual(code, 2)
        self.assertEqual(errors, "")
        error = json.loads(output)["error"]
        self.assertEqual(error["code"], "scope_or_policy")
        self.assertFalse(error["retryable"])
        self.assert_sanitized(output)


if __name__ == "__main__":
    unittest.main()
