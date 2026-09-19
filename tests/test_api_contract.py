from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from jsonschema import Draft202012Validator

from reposteward.api_contract import error_details, tool_output_schema
from reposteward.cli import main
from reposteward.config import ConfigError
from reposteward.external_tasks import TaskConflict
from reposteward.policy import PolicyError


class MachineContractTests(unittest.TestCase):
    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_offline_discovery_never_loads_config_or_creates_store(self):
        with (
            patch("reposteward.cli.load_config", side_effect=AssertionError("config")),
            patch(
                "reposteward.store.Store.__init__", side_effect=AssertionError("store")
            ),
        ):
            code, output, errors = self.invoke("--json-envelope", "capabilities")
        value = json.loads(output)
        self.assertEqual((code, errors, value["error"]), (0, "", None))
        self.assertEqual(value["schema_version"], 1)
        self.assertIn("task resolve", value["data"]["cli"]["commands"])
        self.assertFalse(value["data"]["a2a"]["implemented"])
        for tool in value["data"]["mcp"]["tools"].values():
            Draft202012Validator.check_schema(tool["output_schema"])

    def test_legacy_output_and_envelope_are_separate_invocations(self):
        first = json.loads(self.invoke("--json-envelope", "version")[1])
        second = json.loads(self.invoke("version")[1])
        self.assertEqual(first["data"], second)
        self.assertNotIn("data", second)

    def test_invalid_parameters_have_one_safe_machine_error(self):
        for args in (("bad-secret-command",), ("task", "inspect"), ("--help",)):
            with self.subTest(args=args):
                code, output, errors = self.invoke("--json-envelope", *args)
                self.assertEqual((code, errors), (2, ""))
                result = json.loads(output)
                self.assertIsNone(result["data"])
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertNotIn("bad-secret", output)

    def test_config_error_is_redacted_and_not_retry_authority(self):
        with patch(
            "reposteward.cli.load_config", side_effect=ConfigError("secret-value")
        ):
            code, output, errors = self.invoke("--json-envelope", "project", "list")
        result = json.loads(output)
        self.assertEqual((code, errors), (2, ""))
        self.assertEqual(result["error"]["code"], "configuration_error")
        self.assertFalse(result["error"]["retryable"])
        self.assertNotIn("secret-value", output)

    def test_legacy_argument_failure_still_uses_argparse(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["missing-command"])
        self.assertEqual(error.exception.code, 2)

    def test_long_lived_commands_are_rejected_before_configuration(self):
        with patch("reposteward.cli.load_config", side_effect=AssertionError("config")):
            for args in (("web",), ("mcp", "serve", "."), ("image", "build")):
                with self.subTest(args=args):
                    code, output, errors = self.invoke("--json-envelope", *args)
                    self.assertEqual((code, errors), (2, ""))
                    self.assertEqual(
                        json.loads(output)["error"]["code"], "invalid_request"
                    )

    def test_shared_error_classification_and_schema(self):
        for exception, code in (
            (TaskConflict("secret"), "conflict"),
            (PolicyError("secret"), "scope_or_policy"),
            (KeyError("secret"), "not_found"),
            (RuntimeError("secret"), "unavailable"),
        ):
            result = {"error": error_details(exception), "public_write": False}
            self.assertEqual(result["error"]["code"], code)
            self.assertNotIn("secret", json.dumps(result))
            Draft202012Validator(tool_output_schema("context")).validate(result)

    def test_unavailable_evidence_is_a_valid_successful_observation(self):
        Draft202012Validator(tool_output_schema("evidence")).validate(
            {
                "evidence_id": "source:abc",
                "availability": "unknown",
                "reason": "missing",
            }
        )
