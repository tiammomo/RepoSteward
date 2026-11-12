from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from reposteward.agents.agent import (
    RESULT_SCHEMA,
    AgentError,
    build_harness_prompt,
)
from reposteward.agents.harness import (
    SUPPORTED,
    CapabilitySupport,
    HarnessCapabilities,
    HarnessRequest,
)
from reposteward.core.config import AgentConfig
from reposteward.core.models import AgentExecution, AgentMetrics, AgentResult
from reposteward.storage.workspace import sanitized_environment

DEFAULT_EXECUTABLE = "copilot"
# AgentConfig.executable defaults to the Codex CLI name. When a run selects
# copilot-cli without overriding the executable, resolve to the Copilot CLI
# instead of invoking Codex by accident. The shared default is left untouched.
CODEX_DEFAULT_EXECUTABLE = "codex"

RESULT_FILE_NAME = "agent-result.json"

# Documented permission flags of the Copilot CLI. The CLI has no sandbox
# flag, so the harness grants only what the task needs and explicitly denies
# remote-affecting shell commands instead of blanket-allowing everything.
PERMISSION_FLAGS = (
    ("--allow-tool", "write"),
    ("--allow-tool", "shell"),
    ("--deny-tool", "shell(git push)"),
)

RESULT_CONTRACT = """
OUTPUT CONTRACT (mandatory):
When the task is complete, write the final machine-readable result as ONE JSON
object that conforms to the schema at {schema_path} to the file {result_path}.
Write raw JSON only: no Markdown fences, no commentary in that file. If you
cannot complete the task, still write the JSON with an honest summary and
next_actions. Never run `git push`; leave publishing to the control plane.
"""


def resolve_executable(config: AgentConfig) -> str:
    """Return the CLI name for this run, honouring an explicit executable."""

    executable = (config.executable or "").strip()
    if not executable or executable == CODEX_DEFAULT_EXECUTABLE:
        return DEFAULT_EXECUTABLE
    return executable


class CopilotCliHarness:
    """Explicitly selected adapter for the GitHub Copilot CLI.

    Documented boundaries honoured here (verified against the official docs):
    the programmatic interface is `copilot -p`; there is no schema-validated
    output flag, no session-resume flag, and no machine-readable usage report.
    The adapter therefore collects the structured result from a file the agent
    is instructed to write, fails closed when that file is missing or
    unparseable, and never substitutes another provider or model.
    """

    name = "copilot-cli"
    capabilities = HarnessCapabilities(
        schema_version=1,
        workspace_write=SUPPORTED,
        structured_result=SUPPORTED,
        native_session_resume=CapabilitySupport(
            "unsupported", "no_resume_flag_documented"
        ),
        usage_metrics=CapabilitySupport(
            "best_effort", "no_machine_readable_usage_documented"
        ),
    )

    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    def run(self, request: HarnessRequest) -> AgentExecution:
        request.run_dir.mkdir(parents=True, exist_ok=True)
        schema_path = request.run_dir / "agent-result.schema.json"
        result_path = request.run_dir / RESULT_FILE_NAME
        log_path = request.run_dir / "copilot.log"
        schema_path.write_text(json.dumps(RESULT_SCHEMA, indent=2), encoding="utf-8")
        prompt = self._wrap_prompt(
            build_harness_prompt(request.context),
            schema_path=schema_path,
            result_path=result_path,
        )
        started = time.monotonic()
        warnings: list[str] = []
        if request.native_session_id:
            warnings.append(
                "Copilot CLI cannot resume sessions; started from the Context Pack"
            )

        completed = self._execute(
            request, prompt=prompt, log_path=log_path, started=started
        )
        metrics = self._metrics(
            prompt_chars=len(prompt),
            stderr_bytes=len(completed.stderr.encode("utf-8", errors="replace")),
            duration_seconds=round(time.monotonic() - started, 3),
            log_path=log_path,
            warnings=warnings,
        )
        if completed.returncode:
            raise AgentError(
                f"Copilot CLI exited with {completed.returncode}; see {log_path}",
                metrics=metrics,
                harness=self.name,
                model=self.config.model,
            )
        agent_result = _result_from_file(result_path)
        if agent_result is None:
            raise AgentError(
                f"Copilot CLI returned an invalid result; see {log_path}",
                metrics=metrics,
                harness=self.name,
                model=self.config.model,
            )
        return AgentExecution(
            agent_result,
            metrics,
            harness=self.name,
            model=self.config.model,
        )

    def _wrap_prompt(self, prompt: str, *, schema_path: Path, result_path: Path) -> str:
        contract = RESULT_CONTRACT.format(
            schema_path=schema_path, result_path=result_path
        )
        return f"{prompt}\n{contract}"

    def _build_command(self, *, prompt: str) -> list[str]:
        command = [resolve_executable(self.config), "-p", prompt]
        for flag, value in PERMISSION_FLAGS:
            command.extend([flag, value])
        if self.config.model:
            command.extend(["--model", self.config.model])
        return command

    def _execute(
        self,
        request: HarnessRequest,
        *,
        prompt: str,
        log_path: Path,
        started: float,
    ) -> subprocess.CompletedProcess[str]:
        command = self._build_command(prompt=prompt)
        try:
            completed = subprocess.run(
                command,
                cwd=request.worktree,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                # GitHub REST credentials stay here. The Copilot CLI uses its
                # own /login credentials (or a user-provided GH_TOKEN outside
                # this process); RepoSteward never forwards its own tokens.
                env=sanitized_environment(),
            )
        except subprocess.TimeoutExpired as exc:

            def decoded(value):
                return (
                    value.decode("utf-8", errors="replace")
                    if isinstance(value, bytes)
                    else value or ""
                )

            stdout, stderr = decoded(exc.stdout), decoded(exc.stderr)
            log_path.write_text(
                f"STDOUT\n{stdout}\n\nSTDERR\n{stderr}", encoding="utf-8"
            )
            raise AgentError(
                f"Copilot CLI exceeded the {self.config.timeout_seconds}s timeout",
                metrics=AgentMetrics(
                    prompt_chars=None,
                    event_bytes=None,
                    stderr_bytes=len(stderr.encode("utf-8", errors="replace")),
                    event_count=None,
                    tool_call_count=None,
                    duration_seconds=round(time.monotonic() - started, 3),
                ),
                harness=self.name,
                model=self.config.model,
            ) from exc
        log_path.write_text(
            f"STDOUT\n{completed.stdout}\n\nSTDERR\n{completed.stderr}",
            encoding="utf-8",
        )
        return completed

    def _metrics(
        self,
        *,
        prompt_chars: int,
        stderr_bytes: int,
        duration_seconds: float,
        log_path: Path,
        warnings: list[str],
    ) -> AgentMetrics:
        # The CLI documents no machine-readable usage report; token counts
        # stay unset rather than being inferred.
        return AgentMetrics(
            input_tokens=None,
            cached_input_tokens=None,
            output_tokens=None,
            reasoning_output_tokens=None,
            prompt_chars=prompt_chars,
            event_bytes=None,
            stderr_bytes=stderr_bytes,
            event_count=None,
            tool_call_count=None,
            duration_seconds=duration_seconds,
            log_path=str(log_path),
            warnings=tuple(warnings),
        )


def _result_from_file(result_path: Path) -> AgentResult | None:
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return AgentResult.from_dict(payload)
    except (KeyError, TypeError, ValueError):
        return None
