from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from reposteward.config import RepositoryPolicy, load_config
from reposteward.models import AgentResult, VerificationResult
from reposteward.pipeline import Pipeline
from reposteward.policy import (
    DiffSummary,
    PolicyError,
    conventional_scope,
    enforce_change_policy,
)
from reposteward.verifier import DockerVerifier, VerificationError
from reposteward.workspace import sanitized_environment, slugify

ROOT = Path(__file__).resolve().parents[1]


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(ROOT / "examples" / "tiammomo.toml")
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

    def test_verification_command_count_is_bounded(self) -> None:
        verifier = DockerVerifier(self.config)
        agent_result = AgentResult(
            summary="summary",
            pr_title="fix(sdk): example",
            implementation_notes="notes",
            verification_commands=tuple(
                "cd libs/deepagents && uv run --group test pytest tests/unit_tests"
                for _ in range(13)
            ),
        )
        with (
            patch.object(verifier, "image_available", return_value=True),
            self.assertRaisesRegex(VerificationError, "at most 12"),
        ):
            verifier.verify(Path("/tmp/worktree"), self.repository, agent_result)

    def test_sensitive_environment_is_removed(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GITHUB_TOKEN": "secret",
                "SSH_AUTH_SOCK": "/tmp/agent.sock",
                "SERVICE_PASSWORD": "secret",
                "GIT_CONFIG_COUNT": "1",
            },
        ):
            child = sanitized_environment(keep_codex_credentials=False)
        self.assertNotIn("GITHUB_TOKEN", child)
        self.assertNotIn("SSH_AUTH_SOCK", child)
        self.assertNotIn("SERVICE_PASSWORD", child)
        self.assertNotIn("GIT_CONFIG_COUNT", child)

    def test_ssh_agent_is_available_only_to_explicit_host_git_operations(self) -> None:
        with patch.dict("os.environ", {"SSH_AUTH_SOCK": "/tmp/agent.sock"}):
            child = sanitized_environment(
                keep_codex_credentials=False,
                keep_ssh_credentials=True,
            )

        self.assertEqual(child["SSH_AUTH_SOCK"], "/tmp/agent.sock")

    def test_branch_slug_is_bounded(self) -> None:
        slug = slugify("BaseSandbox.grep path globs fail because shell is unsafe")
        self.assertLessEqual(len(slug), 48)
        self.assertNotIn(".", slug)

    def test_repository_change_limits_cannot_raise_user_capacity(self) -> None:
        repository = RepositoryPolicy(
            name="owner/repo",
            max_files_changed=100,
            max_diff_lines=10_000,
        )
        verification = VerificationResult(passed=True, commands=())
        successful_diff_check = Mock(returncode=0, stdout="")

        with (
            patch(
                "reposteward.policy.subprocess.run", return_value=successful_diff_check
            ),
            patch(
                "reposteward.policy.summarize_diff",
                return_value=DiffSummary(tuple(f"file-{i}" for i in range(41)), 1, 0),
            ),
            self.assertRaisesRegex(PolicyError, "policy limit is 40"),
        ):
            enforce_change_policy(Path("."), verification, repository, self.config)

        with (
            patch(
                "reposteward.policy.subprocess.run", return_value=successful_diff_check
            ),
            patch(
                "reposteward.policy.summarize_diff",
                return_value=DiffSummary(("file",), 2_001, 0),
            ),
            self.assertRaisesRegex(PolicyError, "policy limit is 2000"),
        ):
            enforce_change_policy(Path("."), verification, repository, self.config)

    def test_pull_request_body_contains_review_attestation(self) -> None:
        body = Pipeline._pull_request_body(
            5112,
            {
                "agent_result": {
                    "summary": "Path globs now execute safely.",
                    "implementation_notes": "The complete script is shell quoted.",
                    "verification_commands": ["pytest test_sandbox.py"],
                    "risks": [],
                },
            },
            "tiammomo",
        )
        self.assertIn("Closes #5112", body)
        self.assertIn("tiammomo", body)
        self.assertIn("takes responsibility", body)

    def test_pull_request_body_reports_the_actual_harness(self) -> None:
        body = Pipeline._pull_request_body(
            7,
            {
                "harness": {"name": "external-workspace"},
                "agent_result": {
                    "summary": "The edge case is handled.",
                    "implementation_notes": "The existing commit was verified.",
                    "verification_commands": ["pytest tests/test_edge.py"],
                    "risks": [],
                },
            },
            "tiammomo",
        )

        self.assertIn("an external coding workspace", body)
        self.assertNotIn("OpenAI Codex CLI", body)

    def test_deer_flow_body_preserves_required_ai_disclosure(self) -> None:
        policy = self.config.repositories["bytedance/deer-flow"]
        body = Pipeline._pull_request_body(
            123,
            {
                "changed_files": ["backend/tests/test_example.py"],
                "agent_result": {
                    "summary": "The failing request is handled consistently.",
                    "implementation_notes": "The backend now preserves the error.",
                    "verification_commands": ["cd backend && make test"],
                    "risks": [],
                },
            },
            "tiammomo",
            policy=policy,
        )
        self.assertIn("Fixes #123", body)
        self.assertIn("## Bug fix verification", body)
        self.assertIn("## AI assistance", body)
        self.assertIn("**Tool(s) used:** OpenAI Codex CLI", body)
        self.assertIn("- [x] I've read and understand every line", body)

    def test_openmldb_body_is_concise_and_preserves_template(self) -> None:
        policy = self.config.repositories["4paradigm/openmldb"]
        body = Pipeline._pull_request_body(
            939,
            {
                "agent_result": {
                    "summary": "Add a batch configuration.",
                    "implementation_notes": "Forward it to EngineOptions.",
                    "verification_commands": ["mvn test"],
                    "risks": [],
                }
            },
            "tiammomo",
            policy=policy,
        )
        self.assertIn("What kind of change", body)
        self.assertIn("Closes #939", body)
        self.assertIn("spark.openmldb.window.column.pruning=true", body)
        self.assertNotIn("AI assistance", body)
        self.assertLess(len(body.splitlines()), 20)

    def test_lazyllm_body_uses_the_feature_template(self) -> None:
        policy = self.config.repositories["lazyagi/lazyllm"]
        body = Pipeline._pull_request_body(
            838,
            {
                "agent_result": {
                    "summary": "Excel rows support a custom delimiter.",
                    "implementation_notes": "Store and use col_joiner.",
                    "verification_commands": [
                        ".venv/bin/python -m pytest tests/basic_tests/RAG/test_reader.py"
                    ],
                    "risks": [],
                },
            },
            "tiammomo",
            policy=policy,
        )
        self.assertIn("# 🚀 Feature", body)
        self.assertIn("Closes #838", body)
        self.assertIn("PandasExcelReader", body)

    def test_context_forge_body_uses_required_ai_disclosure(self) -> None:
        policy = self.config.repositories["ibm/mcp-context-forge"]
        body = Pipeline._pull_request_body(
            3590,
            {
                "agent_result": {
                    "summary": "Document PgBouncer settings.",
                    "implementation_notes": "Add the chart guide.",
                    "verification_commands": [
                        "cd charts/mcp-stack && helm lint .",
                        "PATH=.git/starfix-node/node_modules/.bin markdownlint-cli2 charts/mcp-stack/README.md",
                        "PATH=.git/starfix-node/node_modules/.bin UV_TOOL_DIR=.git/starfix-uv-tools make markdownlint spellcheck TARGET=charts/mcp-stack",
                    ],
                    "risks": [],
                }
            },
            "tiammomo",
            policy=policy,
        )
        self.assertIn("# 📚 Documentation PR", body)
        self.assertIn("Closes #3590", body)
        self.assertIn(
            "- [x] If AI-assisted, I understand and can explain the generated changes",
            body,
        )
        self.assertNotIn("PATH=.git", body)
        self.assertIn("make markdownlint spellcheck TARGET=charts/mcp-stack", body)

    def test_boxlite_body_preserves_call_graph_template(self) -> None:
        policy = self.config.repositories["boxlite-ai/boxlite"]
        body = Pipeline._pull_request_body(
            1111,
            {
                "agent_result": {
                    "summary": "Allow the guest-ready timeout to be configured.",
                    "implementation_notes": "Before\n  run ← BUG\n\nAfter\n  run",
                    "verification_commands": ["make test:unit:rust"],
                    "risks": [],
                }
            },
            "tiammomo",
            policy=policy,
        )
        self.assertIn("## Call graph", body)
        self.assertIn("← BUG", body)
        self.assertIn("Fixes #1111", body)
        self.assertNotIn("AI assistance", body)

    def test_paperqa_body_is_concise(self) -> None:
        policy = self.config.repositories["future-house/paper-qa"]
        body = Pipeline._pull_request_body(
            966,
            {
                "agent_result": {
                    "summary": "Honor disabled document validation.",
                    "implementation_notes": "Skip all text-shape checks when disabled.",
                    "verification_commands": [
                        "UV_CACHE_DIR=.git/cache uv run pytest tests/test_paperqa.py",
                        "PREK_HOME=.git/prek uv run prek run --all-files",
                        "UV_CACHE_DIR=.git/cache uv run pylint src packages",
                        "UV_CACHE_DIR=.git/cache uv run refurb .",
                    ],
                    "risks": [],
                }
            },
            "tiammomo",
            policy=policy,
        )
        self.assertIn("Fixes #966", body)
        self.assertIn("Verification:", body)
        self.assertIn("`uv run pytest tests/test_paperqa.py`", body)
        self.assertIn("`uv run prek run --all-files`", body)
        self.assertIn("`uv run pylint src packages`", body)
        self.assertIn("`uv run refurb .`", body)
        self.assertNotIn(".git/", body)
        self.assertNotIn("AI assistance", body)

    def test_hermes_body_preserves_project_template(self) -> None:
        policy = self.config.repositories["nousresearch/hermes-agent"]
        body = Pipeline._pull_request_body(
            77215,
            {
                "agent_result": {
                    "summary": "Accept common Base64 transport wrappers.",
                    "implementation_notes": "Normalize input before strict decoding.",
                    "verification_commands": [
                        "HERMES_PYTHON=.git/starfix-venv/bin/python scripts/run_tests.sh tests/tools/test_kanban_tools.py -q",
                        ".git/starfix-venv/bin/ruff check tools/kanban_tools.py",
                        ".git/starfix-venv/bin/python scripts/check-windows-footguns.py --diff origin/main",
                    ],
                    "risks": [],
                }
            },
            "tiammomo",
            policy=policy,
        )
        self.assertIn("## What does this PR do?", body)
        self.assertIn("Fixes #77215", body)
        self.assertIn("🐛 Bug fix", body)
        self.assertIn(
            "`scripts/run_tests.sh tests/tools/test_kanban_tools.py -q`", body
        )
        self.assertIn("`ruff check tools/kanban_tools.py`", body)
        self.assertIn(
            "`python scripts/check-windows-footguns.py --diff origin/main`", body
        )
        self.assertNotIn(".git/starfix-venv", body)
        self.assertNotIn("AI assistance", body)

    def test_cindy_body_preserves_project_template_with_concise_english_content(
        self,
    ) -> None:
        policy = self.config.repositories["makecindy/cindy"]
        body = Pipeline._pull_request_body(
            1433,
            {
                "changed_files": [
                    "apps/desktop/src/main/localDb/ipc/sessions.ts",
                    "apps/desktop/src/main/localDb/ipc/__tests__/sessionsUpdate.test.ts",
                ],
                "agent_result": {
                    "summary": "Broadcast local pin changes to connected clients.",
                    "implementation_notes": "Include pinnedAt in the existing session patch path.",
                    "verification_commands": [
                        "pnpm test:unit",
                        "NODE_OPTIONS=--max-old-space-size=6144 pnpm --filter desktop run --if-present typecheck",
                        "pnpm check:dco",
                    ],
                    "risks": [],
                },
            },
            "tiammomo",
            policy=policy,
        )
        self.assertIn("## 这次改了什么", body)
        self.assertIn("Fixes #1433", body)
        self.assertIn("Broadcast local pin changes", body)
        self.assertIn("Desktop-to-mobile end-to-end flow", body)
        self.assertIn("SSH remote workspaces: Not affected", body)
        self.assertIn("existing allowlisted `local-db:sessions:patched` push", body)
        self.assertIn("Existing remote-session patch handling applies `pinnedAt`", body)
        self.assertIn("Impact: Desktop session patch broadcasts only", body)
        self.assertIn("- [x] 无已知风险", body)
        self.assertNotIn("AI assistance", body)

    def test_cindy_mobile_body_describes_mobile_scope(self) -> None:
        policy = self.config.repositories["makecindy/cindy"]
        body = Pipeline._pull_request_body(
            1434,
            {
                "changed_files": [
                    "apps/mobile/app/devices/index.tsx",
                    "apps/mobile/src/__tests__/homeDesktopFirst.test.ts",
                    "apps/mobile/src/session/mobileHomeStartup.ts",
                ],
                "agent_result": {
                    "summary": "Allow the first mobile Home sync to proceed when local cache reads stall.",
                    "implementation_notes": "Bound startup cache reads with a two-second fallback.",
                    "verification_commands": [
                        "pnpm --filter mobile exec vitest run src/__tests__/homeDesktopFirst.test.ts",
                        "pnpm test:unit",
                        "pnpm --filter mobile run --if-present typecheck",
                        "pnpm check:dco",
                    ],
                    "risks": [],
                },
            },
            "tiammomo",
            policy=policy,
        )
        self.assertIn("Fixes #1434", body)
        self.assertIn("bounds local mobile cache reads", body)
        self.assertIn("No channel or protocol change", body)
        self.assertIn("No native configuration, fingerprint, or OTA change", body)
        self.assertIn("Real-device first-login flow with stalled native storage", body)
        self.assertIn("Impact: Mobile Home startup cache gating only", body)
        self.assertNotIn("`local-db:sessions:patched`", body)
        self.assertNotIn("`pinnedAt`", body)

    def test_cindy_composer_body_describes_interaction_and_remote_scope(
        self,
    ) -> None:
        policy = self.config.repositories["makecindy/cindy"]
        body = Pipeline._pull_request_body(
            1867,
            {
                "changed_files": [
                    "apps/desktop/src/renderer/components/new-chat/ChatInput.tsx",
                    "apps/desktop/src/renderer/components/new-chat/planModeComposerCommand.ts",
                ],
                "agent_result": {
                    "summary": "Add a capability-gated /plan composer command.",
                    "pr_title": "feat(desktop): add plan mode composer command",
                    "implementation_notes": "Reuse the existing plan-mode callback.",
                    "verification_commands": [
                        "pnpm test:unit",
                        "NODE_OPTIONS=--max-old-space-size=6144 pnpm --filter desktop run --if-present typecheck",
                        "pnpm check:dco",
                    ],
                    "risks": [],
                },
            },
            "tiammomo",
            policy=policy,
        )
        self.assertIn("Interaction only: selecting `/plan`", body)
        self.assertIn("`DESIGN.md` §14.3", body)
        self.assertIn("existing capability-gated plan-mode path", body)
        self.assertIn("No channel or protocol change", body)
        self.assertIn("Impact: Desktop composer plan-mode entry only", body)
        self.assertNotIn("Not included: UI", body)

    def test_cindy_browser_zoom_body_describes_ui_and_platform_scope(self) -> None:
        policy = self.config.repositories["makecindy/cindy"]
        body = Pipeline._pull_request_body(
            518,
            {
                "changed_files": [
                    "apps/desktop/src/renderer/features/right-sidebar/plugins/web-browser/BrowserChrome.tsx",
                    "apps/desktop/src/renderer/features/right-sidebar/plugins/web-browser/lib/browserZoom.ts",
                ],
                "agent_result": {
                    "summary": "Add per-tab page zoom controls.",
                    "pr_title": "feat(desktop): add per-tab browser zoom",
                    "implementation_notes": "Persist and apply the selected zoom level.",
                    "verification_commands": [
                        "pnpm test:unit",
                        "NODE_OPTIONS=--max-old-space-size=6144 pnpm --filter desktop run --if-present typecheck",
                        "pnpm check:dco",
                    ],
                    "risks": [],
                },
            },
            "tiammomo",
            policy=policy,
        )
        self.assertIn("Adds a compact zoom row", body)
        self.assertIn("Included: Per-tab zoom controls, persisted zoom state", body)
        self.assertIn("引用的设计规范：`DESIGN.md` §2", body)
        self.assertIn("`DESIGN.md` §2", body)
        self.assertIn("Light / Dark Dual-Mode Delivery Gate", body)
        self.assertIn(
            "Manual light/dark and native-popup checks on macOS and Windows", body
        )
        self.assertIn("- [x] 跨平台差异", body)
        self.assertIn("Impact: Desktop built-in browser zoom state", body)
        self.assertNotIn("UI 变化\n\nNot applicable", body)

    def test_cindy_test_body_marks_test_scope_without_user_visible_change(
        self,
    ) -> None:
        policy = self.config.repositories["makecindy/cindy"]
        body = Pipeline._pull_request_body(
            1625,
            {
                "changed_files": [
                    "packages/device-link/src/__tests__/client.test.ts",
                ],
                "agent_result": {
                    "summary": "Replace fixed sleeps with bounded condition waits.",
                    "pr_title": "test(device-link): wait for reconnect conditions",
                    "implementation_notes": "Tests only.",
                    "verification_commands": [
                        "pnpm test:unit",
                        "pnpm check:dco",
                    ],
                    "risks": [],
                },
            },
            "tiammomo",
            policy=policy,
        )
        self.assertIn("- [x] `docs` / `test` / `chore`", body)
        self.assertIn("- [ ] `fix`", body)
        self.assertIn(
            "User-visible change: None. This pull request changes tests only.",
            body,
        )
        self.assertIn(
            "Impact: DeviceLinkClient tests only; runtime behavior is unchanged.",
            body,
        )

    def test_xskill_body_preserves_project_template_and_verification_notes(
        self,
    ) -> None:
        policy = self.config.repositories["skillnerds/xskill"]
        body = Pipeline._pull_request_body(
            247,
            {
                "agent_result": {
                    "summary": "Enforce ClusterAgent candidate-write invariants.",
                    "implementation_notes": (
                        "- reject unscoped atoms\n"
                        "- reject duplicate owners\n\n"
                        "Verification notes:\n"
                        "Full suite: baseline-only failures reproduced on main."
                    ),
                    "verification_commands": [
                        ".venv/bin/python -m pytest tests/test_task_cluster_agent.py -q",
                        ".venv/bin/python -m ruff check src tests",
                    ],
                    "risks": [],
                },
            },
            "tiammomo",
            policy=policy,
        )
        self.assertIn("## Summary", body)
        self.assertIn("## Changes", body)
        self.assertIn("## Test plan", body)
        self.assertIn("- [ ] `make test` passes", body)
        self.assertIn("- [x] Added/updated unit tests", body)
        self.assertIn("baseline-only failures reproduced on main", body)
        self.assertIn("Closes #247", body)
        self.assertNotIn("AI assistance", body)


if __name__ == "__main__":
    unittest.main()
