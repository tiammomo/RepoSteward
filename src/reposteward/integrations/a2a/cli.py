"""Explicit local A2A startup; protocol dependencies remain optional."""

from __future__ import annotations

import os
import re
import secrets
import socket
import stat
from pathlib import Path


def add_parser(subparsers):
    parser = subparsers.add_parser("a2a", help="serve scoped project-report delegation")
    commands = parser.add_subparsers(dest="a2a_command", required=True)
    token = commands.add_parser(
        "token", help="create a separate local bearer token file"
    )
    token.add_argument("--output", type=Path, required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--workspace", type=Path, required=True)
    serve.add_argument("--token-file", type=Path, required=True)
    serve.add_argument("--port", type=int, default=0)


def create_token(path: Path) -> dict:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(secrets.token_urlsafe(32) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {"token_file": str(path), "public_write": False}


def read_token(path: Path, workspace: Path) -> str:
    path = path.expanduser().absolute()
    if path.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("keep the A2A token outside the linked repository")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("A2A token must be a private user-owned regular file")
        value = handle.read(513).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", value):
        raise ValueError("invalid A2A token file")
    return value


def serve(config, *, workspace: Path, token_file: Path, port: int = 0):
    if not 0 <= port <= 65535:
        raise ValueError("invalid local port")
    try:
        from reposteward.integrations.a2a.server import create_app
    except ImportError as exc:
        raise RuntimeError("A2A requires reposteward[a2a]") from exc
    import uvicorn

    from reposteward.integrations.mcp import ScopedBridge
    from reposteward.tasks.assistance_operations import AssistanceOperations

    token = read_token(token_file, workspace)
    service = AssistanceOperations(ScopedBridge(config, workspace))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen(64)
        actual = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{actual}"
        app = create_app(service, origin=origin, token=token)
        print(origin + "/.well-known/agent-card.json", flush=True)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="warning",
                access_log=False,
                proxy_headers=False,
                limit_concurrency=16,
            )
        )
        server.run(sockets=[listener])
