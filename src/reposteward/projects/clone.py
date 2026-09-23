"""SSH-only clone into an owned staging directory and no-replace publication."""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from reposteward.projects.registry import ProjectError
from reposteward.storage.workspace import sanitized_environment


def directory_identity(path: Path) -> dict:
    if path.is_symlink() or not path.is_dir():
        raise ProjectError("directory is unavailable or a symbolic link")
    stat = path.stat()
    return {"device": stat.st_dev, "inode": stat.st_ino}


def publish_directory(source: Path, target: Path, expected_parent: dict) -> None:
    """Linux renameat2(RENAME_NOREPLACE); never fall back to replacing rename."""
    if sys.platform != "linux":
        raise ProjectError("atomic clone publication is unsupported on this platform")
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise ProjectError("atomic clone publication is unsupported") from exc
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    source_parent = -1
    try:
        source_parent = os.open(
            source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        stat = os.fstat(parent)
        if {
            "device": stat.st_dev,
            "inode": stat.st_ino,
        } != expected_parent or directory_identity(target.parent) != expected_parent:
            raise ProjectError("clone parent changed; create a new plan")
        if rename(
            source_parent, os.fsencode(source.name), parent, os.fsencode(target.name), 1
        ):
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise ProjectError(
                    "clone target already exists; it was not overwritten"
                )
            raise ProjectError("atomic clone publication failed; staging retained")
        os.fsync(parent)
        os.fsync(source_parent)
    finally:
        if source_parent >= 0:
            os.close(source_parent)
        os.close(parent)


def clone_ssh(host: str, repository: str, destination: Path, guard) -> None:
    """No host Git configuration, templates, hooks, filters or API token reach Git."""
    env = sanitized_environment(keep_codex_credentials=False, keep_ssh_credentials=True)
    for key in list(env):
        if key.startswith("GIT_"):
            env.pop(key)
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_TERMINAL_PROMPT="0",
        GIT_SSH_COMMAND="ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15",
        GIT_LFS_SKIP_SMUDGE="1",
    )
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.ssh.allow=always",
        "clone",
        "--template=",
        "--no-recurse-submodules",
        "--",
        f"git@{host}:{repository}.git",
        str(destination),
    ]
    guard()
    with subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as process:
        deadline = time.monotonic() + 600
        try:
            while process.poll() is None:
                guard()
                if time.monotonic() > deadline:
                    raise ProjectError("SSH clone timed out; staging retained")
                time.sleep(0.2)
            if process.returncode:
                raise ProjectError("SSH clone failed; check host SSH access and retry")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
