from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reposteward.agents.agent import AgentError
from reposteward.agents.claude_code import ClaudeCodeHarness, resolve_executable
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


def _context(root: Path, *, harness: str = "claude-code"):
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
        model="claude-example",
    )


def _payload(**overrides) -> str:
    body = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "session-1",
        "num_turns": 3,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_read_input_tokens": 80,
        },
        "result": json.dumps(STRUCTURED_RESULT),
    }
    body.update(overrides)
    return json.dumps(body)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class RecordingRunner:
    """Stand-in for subprocess.run that records argv and environment."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def commands(self) -> list[list[str]]:
        return [command for command, _ in self.calls]


class ClaudeCodeHarnessTests(unittest.TestCase):
    def _harness(self, **overrides) -> ClaudeCodeHarness:
        options = {"harness": "claude-code", "timeout_seconds": 5}
        options.update(overrides)
        return ClaudeCodeHarness(AgentConfig(**options))

    def test_factory_selects_claude_code_without_importing_at_module_import(self):
        harness = create_harness(AgentConfig(harness="claude-code"))
        self.assertIsInstance(harness, ClaudeCodeHarness)
        self.assertEqual(harness.name, "claude-code")

    def test_default_harness_and_executable_are_unchanged(self):
        config = AgentConfig()
        self.assertEqual(config.harness, "codex-cli")
        self.assertEqual(config.executable, "codex")
        self.assertEqual(AgentConfig(harness="claude-code").executable, "codex")
        self.assertEqual(resolve_executable(AgentConfig()), "claude")

    def test_unknown_harness_still_fails_closed(self):
        with self.assertRaises(ConfigError):
            create_harness(AgentConfig(harness="not-a-harness"))

    def test_capabilities_are_declared_and_survive_fail_closed_check(self):
        declared = ClaudeCodeHarness.capabilities
        self.assertEqual(declared.schema_version, 1)
        self.assertEqual(declared.workspace_write.state, "supported")
        self.assertEqual(declared.structured_result.state, "supported")
        self.assertEqual(declared.native_session_resume.state, "best_effort")
        self.assertEqual(declared.usage_metrics.state, "best_effort")
        self.assertEqual(
            harness_capabilities(self._harness()).to_dict(), declared.to_dict()
        )

    def test_structured_run_returns_result_session_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = RecordingRunner(_completed(stdout=_payload()))
            with patch("reposteward.agents.claude_code.subprocess.run", runner):
                execution = self._harness(model="claude-example").run(
                    HarnessRequest(
                        worktree=root,
                        run_dir=root / "run",
                        context=_context(root),
                    )
                )
            command = runner.commands[0]
            schema_path = root / "run" / "agent-result.schema.json"
            self.assertTrue(schema_path.is_file())
            self.assertEqual(json.loads(schema_path.read_text())["type"], "object")

        self.assertEqual(command[0], "claude")
        self.assertIn("--print", command)
        self.assertEqual(command[command.index("--output-format") + 1], "json")
        self.assertEqual(command[command.index("--json-schema") + 1], str(schema_path))
        self.assertEqual(command[command.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(command[command.index("--add-dir") + 1], str(root))
        self.assertEqual(command[command.index("--model") + 1], "claude-example")
        self.assertNotIn("--resume", command)
        self.assertIn("owner/repo#7", command[-1])

        self.assertEqual(execution.harness, "claude-code")
        self.assertEqual(execution.native_session_id, "session-1")
        self.assertEqual(execution.result.pr_title, "fix(repo): handle edge case")
        self.assertEqual(execution.result.next_actions, ("Review the diff.",))
        self.assertEqual(execution.metrics.input_tokens, 120)
        self.assertEqual(execution.metrics.cached_input_tokens, 80)
        self.assertEqual(execution.metrics.output_tokens, 30)
        self.assertIsNone(execution.metrics.tool_call_count)
        self.assertEqual(runner.calls[0][1]["cwd"], root)

    def test_github_credentials_never_reach_the_harness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = RecordingRunner(_completed(stdout=_payload()))
            environment = {
                "GITHUB_TOKEN": "gh-secret",
                "GH_TOKEN": "gh-secret",
                "ANTHROPIC_API_KEY": "sk-ant",
                "UNRELATED_SECRET": "shh",
                "UNRELATED_TOKEN": "shh",
            }
            with (
                patch.dict("os.environ", environment, clear=False),
                patch("reposteward.agents.claude_code.subprocess.run", runner),
            ):
                self._harness().run(
                    HarnessRequest(
                        worktree=root, run_dir=root / "run", context=_context(root)
                    )
                )
            passed = runner.calls[0][1]["env"]

        self.assertNotIn("GITHUB_TOKEN", passed)
        self.assertNotIn("GH_TOKEN", passed)
        self.assertNotIn("UNRELATED_SECRET", passed)
        self.assertNotIn("UNRELATED_TOKEN", passed)
        self.assertEqual(passed["ANTHROPIC_API_KEY"], "sk-ant")
        self.assertEqual(passed["GIT_TERMINAL_PROMPT"], "0")

    def test_nonzero_exit_raises_without_switching_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = RecordingRunner(_completed(returncode=2, stdout="", stderr="boom"))
            with (
                patch("reposteward.agents.claude_code.subprocess.run", runner),
                self.assertRaisesRegex(AgentError, "exited with 2"),
            ):
                self._harness(model="claude-example").run(
                    HarnessRequest(
                        worktree=root, run_dir=root / "run", context=_context(root)
                    )
                )
            log = (root / "run" / "claude.log").read_text(encoding="utf-8")
            self.assertIn("boom", log)

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][1]["cwd"], root)

    def test_timeout_raises_and_keeps_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = RecordingRunner(
                subprocess.TimeoutExpired(
                    cmd="claude", timeout=5, output="partial", stderr="slow"
                )
            )
            with (
                patch("reposteward.agents.claude_code.subprocess.run", runner),
                self.assertRaisesRegex(AgentError, "exceeded the 5s timeout"),
            ):
                self._harness().run(
                    HarnessRequest(
                        worktree=root, run_dir=root / "run", context=_context(root)
                    )
                )
            log = (root / "run" / "claude.log").read_text(encoding="utf-8")

        self.assertIn("partial", log)
        self.assertIn("slow", log)
        self.assertEqual(len(runner.calls), 1)

    def test_invalid_output_raises_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = RecordingRunner(_completed(stdout="not json at all"))
            with (
                patch("reposteward.agents.claude_code.subprocess.run", runner),
                self.assertRaisesRegex(AgentError, "invalid result"),
            ):
                self._harness().run(
                    HarnessRequest(
                        worktree=root, run_dir=root / "run", context=_context(root)
                    )
                )

        self.assertEqual(len(runner.calls), 1)

    def test_reported_error_payload_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = RecordingRunner(
                _completed(
                    returncode=0,
                    stdout=_payload(
                        is_error=True, subtype="error_max_turns", result=""
                    ),
                )
            )
            with (
                patch("reposteward.agents.claude_code.subprocess.run", runner),
                self.assertRaisesRegex(AgentError, "error_max_turns"),
            ):
                self._harness().run(
                    HarnessRequest(
                        worktree=root, run_dir=root / "run", context=_context(root)
                    )
                )

    def test_unusable_session_falls_back_to_context_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = RecordingRunner(
                _completed(returncode=1, stderr="session not found"),
                _completed(stdout=_payload(session_id="session-2")),
            )
            with patch("reposteward.agents.claude_code.subprocess.run", runner):
                execution = self._harness().run(
                    HarnessRequest(
                        worktree=root,
                        run_dir=root / "run",
                        context=_context(root),
                        native_session_id="session-old",
                    )
                )

        self.assertEqual(len(runner.calls), 2)
        self.assertIn("--resume", runner.commands[0])
        self.assertEqual(
            runner.commands[0][runner.commands[0].index("--resume") + 1], "session-old"
        )
        self.assertNotIn("--resume", runner.commands[1])
        self.assertEqual(execution.native_session_id, "session-2")
        self.assertTrue(
            any("could not resume" in value for value in execution.metrics.warnings)
        )

    def test_missing_usage_leaves_metrics_unknown(self):
        payload = _payload()
        body = json.loads(payload)
        body.pop("usage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = RecordingRunner(_completed(stdout=json.dumps(body)))
            with patch("reposteward.agents.claude_code.subprocess.run", runner):
                execution = self._harness().run(
                    HarnessRequest(
                        worktree=root, run_dir=root / "run", context=_context(root)
                    )
                )

        self.assertIsNone(execution.metrics.input_tokens)
        self.assertIsNone(execution.metrics.output_tokens)
        self.assertIsNone(execution.metrics.event_count)
        self.assertEqual(execution.result.summary, "Fixed the edge case.")

    def test_explicit_executable_is_honoured(self):
        harness = self._harness(executable="/opt/claude/bin/claude")
        self.assertEqual(resolve_executable(harness.config), "/opt/claude/bin/claude")


if __name__ == "__main__":
    unittest.main()
