from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .config import AgentConfig, ConfigError
from .context import ContextPack
from .models import HarnessExecution


@dataclass(frozen=True, slots=True)
class HarnessRequest:
    worktree: Path
    run_dir: Path
    context: ContextPack
    native_session_id: str = ""


@runtime_checkable
class Harness(Protocol):
    """A coding harness that edits one workspace and returns a normalized result."""

    name: str

    def run(self, request: HarnessRequest) -> HarnessExecution: ...


def create_harness(config: AgentConfig) -> Harness:
    if config.harness == "codex-cli":
        from .agent import CodexCliHarness

        return CodexCliHarness(config)
    if config.harness == "codex-sdk":
        from .codex_sdk import CodexSdkHarness

        return CodexSdkHarness(config)
    raise ConfigError(f"unsupported coding harness: {config.harness!r}")
