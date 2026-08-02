from __future__ import annotations

import unittest
from pathlib import Path

from starfix.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_project_config_is_pinned_to_tiammomo_and_deepagents(self) -> None:
        config = load_config(ROOT / "starfix.toml")

        self.assertEqual(config.github.login, "tiammomo")
        policy = config.repositories["langchain-ai/deepagents"]
        self.assertTrue(policy.enabled)
        self.assertTrue(policy.require_assignment_before_submit)
        self.assertIn("pytest", policy.required_verification_markers)

    def test_missing_config_has_clear_error(self) -> None:
        with self.assertRaisesRegex(ConfigError, "configuration not found"):
            load_config(ROOT / "does-not-exist.toml")


if __name__ == "__main__":
    unittest.main()
