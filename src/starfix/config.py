from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when the project configuration is invalid."""


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    login: str = "tiammomo"
    git_name: str = "tiammomo"
    git_email: str = "pearfl@qq.com"
    api_url: str = "https://api.github.com"
    token_env: tuple[str, ...] = ("GITHUB_TOKEN", "GH_TOKEN")


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    issues_per_repo: int = 100
    max_pages: int = 2
    min_score: float = 30.0
    preferred_labels: tuple[str, ...] = (
        "good first issue",
        "help wanted",
        "bug",
        "documentation",
        "external",
    )
    blocked_labels: tuple[str, ...] = (
        "security",
        "in progress",
        "inprogress",
        "duplicate",
        "invalid",
        "wontfix",
        "question",
        "needs reproduction",
        "needs confirmation",
        "server issue",
    )


@dataclass(frozen=True, slots=True)
class SafetyConfig:
    max_files_changed: int = 18
    max_diff_lines: int = 700
    max_daily_submissions: int = 2
    require_verification: bool = True
    draft_pull_requests: bool = True
    forbidden_paths: tuple[str, ...] = (
        ".github/workflows/",
        ".git/",
        ".env",
        "credentials",
        "secrets",
    )


@dataclass(frozen=True, slots=True)
class AgentConfig:
    executable: str = "codex"
    timeout_seconds: int = 1800
    model: str = ""
    reasoning_effort: str = ""


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    image: str = "starfix-runner:latest"
    timeout_seconds: int = 1800
    cpus: float = 4.0
    memory: str = "8g"
    pids_limit: int = 1024
    max_output_chars: int = 60_000


@dataclass(frozen=True, slots=True)
class RepositoryPolicy:
    name: str
    enabled: bool = True
    auto_prepare: bool = False
    min_stars: int = 1_000
    preferred_labels: tuple[str, ...] = ()
    blocked_labels: tuple[str, ...] = ()
    maintainer_approval: str = ""
    require_assignment_before_submit: bool = False
    allowed_approver_associations: tuple[str, ...] = (
        "OWNER",
        "MEMBER",
        "COLLABORATOR",
    )
    bootstrap_commands: tuple[str, ...] = ()
    verification_prefixes: tuple[str, ...] = ()
    required_verification_markers: tuple[str, ...] = ()
    branch_template: str = "{login}/issue-{issue}-{slug}"
    default_scope: str = "repo"
    max_files_changed: int | None = None
    max_diff_lines: int | None = None


@dataclass(frozen=True, slots=True)
class AppConfig:
    path: Path
    state_dir: Path
    github: GitHubConfig
    discovery: DiscoveryConfig
    safety: SafetyConfig
    agent: AgentConfig
    runner: RunnerConfig
    repositories: dict[str, RepositoryPolicy] = field(default_factory=dict)


def _tuple(value: Any, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("expected an array of strings")
    return tuple(value)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def load_config(path: str | Path = "starfix.toml") -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    project = _section(raw, "project")
    state_value = str(project.get("state_dir", ".starfix"))
    state_dir = Path(state_value).expanduser()
    if not state_dir.is_absolute():
        state_dir = (config_path.parent / state_dir).resolve()

    github_raw = _section(raw, "github")
    github = GitHubConfig(
        login=str(github_raw.get("login", "tiammomo")),
        git_name=str(github_raw.get("git_name", "tiammomo")),
        git_email=str(github_raw.get("git_email", "pearfl@qq.com")),
        api_url=str(github_raw.get("api_url", "https://api.github.com")).rstrip("/"),
        token_env=_tuple(github_raw.get("token_env"), ("GITHUB_TOKEN", "GH_TOKEN")),
    )

    discovery_raw = _section(raw, "discovery")
    discovery_defaults = DiscoveryConfig()
    discovery = DiscoveryConfig(
        issues_per_repo=int(discovery_raw.get("issues_per_repo", 100)),
        max_pages=int(discovery_raw.get("max_pages", 2)),
        min_score=float(discovery_raw.get("min_score", 30.0)),
        preferred_labels=_tuple(
            discovery_raw.get("preferred_labels"), discovery_defaults.preferred_labels
        ),
        blocked_labels=_tuple(
            discovery_raw.get("blocked_labels"), discovery_defaults.blocked_labels
        ),
    )

    safety_raw = _section(raw, "safety")
    safety_defaults = SafetyConfig()
    safety = SafetyConfig(
        max_files_changed=int(safety_raw.get("max_files_changed", 18)),
        max_diff_lines=int(safety_raw.get("max_diff_lines", 700)),
        max_daily_submissions=int(safety_raw.get("max_daily_submissions", 2)),
        require_verification=bool(safety_raw.get("require_verification", True)),
        draft_pull_requests=bool(safety_raw.get("draft_pull_requests", True)),
        forbidden_paths=_tuple(
            safety_raw.get("forbidden_paths"), safety_defaults.forbidden_paths
        ),
    )

    agent_raw = _section(raw, "agent")
    agent = AgentConfig(
        executable=str(agent_raw.get("executable", "codex")),
        timeout_seconds=int(agent_raw.get("timeout_seconds", 1800)),
        model=str(agent_raw.get("model", "")),
        reasoning_effort=str(agent_raw.get("reasoning_effort", "")),
    )

    runner_raw = _section(raw, "runner")
    runner = RunnerConfig(
        image=str(runner_raw.get("image", "starfix-runner:latest")),
        timeout_seconds=int(runner_raw.get("timeout_seconds", 1800)),
        cpus=float(runner_raw.get("cpus", 4.0)),
        memory=str(runner_raw.get("memory", "8g")),
        pids_limit=int(runner_raw.get("pids_limit", 1024)),
        max_output_chars=int(runner_raw.get("max_output_chars", 60_000)),
    )

    repositories_raw = _section(raw, "repositories")
    repositories: dict[str, RepositoryPolicy] = {}
    for name, repo_value in repositories_raw.items():
        if not isinstance(repo_value, dict):
            raise ConfigError(f"[repositories.{name!r}] must be a table")
        if name.count("/") != 1:
            raise ConfigError(f"repository must use owner/name form: {name!r}")
        repositories[name.lower()] = RepositoryPolicy(
            name=name,
            enabled=bool(repo_value.get("enabled", True)),
            auto_prepare=bool(repo_value.get("auto_prepare", False)),
            min_stars=int(repo_value.get("min_stars", 1_000)),
            preferred_labels=_tuple(repo_value.get("preferred_labels")),
            blocked_labels=_tuple(repo_value.get("blocked_labels")),
            maintainer_approval=str(repo_value.get("maintainer_approval", "")).strip(),
            require_assignment_before_submit=bool(
                repo_value.get("require_assignment_before_submit", False)
            ),
            allowed_approver_associations=_tuple(
                repo_value.get("allowed_approver_associations"),
                ("OWNER", "MEMBER", "COLLABORATOR"),
            ),
            bootstrap_commands=_tuple(repo_value.get("bootstrap_commands")),
            verification_prefixes=_tuple(repo_value.get("verification_prefixes")),
            required_verification_markers=_tuple(
                repo_value.get("required_verification_markers")
            ),
            branch_template=str(
                repo_value.get("branch_template", "{login}/issue-{issue}-{slug}")
            ),
            default_scope=str(repo_value.get("default_scope", "repo")),
            max_files_changed=(
                int(repo_value["max_files_changed"])
                if "max_files_changed" in repo_value
                else None
            ),
            max_diff_lines=(
                int(repo_value["max_diff_lines"])
                if "max_diff_lines" in repo_value
                else None
            ),
        )

    if not repositories:
        raise ConfigError('configure at least one [repositories."owner/name"] entry')
    if github.login.lower() != "tiammomo":
        raise ConfigError("this project is pinned to the GitHub login 'tiammomo'")
    if discovery.issues_per_repo < 1 or discovery.issues_per_repo > 100:
        raise ConfigError("discovery.issues_per_repo must be between 1 and 100")
    if discovery.max_pages < 1 or discovery.max_pages > 10:
        raise ConfigError("discovery.max_pages must be between 1 and 10")

    return AppConfig(
        path=config_path,
        state_dir=state_dir,
        github=github,
        discovery=discovery,
        safety=safety,
        agent=agent,
        runner=runner,
        repositories=repositories,
    )
