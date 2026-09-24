from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from reposteward.core.config import (
    AppConfig,
    ConfigError,
    RepositoryPolicy,
    fresh_workflow_grants,
)
from reposteward.core.models import VerificationResult
from reposteward.maintenance.capacity import (
    effective_capacity_limit,
    effective_diff_line_limit,
)


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
    workflow_review: dict | None = None

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
    tracked = (
        subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", base_ref],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.rstrip("\0")
        .split("\0")
    )
    untracked = (
        subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.rstrip("\0")
        .split("\0")
    )
    return sorted(set(tracked + untracked) - {""})


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


def workflow_review_for_change(
    worktree: Path,
    files: tuple[str, ...],
    config: AppConfig,
    repository: str,
    issue_number: int,
    base_ref: str,
) -> dict | None:
    paths = tuple(sorted(p for p in files if p.startswith(".github/workflows/")))
    if not paths:
        return None
    try:
        grants = fresh_workflow_grants(config)
    except (ConfigError, OSError) as exc:
        raise PolicyError(f"workflow review cannot be refreshed: {exc}") from exc
    base = subprocess.run(
        ["git", "rev-parse", "--verify", f"{base_ref}^{{commit}}"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    hashes = {}
    for path in paths:
        source = worktree / path
        if any(
            parent.is_symlink()
            for parent in (source, *source.parents)
            if parent != worktree.parent
        ):
            raise PolicyError("workflow review refuses symlink paths")
        if not source.is_file():
            raise PolicyError("workflow review requires existing regular YAML files")
        hashes[path] = hashlib.sha256(source.read_bytes()).hexdigest()
    for grant in grants:
        if (
            grant.repository == repository.casefold()
            and grant.issue == issue_number
            and grant.base_commit == base
            and grant.reviewed_by == config.github.login.casefold()
            and grant.api_url == config.github.api_url
            and dict(grant.files) == hashes
        ):
            evidence = asdict(grant)
            evidence["files"] = hashes
            evidence["digest"] = hashlib.sha256(
                json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return evidence
    raise PolicyError("workflow changes require an exact trusted workflow review grant")


def enforce_change_policy(
    worktree: Path,
    verification: VerificationResult,
    repository: RepositoryPolicy,
    config: AppConfig,
    *,
    base_ref: str = "HEAD",
    issue_number: int = 0,
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
    line_limit = effective_diff_line_limit(
        config.safety.max_diff_lines,
        repository.max_diff_lines,
        user_allows_unlimited=repository.unlimited_diff_lines,
    )
    if len(summary.files) > file_limit:
        raise PolicyError(
            f"change touches {len(summary.files)} files; policy limit is {file_limit}"
        )
    if line_limit is not None and summary.total_lines > line_limit:
        raise PolicyError(
            f"change has {summary.total_lines} changed lines; policy limit is {line_limit}"
        )
    review = workflow_review_for_change(
        worktree, summary.files, config, repository.name, issue_number, base_ref
    )
    forbidden = tuple(value.casefold() for value in config.safety.forbidden_paths)
    rejected = [
        path
        for path in summary.files
        if any(
            value in path.casefold()
            and not (
                value == ".github/workflows/" and review and path in review["files"]
            )
            for value in forbidden
        )
    ]
    if rejected:
        raise PolicyError(f"change touches forbidden paths: {', '.join(rejected)}")
    if config.safety.require_verification and not verification.passed:
        raise PolicyError(f"verification did not pass: {verification.reason}")
    return replace(summary, workflow_review=review)
