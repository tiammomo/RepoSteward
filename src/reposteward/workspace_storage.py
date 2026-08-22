from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat as stat_module
import subprocess
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .workspace import sanitized_environment

WORKSPACE_KIND = "workspace_cache"
MANAGED_WORKSPACE = re.compile(r"issue-[1-9][0-9]*-\d{8}T\d{6}Z\Z")
TERMINAL_RUN_STATUSES = {"ready", "submitted", "failed"}


def _absolute(path: Path) -> Path:
    """Normalize lexical components without following a symlink."""
    return Path(os.path.abspath(os.fspath(path)))


def _iso_from_ns(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000_000, UTC).isoformat()


def _later_timestamp(left: str, right: str) -> str:
    return max((value for value in (left, right) if value), default="")


def _tree_snapshot(path: Path) -> dict[str, Any]:
    """Return bounded metadata for one tree without following symlinks."""
    digest_total = 0
    try:
        root_stat = path.lstat()
    except OSError as exc:
        return {"error": str(exc)}
    total_bytes = root_stat.st_size
    records = 1
    root_timestamp = _iso_from_ns(root_stat.st_mtime_ns)
    oldest_at = root_timestamp
    newest_at = root_timestamp
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    stat = entry.stat(follow_symlinks=False)
                    entry_path = Path(entry.path)
                    relative = entry_path.relative_to(path).as_posix()
                    if stat_module.S_ISLNK(stat.st_mode):
                        kind = "symlink"
                    elif stat_module.S_ISDIR(stat.st_mode):
                        kind = "directory"
                        stack.append(entry_path)
                    elif stat_module.S_ISREG(stat.st_mode):
                        kind = "file"
                    else:
                        kind = "other"
                    material = {
                        "path": relative,
                        "kind": kind,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "device": stat.st_dev,
                        "inode": stat.st_ino,
                    }
                    encoded = json.dumps(
                        material,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    digest_total = (
                        digest_total
                        + int.from_bytes(
                            hashlib.sha256(encoded).digest(), byteorder="big"
                        )
                    ) % (1 << 256)
                    total_bytes += stat.st_size
                    records += 1
                    timestamp = _iso_from_ns(stat.st_mtime_ns)
                    oldest_at = min(value for value in (oldest_at, timestamp) if value)
                    newest_at = _later_timestamp(newest_at, timestamp)
        except OSError as exc:
            return {"error": str(exc)}
    return {
        "bytes": total_bytes,
        "records": records,
        "oldest_at": oldest_at,
        "newest_at": newest_at,
        "snapshot_digest": f"{digest_total:064x}",
    }


def _repository_containers(repositories: tuple[str, ...]) -> dict[str, str]:
    result = {}
    for repository in repositories:
        owner, name = repository.casefold().split("/", 1)
        result[f"{owner}__{name}"] = repository.casefold()
    return result


def scan_workspaces(
    workspace_root: Path,
    *,
    repositories: tuple[str, ...],
    runs: dict[str, dict[str, Any]],
    paths: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Inventory managed workspace directories in O(total directory entries)."""
    root = _absolute(workspace_root)
    if not root.exists():
        return {"items": [], "errors": []}
    if root.is_symlink() or not root.is_dir():
        return {
            "items": [],
            "errors": [{"path": ".", "reason": "workspace_root_not_safe"}],
        }

    run_paths: dict[Path, list[dict[str, Any]]] = {}
    for run_id, raw in runs.items():
        worktree = str(raw.get("worktree") or "").strip()
        if not worktree:
            continue
        path = _absolute(Path(worktree))
        try:
            path.relative_to(root)
        except ValueError:
            continue
        run_paths.setdefault(path, []).append({"id": str(run_id), **raw})

    containers = _repository_containers(repositories)
    selected_containers = (
        {PurePosixPath(value).parts[0] for value in paths if PurePosixPath(value).parts}
        if paths is not None
        else None
    )
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    try:
        with os.scandir(root) as iterator:
            repository_entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        return {"items": [], "errors": [{"path": ".", "reason": str(exc)}]}

    for repository_entry in repository_entries:
        if (
            selected_containers is not None
            and repository_entry.name not in selected_containers
        ):
            continue
        if repository_entry.is_symlink() or not repository_entry.is_dir(
            follow_symlinks=False
        ):
            continue
        repository_path = Path(repository_entry.path)
        try:
            with os.scandir(repository_path) as iterator:
                workspace_entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            errors.append(
                {
                    "path": repository_path.relative_to(root).as_posix(),
                    "reason": str(exc),
                }
            )
            continue
        for workspace_entry in workspace_entries:
            if not MANAGED_WORKSPACE.fullmatch(workspace_entry.name):
                continue
            path = Path(workspace_entry.path)
            relative = path.relative_to(root).as_posix()
            if paths is not None and relative not in paths:
                continue
            states = sorted(
                run_paths.get(_absolute(path), []), key=lambda value: value["id"]
            )
            run_repositories = {
                str(value.get("repository") or "").casefold()
                for value in states
                if value.get("repository")
            }
            repository = containers.get(repository_entry.name.casefold(), "")
            if not repository and len(run_repositories) == 1:
                repository = next(iter(run_repositories))
            repository = repository or "*"
            if workspace_entry.is_symlink():
                items.append(
                    {
                        "kind": WORKSPACE_KIND,
                        "path": relative,
                        "repository": repository,
                        "run_states": states,
                        "scan_error": "workspace_symlink",
                    }
                )
                continue
            if not workspace_entry.is_dir(follow_symlinks=False):
                continue
            try:
                root_stat = path.lstat()
            except OSError as exc:
                errors.append({"path": relative, "reason": str(exc)})
                continue
            snapshot = _tree_snapshot(path)
            if snapshot.get("error"):
                items.append(
                    {
                        "kind": WORKSPACE_KIND,
                        "path": relative,
                        "repository": repository,
                        "run_states": states,
                        "scan_error": str(snapshot["error"]),
                    }
                )
                continue
            created_at = _iso_from_ns(root_stat.st_mtime_ns)
            run_updated_at = max(
                (str(value.get("updated_at") or "") for value in states), default=""
            )
            items.append(
                {
                    "kind": WORKSPACE_KIND,
                    "path": relative,
                    "repository": repository,
                    "run_states": states,
                    "run_ids": [value["id"] for value in states],
                    "bytes": int(snapshot["bytes"]),
                    "records": int(snapshot["records"]),
                    "created_at": created_at,
                    "last_activity_at": _later_timestamp(
                        str(snapshot["newest_at"]), run_updated_at
                    ),
                    "oldest_at": str(snapshot["oldest_at"]),
                    "newest_at": str(snapshot["newest_at"]),
                    "mtime_ns": root_stat.st_mtime_ns,
                    "device": root_stat.st_dev,
                    "inode": root_stat.st_ino,
                    "snapshot_digest": str(snapshot["snapshot_digest"]),
                }
            )
    return {"items": items, "errors": errors}


def workspace_statistics(
    inventory: dict[str, Any], *, repository: str, cutoff: str
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    groups: dict[str, dict[str, Any]] = {}
    errors = list(inventory.get("errors", ()))
    for item in inventory.get("items", ()):
        item_repository = str(item.get("repository") or "*")
        if repository and item_repository != repository:
            continue
        if item.get("scan_error"):
            errors.append(
                {"path": str(item["path"]), "reason": str(item["scan_error"])}
            )
            continue
        last_activity = str(item["last_activity_at"])
        if cutoff and last_activity < cutoff:
            continue
        group = groups.setdefault(
            item_repository,
            {
                "repository": item_repository,
                "category": WORKSPACE_KIND,
                "records": 0,
                "bytes": 0,
                "oldest_at": "",
                "newest_at": "",
            },
        )
        group["records"] += 1
        group["bytes"] += int(item["bytes"])
        group["oldest_at"] = min(
            value for value in (group["oldest_at"], item["created_at"]) if value
        )
        group["newest_at"] = _later_timestamp(str(group["newest_at"]), last_activity)
    return list(groups.values()), errors


def _git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *arguments],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
        env={
            **sanitized_environment(keep_codex_credentials=False),
            "GIT_OPTIONAL_LOCKS": "0",
        },
    )


def _git_recovery_state(
    workspace: Path, run_states: list[dict[str, Any]]
) -> dict[str, Any]:
    status = _git(
        workspace,
        "status",
        "--porcelain=v2",
        "--branch",
        "--untracked-files=all",
    )
    if status.returncode:
        return {"reason": "git_status_unavailable", "head_commit": ""}
    lines = status.stdout.splitlines()
    head_commit = next(
        (
            line.removeprefix("# branch.oid ").strip()
            for line in lines
            if line.startswith("# branch.oid ")
        ),
        "",
    )
    if not re.fullmatch(r"[a-f0-9]{40,64}", head_commit):
        return {"reason": "not_git_workspace", "head_commit": ""}
    if any(line and not line.startswith("# ") for line in lines):
        return {"reason": "dirty_workspace", "head_commit": head_commit}
    submitted = any(
        str(value.get("status") or "") == "submitted"
        and str(value.get("head_commit") or "") == head_commit
        for value in run_states
    )
    if not submitted:
        remote_refs = _git(
            workspace,
            "for-each-ref",
            "--contains",
            head_commit,
            "--format=%(refname)",
            "refs/remotes",
        )
        submitted = remote_refs.returncode == 0 and bool(remote_refs.stdout.strip())
    return {
        "reason": "" if submitted else "unpushed_commits",
        "head_commit": head_commit,
    }


def workspace_gc_inventory(
    workspace_root: Path,
    *,
    repositories: tuple[str, ...],
    runs: dict[str, dict[str, Any]],
    repository: str,
    cutoff: str,
    paths: frozenset[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    inventory = scan_workspaces(
        workspace_root,
        repositories=repositories,
        runs=runs,
        paths=paths,
    )
    candidates = []
    retained = []
    for raw in inventory["items"]:
        item = dict(raw)
        item.setdefault("bytes", 0)
        item.setdefault("created_at", "")
        item.setdefault("last_activity_at", "")
        item.setdefault("run_ids", [])
        item_repository = str(item.get("repository") or "*")
        if repository and item_repository != repository:
            continue
        run_states = list(item.pop("run_states", ()))
        reasons: set[str] = set()
        if item.pop("scan_error", ""):
            reasons.add("workspace_scan_unsafe")
        if not run_states:
            reasons.add("unknown_run")
        run_repositories = {
            str(value.get("repository") or "").casefold() for value in run_states
        }
        if run_repositories and run_repositories != {item_repository}:
            reasons.add("repository_mismatch")
        if any(str(value.get("status") or "") == "running" for value in run_states):
            reasons.add("active_run")
        if any(
            str(value.get("status") or "") not in TERMINAL_RUN_STATUSES
            or not bool(value.get("terminal_checkpoint"))
            for value in run_states
        ):
            reasons.add("no_terminal_checkpoint")
        last_activity = str(item.get("last_activity_at") or "")
        if not last_activity or last_activity >= cutoff:
            reasons.add("within_workspace_retention")
        if not reasons:
            git_state = _git_recovery_state(
                _absolute(workspace_root) / str(item["path"]), run_states
            )
            item["head_commit"] = git_state["head_commit"]
            if git_state["reason"]:
                reasons.add(str(git_state["reason"]))
        if reasons:
            item["reasons"] = sorted(reasons)
            retained.append(item)
        else:
            item["reason"] = "expired_recoverable_terminal_workspace"
            candidates.append(item)
    for error in inventory["errors"]:
        retained.append(
            {
                "kind": WORKSPACE_KIND,
                "path": str(error["path"]),
                "repository": "*",
                "bytes": 0,
                "created_at": "",
                "reasons": ["workspace_scan_unsafe"],
            }
        )
    key = lambda value: (str(value.get("created_at") or ""), str(value["path"]))
    return {
        "candidates": sorted(candidates, key=key),
        "retained": sorted(retained, key=key),
    }


def delete_workspace(workspace_root: Path, candidate: dict[str, Any]) -> None:
    relative = PurePosixPath(str(candidate["path"]))
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or any(part in {"", ".", ".."} for part in relative.parts)
        or not MANAGED_WORKSPACE.fullmatch(relative.parts[1])
    ):
        raise OSError("workspace path no longer satisfies the managed layout")
    root = _absolute(workspace_root)
    parent = root / relative.parts[0]
    target = parent / relative.parts[1]
    if root.is_symlink() or parent.is_symlink() or target.is_symlink():
        raise OSError("workspace path became a symlink")
    try:
        stat = target.lstat()
    except OSError as exc:
        raise OSError("workspace no longer exists") from exc
    if not target.is_dir():
        raise OSError("workspace is no longer a directory")
    if stat.st_dev != int(candidate["device"]) or stat.st_ino != int(
        candidate["inode"]
    ):
        raise OSError("workspace identity changed after planning")
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise OSError("workspace escaped the configured root") from exc
    shutil.rmtree(target)
