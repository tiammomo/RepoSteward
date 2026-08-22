from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

CONFIG_VERSION = 1
PROJECT_CONFIG_NAMES = (".reposteward.toml", "reposteward.toml", "starfix.toml")


class ConfigError(ValueError):
    """Raised when the project configuration is invalid."""


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    login: str = ""
    git_name: str = ""
    git_email: str = ""
    sign_commits: bool = False
    signoff_commits: bool = True
    api_url: str = "https://api.github.com"
    token_env: tuple[str, ...] = ("GITHUB_TOKEN", "GH_TOKEN")
    gh_auth_command: tuple[str, ...] = (
        "gh",
        "auth",
        "token",
        "--hostname",
        "github.com",
    )


@dataclass(frozen=True, slots=True)
class IssueReviewConfig:
    project_owner: str = ""
    project_number: int = 0
    project_owner_type: str = "user"
    require_distinct_reviewer: bool = True


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    issues_per_repo: int = 100
    max_pages: int = 2
    competing_work_checks_per_repo: int = 10
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
    max_active_pull_requests: int = 4
    max_files_changed: int = 40
    max_diff_lines: int = 2_000
    require_verification: bool = True
    draft_pull_requests: bool = True
    forbidden_paths: tuple[str, ...] = (
        ".github/workflows/",
        ".git/",
        ".reposteward/",
        ".starfix/",
        ".reposteward.toml",
        "reposteward.local.toml",
        ".env",
        "credentials",
        "secrets",
    )


@dataclass(frozen=True, slots=True)
class AgentConfig:
    harness: str = "codex-cli"
    executable: str = "codex"
    timeout_seconds: int = 1800
    model: str = ""
    reasoning_effort: str = ""


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    image: str = "reposteward-runner:latest"
    timeout_seconds: int = 1800
    cpus: float = 4.0
    memory: str = "8g"
    pids_limit: int = 1024
    max_output_chars: int = 12_000
    passed_output_chars: int = 2_000
    max_log_chars: int = 2_000_000


@dataclass(frozen=True, slots=True)
class StorageConfig:
    cache_retention_days: int = 30
    workspace_retention_days: int = 30
    max_gc_items: int = 1_000


@dataclass(frozen=True, slots=True)
class ContextConfig:
    follow_up_max_tokens: int = 24_000


@dataclass(frozen=True, slots=True)
class UsagePrice:
    harness: str
    model: str
    effective_from: str
    currency: str
    input_per_million: Decimal
    cached_input_per_million: Decimal
    output_per_million: Decimal
    reasoning_output_per_million: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    prices: tuple[UsagePrice, ...] = ()


@dataclass(frozen=True, slots=True)
class RepositoryPolicy:
    name: str
    enabled: bool = True
    auto_prepare: bool = False
    auto_merge: bool = False
    auto_merge_method: str = "squash"
    mode: str = "contributor"
    submission_strategy: str = "fork"
    min_stars: int = 1_000
    preferred_labels: tuple[str, ...] = ()
    blocked_labels: tuple[str, ...] = ()
    maintainer_approval: str = ""
    require_assignment_before_submit: bool = False
    require_no_competing_work: bool = False
    allowed_approver_associations: tuple[str, ...] = (
        "OWNER",
        "MEMBER",
        "COLLABORATOR",
    )
    bootstrap_commands: tuple[str, ...] = ()
    verification_prefixes: tuple[str, ...] = ()
    required_verification_markers: tuple[str, ...] = ()
    required_contribution_files: tuple[str, ...] = ()
    pull_request_body_style: str = "generic"
    pull_request_template_path: str = ""
    pull_request_template_sha256: str = ""
    branch_template: str = "{login}/issue-{issue}-{slug}"
    default_scope: str = "repo"
    max_active_pull_requests: int | None = None
    max_files_changed: int | None = None
    max_diff_lines: int | None = None
    merge_risk_paths: tuple[str, ...] = ()
    event_payload_retention_days: int | None = None
    owner_attestation: bool = False


@dataclass(frozen=True, slots=True)
class AppConfig:
    config_version: int
    path: Path
    state_dir: Path
    workspace_dir: Path
    state_namespace: bool
    github: GitHubConfig
    issue_review: IssueReviewConfig
    discovery: DiscoveryConfig
    safety: SafetyConfig
    agent: AgentConfig
    runner: RunnerConfig
    storage: StorageConfig
    context: ContextConfig
    observability: ObservabilityConfig
    repositories: dict[str, RepositoryPolicy] = field(default_factory=dict)


def _tuple(value: Any, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("expected an array of strings")
    return tuple(value)


def _boolean(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError("expected a boolean")
    return value


def _price_rate(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigError(f"observability price {name} must be numeric")
    try:
        rate = Decimal(str(value))
    except InvalidOperation as exc:
        raise ConfigError(f"observability price {name} is invalid") from exc
    if not rate.is_finite() or rate < 0:
        raise ConfigError(f"observability price {name} must be finite and non-negative")
    return rate


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def default_user_config_path() -> Path:
    """Return the per-user configuration path without creating it."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return (root / "reposteward" / "config.toml").resolve()


def default_state_dir() -> Path:
    """Return the per-user runtime-state root without creating it."""
    if os.name == "nt" and os.environ.get("LOCALAPPDATA", "").strip():
        return (Path(os.environ["LOCALAPPDATA"]).expanduser() / "RepoSteward").resolve()
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return (root / "reposteward").resolve()


def default_workspace_dir() -> Path:
    """Return the per-user disposable-workspace root without creating it."""
    if os.name == "nt" and os.environ.get("LOCALAPPDATA", "").strip():
        root = Path(os.environ["LOCALAPPDATA"]).expanduser() / "RepoSteward"
    else:
        base = os.environ.get("XDG_DATA_HOME", "").strip()
        root = (
            Path(base).expanduser() / "reposteward"
            if base
            else Path.home() / ".local" / "share" / "reposteward"
        )
    return (root / "workspaces").resolve()


def discover_project_config(start: Path | None = None) -> Path | None:
    """Find the nearest RepoSteward project configuration."""
    current = (start or Path.cwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        for name in PROJECT_CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def _read_config(path: Path, *, required: bool) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except FileNotFoundError as exc:
        if required:
            raise ConfigError(f"configuration not found: {path}") from exc
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"configuration root must be a table: {path}")
    return value


def _validate_config_version(raw: dict[str, Any], path: Path | None) -> None:
    if not raw:
        return
    value = raw.get("config_version", 0)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"config_version must be an integer: {path}")
    if value not in {0, CONFIG_VERSION}:
        raise ConfigError(
            f"unsupported config_version {value} in {path}; expected {CONFIG_VERSION}"
        )


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _merge(existing, value)
        else:
            result[key] = value
    return result


def _merge_layers(user: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """Merge layers while keeping identity and execution trust user-owned."""
    result = _merge(user, project)
    if not user:
        return result

    # Runtime state and disposable clones belong to the user/machine trust layer.
    # A repository-local config must not redirect them into the target repository.
    user_project = user.get("project")
    project_project = project.get("project")
    trusted_project = user_project if isinstance(user_project, dict) else {}
    merged_project = _merge(
        trusted_project,
        project_project if isinstance(project_project, dict) else {},
    )
    for key in ("state_dir", "workspace_dir", "namespace_state"):
        if key in trusted_project:
            merged_project[key] = trusted_project[key]
        else:
            merged_project.pop(key, None)
    if merged_project:
        result["project"] = merged_project
    else:
        result.pop("project", None)

    user_github = user.get("github")
    if isinstance(user_github, dict):
        result["github"] = dict(user_github)

    user_issue_review = user.get("issue_review")
    if isinstance(user_issue_review, dict):
        result["issue_review"] = dict(user_issue_review)
    else:
        result.pop("issue_review", None)

    user_storage = user.get("storage")
    if isinstance(user_storage, dict):
        result["storage"] = dict(user_storage)
    else:
        result.pop("storage", None)

    user_context = user.get("context")
    if isinstance(user_context, dict):
        result["context"] = dict(user_context)
    else:
        result.pop("context", None)

    user_observability = user.get("observability")
    if isinstance(user_observability, dict):
        result["observability"] = dict(user_observability)
    else:
        result.pop("observability", None)

    user_agent = user.get("agent")
    project_agent = project.get("agent")
    if isinstance(project_agent, dict):
        trusted_agent = user_agent if isinstance(user_agent, dict) else {}
        merged_agent = _merge(trusted_agent, project_agent)
        merged_agent["harness"] = trusted_agent.get("harness", "codex-cli")
        merged_agent["executable"] = trusted_agent.get("executable", "codex")
        result["agent"] = merged_agent

    user_runner = user.get("runner")
    project_runner = project.get("runner")
    if isinstance(project_runner, dict):
        trusted_runner = user_runner if isinstance(user_runner, dict) else {}
        merged_runner = _merge(trusted_runner, project_runner)
        merged_runner["image"] = trusted_runner.get(
            "image", "reposteward-runner:latest"
        )
        result["runner"] = merged_runner

    user_safety = user.get("safety", {})
    project_safety = project.get("safety", {})
    if isinstance(user_safety, dict) and isinstance(project_safety, dict):
        merged_safety = _merge(user_safety, project_safety)
        defaults = SafetyConfig()
        for key, default in (
            ("max_active_pull_requests", defaults.max_active_pull_requests),
            ("max_files_changed", defaults.max_files_changed),
            ("max_diff_lines", defaults.max_diff_lines),
        ):
            user_limit = int(user_safety.get(key, default))
            project_limit = project_safety.get(key)
            merged_safety[key] = (
                min(user_limit, int(project_limit))
                if project_limit is not None
                else user_limit
            )
        for key in ("require_verification", "draft_pull_requests"):
            user_value = _boolean(user_safety.get(key), True)
            project_value = _boolean(project_safety.get(key), True)
            merged_safety[key] = user_value or project_value
        forbidden = list(defaults.forbidden_paths)
        for layer in (user_safety, project_safety):
            values = layer.get("forbidden_paths", [])
            if isinstance(values, list):
                forbidden.extend(str(value) for value in values)
        merged_safety["forbidden_paths"] = list(dict.fromkeys(forbidden))
        result["safety"] = merged_safety
    return result


def load_config(
    path: str | Path | None = None,
    *,
    user_path: str | Path | None = None,
    include_user: bool | None = None,
) -> AppConfig:
    """Load a project configuration, optionally layered over user defaults.

    Direct library callers using an explicit ``path`` remain deterministic by
    default. The CLI opts into the standard per-user layer even when ``--config``
    points at a project file.
    """
    explicit_project = path is not None
    project_path = (
        Path(path).expanduser().resolve()
        if explicit_project
        else discover_project_config()
    )
    if include_user is None:
        include_user = not explicit_project
    if user_path is not None:
        include_user = True
    resolved_user_path = None
    if include_user:
        resolved_user_path = (
            Path(user_path).expanduser().resolve()
            if user_path is not None
            else default_user_config_path()
        )
    user_raw = (
        _read_config(resolved_user_path, required=user_path is not None)
        if resolved_user_path is not None
        else {}
    )
    project_raw = (
        _read_config(project_path, required=explicit_project)
        if project_path is not None
        else {}
    )
    if not user_raw and not project_raw:
        expected = (
            project_path or discover_project_config() or default_user_config_path()
        )
        raise ConfigError(
            f"configuration not found: {expected}; run 'reposteward init' first"
        )
    _validate_config_version(user_raw, resolved_user_path)
    _validate_config_version(project_raw, project_path)
    raw = _merge_layers(user_raw, project_raw)
    config_path = project_path or resolved_user_path
    assert config_path is not None

    version_value = raw.get("config_version", 0)
    _validate_config_version(raw, config_path)

    project = _section(raw, "project")
    state_is_explicit = "state_dir" in project
    state_value = str(project.get("state_dir", default_state_dir()))
    state_dir = Path(state_value).expanduser()
    if not state_dir.is_absolute():
        project_section = project_raw.get("project", {})
        user_section = user_raw.get("project", {})
        if isinstance(project_section, dict) and "state_dir" in project_section:
            state_base = project_path.parent if project_path else Path.cwd()
        elif isinstance(user_section, dict) and "state_dir" in user_section:
            state_base = resolved_user_path.parent if resolved_user_path else Path.cwd()
        else:
            state_base = default_state_dir().parent
        state_dir = (state_base / state_dir).resolve()

    workspace_is_explicit = "workspace_dir" in project
    workspace_dir: Path | None
    if workspace_is_explicit:
        workspace_dir = Path(str(project["workspace_dir"])).expanduser()
        if not workspace_dir.is_absolute():
            project_section = project_raw.get("project", {})
            user_section = user_raw.get("project", {})
            if isinstance(project_section, dict) and "workspace_dir" in project_section:
                workspace_base = project_path.parent if project_path else Path.cwd()
            elif isinstance(user_section, dict) and "workspace_dir" in user_section:
                workspace_base = (
                    resolved_user_path.parent if resolved_user_path else Path.cwd()
                )
            else:
                workspace_base = default_workspace_dir().parent
            workspace_dir = (workspace_base / workspace_dir).resolve()
    elif state_is_explicit:
        # Preserve the layout of existing configs that only define state_dir.
        workspace_dir = None
    else:
        workspace_dir = default_workspace_dir()

    github_raw = _section(raw, "github")
    login = str(github_raw.get("login", "")).strip()
    github = GitHubConfig(
        login=login,
        git_name=str(github_raw.get("git_name", login)).strip(),
        git_email=str(
            github_raw.get("git_email", f"{login}@users.noreply.github.com")
        ).strip(),
        sign_commits=_boolean(github_raw.get("sign_commits"), False),
        signoff_commits=_boolean(github_raw.get("signoff_commits"), True),
        api_url=str(github_raw.get("api_url", "https://api.github.com")).rstrip("/"),
        token_env=_tuple(github_raw.get("token_env"), ("GITHUB_TOKEN", "GH_TOKEN")),
        gh_auth_command=_tuple(
            github_raw.get("gh_auth_command"),
            ("gh", "auth", "token", "--hostname", "github.com"),
        ),
    )

    issue_review_raw = _section(raw, "issue_review")
    issue_review = IssueReviewConfig(
        project_owner=str(issue_review_raw.get("project_owner", "")).strip(),
        project_number=int(issue_review_raw.get("project_number", 0)),
        project_owner_type=str(
            issue_review_raw.get("project_owner_type", "user")
        ).strip(),
        require_distinct_reviewer=_boolean(
            issue_review_raw.get("require_distinct_reviewer"), True
        ),
    )
    legacy_project = bool(project_raw) and "config_version" not in project_raw
    state_namespace = _boolean(project.get("namespace_state"), not legacy_project)
    if state_namespace:
        host = urlparse(github.api_url).netloc or "github.com"
        safe_host = re.sub(r"[^a-z0-9.-]+", "-", host.casefold()).strip("-")
        safe_login = re.sub(r"[^a-z0-9._-]+", "-", github.login.casefold()).strip("-")
        state_dir = state_dir / (safe_host or "forge") / (safe_login or "user")
        if workspace_dir is not None:
            workspace_dir = (
                workspace_dir / (safe_host or "forge") / (safe_login or "user")
            )
    if workspace_dir is None:
        workspace_dir = state_dir / "workspaces"

    discovery_raw = _section(raw, "discovery")
    discovery_defaults = DiscoveryConfig()
    discovery = DiscoveryConfig(
        issues_per_repo=int(discovery_raw.get("issues_per_repo", 100)),
        max_pages=int(discovery_raw.get("max_pages", 2)),
        competing_work_checks_per_repo=int(
            discovery_raw.get("competing_work_checks_per_repo", 10)
        ),
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
    configured_forbidden = _tuple(safety_raw.get("forbidden_paths"))
    safety = SafetyConfig(
        max_active_pull_requests=int(safety_raw.get("max_active_pull_requests", 4)),
        max_files_changed=int(safety_raw.get("max_files_changed", 40)),
        max_diff_lines=int(safety_raw.get("max_diff_lines", 2_000)),
        require_verification=_boolean(safety_raw.get("require_verification"), True),
        draft_pull_requests=_boolean(safety_raw.get("draft_pull_requests"), True),
        forbidden_paths=tuple(
            dict.fromkeys(safety_defaults.forbidden_paths + configured_forbidden)
        ),
    )

    agent_raw = _section(raw, "agent")
    agent = AgentConfig(
        harness=str(agent_raw.get("harness", "codex-cli")).strip(),
        executable=str(agent_raw.get("executable", "codex")),
        timeout_seconds=int(agent_raw.get("timeout_seconds", 1800)),
        model=str(agent_raw.get("model", "")),
        reasoning_effort=str(agent_raw.get("reasoning_effort", "")),
    )

    runner_raw = _section(raw, "runner")
    runner = RunnerConfig(
        image=str(runner_raw.get("image", "reposteward-runner:latest")),
        timeout_seconds=int(runner_raw.get("timeout_seconds", 1800)),
        cpus=float(runner_raw.get("cpus", 4.0)),
        memory=str(runner_raw.get("memory", "8g")),
        pids_limit=int(runner_raw.get("pids_limit", 1024)),
        max_output_chars=int(runner_raw.get("max_output_chars", 12_000)),
        passed_output_chars=int(runner_raw.get("passed_output_chars", 2_000)),
        max_log_chars=int(runner_raw.get("max_log_chars", 2_000_000)),
    )

    storage_raw = _section(raw, "storage")
    storage = StorageConfig(
        cache_retention_days=int(storage_raw.get("cache_retention_days", 30)),
        workspace_retention_days=int(storage_raw.get("workspace_retention_days", 30)),
        max_gc_items=int(storage_raw.get("max_gc_items", 1_000)),
    )

    context_raw = _section(raw, "context")
    context = ContextConfig(
        follow_up_max_tokens=int(context_raw.get("follow_up_max_tokens", 24_000))
    )

    observability_raw = _section(raw, "observability")
    raw_prices = observability_raw.get("prices", [])
    if not isinstance(raw_prices, list) or not all(
        isinstance(value, dict) for value in raw_prices
    ):
        raise ConfigError("observability.prices must be an array of tables")
    prices: list[UsagePrice] = []
    price_keys: set[tuple[str, str, str]] = set()
    for raw_price in raw_prices:
        text_fields = {
            key: raw_price.get(key)
            for key in ("harness", "model", "effective_from", "currency")
        }
        if not all(isinstance(value, str) for value in text_fields.values()):
            raise ConfigError("observability price identity fields must be strings")
        harness = str(text_fields["harness"]).strip()
        model = str(text_fields["model"]).strip()
        effective_from = str(text_fields["effective_from"]).strip()
        currency = str(text_fields["currency"]).strip().upper()
        if not harness or not model:
            raise ConfigError("observability price harness and model must not be empty")
        try:
            parsed_date = date.fromisoformat(effective_from)
        except ValueError as exc:
            raise ConfigError(
                "observability price effective_from must use YYYY-MM-DD"
            ) from exc
        if parsed_date.isoformat() != effective_from:
            raise ConfigError("observability price effective_from must use YYYY-MM-DD")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ConfigError("observability price currency must use three letters")
        key = (harness.casefold(), model.casefold(), effective_from)
        if key in price_keys:
            raise ConfigError("observability prices contain a duplicate effective row")
        price_keys.add(key)
        reasoning_rate = raw_price.get("reasoning_output_per_million")
        prices.append(
            UsagePrice(
                harness=harness,
                model=model,
                effective_from=effective_from,
                currency=currency,
                input_per_million=_price_rate(
                    raw_price.get("input_per_million"), "input_per_million"
                ),
                cached_input_per_million=_price_rate(
                    raw_price.get("cached_input_per_million"),
                    "cached_input_per_million",
                ),
                output_per_million=_price_rate(
                    raw_price.get("output_per_million"), "output_per_million"
                ),
                reasoning_output_per_million=(
                    _price_rate(reasoning_rate, "reasoning_output_per_million")
                    if reasoning_rate is not None
                    else None
                ),
            )
        )
    observability = ObservabilityConfig(
        prices=tuple(
            sorted(
                prices,
                key=lambda value: (
                    value.harness.casefold(),
                    value.model.casefold(),
                    value.effective_from,
                ),
            )
        )
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
            enabled=_boolean(repo_value.get("enabled"), True),
            auto_prepare=_boolean(repo_value.get("auto_prepare"), False),
            auto_merge=_boolean(repo_value.get("auto_merge"), False),
            auto_merge_method=str(
                repo_value.get("auto_merge_method", "squash")
            ).strip(),
            owner_attestation=_boolean(repo_value.get("owner_attestation"), False),
            mode=str(repo_value.get("mode", "contributor")).strip(),
            submission_strategy=str(
                repo_value.get("submission_strategy", "fork")
            ).strip(),
            min_stars=int(repo_value.get("min_stars", 1_000)),
            preferred_labels=_tuple(repo_value.get("preferred_labels")),
            blocked_labels=_tuple(repo_value.get("blocked_labels")),
            maintainer_approval=str(repo_value.get("maintainer_approval", "")).strip(),
            require_assignment_before_submit=_boolean(
                repo_value.get("require_assignment_before_submit"), False
            ),
            require_no_competing_work=_boolean(
                repo_value.get("require_no_competing_work"), False
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
            required_contribution_files=_tuple(
                repo_value.get("required_contribution_files")
            ),
            pull_request_body_style=str(
                repo_value.get("pull_request_body_style", "generic")
            ),
            pull_request_template_path=str(
                repo_value.get("pull_request_template_path", "")
            ),
            pull_request_template_sha256=str(
                repo_value.get("pull_request_template_sha256", "")
            ),
            branch_template=str(
                repo_value.get("branch_template", "{login}/issue-{issue}-{slug}")
            ),
            default_scope=str(repo_value.get("default_scope", "repo")),
            max_active_pull_requests=(
                int(repo_value["max_active_pull_requests"])
                if "max_active_pull_requests" in repo_value
                else None
            ),
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
            merge_risk_paths=_tuple(repo_value.get("merge_risk_paths")),
            event_payload_retention_days=(
                int(repo_value["event_payload_retention_days"])
                if "event_payload_retention_days" in repo_value
                else None
            ),
        )

    if not github.login:
        raise ConfigError("[github].login is required; run 'reposteward init'")
    if not github.git_name:
        raise ConfigError("[github].git_name must not be empty")
    if not github.git_email:
        raise ConfigError("[github].git_email must not be empty")
    if issue_review.project_owner_type not in {"user", "organization"}:
        raise ConfigError(
            "issue_review.project_owner_type must be 'user' or 'organization'"
        )
    if issue_review.project_number < 0:
        raise ConfigError("issue_review.project_number must not be negative")
    if bool(issue_review.project_owner) != bool(issue_review.project_number):
        raise ConfigError(
            "issue_review.project_owner and project_number must be configured together"
        )
    if discovery.issues_per_repo < 1 or discovery.issues_per_repo > 100:
        raise ConfigError("discovery.issues_per_repo must be between 1 and 100")
    if discovery.max_pages < 1 or discovery.max_pages > 10:
        raise ConfigError("discovery.max_pages must be between 1 and 10")
    if not 1 <= discovery.competing_work_checks_per_repo <= 25:
        raise ConfigError(
            "discovery.competing_work_checks_per_repo must be between 1 and 25"
        )
    if runner.passed_output_chars < 1:
        raise ConfigError("runner.passed_output_chars must be positive")
    if runner.max_output_chars < runner.passed_output_chars:
        raise ConfigError(
            "runner.max_output_chars must be at least runner.passed_output_chars"
        )
    if runner.max_log_chars < runner.max_output_chars:
        raise ConfigError(
            "runner.max_log_chars must be at least runner.max_output_chars"
        )
    if not 1 <= storage.cache_retention_days <= 36_500:
        raise ConfigError("storage.cache_retention_days must be between 1 and 36500")
    if not 1 <= storage.workspace_retention_days <= 36_500:
        raise ConfigError(
            "storage.workspace_retention_days must be between 1 and 36500"
        )
    if not 1 <= storage.max_gc_items <= 10_000:
        raise ConfigError("storage.max_gc_items must be between 1 and 10000")
    if not 512 <= context.follow_up_max_tokens <= 100_000:
        raise ConfigError("context.follow_up_max_tokens must be between 512 and 100000")
    if any(
        value < 1
        for value in (
            safety.max_active_pull_requests,
            safety.max_files_changed,
            safety.max_diff_lines,
        )
    ):
        raise ConfigError("safety capacity limits must be positive")
    if not agent.harness:
        raise ConfigError("agent.harness must not be empty")
    for repository in repositories.values():
        if any(
            value is not None and value < 1
            for value in (
                repository.max_active_pull_requests,
                repository.max_files_changed,
                repository.max_diff_lines,
            )
        ):
            raise ConfigError(f"{repository.name} capacity limits must be positive")
        if repository.mode not in {"contributor", "maintainer"}:
            raise ConfigError(
                f"unsupported mode for {repository.name}: {repository.mode!r}"
            )
        if repository.auto_merge_method not in {"merge", "squash", "rebase"}:
            raise ConfigError(
                f"unsupported auto_merge_method for {repository.name}: "
                f"{repository.auto_merge_method!r}"
            )
        if repository.auto_merge and (
            repository.mode != "maintainer"
            or repository.submission_strategy != "same-repository"
        ):
            raise ConfigError(
                f"{repository.name} may enable auto_merge only in maintainer "
                "same-repository mode"
            )
        if repository.owner_attestation and (
            repository.mode != "maintainer"
            or repository.submission_strategy != "same-repository"
        ):
            raise ConfigError(
                f"{repository.name} may enable owner_attestation only in "
                "maintainer same-repository mode"
            )
        if repository.submission_strategy not in {"fork", "same-repository"}:
            raise ConfigError(
                f"unsupported submission_strategy for {repository.name}: "
                f"{repository.submission_strategy!r}"
            )
        if (
            repository.submission_strategy == "same-repository"
            and repository.mode != "maintainer"
        ):
            raise ConfigError(
                f"{repository.name} may use same-repository submission only in "
                "maintainer mode"
            )
        if repository.pull_request_body_style not in {
            "generic",
            "boxlite",
            "cindy",
            "deer-flow",
            "lazyllm-feature",
            "mcp-context-forge-docs",
            "hermes-agent",
            "openmldb",
            "paperqa",
            "xskill",
        }:
            raise ConfigError(
                f"unsupported pull_request_body_style for {repository.name}: "
                f"{repository.pull_request_body_style!r}"
            )
        if bool(repository.pull_request_template_path) != bool(
            repository.pull_request_template_sha256
        ):
            raise ConfigError(
                f"{repository.name} must configure both pull_request_template_path "
                "and pull_request_template_sha256"
            )
        if (
            repository.event_payload_retention_days is not None
            and repository.event_payload_retention_days < 1
        ):
            raise ConfigError(
                f"{repository.name} event_payload_retention_days must be positive"
            )

    return AppConfig(
        config_version=version_value or CONFIG_VERSION,
        path=config_path,
        state_dir=state_dir,
        workspace_dir=workspace_dir,
        state_namespace=state_namespace,
        github=github,
        issue_review=issue_review,
        discovery=discovery,
        safety=safety,
        agent=agent,
        runner=runner,
        storage=storage,
        context=context,
        observability=observability,
        repositories=repositories,
    )
