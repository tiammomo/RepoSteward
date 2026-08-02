from __future__ import annotations

import os
import unittest
from pathlib import Path

from starfix.config import load_config
from starfix.pipeline import Pipeline
from starfix.policy import PolicyError, conventional_scope
from starfix.verifier import DockerVerifier, VerificationError
from starfix.workspace import sanitized_environment, slugify

ROOT = Path(__file__).resolve().parents[1]


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(ROOT / "starfix.toml")
        self.repository = self.config.repositories["langchain-ai/deepagents"]

    def test_conventional_title_returns_scope(self) -> None:
        self.assertEqual(
            conventional_scope("fix(sdk): safely quote path glob", "repo"), "sdk"
        )

    def test_unscoped_title_is_rejected(self) -> None:
        with self.assertRaises(PolicyError):
            conventional_scope("fix: safely quote path glob", "repo")

    def test_verification_command_requires_allowlisted_prefix(self) -> None:
        with self.assertRaises(VerificationError):
            DockerVerifier._validate_command("python arbitrary.py", self.repository)

    def test_verification_command_blocks_command_substitution(self) -> None:
        command = (
            "cd libs/deepagents && uv run --group test pytest "
            "tests/unit_tests/$(printenv SECRET)"
        )
        with self.assertRaises(VerificationError):
            DockerVerifier._validate_command(command, self.repository)

    def test_sensitive_environment_is_removed(self) -> None:
        old = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "secret"
        try:
            child = sanitized_environment(keep_codex_credentials=False)
        finally:
            if old is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = old
        self.assertNotIn("GITHUB_TOKEN", child)

    def test_branch_slug_is_bounded(self) -> None:
        slug = slugify("BaseSandbox.grep path globs fail because shell is unsafe")
        self.assertLessEqual(len(slug), 48)
        self.assertNotIn(".", slug)

    def test_pull_request_body_contains_review_attestation(self) -> None:
        body = Pipeline._pull_request_body(
            5112,
            {
                "agent_result": {
                    "summary": "Path globs now execute safely.",
                    "implementation_notes": "The complete script is shell quoted.",
                    "verification_commands": ["pytest test_sandbox.py"],
                    "risks": [],
                }
            },
            "tiammomo",
        )
        self.assertIn("Closes #5112", body)
        self.assertIn("tiammomo", body)
        self.assertIn("takes responsibility", body)


if __name__ == "__main__":
    unittest.main()
