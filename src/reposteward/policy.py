from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .capacity import effective_capacity_limit
from .config import AppConfig, RepositoryPolicy
from .models import VerificationResult


class PolicyError(RuntimeError):
    """A change failed an automation safety or contribution policy."""


TITLE_PATTERN = re.compile(
    r"^(?:feat|fix|docs|test|refactor|perf|build|ci|chore|style|revert)"
    r"\(([-a-z0-9]+)\): ([a-z`].+)$"
)


@dataclass(frozen=True, slots=True)
class DiffSummary:
    files: tuple[str, ...]
    added_lines: int
    deleted_lines: int

    @property
    def total_lines(self) -> int:
        return self.added_lines + self.deleted_lines


def conventional_scope(title: str, default: str) -> str:
    match = TITLE_PATTERN.fullmatch(title.strip())
    if not match:
        raise PolicyError(
            "PR title must use a scoped Conventional Commit, for example "
            "'fix(sdk): handle empty paths'"
        )
    return match.group(1) or default


def changed_files(worktree: Path, base_ref: str = "HEAD") -> list[str]:
    tracked = subprocess.run(
        ["git", "diff", "--name-only", base_ref],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return sorted(set(tracked + untracked))


def summarize_diff(worktree: Path, base_ref: str = "HEAD") -> DiffSummary:
    files = changed_files(worktree, base_ref)
    added = 0
    deleted = 0
    output = subprocess.run(
        ["git", "diff", "--numstat", base_ref],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    tracked_paths: set[str] = set()
    for line in output.splitlines():
        add_text, delete_text, path = line.split("\t", 2)
        tracked_paths.add(path)
        if add_text != "-":
            added += int(add_text)
        if delete_text != "-":
            deleted += int(delete_text)
    for path_text in files:
        if path_text in tracked_paths:
            continue
        path = worktree / path_text
        if not path.is_file() or path.is_symlink():
            added += 1
            continue
        try:
            added += len(path.read_text(encoding="utf-8").splitlines())
        except UnicodeDecodeError:
            added += 1
    return DiffSummary(tuple(files), added, deleted)


def enforce_change_policy(
    worktree: Path,
    verification: VerificationResult,
    repository: RepositoryPolicy,
    config: AppConfig,
    *,
    base_ref: str = "HEAD",
) -> DiffSummary:
    diff_check = subprocess.run(
        ["git", "diff", "--check", base_ref],
        cwd=worktree,
        check=False,
        capture_output=True,
        text=True,
    )
    if diff_check.returncode:
        raise PolicyError(f"git diff --check failed:\n{diff_check.stdout.strip()}")

    summary = summarize_diff(worktree, base_ref)
    if not summary.files:
        raise PolicyError("agent produced no repository changes")
    file_limit = effective_capacity_limit(
        config.safety.max_files_changed, repository.max_files_changed
    )
    line_limit = effective_capacity_limit(
        config.safety.max_diff_lines, repository.max_diff_lines
    )
    if len(summary.files) > file_limit:
        raise PolicyError(
            f"change touches {len(summary.files)} files; policy limit is {file_limit}"
        )
    if summary.total_lines > line_limit:
        raise PolicyError(
            f"change has {summary.total_lines} changed lines; policy limit is {line_limit}"
        )
    forbidden = tuple(value.casefold() for value in config.safety.forbidden_paths)
    rejected = [
        path
        for path in summary.files
        if any(value in path.casefold() for value in forbidden)
    ]
    if rejected:
        raise PolicyError(f"change touches forbidden paths: {', '.join(rejected)}")
    if config.safety.require_verification and not verification.passed:
        raise PolicyError(f"verification did not pass: {verification.reason}")
    return summary
