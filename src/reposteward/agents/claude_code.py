from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from reposteward.agents.agent import (
    RESULT_SCHEMA,
    WARN_INPUT_TOKENS,
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

DEFAULT_EXECUTABLE = "claude"
# AgentConfig.executable defaults to the Codex CLI name. When a run selects
# claude-code without overriding the executable, resolve to the Claude CLI
# instead of invoking Codex by accident. The shared default is left untouched.
CODEX_DEFAULT_EXECUTABLE = "codex"


def resolve_executable(config: AgentConfig) -> str:
    """Return the CLI name for this run, honouring an explicit executable."""

    executable = (config.executable or "").strip()
    if not executable or executable == CODEX_DEFAULT_EXECUTABLE:
        return DEFAULT_EXECUTABLE
    return executable


class ClaudeCodeHarness:
    """Explicitly selected adapter for the Claude Code CLI.

    The adapter is never chosen by default, never substitutes another provider
    or model after a failure, and never treats a native session as the only
    record of the task: an unusable session falls back to the Context Pack.
    """

    name = "claude-code"
    capabilities = HarnessCapabilities(
        schema_version=1,
        workspace_write=SUPPORTED,
        structured_result=SUPPORTED,
        native_session_resume=CapabilitySupport(
            "best_effort", "session_resume_optional"
        ),
        usage_metrics=CapabilitySupport("best_effort", "json_usage_optional"),
    )

    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    def run(self, request: HarnessRequest) -> AgentExecution:
        request.run_dir.mkdir(parents=True, exist_ok=True)
        schema_path = request.run_dir / "agent-result.schema.json"
        log_path = request.run_dir / "claude.log"
        schema_path.write_text(json.dumps(RESULT_SCHEMA, indent=2), encoding="utf-8")
        prompt = build_harness_prompt(request.context)
        started = time.monotonic()
        warnings: list[str] = []

        completed, resume_warning = self._invoke(
            request,
            prompt=prompt,
            schema_path=schema_path,
            log_path=log_path,
            started=started,
        )
        if resume_warning:
            warnings.append(resume_warning)
        metrics = self._metrics(
            completed.stdout,
            prompt_chars=len(prompt),
            stderr_bytes=len(completed.stderr.encode("utf-8", errors="replace")),
            duration_seconds=round(time.monotonic() - started, 3),
            log_path=log_path,
            warnings=warnings,
        )
        if completed.returncode:
            raise AgentError(
                f"Claude Code exited with {completed.returncode}; see {log_path}",
                metrics=metrics,
                harness=self.name,
                model=self.config.model,
            )
        payload = _load_payload(completed.stdout)
        if payload is None:
            raise AgentError(
                f"Claude Code returned an invalid result; see {log_path}",
                metrics=metrics,
                harness=self.name,
                model=self.config.model,
            )
        if payload.get("is_error"):
            subtype = str(payload.get("subtype") or "error").strip()
            raise AgentError(
                f"Claude Code reported {subtype}; see {log_path}",
                metrics=metrics,
                harness=self.name,
                model=self.config.model,
            )
        agent_result = _result_from_payload(payload)
        if agent_result is None:
            raise AgentError(
                f"Claude Code returned an invalid result; see {log_path}",
                metrics=metrics,
                harness=self.name,
                model=self.config.model,
            )
        session_id = str(payload.get("session_id") or "").strip()
        return AgentExecution(
            agent_result,
            metrics,
            harness=self.name,
            model=self.config.model,
            native_session_id=session_id or request.native_session_id,
        )

    def _invoke(
        self,
        request: HarnessRequest,
        *,
        prompt: str,
        schema_path: Path,
        log_path: Path,
        started: float,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        command = self._build_command(
            prompt=prompt,
            worktree=request.worktree,
            schema_path=schema_path,
            session_id=request.native_session_id,
        )
        completed = self._execute(command, request, log_path, started)
        if completed.returncode == 0 or not request.native_session_id:
            return completed, ""
        # Resume is best effort. Keep the failed attempt on disk, then rebuild
        # the turn from the Context Pack without changing model or provider.
        log_path.write_text(
            f"RESUME ATTEMPT\n{completed.stdout}\n\nSTDERR\n{completed.stderr}",
            encoding="utf-8",
        )
        warning = (
            "Claude Code could not resume the previous session; "
            "started from the Context Pack instead"
        )
        fallback = self._build_command(
            prompt=prompt,
            worktree=request.worktree,
            schema_path=schema_path,
            session_id="",
        )
        return self._execute(fallback, request, log_path, started), warning

    def _build_command(
        self,
        *,
        prompt: str,
        worktree: Path,
        schema_path: Path,
        session_id: str,
    ) -> list[str]:
        command = [
            resolve_executable(self.config),
            "--print",
            "--output-format",
            "json",
            "--json-schema",
            str(schema_path),
            "--permission-mode",
            "acceptEdits",
            "--add-dir",
            str(worktree),
        ]
        if self.config.model:
            command.extend(["--model", self.config.model])
        if session_id:
            command.extend(["--resume", session_id])
        command.append(prompt)
        return command

    def _execute(
        self,
        command: list[str],
        request: HarnessRequest,
        log_path: Path,
        started: float,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                command,
                cwd=request.worktree,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                env=sanitized_environment(keep_anthropic_credentials=True),
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
                f"Claude Code exceeded the {self.config.timeout_seconds}s timeout",
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
        stdout: str,
        *,
        prompt_chars: int,
        stderr_bytes: int,
        duration_seconds: float,
        log_path: Path,
        warnings: list[str],
    ) -> AgentMetrics:
        payload = _load_payload(stdout) or {}
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = {}

        def token_value(*names: str) -> int | None:
            for name in names:
                value = usage.get(name)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
            return None

        input_tokens = token_value("input_tokens", "prompt_tokens")
        pending = list(warnings)
        if input_tokens is not None and input_tokens > WARN_INPUT_TOKENS:
            pending.append(
                f"Claude Code input tokens {input_tokens} exceed warning budget "
                f"{WARN_INPUT_TOKENS}"
            )
        return AgentMetrics(
            input_tokens=input_tokens,
            cached_input_tokens=token_value(
                "cache_read_input_tokens", "cached_input_tokens"
            ),
            output_tokens=token_value("output_tokens", "completion_tokens"),
            # The CLI does not report a separate reasoning-token count; leave it
            # unset rather than inferring a value.
            reasoning_output_tokens=None,
            prompt_chars=prompt_chars,
            event_bytes=None,
            stderr_bytes=stderr_bytes,
            event_count=None,
            # Tool-call counts are not part of the documented JSON payload.
            tool_call_count=None,
            duration_seconds=duration_seconds,
            log_path=str(log_path),
            warnings=tuple(pending),
        )


def _load_payload(stdout: str) -> dict[str, Any] | None:
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
        for line in reversed(text.splitlines()):
            candidate = line.strip()
            if not candidate.startswith("{"):
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                payload = parsed
                break
    return payload if isinstance(payload, dict) else None


def _result_from_payload(payload: dict[str, Any]) -> AgentResult | None:
    for candidate in _result_candidates(payload):
        if not isinstance(candidate, dict):
            continue
        try:
            return AgentResult.from_dict(candidate)
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _result_candidates(payload: dict[str, Any]):
    yield payload
    for key in ("result", "structured_result", "output"):
        value = payload.get(key)
        if isinstance(value, dict):
            yield value
        elif isinstance(value, str) and value.strip().startswith("{"):
            try:
                yield json.loads(value)
            except json.JSONDecodeError:
                continue
