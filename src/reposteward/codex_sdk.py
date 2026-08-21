from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from types import ModuleType
from typing import Any

from .agent import (
    RESULT_SCHEMA,
    TOOL_ITEM_TYPES,
    WARN_INPUT_TOKENS,
    WARN_TOOL_CALLS,
    AgentError,
    build_harness_prompt,
)
from .config import AgentConfig
from .harness import HarnessRequest
from .models import AgentExecution, AgentMetrics, AgentResult
from .workspace import sanitized_environment

SHELL_ENVIRONMENT_EXCLUDE = (
    'shell_environment_policy.exclude=["GITHUB_TOKEN","GH_TOKEN","CODEX_API_KEY",'
    '"OPENAI_API_KEY","ANTHROPIC_API_KEY","AWS_*","SSH_AUTH_SOCK",'
    '"GIT_ASKPASS","SSH_ASKPASS","*_TOKEN","*_SECRET","*_PASSWORD",'
    '"*_CREDENTIAL"]'
)


class CodexSdkHarness:
    """Optional adapter for the official Python Codex SDK."""

    name = "codex-sdk"

    def __init__(
        self, config: AgentConfig, *, sdk_module: ModuleType | Any = None
    ) -> None:
        self.config = config
        self._sdk_module = sdk_module

    def _sdk(self) -> ModuleType | Any:
        if self._sdk_module is not None:
            return self._sdk_module
        try:
            import openai_codex
        except ImportError as exc:
            raise AgentError(
                "codex-sdk harness requires 'reposteward[codex-sdk]'"
            ) from exc
        return openai_codex

    @staticmethod
    def _sdk_environment() -> dict[str, str]:
        environment = sanitized_environment(keep_codex_credentials=True)
        # CodexConfig overlays its values on the current process environment.
        # Explicitly blank every excluded value so GitHub and unrelated provider
        # credentials cannot survive that merge.
        for key in os.environ:
            if key not in environment:
                environment[key] = ""
        return environment

    def run(self, request: HarnessRequest) -> AgentExecution:
        sdk = self._sdk()
        request.run_dir.mkdir(parents=True, exist_ok=True)
        log_path = request.run_dir / "codex-sdk.log"
        prompt = build_harness_prompt(request.context)
        warnings: list[str] = []
        started = time.monotonic()
        sdk_config = sdk.CodexConfig(
            cwd=str(request.worktree),
            env=self._sdk_environment(),
            config_overrides=(SHELL_ENVIRONMENT_EXCLUDE,),
            client_name="reposteward",
            client_title="RepoSteward",
        )
        model = self.config.model or None
        thread = None
        try:
            with sdk.Codex(sdk_config) as codex:
                thread_options = {
                    "approval_mode": sdk.ApprovalMode.deny_all,
                    "cwd": str(request.worktree),
                    "model": model,
                    "sandbox": sdk.Sandbox.workspace_write,
                }
                if request.native_session_id:
                    try:
                        thread = codex.thread_resume(
                            request.native_session_id, **thread_options
                        )
                    except (sdk.CodexError, RuntimeError) as exc:
                        warnings.append(
                            "Codex SDK could not resume the previous thread; "
                            f"started from the Context Pack instead ({exc})"
                        )
                if thread is None:
                    thread = codex.thread_start(ephemeral=False, **thread_options)
                turn_options: dict[str, Any] = {
                    "approval_mode": sdk.ApprovalMode.deny_all,
                    "cwd": str(request.worktree),
                    "output_schema": RESULT_SCHEMA,
                    "sandbox": sdk.Sandbox.workspace_write,
                }
                if self.config.reasoning_effort:
                    turn_options["effort"] = self.config.reasoning_effort
                turn = thread.turn(prompt, **turn_options)
                executor = ThreadPoolExecutor(max_workers=1)
                future = executor.submit(turn.run)
                try:
                    result = future.result(timeout=self.config.timeout_seconds)
                except FutureTimeoutError as exc:
                    try:
                        turn.interrupt()
                    except (sdk.CodexError, RuntimeError):
                        pass
                    raise AgentError(
                        f"Codex SDK exceeded the {self.config.timeout_seconds}s timeout"
                    ) from exc
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
        except AgentError as exc:
            if not log_path.exists():
                log_path.write_text(
                    json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2)
                    + "\n",
                    encoding="utf-8",
                )
            raise
        except (sdk.CodexError, RuntimeError, OSError) as exc:
            log_path.write_text(
                json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            raise AgentError(f"Codex SDK failed; see {log_path}: {exc}") from exc

        if thread is None:
            raise AgentError("Codex SDK did not create a thread")
        final_response = result.final_response
        if not isinstance(final_response, str) or not final_response.strip():
            message = "Codex SDK returned no final structured response"
            log_path.write_text(
                json.dumps(
                    {
                        "thread_id": thread.id,
                        "turn_id": result.id,
                        "status": str(getattr(result.status, "value", result.status)),
                        "error": message,
                        "warnings": warnings,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            raise AgentError(f"{message}; see {log_path}")
        log_path.write_text(
            json.dumps(
                {
                    "thread_id": thread.id,
                    "turn_id": result.id,
                    "status": str(getattr(result.status, "value", result.status)),
                    "final_response": final_response,
                    "warnings": warnings,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            payload = json.loads(final_response)
            agent_result = AgentResult.from_dict(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise AgentError(
                f"Codex SDK returned an invalid result; see {log_path}"
            ) from exc

        usage = getattr(result.usage, "last", None)

        def token_value(name: str) -> int | None:
            value = getattr(usage, name, None)
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool)
                else None
            )

        input_tokens = token_value("input_tokens")
        tool_calls = sum(self._is_tool_item(item) for item in result.items)
        if input_tokens is not None and input_tokens > WARN_INPUT_TOKENS:
            warnings.append(
                f"Codex input tokens {input_tokens} exceed warning budget "
                f"{WARN_INPUT_TOKENS}"
            )
        if tool_calls > WARN_TOOL_CALLS:
            warnings.append(
                f"Codex tool calls {tool_calls} exceed warning budget {WARN_TOOL_CALLS}"
            )
        metrics = AgentMetrics(
            input_tokens=input_tokens,
            cached_input_tokens=token_value("cached_input_tokens"),
            output_tokens=token_value("output_tokens"),
            reasoning_output_tokens=token_value("reasoning_output_tokens"),
            prompt_chars=len(prompt),
            event_count=len(result.items),
            tool_call_count=tool_calls,
            duration_seconds=round(time.monotonic() - started, 3),
            log_path=str(log_path),
            warnings=tuple(warnings),
        )
        return AgentExecution(
            agent_result,
            metrics,
            harness=self.name,
            model=self.config.model,
            native_session_id=str(thread.id),
        )

    @staticmethod
    def _is_tool_item(item: object) -> bool:
        value = getattr(item, "root", item)
        kind = getattr(value, "type", "")
        kind = str(getattr(kind, "value", kind))
        normalized = kind.replace("-", "_").replace(" ", "_").casefold()
        return normalized in TOOL_ITEM_TYPES or normalized in {
            "commandexecution",
            "mcptoolcall",
            "websearch",
        }
