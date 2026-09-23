"""Durable, host-owned Docker identities for interrupted external verification."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path


def read_record(path: Path) -> dict:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("execution record path contains a symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("execution record is not a file")
        raw = stream.read(64001)
    if len(raw) > 64000:
        raise ValueError("execution record exceeds limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid execution record")  # noqa: TRY004 - malformed persisted JSON
    return value


def write_record(path: Path, value: dict) -> None:
    # Publish before starting Docker. A truncated record after a crash fails closed.
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def begin(directory: Path) -> None:
    if (directory / "execution.json").exists():
        marker(directory)
        if any((directory / "verification").glob("*.container.json")):
            raise ValueError("prior execution requires reconciliation")
        return
    write_record(
        directory / "execution.json", {"schema_version": 1, "token": uuid.uuid4().hex}
    )


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env={"PATH": os.environ.get("PATH", "")},
    )
    if result.returncode:
        raise RuntimeError("Docker execution state is unavailable")
    return result.stdout


def marker(directory: Path) -> dict:
    value = read_record(directory / "execution.json")
    if value.get("schema_version") != 1 or not re.fullmatch(
        "[a-f0-9]{32}", value.get("token", "")
    ):
        raise ValueError("invalid execution identity")
    return value


def record_container(log_path: Path | None, name: str) -> str:
    if log_path is None or not (log_path.parent.parent / "execution.json").exists():
        return ""
    identity = marker(log_path.parent.parent)
    daemon = docker("info", "--format", "{{.ID}}").strip()
    if not daemon or len(daemon) > 200:
        raise ValueError("invalid Docker daemon identity")
    write_record(
        log_path.with_suffix(".container.json"),
        {
            "schema_version": 1,
            "token": identity["token"],
            "name": name,
            "daemon": daemon,
        },
    )
    return identity["token"]


def observe(directory: Path) -> dict:
    identity = marker(directory)
    paths = sorted((directory / "verification").glob("*.container.json"))
    if len(paths) > 101:
        raise ValueError("too many execution records")
    receipts = [read_record(path) for path in paths]
    daemon = docker("info", "--format", "{{.ID}}").strip() if receipts else ""
    observed = []
    for receipt in receipts:
        name = receipt.get("name", "")
        if (
            receipt.get("schema_version") != 1
            or receipt.get("token") != identity["token"]
            or receipt.get("daemon") != daemon
            or not re.fullmatch("reposteward-verify-[a-f0-9]{32}", name)
        ):
            raise ValueError("execution receipt identity mismatch")
        identifiers = docker(
            "ps",
            "-a",
            "--no-trunc",
            "--filter",
            f"name=^/{name}$",
            "--format",
            "{{.ID}}",
        ).split()
        if not identifiers:
            observed.append({"receipt": receipt, "state": "absent"})
            continue
        if len(identifiers) != 1 or not re.fullmatch("[a-f0-9]{64}", identifiers[0]):
            raise ValueError("ambiguous container identity")
        value = json.loads(docker("inspect", identifiers[0]))
        if not isinstance(value, list) or len(value) != 1:
            raise ValueError("invalid container observation")
        container = value[0]
        if (
            container.get("Name") != "/" + name
            or container.get("Id") != identifiers[0]
            or container.get("Config", {})
            .get("Labels", {})
            .get("reposteward.execution")
            != identity["token"]
        ):
            raise ValueError("container identity changed")
        status = container.get("State", {}).get("Status")
        if status not in {"exited", "dead"}:
            raise RuntimeError("verification container remains active or unconfirmed")
        observed.append({"receipt": receipt, "state": status, "id": identifiers[0]})
    return {"identity": identity, "containers": observed}
