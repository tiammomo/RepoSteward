from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from .config import AgentConfig, ConfigError
from .context import ContextPack
from .models import HarnessExecution


@dataclass(frozen=True, slots=True)
class HarnessRequest:
    worktree: Path
    run_dir: Path
    context: ContextPack
    native_session_id: str = ""


CapabilityState = Literal["supported", "unsupported", "best_effort", "unknown"]
CAPABILITY_STATES = frozenset({"supported", "unsupported", "best_effort", "unknown"})


@dataclass(frozen=True, slots=True)
class CapabilitySupport:
    state: CapabilityState
    reason: str = ""

    def __post_init__(self) -> None:
        if self.state not in CAPABILITY_STATES:
            raise ValueError(f"invalid harness capability state: {self.state!r}")
        if not isinstance(self.reason, str):
            raise TypeError("harness capability reason must be a string")

    @property
    def supported(self) -> bool:
        return self.state == "supported"


@dataclass(frozen=True, slots=True)
class HarnessCapabilities:
    """Versioned, harness-neutral facts used for conservative orchestration."""

    schema_version: int
    workspace_write: CapabilitySupport
    structured_result: CapabilitySupport
    native_session_resume: CapabilitySupport
    usage_metrics: CapabilitySupport

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, int) or isinstance(
            self.schema_version, bool
        ):
            raise TypeError("harness capability schema version must be an integer")
        for name in (
            "workspace_write",
            "structured_result",
            "native_session_resume",
            "usage_metrics",
        ):
            if not isinstance(getattr(self, name), CapabilitySupport):
                raise TypeError(f"harness capability {name} has an invalid value")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def unknown(cls, reason: str = "capabilities_not_declared") -> HarnessCapabilities:
        value = CapabilitySupport("unknown", reason)
        return cls(1, value, value, value, value)


SUPPORTED = CapabilitySupport("supported")


@runtime_checkable
class Harness(Protocol):
    """A coding harness that edits one workspace and returns a normalized result."""

    name: str
    capabilities: HarnessCapabilities

    def run(self, request: HarnessRequest) -> HarnessExecution: ...


def harness_capabilities(harness: object) -> HarnessCapabilities:
    """Return declared capabilities, failing closed for legacy adapters."""
    value = getattr(harness, "capabilities", None)
    if (
        isinstance(value, HarnessCapabilities)
        and value.schema_version == 1
        and all(
            isinstance(getattr(value, name, None), CapabilitySupport)
            and getattr(value, name).state in CAPABILITY_STATES
            for name in (
                "workspace_write",
                "structured_result",
                "native_session_resume",
                "usage_metrics",
            )
        )
    ):
        return value
    return HarnessCapabilities.unknown()


def create_harness(config: AgentConfig) -> Harness:
    if config.harness == "codex-cli":
        from .agent import CodexCliHarness

        return CodexCliHarness(config)
    if config.harness == "codex-sdk":
        from .codex_sdk import CodexSdkHarness

        return CodexSdkHarness(config)
    raise ConfigError(f"unsupported coding harness: {config.harness!r}")
