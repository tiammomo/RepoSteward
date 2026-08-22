from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reposteward.agent import CodexCliHarness
from reposteward.codex_sdk import CodexSdkHarness
from reposteward.config import AgentConfig, load_config
from reposteward.doctor import run_doctor
from reposteward.harness import (
    CapabilitySupport,
    HarnessCapabilities,
    harness_capabilities,
)


class HarnessCapabilitiesTests(unittest.TestCase):
    def test_builtin_harnesses_publish_versioned_capabilities(self) -> None:
        cli = CodexCliHarness(AgentConfig())
        sdk = CodexSdkHarness(AgentConfig(harness="codex-sdk"))

        self.assertEqual(cli.capabilities.schema_version, 1)
        self.assertTrue(cli.capabilities.workspace_write.supported)
        self.assertTrue(cli.capabilities.structured_result.supported)
        self.assertEqual(cli.capabilities.native_session_resume.state, "unsupported")
        self.assertTrue(sdk.capabilities.native_session_resume.supported)
        self.assertEqual(sdk.capabilities.usage_metrics.state, "best_effort")

    def test_legacy_or_invalid_manifest_fails_closed(self) -> None:
        class LegacyHarness:
            name = "legacy"

        class FutureHarness:
            name = "future"
            capabilities = HarnessCapabilities.unknown("future_schema")

        class MalformedHarness:
            name = "malformed"
            capabilities = HarnessCapabilities.unknown("malformed")

        future = FutureHarness()
        object.__setattr__(future.capabilities, "schema_version", 2)
        malformed = MalformedHarness()
        object.__setattr__(malformed.capabilities, "native_session_resume", "supported")

        for harness in (LegacyHarness(), future, malformed):
            capabilities = harness_capabilities(harness)
            self.assertEqual(capabilities.schema_version, 1)
            self.assertEqual(capabilities.workspace_write.state, "unknown")
            self.assertFalse(capabilities.native_session_resume.supported)

    def test_capability_values_are_validated_at_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid harness capability state"):
            CapabilitySupport("yes")  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "reason must be a string"):
            CapabilitySupport("supported", 1)  # type: ignore[arg-type]

    def test_manifest_serialization_contains_no_implicit_booleans(self) -> None:
        payload = CodexCliHarness(AgentConfig()).capabilities.to_dict()

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(
            payload["native_session_resume"],
            {"state": "unsupported", "reason": "ephemeral_process"},
        )
        self.assertEqual(
            payload["usage_metrics"],
            {"state": "best_effort", "reason": "event_stream_optional"},
        )

    def test_doctor_reports_the_adapter_manifest(self) -> None:
        config = load_config(Path("examples/tiammomo.toml"), include_user=False)
        completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")

        with (
            patch("reposteward.doctor.shutil.which", return_value="/bin/tool"),
            patch("reposteward.doctor.subprocess.run", return_value=completed),
            patch(
                "reposteward.doctor.DockerVerifier.image_available",
                return_value=True,
            ),
            patch(
                "reposteward.doctor.resolve_authentication",
                return_value=(None, "missing"),
            ),
        ):
            report, ok = run_doctor(config)

        self.assertFalse(ok)
        self.assertEqual(report["harness"]["capabilities"]["schema_version"], 1)
        self.assertEqual(
            report["harness"]["capabilities"]["native_session_resume"]["state"],
            "unsupported",
        )


if __name__ == "__main__":
    unittest.main()
