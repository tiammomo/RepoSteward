"""Offline bundle diagnostics and installation advice, never client execution."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path

from reposteward import __version__
from reposteward.core.config import AppConfig
from reposteward.integrations.mcp import ScopedBridge
from reposteward.plugins.bundle import MANIFEST, SKILLS, PluginBundle
from reposteward.projects.registry import ProjectError, canonical_digest
from reposteward.workflows.policy import PolicyError

MAX_FILE_BYTES = 512 * 1024
FILES = {MANIFEST, ".mcp.json", "connection.json", "README.md"} | {
    f"skills/{name}/SKILL.md" for name in SKILLS
}
NAME = r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
MARKET = r"[A-Za-z0-9_-]+"
DIGEST = r"[a-f0-9]{64}"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextmanager
def _directory(path: Path):
    """Anchor every component, rejecting symlinks including ancestor directories."""
    path = path.expanduser().absolute()
    if ".." in path.parts:
        raise ValueError("non-canonical path")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _read_at(fd: int, name: str) -> bytes:
    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(child, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise ValueError("unsupported file")
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
        if len(data) > MAX_FILE_BYTES or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("file changed while reading")
        return data


def _read_file(path: Path) -> bytes:
    with _directory(path.parent) as fd:
        return _read_at(fd, path.name)


def _tree(root: Path) -> dict[str, bytes]:
    expected = FILES | {"export.json"}
    result = {}

    def walk(fd: int, prefix: str) -> None:
        before = os.fstat(fd)
        children = {
            name[len(prefix) :].split("/")[0]
            for name in expected
            if name.startswith(prefix)
        }
        observed = set()
        with os.scandir(fd) as entries:
            for entry in entries:
                observed.add(entry.name)
                if entry.name not in children:
                    raise ValueError("unexpected bundle entry")
        if observed != children:
            raise ValueError("incomplete bundle")
        for name in sorted(children):
            relative = prefix + name
            if relative in expected:
                result[relative] = _read_at(fd, name)
            else:
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                try:
                    walk(child, relative + "/")
                finally:
                    os.close(child)
        after = os.fstat(fd)
        if (before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("directory changed while reading")

    with _directory(root) as fd:
        identity = os.fstat(fd)
        walk(fd, "")
        with _directory(root) as current:
            fresh = os.fstat(current)
            if (identity.st_dev, identity.st_ino) != (fresh.st_dev, fresh.st_ino):
                raise ValueError("bundle path changed while reading")
    return result


def _object(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise TypeError("expected object")
    return value


def _matches(pattern: str, value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


class PluginDiagnostics:
    def __init__(self, config: AppConfig):
        self.config = config

    def doctor(
        self,
        workspace: Path,
        *,
        bundle: Path,
        marketplace: Path | None = None,
        codex_home: Path | None = None,
    ) -> dict:
        bundle = bundle.expanduser().absolute()
        home = (
            (
                codex_home
                or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            )
            .expanduser()
            .absolute()
        )
        market = (
            (marketplace or Path.home() / ".agents/plugins/marketplace.json")
            .expanduser()
            .absolute()
        )
        report = {
            "schema_version": 1,
            "bundle": str(bundle),
            "workspace": str(workspace.expanduser().absolute()),
            "checks": [],
            "bundle_compatible": False,
            "current_runtime": {"version": __version__, "command": sys.executable},
            "client": {
                "codex_home": str(home),
                "marketplace_path": str(market),
                "registration": "not_checked",
                "user_enabled": None,
                "cache": "not_checked",
                "effective_installation": "not_probed",
                "client_version": "not_probed",
                "mcp_health": "not_probed",
            },
            "actual_session_validation": "not_run",
            "public_write": False,
            "writes_files": False,
        }

        def check(code: str, ok: bool, remedy: str, *, warning: bool = False):
            report["checks"].append(
                {
                    "code": code,
                    "status": "pass" if ok else ("warning" if warning else "error"),
                    "remedy": "" if ok else remedy,
                }
            )

        def finish():
            report["observation_digest"] = canonical_digest(report)
            return report

        try:
            data = _tree(bundle)
            receipt = _object(data["export.json"])
            manifest = _object(data[MANIFEST])
            connection = _object(data["connection.json"])
            name = manifest.get("name")
            version = manifest.get("version")
            schema = receipt.get("schema_version", 1)
            if (
                not _matches(NAME, name)
                or len(name) > 64
                or name != bundle.name
                or type(schema) is not int
                or schema not in (1, 2)
                or not _matches(DIGEST, receipt.get("plan_digest"))
                or not _matches(DIGEST, receipt.get("scope_digest"))
                or receipt.get("client_installation") != "not_attempted"
                or not isinstance(receipt.get("files"), dict)
                or set(receipt["files"]) != FILES
            ):
                raise ValueError("invalid receipt or manifest")
            if any(receipt["files"][key] != _sha(data[key]) for key in FILES):
                raise ValueError("file digest mismatch")
            content = {
                key: data[key].decode("utf-8") for key in FILES if key != MANIFEST
            }
            runtime = connection.get("runtime")
            if not isinstance(runtime, dict) or not _matches(
                r"[0-9]+\.[0-9]+\.[0-9]+", runtime.get("version")
            ):
                raise ValueError("invalid runtime metadata")
            if (
                version
                != f"{runtime['version']}+bundle.{canonical_digest(content)[:12]}"
            ):
                raise ValueError("version content digest mismatch")
            if receipt["scope_digest"] != connection.get("scope_digest"):
                raise ValueError("inconsistent scope receipt")
        except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
            check(
                "bundle_integrity",
                False,
                "Inspect the bundle and export again into a new directory; do not execute its MCP command.",
            )
            return finish()
        report["plugin"] = {"name": name, "version": version, "export_schema": schema}
        report["files"] = {key: _sha(value) for key, value in sorted(data.items())}
        check("bundle_integrity", True, "")
        try:
            bridge = ScopedBridge(self.config, workspace)
            expected = PluginBundle(self.config).contents(bridge, name=name)
            expected_connection = json.loads(expected["connection.json"])
        except (ProjectError, PolicyError, OSError, ValueError):
            check(
                "workspace_binding",
                False,
                "Inspect/link the intended workspace and configure its repository policy, then export again.",
            )
            return finish()
        scope_keys = (
            "workspace",
            "repository",
            "project_id",
            "binding_id",
            "scope_digest",
        )
        check(
            "workspace_binding",
            all(connection.get(k) == expected_connection[k] for k in scope_keys),
            "Confirm the account and workspace; relinking or switching account requires a new export.",
        )
        check(
            "runtime_matches",
            runtime == expected_connection["runtime"],
            "Use the original exporting CLI to inspect this bundle, or re-export with the intended installed CLI; a difference does not prove the old runtime is broken.",
        )
        check(
            "mcp_available",
            importlib.util.find_spec("mcp") is not None,
            "Install the mcp extra in the intended exporting Python environment.",
        )
        check(
            "trusted_contents",
            all(data[key].decode("utf-8") == value for key, value in expected.items()),
            "Review and re-export from the intended CLI/configuration; self-consistent hashes alone do not authenticate a bundle.",
        )
        try:
            config_digests = {
                str(path): _sha(_read_file(Path(path)))
                for _, path in self.config.config_files
            }
            report["config_digests"] = config_digests
            check(
                "configuration_snapshot",
                schema == 2 and receipt.get("config_digests") == config_digests,
                "Legacy receipts do not record configuration bytes; re-export to record them."
                if schema == 1
                else "Configuration files changed; review a new export.",
                warning=schema == 1,
            )
        except (OSError, ValueError):
            check(
                "configuration_snapshot",
                False,
                "Configuration could not be read safely; inspect its path and export again.",
            )
        report["bundle_compatible"] = not any(
            item["status"] == "error" for item in report["checks"]
        )
        if not report["bundle_compatible"]:
            return finish()
        self._client(report, data, market, home, check)
        return finish()

    @staticmethod
    def _client(report: dict, data: dict, market: Path, home: Path, check) -> None:
        name, version = report["plugin"]["name"], report["plugin"]["version"]
        client = report["client"]
        try:
            raw = _read_file(market)
            catalog = _object(raw)
            entries = catalog.get("plugins")
            if (
                not _matches(MARKET, catalog.get("name"))
                or len(catalog["name"]) > 128
                or not isinstance(entries, list)
            ):
                raise ValueError("invalid marketplace")
            matches = [
                item
                for item in entries
                if isinstance(item, dict) and item.get("name") == name
            ]
            client["marketplace_name"] = catalog["name"]
            client["marketplace_digest"] = _sha(raw)
            if not matches:
                client["registration"] = "missing_entry"
            elif len(matches) != 1:
                raise ValueError("ambiguous marketplace")
            else:
                policy = matches[0].get("policy", {})
                if not isinstance(policy, dict):
                    raise ValueError("invalid installation policy")
                installation = policy.get("installation")
                if installation not in {
                    "AVAILABLE",
                    "INSTALLED_BY_DEFAULT",
                    "NOT_AVAILABLE",
                }:
                    raise ValueError("unknown installation policy")
                client["installation_policy"] = installation
                source = matches[0].get("source", {})
                path = source.get("path") if isinstance(source, dict) else None
                if (
                    not isinstance(path, str)
                    or not path.startswith("./")
                    or ".." in Path(path).parts
                    or source.get("source") != "local"
                ):
                    raise ValueError("unsupported source")
                # Personal/repository catalog paths resolve from the root above .agents/plugins.
                if market.parts[-3:] != (".agents", "plugins", "marketplace.json"):
                    raise ValueError("unsupported marketplace layout")
                target = market.parents[2] / path
                with (
                    _directory(target) as fd,
                    _directory(Path(report["bundle"])) as bundle_fd,
                ):
                    one, two = os.fstat(fd), os.fstat(bundle_fd)
                    matches_target = (one.st_dev, one.st_ino) == (
                        two.st_dev,
                        two.st_ino,
                    )
                client["registration"] = (
                    "matches" if matches_target else "different_source"
                )
        except FileNotFoundError:
            client["registration"] = "missing"
        except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
            client["registration"] = "unreadable_or_unsupported"
        check(
            "marketplace_registration",
            client["registration"] == "matches",
            "Register the reviewed bundle with the client-supported marketplace workflow, preserving other entries.",
            warning=True,
        )
        check(
            "marketplace_installation_policy",
            client.get("installation_policy") in {"AVAILABLE", "INSTALLED_BY_DEFAULT"},
            "The selected marketplace must allow installation; inspect its policy before preparing a client action.",
            warning=True,
        )
        if "marketplace_name" not in client:
            return
        plugin_id = f"{name}@{client['marketplace_name']}"
        client["plugin_id"] = plugin_id
        try:
            raw = _read_file(home / "config.toml")
            settings = tomllib.loads(raw.decode("utf-8"))
            enabled = settings.get("plugins", {}).get(plugin_id, {}).get("enabled")
            if enabled is not None and type(enabled) is not bool:
                raise ValueError("invalid enabled setting")
            client["user_enabled"] = enabled
            client["config_observation"] = "read"
            client["config_digest"] = _sha(raw)
        except FileNotFoundError:
            client["config_observation"] = "missing"
        except (OSError, ValueError, UnicodeError, TypeError, AttributeError):
            client["config_observation"] = "unreadable"
        check(
            "client_user_setting",
            client["user_enabled"] is True,
            "Check this plugin in Codex; absent user settings and repository/managed overrides do not establish the effective enabled state.",
            warning=True,
        )
        cache = home / "plugins/cache" / client["marketplace_name"] / name / version
        client["cache_path"] = str(cache)
        try:
            installed = _tree(cache)
            client["cache"] = "matches" if installed == data else "different_contents"
        except FileNotFoundError:
            client["cache"] = "not_found_at_version_path"
        except (OSError, ValueError):
            client["cache"] = "unreadable_or_unsupported"
        check(
            "client_cache",
            client["cache"] == "matches",
            "Inspect codex plugin list --json; reinstall from the reviewed source if needed. Other client cache layouts are not inferred.",
            warning=True,
        )

    def install_plan(self, workspace: Path, **kwargs) -> dict:
        report = self.doctor(workspace, **kwargs)
        client = report["client"]
        ready = (
            report["bundle_compatible"]
            and client["registration"] == "matches"
            and client.get("config_observation") != "unreadable"
            and client.get("installation_policy")
            in {"AVAILABLE", "INSTALLED_BY_DEFAULT"}
        )
        steps = []
        if ready:
            if client["user_enabled"] is False:
                ready = False
                steps.append(
                    {
                        "action": "review_disabled_plugin",
                        "note": "The user disabled this plugin; review that choice in Codex before preparing installation.",
                    }
                )
            else:
                if (
                    Path(client["marketplace_path"])
                    != Path.home() / ".agents/plugins/marketplace.json"
                ):
                    steps.append(
                        {
                            "action": "register_explicit_marketplace",
                            "argv": [
                                "codex",
                                "plugin",
                                "marketplace",
                                "add",
                                str(Path(client["marketplace_path"]).parents[2]),
                            ],
                        }
                    )
                steps += [
                    {
                        "action": "install",
                        "argv": ["codex", "plugin", "add", client["plugin_id"]],
                    },
                    {
                        "action": "inspect_client",
                        "argv": ["codex", "plugin", "list", "--json"],
                    },
                    {
                        "action": "validate_new_session",
                        "note": "Start a new conversation; confirm project identity and read its guide before resuming work.",
                    },
                ]
        result = {
            "schema_version": 1,
            "ready_for_client_install": ready,
            "observation": report,
            "environment": {"CODEX_HOME": client["codex_home"]},
            "steps": steps,
            "public_write": False,
            "writes_files": False,
            "client_installation": "not_attempted",
            "note": "Point-in-time advice, not an apply authorization. Recheck before client actions; no shell or bundle command was executed.",
        }
        result["plan_digest"] = canonical_digest(result)
        return result
