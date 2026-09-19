"""Compose existing read-only runtime and scoped plugin diagnostics."""

from __future__ import annotations

from pathlib import Path

from .config import AppConfig
from .plugin_diagnostics import PluginDiagnostics
from .runtime import local_diagnostics


def alignment_report(
    config: AppConfig | None,
    *,
    workspace: Path,
    bundle: Path,
    expected_state_dir: Path | None = None,
    marketplace: Path | None = None,
    codex_home: Path | None = None,
) -> tuple[dict, bool]:
    report, runtime_ok = local_diagnostics(
        config, expected_state_dir=expected_state_dir
    )
    report.update(
        schema_version=1,
        plugin=None,
        compatibility={
            "status": "not_checked",
            "client_health": "not_probed",
            "actual_session_validation": "not_run",
        },
    )
    configuration = report["configuration"]
    if (
        config is None
        or configuration["status"] != "loaded"
        or not configuration.get("state_dir_matches")
    ):
        return report, False
    plugin = PluginDiagnostics(config).doctor(
        workspace,
        bundle=bundle,
        marketplace=marketplace,
        codex_home=codex_home,
    )
    report["plugin"] = plugin
    errors = [item for item in plugin["checks"] if item["status"] != "pass"]
    report["next_actions"] = list(
        dict.fromkeys(
            [
                *report["next_actions"],
                *[item["remedy"] for item in errors if item["remedy"]],
            ]
        )
    )
    matched = runtime_ok and plugin["bundle_compatible"]
    report["compatibility"].update(
        status="static_match" if matched else "needs_attention",
        state_compatible=runtime_ok,
        bundle_compatible=plugin["bundle_compatible"],
        client_registration=plugin["client"]["registration"],
        client_enabled=plugin["client"]["user_enabled"],
    )
    report["scope"] = (
        "Static compatibility of this CLI, selected state and explicit workspace bundle. "
        "A matching bundle is not proof that a client loaded it or that its MCP connection is healthy."
    )
    return report, matched
