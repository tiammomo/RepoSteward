from __future__ import annotations

import unittest
from pathlib import Path

from starfix.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_project_config_is_pinned_to_tiammomo_and_project_policies(self) -> None:
        config = load_config(ROOT / "starfix.toml")

        self.assertEqual(config.github.login, "tiammomo")
        policy = config.repositories["langchain-ai/deepagents"]
        self.assertTrue(policy.enabled)
        self.assertTrue(policy.require_assignment_before_submit)
        self.assertIn("pytest", policy.required_verification_markers)
        deer_flow = config.repositories["bytedance/deer-flow"]
        self.assertTrue(deer_flow.enabled)
        self.assertFalse(deer_flow.auto_prepare)
        self.assertTrue(deer_flow.require_no_competing_work)
        self.assertEqual(deer_flow.pull_request_body_style, "deer-flow")
        self.assertIn("CONTRIBUTING.md", deer_flow.required_contribution_files)
        openmldb = config.repositories["4paradigm/openmldb"]
        self.assertTrue(openmldb.enabled)
        self.assertFalse(openmldb.auto_prepare)
        self.assertTrue(openmldb.require_assignment_before_submit)
        self.assertTrue(openmldb.require_no_competing_work)
        self.assertEqual(openmldb.pull_request_body_style, "openmldb")
        self.assertIn("TestSparkPlanner", openmldb.required_verification_markers)
        self.assertEqual(openmldb.default_scope, "batch")

    def test_missing_config_has_clear_error(self) -> None:
        with self.assertRaisesRegex(ConfigError, "configuration not found"):
            load_config(ROOT / "does-not-exist.toml")


if __name__ == "__main__":
    unittest.main()
