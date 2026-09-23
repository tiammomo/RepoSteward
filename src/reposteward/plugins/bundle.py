"""Export a reviewed local plugin; client installation remains a separate action."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from importlib.resources import files
from pathlib import Path

from reposteward import __version__
from reposteward.core.config import AppConfig
from reposteward.integrations.mcp import ScopedBridge
from reposteward.integrations.mcp_config import server_entry
from reposteward.projects.registry import ProjectError, canonical_digest

SKILLS = ("understand-project", "resume-task", "verify-change", "maintain-pr")
MANIFEST = ".codex-plugin/plugin.json"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _runtime_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    return canonical_digest(
        {
            str(p.relative_to(root)): _sha(p.read_bytes())
            for p in sorted(root.rglob("*.py"))
        }
    )


class PluginBundle:
    def __init__(self, config: AppConfig):
        self.config = config

    def plan(self, workspace: Path, *, output: Path) -> dict:
        bridge = ScopedBridge(self.config, workspace)
        # Resolve the parent, never the final name: even a dangling symlink is
        # an occupied destination, not permission to follow or replace it.
        output = output.expanduser().absolute()
        parent = output.parent.resolve(strict=True)
        output = parent / output.name
        if (
            not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", output.name)
            or len(output.name) > 64
        ):
            raise ValueError(
                "plugin output name must be lowercase kebab-case, at most 64 characters"
            )
        if output.is_relative_to(bridge.root):
            raise ProjectError("plugin output must be outside the linked workspace")
        if output.exists() or output.is_symlink():
            raise ProjectError(
                "plugin output already exists; inspect it and choose a new directory"
            )
        content = self.contents(bridge, name=output.name)
        connection = json.loads(content["connection.json"])
        stat = parent.stat()
        result = {
            "schema_version": 1,
            "client": "codex",
            "output": str(output),
            "parent_identity": [stat.st_dev, stat.st_ino],
            "connection": connection,
            "config_digests": {
                str(p): _sha(Path(p).read_bytes()) for _, p in self.config.config_files
            },
            "diagnostics": {
                "mcp_available": importlib.util.find_spec("mcp") is not None,
                "client_version": "not_probed",
                "client_installation": "not_attempted",
                "actual_session_validation": "not_run",
            },
            "files": {
                name: {"sha256": _sha(text.encode()), "content": text}
                for name, text in sorted(content.items())
            },
            "public_write": False,
            "writes_files": False,
        }
        result["plan_digest"] = canonical_digest(result)
        return result

    def contents(self, bridge: ScopedBridge, *, name: str) -> dict[str, str]:
        """Render the current trusted bundle without creating an output directory."""
        scope = bridge.scope_digest()
        entry = server_entry(self.config, bridge.root)
        entry["args"] += ["--expected-scope", scope]
        content = {}
        templates = files("reposteward").joinpath("data", "plugin-skills")
        for skill_name in SKILLS:
            content[f"skills/{skill_name}/SKILL.md"] = templates.joinpath(
                skill_name, "SKILL.md"
            ).read_text(encoding="utf-8")
        runtime = {
            "version": __version__,
            "code_digest": _runtime_digest(),
            "command": entry["command"],
        }
        connection = {
            "schema_version": 1,
            "workspace": str(bridge.root),
            "repository": bridge.bound["project"]["repository"],
            "project_id": bridge.bound["project"]["id"],
            "binding_id": bridge.bound["binding"]["id"],
            "scope_digest": scope,
            "cli_prefix": [entry["command"], *entry["args"][:4]],
            "runtime": runtime,
            "privacy": "Local machine paths. Do not commit or share this generated bundle.",
        }
        content["connection.json"] = _json(connection)
        content[".mcp.json"] = _json({"mcpServers": {name: entry}})
        content["README.md"] = (
            "# Local RepoSteward plugin\n\n"
            "This bundle belongs to the workspace in connection.json. Review .mcp.json "
            "and the four skills before installing with your client's supported local "
            "plugin workflow. Exporting does not install a plugin or validate a model session.\n\n"
            "Keep this directory private: it contains local paths, not credentials. "
            "Retain the referenced Python installation. After upgrading, changing the "
            "workspace binding, or editing configuration, review and export a new bundle "
            "to a new directory, then reinstall and start a new client thread. "
            "Remove the plugin through the client before removing its directory. "
            "Existing client instructions are never overwritten by export.\n"
        )
        bundle_digest = canonical_digest(content)
        content[MANIFEST] = _json(
            {
                "name": name,
                "version": f"{__version__}+bundle.{bundle_digest[:12]}",
                "description": "Workspace-scoped project understanding, task handoff and verification assistance.",
                "author": {"name": "RepoSteward contributors"},
                "repository": "https://github.com/tiammomo/RepoSteward",
                "license": "MIT",
                "skills": "./skills/",
                "mcpServers": "./.mcp.json",
                "interface": {
                    "displayName": "RepoSteward",
                    "shortDescription": "Project context and verified task continuity",
                    "longDescription": "Read project evidence, resume work and verify changes in one explicitly linked workspace.",
                    "developerName": "RepoSteward contributors",
                    "category": "Developer Tools",
                    "capabilities": ["Read", "Write"],
                    "defaultPrompt": [
                        "Help me understand this project",
                        "Continue the current task",
                        "Verify my changes",
                    ],
                },
            }
        )
        return content

    def export(self, workspace: Path, *, output: Path, plan_digest: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{64}", plan_digest):
            raise ValueError("plan digest must be a SHA-256 hex digest")
        plan = self.plan(workspace, output=output)
        if plan["plan_digest"] != plan_digest:
            raise ProjectError("plugin plan changed; review a fresh plan before export")
        if not plan["diagnostics"]["mcp_available"]:
            raise ProjectError(
                "install reposteward[mcp] in the exporting Python environment first"
            )
        output = Path(plan["output"])
        parent_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent_stat = os.fstat(parent_fd)
            if [parent_stat.st_dev, parent_stat.st_ino] != plan["parent_identity"]:
                raise ProjectError("plugin output parent changed; review a new plan")
            # Exclusive creation never replaces an existing directory or link.
            # A failed export is retained for inspection; retry into a new name.
            os.mkdir(output.name, mode=0o700, dir_fd=parent_fd)
            root_fd = os.open(
                output.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                for name, item in plan["files"].items():
                    if name != MANIFEST:
                        self._write(root_fd, name, item["content"])
                self._write(
                    root_fd,
                    "export.json",
                    _json(
                        {
                            "schema_version": 2,
                            "plan_digest": plan_digest,
                            "config_digests": plan["config_digests"],
                            "scope_digest": plan["connection"]["scope_digest"],
                            "files": {
                                name: item["sha256"]
                                for name, item in plan["files"].items()
                            },
                            "client_installation": "not_attempted",
                        }
                    ),
                )
                # The client-visible manifest is the last file published.
                self._write(root_fd, MANIFEST, plan["files"][MANIFEST]["content"])
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return {
            "output": str(output),
            "plan_digest": plan_digest,
            "files": list(plan["files"]),
            "exported": True,
            "client_installation": "not_attempted",
            "actual_session_validation": "not_run",
            "public_write": False,
            "writes_files": True,
        }

    @staticmethod
    def _write(root_fd: int, name: str, content: str) -> None:
        descriptor = os.dup(root_fd)
        try:
            parts = name.split("/")
            for component in parts[:-1]:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=descriptor,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
