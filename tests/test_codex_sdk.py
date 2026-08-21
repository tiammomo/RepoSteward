from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reposteward.agent import AgentError
from reposteward.codex_sdk import CodexSdkHarness
from reposteward.config import AgentConfig, RepositoryPolicy
from reposteward.context import build_context_pack
from reposteward.harness import HarnessRequest, create_harness
from reposteward.models import Candidate, Issue, RepositoryInfo


def _context(root: Path):
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
        harness="codex-sdk",
        model="gpt-example",
    )


class FakeCodexError(RuntimeError):
    pass


class FakeTurn:
    def __init__(self, response: str) -> None:
        self.response = response
        self.interrupted = False

    def run(self):
        tokens = SimpleNamespace(
            input_tokens=120,
            cached_input_tokens=80,
            output_tokens=30,
            reasoning_output_tokens=10,
        )
        return SimpleNamespace(
            id="turn-1",
            status=SimpleNamespace(value="completed"),
            final_response=self.response,
            items=(SimpleNamespace(type="command_execution"),),
            usage=SimpleNamespace(last=tokens),
        )

    def interrupt(self):
        self.interrupted = True


class FakeThread:
    def __init__(self, response: str, thread_id: str) -> None:
        self.id = thread_id
        self.response = response
        self.turn_options = None

    def turn(self, _prompt: str, **options):
        self.turn_options = options
        return FakeTurn(self.response)


class FakeCodexClient:
    def __init__(self, sdk, _config) -> None:
        self.sdk = sdk

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def thread_resume(self, thread_id: str, **_options):
        self.sdk.resumed.append(thread_id)
        if self.sdk.resume_error:
            raise FakeCodexError("thread unavailable")
        thread = FakeThread(self.sdk.response, thread_id)
        self.sdk.thread = thread
        return thread

    def thread_start(self, **options):
        self.sdk.started.append(options)
        thread = FakeThread(self.sdk.response, "thread-new")
        self.sdk.thread = thread
        return thread


class FakeSdk:
    CodexError = FakeCodexError
    ApprovalMode = SimpleNamespace(deny_all="deny_all")
    Sandbox = SimpleNamespace(workspace_write="workspace_write")

    def __init__(self, *, resume_error: bool = False) -> None:
        self.resume_error = resume_error
        self.resumed = []
        self.started = []
        self.thread = None
        self.config = None
        self.response = json.dumps(
            {
                "summary": "Fixed the edge case.",
                "pr_title": "fix(repo): handle edge case",
                "implementation_notes": "Changed one branch.",
                "verification_commands": ["pytest tests/test_edge.py"],
                "tests_observed": ["pytest tests/test_edge.py"],
                "risks": [],
                "decisions": [],
                "next_actions": ["Review the diff."],
            }
        )

    def CodexConfig(self, **values):
        self.config = values
        return values

    def Codex(self, config):
        return FakeCodexClient(self, config)


class CodexSdkHarnessTests(unittest.TestCase):
    def test_factory_selects_sdk_without_importing_it_eagerly(self) -> None:
        harness = create_harness(AgentConfig(harness="codex-sdk"))
        self.assertIsInstance(harness, CodexSdkHarness)

    def test_sdk_returns_structured_result_usage_and_native_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = FakeSdk()
            harness = CodexSdkHarness(
                AgentConfig(
                    harness="codex-sdk", model="gpt-example", timeout_seconds=5
                ),
                sdk_module=sdk,
            )
            with patch.dict(
                "os.environ",
                {"GITHUB_TOKEN": "secret", "UNRELATED_API_KEY": "secret"},
            ):
                execution = harness.run(
                    HarnessRequest(
                        worktree=root,
                        run_dir=root / "run",
                        context=_context(root),
                    )
                )

        self.assertEqual(execution.harness, "codex-sdk")
        self.assertEqual(execution.native_session_id, "thread-new")
        self.assertEqual(execution.metrics.input_tokens, 120)
        self.assertEqual(execution.metrics.cached_input_tokens, 80)
        self.assertEqual(execution.metrics.tool_call_count, 1)
        self.assertEqual(execution.result.next_actions, ("Review the diff.",))
        self.assertEqual(sdk.config["env"]["GITHUB_TOKEN"], "")
        self.assertEqual(sdk.config["env"]["UNRELATED_API_KEY"], "")
        self.assertFalse(sdk.started[0]["ephemeral"])
        self.assertIsNotNone(sdk.thread.turn_options["output_schema"])

    def test_missing_native_thread_falls_back_to_context_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = FakeSdk(resume_error=True)
            execution = CodexSdkHarness(
                AgentConfig(harness="codex-sdk", timeout_seconds=5),
                sdk_module=sdk,
            ).run(
                HarnessRequest(
                    worktree=root,
                    run_dir=root / "run",
                    context=_context(root),
                    native_session_id="thread-old",
                )
            )

        self.assertEqual(sdk.resumed, ["thread-old"])
        self.assertEqual(execution.native_session_id, "thread-new")
        self.assertTrue(
            any("could not resume" in value for value in execution.metrics.warnings)
        )

    def test_missing_structured_response_leaves_a_diagnostic_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = FakeSdk()
            sdk.response = ""
            harness = CodexSdkHarness(
                AgentConfig(harness="codex-sdk", timeout_seconds=5),
                sdk_module=sdk,
            )

            with self.assertRaisesRegex(AgentError, "no final structured response"):
                harness.run(
                    HarnessRequest(
                        worktree=root,
                        run_dir=root / "run",
                        context=_context(root),
                    )
                )
            log = json.loads((root / "run" / "codex-sdk.log").read_text())

        self.assertIn("no final structured response", log["error"])
        self.assertEqual(log["thread_id"], "thread-new")


if __name__ == "__main__":
    unittest.main()
