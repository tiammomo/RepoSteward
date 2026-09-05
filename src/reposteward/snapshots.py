"""Bounded code identities for development handoff, including uncommitted files."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from .projects import ProjectError, canonical_digest, local_git, workspace_state
from .verifier import UNTRACKED_SANDBOX_EXCLUDED_NAMES, DockerVerifier

MAX_SNAPSHOT_FILES = 20_000
MAX_SNAPSHOT_FILE_BYTES = 20_000_000
MAX_SNAPSHOT_BYTES = 200_000_000


def _entries(
    root: Path, *, trusted_sensitive_paths: tuple[str, ...]
) -> tuple[list[dict[str, Any]], int]:
    tracked = local_git(root, "ls-files", "--cached", "-z").split("\0")
    untracked = local_git(
        root, "ls-files", "--others", "--exclude-standard", "-z"
    ).split("\0")
    names = {name: True for name in tracked if name}
    for name in untracked:
        if name:
            names.setdefault(name, False)
    if len(names) > MAX_SNAPSHOT_FILES:
        raise ProjectError("workspace exceeds snapshot file limit")
    manifest, total, excluded = [], 0, 0
    for name, is_tracked in sorted(names.items()):
        relative = Path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or any(ord(c) < 32 for c in name)
        ):
            raise ProjectError("unsupported snapshot path")
        source = root / relative
        if DockerVerifier._sensitive_path(relative):
            if is_tracked and DockerVerifier._is_supported_env_template(relative):
                DockerVerifier._validate_env_template(source, relative)
            elif (
                is_tracked
                and not DockerVerifier._environment_path(relative)
                and DockerVerifier._is_allowlisted_tracked_sensitive_path(
                    relative, trusted_sensitive_paths
                )
            ):
                DockerVerifier._validate_allowlisted_sensitive_source(source, relative)
            elif is_tracked:
                raise ProjectError(
                    "tracked sensitive path cannot enter a task snapshot"
                )
            else:
                excluded += 1
                continue
        if not is_tracked and any(
            part in UNTRACKED_SANDBOX_EXCLUDED_NAMES for part in relative.parts
        ):
            excluded += 1
            continue
        if not source.parent.resolve().is_relative_to(root):
            raise ProjectError("snapshot path escapes the workspace")
        # Do not follow directory links even when they resolve back inside the root.
        parent = source.parent
        while parent != root:
            if parent.is_symlink():
                raise ProjectError("snapshot parent must not be a symlink")
            parent = parent.parent
        try:
            before = source.lstat()
        except FileNotFoundError:
            manifest.append({"path": name, "kind": "deleted", "tracked": is_tracked})
            continue
        if stat.S_ISLNK(before.st_mode):
            payload = os.readlink(source).encode()
            kind = "symlink"
            size = len(payload)
            digest = hashlib.sha256(payload).hexdigest()
        elif stat.S_ISREG(before.st_mode):
            size = before.st_size
            if size > MAX_SNAPSHOT_FILE_BYTES or total + size > MAX_SNAPSHOT_BYTES:
                raise ProjectError("workspace exceeds snapshot byte limit")
            hasher = hashlib.sha256()
            # O_NOFOLLOW closes a replacement-to-symlink race between lstat/open.
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode) or (
                    opened.st_dev,
                    opened.st_ino,
                ) != (before.st_dev, before.st_ino):
                    raise ProjectError("workspace changed during snapshot")
                count = 0
                while chunk := handle.read(1024 * 1024):
                    count += len(chunk)
                    if (
                        count > MAX_SNAPSHOT_FILE_BYTES
                        or total + count > MAX_SNAPSHOT_BYTES
                    ):
                        raise ProjectError(
                            "workspace changed beyond snapshot byte limit"
                        )
                    hasher.update(chunk)
            if count != size:
                raise ProjectError("workspace changed during snapshot")
            digest = hasher.hexdigest()
            kind = "file"
        else:
            raise ProjectError(
                "submodules and special files require an explicit snapshot adapter"
            )
        after = source.lstat()
        signature = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            value.st_mode,
        )
        if signature(before) != signature(after):
            raise ProjectError("workspace changed during snapshot")
        total += size
        manifest.append(
            {
                "path": name,
                "kind": kind,
                "tracked": is_tracked,
                "bytes": size,
                "executable": bool(before.st_mode & stat.S_IXUSR),
                "digest": digest,
            }
        )
    return manifest, excluded


def workspace_snapshot(
    root: Path, *, trusted_sensitive_paths: tuple[str, ...] = ()
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    before = workspace_state(root)
    manifest, excluded = _entries(root, trusted_sensitive_paths=trusted_sensitive_paths)
    repeated, repeated_excluded = _entries(
        root, trusted_sensitive_paths=trusted_sensitive_paths
    )
    after = workspace_state(root)
    if before != after or manifest != repeated or excluded != repeated_excluded:
        raise ProjectError(
            "workspace changed during snapshot; retry after editing stops"
        )
    material = {
        "schema_version": 1,
        "head": before["head"],
        "branch": before["branch"],
        "dirty": before["dirty"],
        "files": manifest,
        "excluded_untracked": excluded,
    }
    return {**material, "digest": canonical_digest(material)}


def snapshot_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if key != "files"} | {
        "file_count": len(snapshot["files"])
    }
