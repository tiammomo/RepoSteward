"""Preview client-specific local STDIO configurations without changing client settings."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import AppConfig
from .mcp_bridge import ScopedBridge


def client_config(config: AppConfig, workspace: Path, *, client: str) -> dict:
    bridge = ScopedBridge(config, workspace)
    entry = {
        "command": sys.executable,
        "args": [
            "-m",
            "reposteward.cli",
            "--config",
            str(config.path),
            "mcp",
            "serve",
            str(bridge.root),
        ],
    }
    if client == "codex":
        fragment = (
            "[mcp_servers.reposteward]\n"
            + "\n".join(
                f"{key} = {json.dumps(value, ensure_ascii=False)}"
                for key, value in entry.items()
            )
            + "\n"
        )
        destination = "user Codex config.toml"
        syntax = "toml"
    elif client == "claude-code":
        fragment = json.dumps(
            {"mcpServers": {"reposteward": {"type": "stdio", **entry}}},
            ensure_ascii=False,
            indent=2,
        )
        destination = "a user-owned temporary file passed to Claude Code --mcp-config"
        syntax = "json"
    elif client == "copilot-vscode":
        fragment = json.dumps(
            {"servers": {"reposteward": {"type": "stdio", **entry}}},
            ensure_ascii=False,
            indent=2,
        )
        destination = "VS Code user MCP configuration"
        syntax = "json"
    else:
        raise ValueError("unsupported MCP client; use the file/CLI handoff")
    return {
        "client": client,
        "format": syntax,
        "destination": destination,
        "fragment": fragment,
        "writes_files": False,
        "client_version": "not_probed",
        "actual_session_validation": "not_run",
        "scope": "one local workspace",
        "note": "This machine-specific preview is for local client settings. Keep it outside Git. Install reposteward[mcp] in the indicated Python environment first.",
        "public_write": False,
    }
