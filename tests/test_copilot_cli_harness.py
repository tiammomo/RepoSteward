from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reposteward.agents.agent import AgentError
from reposteward.agents.copilot_cli import CopilotCliHarness, resolve_executable
from reposteward.agents.harness import (
    HarnessRequest,
    create_harness,
    harness_capabilities,
)
from reposteward.context.pack import build_context_pack
from reposteward.core.config import AgentConfig, ConfigError, RepositoryPolicy
from reposteward.core.models import Candidate, Issue, RepositoryInfo

STRUCTURED_RESULT = {
    "summary": "Fixed the edge case.",
    "pr_title": "fix(repo): handle edge case",
    "implementation_notes": "Changed one branch.",
    "verification_commands": ["pytest tests/test_edge.py"],
    "tests_observed": ["pytest tests/test_edge.py"],
    "risks": [],
    "decisions": [],
    "next_actions": ["Review the diff."],
}


def _context(root: Path, *, harness: str = "copilot-cli"):
    candidate = Candidate(
        issue=Issue(
            repository="owner/repo",
            number=7,
            node_id=8,
            title="Fix the edge case",
            body="Reproduce the bug",
            url="https://github.com/owner/repo/issues/7",
            labels=("bug",),
            comments=0,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
            author_login="reporter",
            author_association="NONE",
        ),
        repository=RepositoryInfo(
            full_name="owner/repo",
            default_branch="main",
            stars=1000,
            forks=20,
            open_issues=5,
            pushed_at="2026-01-02T00:00:00Z",
            archived=False,
            is_fork=False,
        ),
        score=50,
    )
    return build_context_pack(
        candidate,
        RepositoryPolicy(name="owner/repo", verification_prefixes=("pytest ",)),
        work_item_id="work-1",
        run_id="run-1",
        worktree=root,
        base_commit="a" * 40,
        harness=harness,
        model="gpt-5",
    )


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _success_runner(calls: list, result_path: Path, body: dict):
    """subprocess.run stand-in that makes the agent write the result file."""

    def runner(command, **kwargs):
        calls.append((list(command), kwargs))
        result_path.write_text(json.dumps(body), encoding="utf-8")
        return _completed()

    return runner


class CopilotCliHarnessTests(unittest.TestCase):
    def _harness(self, **overrides) -> CopilotCliHarness:
        options = {"harness": "copilot-cli", "timeout_seconds": 5}
        options.update(overrides)
        return CopilotCliHarness(AgentConfig(**options))

    def test_factory_selects_copilot_cli_without_importing_at_module_import(self):
        harness = create_harness(AgentConfig(harness="copilot-cli"))
        self.assertIsInstance(harness, CopilotCliHarness)
        self.assertEqual(harness.name, "copilot-cli")

    def test_default_harness_and_executable_are_unchanged(self):
        config = AgentConfig()
        self.assertEqual(config.harness, "codex-cli")
        self.assertEqual(config.executable, "codex")
        self.assertEqual(AgentConfig(harness="copilot-cli").executable, "codex")
        self.assertEqual(resolve_executable(AgentConfig()), "copilot")

    def test_unknown_harness_still_fails_closed(self):
        with self.assertRaises(ConfigError):
            create_harness(AgentConfig(harness="not-a-harness"))

    def test_capabilities_are_declared_and_survive_fail_closed_check(self):
        declared = CopilotCliHarness.capabilities
        self.assertEqual(declared.schema_version, 1)
        self.assertEqual(declared.workspace_write.state, "supported")
        self.assertEqual(declared.structured_result.state, "supported")
        self.assertEqual(declared.native_session_resume.state, "unsupported")
        self.assertEqual(declared.usage_metrics.state, "best_effort")
        self.assertEqual(
            harness_capabilities(self._harness()).to_dict(), declared.to_dict()
        )

    def test_structured_run_reads_the_result_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            result_path = run_dir / "agent-result.json"
            calls: list = []
            runner = _success_runner(calls, result_path, STRUCTURED_RESULT)
            with patch("reposteward.agents.copilot_cli.subprocess.run", runner):
                execution = self._harness(model="gpt-5").run(
                    HarnessRequest(
                        worktree=root, run_dir=run_dir, context=_context(root)
                    )
                )
            command = calls[0][0]

        self.assertEqual(command[0], "copilot")
        self.assertEqual(command[1], "-p")
        self.assertIn("owner/repo#7", command[2])
        self.assertIn("agent-result.json", command[2])
        # --allow-tool 出现两次，用相邻对扫描才能区分两个取值。
        pairs = {(command[i], command[i + 1]) for i in range(len(command) - 1)}
        self.assertIn(("--allow-tool", "write"), pairs)
        self.assertIn(("--allow-tool", "shell"), pairs)
        self.assertIn(("--deny-tool", "shell(git push)"), pairs)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5")
        self.assertNotIn("--resume", command)
        self.assertNotIn("--allow-all-tools", command)

        self.assertEqual(execution.harness, "copilot-cli")
        self.assertEqual(execution.result.pr_title, "fix(repo): handle edge case")
        self.assertEqual(execution.result.next_actions, ("Review the diff.",))
        self.assertIsNone(execution.metrics.input_tokens)
        self.assertIsNone(execution.metrics.output_tokens)
        self.assertEqual(calls[0][1]["cwd"], root)

    def test_github_credentials_never_reach_the_harness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_path = root / "run" / "agent-result.json"
            calls: list = []
            runner = _success_runner(calls, result_path, STRUCTURED_RESULT)
            environment = {
                "GITHUB_TOKEN": "gh-secret",
                "GH_TOKEN": "gh-secret",
                "UNRELATED_SECRET": "shh",
                "UNRELATED_TOKEN": "shh",
            }
            with (
                patch.dict("os.environ", environment, clear=False),
                patch("reposteward.agents.copilot_cli.subprocess.run", runner),
            ):
                self._harness().run(
                    HarnessRequest(
                        worktree=root, run_dir=root / "run", context=_context(root)
                    )
                )
            passed = calls[0][1]["env"]

        self.assertNotIn("GITHUB_TOKEN", passed)
        self.assertNotIn("GH_TOKEN", passed)
        self.assertNotIn("UNRELATED_SECRET", passed)
        self.assertNotIn("UNRELATED_TOKEN", passed)
        self.assertEqual(passed["GIT_TERMINAL_PROMPT"], "0")

    def test_nonzero_exit_raises_without_switching_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls: list = []

            def runner(command, **kwargs):
                calls.append((list(command), kwargs))
                return _completed(returncode=2, stdout="", stderr="boom")

            with (
                patch("reposteward.agents.copilot_cli.subprocess.run", runner),
                self.assertRaisesRegex(AgentError, "exited with 2"),
            ):
                self._harness().run(
                    HarnessRequest(
                        worktree=root, run_dir=root / "run", context=_context(root)
                    )
                )
            log = (root / "run" / "copilot.log").read_text(encoding="utf-8")
            self.assertIn("boom", log)

        self.assertEqual(len(calls), 1)

    def test_timeout_raises_and_keeps_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls: list = []

            def runner(command, **kwargs):
                calls.append((list(command), kwargs))
                raise subprocess.TimeoutExpired(
                    cmd="copilot", timeout=5, output="partial", stderr="slow"
                )

            with (
                patch("reposteward.agents.copilot_cli.subprocess.run", runner),
                self.assertRaisesRegex(AgentError, "exceeded the 5s timeout"),
            ):
                self._harness().run(
                    HarnessRequest(
                        worktree=root, run_dir=root / "run", context=_context(root)
                    )
                )
            log = (root / "run" / "copilot.log").read_text(encoding="utf-8")

        self.assertIn("partial", log)
        self.assertIn("slow", log)
        self.assertEqual(len(calls), 1)

    def test_missing_result_file_raises_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls: list = []

            def runner(command, **kwargs):
                calls.append((list(command), kwargs))
                return _completed()

            with (
                patch("reposteward.agents.copilot_cli.subprocess.run", runner),
                self.assertRaisesRegex(AgentError, "invalid result"),
            ):
                self._harness().run(
                    HarnessRequest(
                        worktree=root, run_dir=root / "run", context=_context(root)
                    )
                )

        self.assertEqual(len(calls), 1)

    def test_unparseable_result_file_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_path = root / "run" / "agent-result.json"
            calls: list = []

            def runner(command, **kwargs):
                calls.append((list(command), kwargs))
                result_path.write_text("not json at all", encoding="utf-8")
                return _completed()

            with (
                patch("reposteward.agents.copilot_cli.subprocess.run", runner),
                self.assertRaisesRegex(AgentError, "invalid result"),
            ):
                self._harness().run(
                    HarnessRequest(
                        worktree=root, run_dir=root / "run", context=_context(root)
                    )
                )

        self.assertEqual(len(calls), 1)

    def test_resume_is_never_attempted_and_is_reported_honestly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_path = root / "run" / "agent-result.json"
            calls: list = []
            runner = _success_runner(calls, result_path, STRUCTURED_RESULT)
            with patch("reposteward.agents.copilot_cli.subprocess.run", runner):
                execution = self._harness().run(
                    HarnessRequest(
                        worktree=root,
                        run_dir=root / "run",
                        context=_context(root),
                        native_session_id="session-old",
                    )
                )

        self.assertEqual(len(calls), 1)
        self.assertNotIn("--resume", calls[0][0])
        self.assertFalse(execution.native_session_id)
        self.assertTrue(
            any(
                "cannot resume sessions" in value
                for value in execution.metrics.warnings
            )
        )

    def test_explicit_executable_is_honoured(self):
        harness = self._harness(executable="/opt/tools/bin/copilot")
        self.assertEqual(resolve_executable(harness.config), "/opt/tools/bin/copilot")


if __name__ == "__main__":
    unittest.main()
