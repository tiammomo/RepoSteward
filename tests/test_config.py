from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from reposteward.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_personal_example_keeps_existing_repository_policies(self) -> None:
        config = load_config(ROOT / "examples" / "tiammomo.toml")

        self.assertEqual(config.github.login, "tiammomo")
        self.assertEqual(config.workspace_dir, config.state_dir / "workspaces")
        self.assertEqual(config.runner.max_output_chars, 12_000)
        self.assertEqual(config.runner.passed_output_chars, 2_000)
        self.assertEqual(config.runner.max_log_chars, 2_000_000)
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
        lazyllm = config.repositories["lazyagi/lazyllm"]
        self.assertEqual(lazyllm.pull_request_body_style, "lazyllm-feature")
        self.assertTrue(lazyllm.require_no_competing_work)
        self.assertIn("make lint-only-diff", lazyllm.required_verification_markers)
        context_forge = config.repositories["ibm/mcp-context-forge"]
        self.assertEqual(
            context_forge.pull_request_body_style, "mcp-context-forge-docs"
        )
        self.assertIn("triage", context_forge.blocked_labels)
        self.assertIn("helm lint", context_forge.required_verification_markers)
        self.assertIn(
            "make markdownlint spellcheck",
            context_forge.required_verification_markers,
        )
        boxlite = config.repositories["boxlite-ai/boxlite"]
        self.assertEqual(boxlite.pull_request_body_style, "boxlite")
        self.assertIn("src/CLAUDE.md", boxlite.required_contribution_files)
        paperqa = config.repositories["future-house/paper-qa"]
        self.assertEqual(paperqa.pull_request_body_style, "paperqa")
        self.assertIn("pytest", paperqa.required_verification_markers)
        hermes = config.repositories["nousresearch/hermes-agent"]
        self.assertEqual(hermes.pull_request_body_style, "hermes-agent")
        self.assertIn("scripts/run_tests.sh", hermes.required_verification_markers)
        cindy = config.repositories["makecindy/cindy"]
        self.assertEqual(cindy.pull_request_body_style, "cindy")
        self.assertTrue(cindy.require_no_competing_work)
        self.assertIn("AGENTS.md", cindy.required_contribution_files)
        self.assertIn("test:unit", cindy.required_verification_markers)
        self.assertIn(
            "NODE_OPTIONS=--max-old-space-size=6144 pnpm --filter desktop run --if-present typecheck",
            cindy.verification_prefixes,
        )
        self.assertIn(
            "pnpm --filter mobile exec vitest run ",
            cindy.verification_prefixes,
        )
        self.assertIn(
            "pnpm --filter mobile run --if-present typecheck",
            cindy.verification_prefixes,
        )
        xskill = config.repositories["skillnerds/xskill"]
        self.assertEqual(xskill.pull_request_body_style, "xskill")
        self.assertTrue(xskill.require_no_competing_work)
        self.assertIn("pytest", xskill.required_verification_markers)
        self.assertIn(
            ".github/PULL_REQUEST_TEMPLATE.md", xskill.required_contribution_files
        )
        self.assertIn("ruff==0.15.20", " ".join(xskill.bootstrap_commands))

    def test_missing_config_has_clear_error(self) -> None:
        with self.assertRaisesRegex(ConfigError, "configuration not found"):
            load_config(ROOT / "does-not-exist.toml")

    def test_project_configuration_layers_over_arbitrary_user_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            user = root / "user.toml"
            project = root / "project" / ".reposteward.toml"
            project.parent.mkdir()
            user.write_text(
                """config_version = 1
[github]
login = "alice"
git_name = "Alice"
git_email = "alice@example.com"
[safety]
max_diff_lines = 120
require_verification = true
[runner]
cpus = 2
image = "trusted-runner:latest"
[agent]
harness = "codex-cli"
executable = "codex"
""",
                encoding="utf-8",
            )
            project.write_text(
                """config_version = 1
[project]
state_dir = ".state"
workspace_dir = ".workspaces"
[runner]
memory = "4g"
image = "untrusted-runner:latest"
[agent]
harness = "unknown"
executable = "untrusted-agent"
[github]
login = "mallory"
[issue_review]
project_owner = "mallory"
project_number = 99
project_owner_type = "organization"
require_distinct_reviewer = false
[safety]
max_diff_lines = 500
require_verification = false
forbidden_paths = []
[repositories."owner/repo"]
max_diff_lines = 99
merge_risk_paths = ["docs/operator/**"]
event_payload_retention_days = 45
""",
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "XDG_STATE_HOME": str(root / "user-state"),
                    "XDG_DATA_HOME": str(root / "user-data"),
                },
            ):
                config = load_config(project, user_path=user)

        self.assertEqual(config.github.login, "alice")
        self.assertEqual(config.github.git_name, "Alice")
        self.assertEqual(config.runner.cpus, 2)
        self.assertEqual(config.runner.memory, "4g")
        self.assertEqual(config.runner.image, "trusted-runner:latest")
        self.assertEqual(config.agent.harness, "codex-cli")
        self.assertEqual(config.agent.executable, "codex")
        self.assertEqual(config.issue_review.project_owner, "")
        self.assertEqual(config.issue_review.project_number, 0)
        self.assertTrue(config.issue_review.require_distinct_reviewer)
        self.assertEqual(config.safety.max_diff_lines, 120)
        self.assertTrue(config.safety.require_verification)
        self.assertIn(".github/workflows/", config.safety.forbidden_paths)
        self.assertEqual(
            config.state_dir,
            root / "user-state" / "reposteward" / "api.github.com" / "alice",
        )
        self.assertEqual(
            config.workspace_dir,
            root
            / "user-data"
            / "reposteward"
            / "workspaces"
            / "api.github.com"
            / "alice",
        )
        self.assertTrue(config.state_namespace)
        self.assertEqual(config.repositories["owner/repo"].max_diff_lines, 99)
        self.assertEqual(
            config.repositories["owner/repo"].merge_risk_paths,
            ("docs/operator/**",),
        )
        self.assertEqual(
            config.repositories["owner/repo"].event_payload_retention_days, 45
        )

    def test_event_payload_retention_must_be_explicit_and_positive(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """config_version = 1
[github]
login = "alice"
[repositories."owner/repo"]
event_payload_retention_days = 0
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "must be positive"):
                load_config(path)

    def test_configuration_is_not_pinned_to_one_github_login(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """config_version = 1
[github]
login = "bob"
""",
                encoding="utf-8",
            )

            config = load_config(path)

        self.assertEqual(config.github.login, "bob")
        self.assertEqual(config.github.git_name, "bob")
        self.assertEqual(config.github.git_email, "bob@users.noreply.github.com")
        self.assertEqual(config.repositories, {})

    def test_project_cannot_select_a_harness_or_runner_image_when_user_omits_them(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            user = root / "user.toml"
            project = root / "project.toml"
            user.write_text(
                """config_version = 1
[github]
login = "alice"
""",
                encoding="utf-8",
            )
            project.write_text(
                """config_version = 1
[agent]
harness = "untrusted"
executable = "untrusted-agent"
[runner]
image = "untrusted-runner:latest"
""",
                encoding="utf-8",
            )

            config = load_config(project, user_path=user)

        self.assertEqual(config.agent.harness, "codex-cli")
        self.assertEqual(config.agent.executable, "codex")
        self.assertEqual(config.runner.image, "reposteward-runner:latest")

    def test_user_config_owns_the_shared_issue_review_project(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            user = root / "user.toml"
            project = root / "project.toml"
            user.write_text(
                """config_version = 1
[github]
login = "alice"
[issue_review]
project_owner = "trusted-team"
project_number = 7
project_owner_type = "organization"
require_distinct_reviewer = true
""",
                encoding="utf-8",
            )
            project.write_text(
                """config_version = 1
[issue_review]
project_owner = "attacker"
project_number = 99
project_owner_type = "user"
require_distinct_reviewer = false
""",
                encoding="utf-8",
            )

            config = load_config(project, user_path=user)

        self.assertEqual(config.issue_review.project_owner, "trusted-team")
        self.assertEqual(config.issue_review.project_number, 7)
        self.assertEqual(config.issue_review.project_owner_type, "organization")
        self.assertTrue(config.issue_review.require_distinct_reviewer)

    def test_unknown_configuration_version_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """config_version = 2
[github]
login = "alice"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "unsupported config_version"):
                load_config(path)

    def test_project_version_cannot_mask_unsupported_user_version(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            user = root / "user.toml"
            project = root / "project.toml"
            user.write_text(
                """config_version = 2
[github]
login = "alice"
""",
                encoding="utf-8",
            )
            project.write_text(
                """config_version = 1
[repositories."owner/repo"]
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "unsupported config_version 2"):
                load_config(project, user_path=user)

    def test_same_repository_submission_requires_maintainer_mode(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """config_version = 1
[github]
login = "alice"
[repositories."owner/repo"]
mode = "contributor"
submission_strategy = "same-repository"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "only in maintainer mode"):
                load_config(path)

    def test_quoted_boolean_is_rejected_instead_of_becoming_true(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """config_version = 1
[github]
login = "alice"
[safety]
require_verification = "false"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "expected a boolean"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
