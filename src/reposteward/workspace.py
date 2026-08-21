from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .config import AppConfig, RepositoryPolicy
from .models import Candidate
from .policy import changed_files


class WorkspaceError(RuntimeError):
    """Git workspace preparation or publication failed."""


SENSITIVE_ENV_NAMES = {
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "SSH_AUTH_SOCK",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_SSH_COMMAND",
    "NETRC",
}


def sanitized_environment(
    *,
    keep_codex_credentials: bool = True,
    keep_ssh_credentials: bool = False,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in SENSITIVE_ENV_NAMES:
            if (keep_codex_credentials and upper == "CODEX_API_KEY") or (
                keep_ssh_credentials and upper == "SSH_AUTH_SOCK"
            ):
                result[key] = value
            continue
        if upper.startswith("GIT_CONFIG_") or upper.endswith(
            ("_TOKEN", "_API_KEY", "_SECRET", "_PASSWORD", "_CREDENTIAL")
        ):
            continue
        result[key] = value
    result["GIT_TERMINAL_PROMPT"] = "0"
    return result


def slugify(value: str, *, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:limit].rstrip("-") or "fix"


class WorkspaceManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = config.workspace_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def clone(self, candidate: Candidate) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        owner, name = candidate.issue.repository.split("/", 1)
        target = (
            self.root
            / f"{owner}__{name}"
            / f"issue-{candidate.issue.number}-{timestamp}"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "git",
            "clone",
            "--filter=blob:none",
            "--no-tags",
            "--branch",
            candidate.repository.default_branch,
            f"https://github.com/{candidate.issue.repository}.git",
            str(target),
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=sanitized_environment(keep_codex_credentials=False),
        )
        if result.returncode:
            raise WorkspaceError(f"git clone failed: {result.stderr.strip()}")
        return target

    def create_branch(
        self,
        worktree: Path,
        candidate: Candidate,
        repository: RepositoryPolicy,
        scope: str,
    ) -> str:
        branch = repository.branch_template.format(
            login=self.config.github.login,
            scope=scope,
            issue=candidate.issue.number,
            slug=slugify(candidate.issue.title),
        )
        result = subprocess.run(
            ["git", "switch", "-c", branch],
            cwd=worktree,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise WorkspaceError(
                f"could not create branch {branch}: {result.stderr.strip()}"
            )
        return branch

    def commit(self, worktree: Path, title: str) -> str:
        files = changed_files(worktree)
        if not files:
            raise WorkspaceError("nothing to commit")
        for path in files:
            result = subprocess.run(
                ["git", "add", "--", path],
                cwd=worktree,
                check=False,
                capture_output=True,
                text=True,
                env=sanitized_environment(keep_codex_credentials=False),
            )
            if result.returncode:
                raise WorkspaceError(f"could not stage {path}: {result.stderr.strip()}")
        command = [
            "git",
            "-c",
            f"user.name={self.config.github.git_name}",
            "-c",
            f"user.email={self.config.github.git_email}",
            "commit",
        ]
        if self.config.github.sign_commits:
            command.append("-S")
        if self.config.github.signoff_commits:
            command.append("-s")
        command.extend(["-m", title])
        result = subprocess.run(
            command,
            cwd=worktree,
            check=False,
            capture_output=True,
            text=True,
            env=sanitized_environment(keep_codex_credentials=False),
        )
        if result.returncode:
            raise WorkspaceError(f"git commit failed: {result.stderr.strip()}")
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def push(
        self,
        worktree: Path,
        destination: str,
        branch: str,
        *,
        expected_remote_sha: str = "",
    ) -> None:
        destination_url = f"git@github.com:{destination}.git"
        remote_name = "publish"
        remotes = subprocess.run(
            ["git", "remote"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        remote_command = (
            ["git", "remote", "set-url", remote_name, destination_url]
            if remote_name in remotes
            else ["git", "remote", "add", remote_name, destination_url]
        )
        subprocess.run(remote_command, cwd=worktree, check=True, capture_output=True)

        command = ["git", "push", "--no-verify"]
        if expected_remote_sha:
            command.append(
                f"--force-with-lease=refs/heads/{branch}:{expected_remote_sha}"
            )
        command.extend([remote_name, f"HEAD:refs/heads/{branch}"])
        result = subprocess.run(
            command,
            cwd=worktree,
            check=False,
            capture_output=True,
            text=True,
            env=sanitized_environment(
                keep_codex_credentials=False,
                keep_ssh_credentials=True,
            ),
        )
        if result.returncode:
            raise WorkspaceError(f"git push failed: {result.stderr.strip()}")
