from __future__ import annotations

import json
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from .config import CONFIG_VERSION, ConfigError, default_user_config_path

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _capture(command: list[str]) -> str:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def detected_identity() -> tuple[str, str, str]:
    login = _capture(["gh", "api", "user", "--jq", ".login"])
    if not login:
        raise ConfigError(
            "could not detect a GitHub login; run 'gh auth login' or pass --login"
        )
    git_name = _capture(["git", "config", "--global", "user.name"]) or login
    git_email = _capture(["git", "config", "--global", "user.email"])
    if not git_email:
        git_email = f"{login}@users.noreply.github.com"
    return login, git_name, git_email


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_user_config(login: str, git_name: str, git_email: str) -> str:
    return f"""config_version = {CONFIG_VERSION}

[github]
login = {_toml_string(login)}
git_name = {_toml_string(git_name)}
git_email = {_toml_string(git_email)}
sign_commits = false
signoff_commits = true
api_url = "https://api.github.com"
token_env = ["GITHUB_TOKEN", "GH_TOKEN"]
gh_auth_command = ["gh", "auth", "token", "--hostname", "github.com"]

[agent]
harness = "codex-cli"
executable = "codex"
timeout_seconds = 1800
model = ""
reasoning_effort = ""

[runner]
image = "reposteward-runner:latest"
timeout_seconds = 1800
cpus = 4
memory = "8g"
pids_limit = 1024
max_output_chars = 12000
passed_output_chars = 2000
max_log_chars = 2000000
"""


def _write_atomic(path: Path, content: str, *, force: bool) -> None:
    path = path.expanduser().resolve()
    if path.exists() and not force:
        raise ConfigError(f"configuration already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
    temporary.replace(path)


def initialize_user_config(
    *,
    path: Path | None = None,
    login: str = "",
    git_name: str = "",
    git_email: str = "",
    force: bool = False,
) -> dict[str, Any]:
    if not login:
        detected_login, detected_name, detected_email = detected_identity()
        login = detected_login
        git_name = git_name or detected_name
        git_email = git_email or detected_email
    git_name = git_name or login
    git_email = git_email or f"{login}@users.noreply.github.com"
    target = (path or default_user_config_path()).expanduser().resolve()
    _write_atomic(
        target,
        render_user_config(login, git_name, git_email),
        force=force,
    )
    return {
        "config": str(target),
        "github_login": login,
        "next_action": "run 'reposteward repo add owner/name' in a project directory",
    }


def add_repository(
    repository: str,
    *,
    path: Path | None = None,
    mode: str = "contributor",
) -> dict[str, Any]:
    repository = repository.strip()
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ConfigError("repository must use owner/name form")
    if mode not in {"contributor", "maintainer"}:
        raise ConfigError("mode must be 'contributor' or 'maintainer'")
    target = (path or Path.cwd() / ".reposteward.toml").expanduser().resolve()
    existing = ""
    parsed: dict[str, Any] = {}
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        try:
            parsed = tomllib.loads(existing)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {target}: {exc}") from exc
        configured = parsed.get("repositories", {})
        if isinstance(configured, dict) and any(
            str(value).casefold() == repository.casefold() for value in configured
        ):
            raise ConfigError(f"repository is already configured: {repository}")
    else:
        existing = (
            f"config_version = {CONFIG_VERSION}\n\n"
            "[project]\n"
            'state_dir = ".reposteward"\n'
            "namespace_state = true\n"
        )
    separator = (
        ""
        if existing.endswith("\n\n")
        else ("\n" if existing.endswith("\n") else "\n\n")
    )
    section = f"""[repositories.{_toml_string(repository)}]
enabled = true
auto_prepare = false
mode = {_toml_string(mode)}
submission_strategy = {_toml_string("same-repository" if mode == "maintainer" else "fork")}
require_no_competing_work = true
bootstrap_commands = []
verification_prefixes = []
required_verification_markers = []
pull_request_body_style = "generic"
branch_template = "{{login}}/{{scope}}/{{slug}}"
default_scope = "repo"
"""
    _write_atomic(target, existing + separator + section, force=target.exists())
    return {
        "config": str(target),
        "repository": repository,
        "mode": mode,
        "next_action": "configure bootstrap_commands and verification_prefixes",
    }
