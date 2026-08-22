from __future__ import annotations

import importlib.util
import shutil
import subprocess
from typing import Any

from .config import AppConfig
from .github import GitHubClient, GitHubError, resolve_authentication
from .harness import create_harness, harness_capabilities
from .verifier import DockerVerifier


def run_doctor(config: AppConfig) -> tuple[dict[str, Any], bool]:
    report: dict[str, Any] = {
        "harness": {
            "name": config.agent.harness,
            "capabilities": harness_capabilities(
                create_harness(config.agent)
            ).to_dict(),
        },
        "tools": {},
        "github": {},
        "runner": {},
    }
    ok = True
    tools = ["git", "docker"]
    if config.agent.harness == "codex-cli":
        tools.append(config.agent.executable)
    for tool in tools:
        path = shutil.which(tool)
        report["tools"][tool] = path or "missing"
        ok = ok and path is not None
    if config.agent.harness == "codex-cli" and shutil.which(config.agent.executable):
        harness_auth = subprocess.run(
            [config.agent.executable, "login", "status"],
            check=False,
            capture_output=True,
            text=True,
        )
        report["harness"]["authentication"] = (
            (harness_auth.stdout + harness_auth.stderr).strip()
            if harness_auth.returncode == 0
            else "not authenticated"
        )
        ok = ok and harness_auth.returncode == 0
    if config.agent.harness == "codex-sdk":
        sdk_available = importlib.util.find_spec("openai_codex") is not None
        report["harness"]["sdk_available"] = sdk_available
        report["harness"]["install_hint"] = "uv sync --extra codex-sdk"
        ok = ok and sdk_available
    if shutil.which("docker"):
        docker = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
        )
        report["runner"]["docker_server"] = (
            docker.stdout.strip() if docker.returncode == 0 else "unavailable"
        )
        ok = ok and docker.returncode == 0
        image = DockerVerifier(config).image_available()
        report["runner"]["image"] = config.runner.image
        report["runner"]["image_available"] = image
        ok = ok and image
    token, source = resolve_authentication(config.github)
    report["github"]["authentication"] = source
    if token:
        try:
            login = GitHubClient(config.github, token).authenticated_login()
            report["github"]["authenticated_login"] = login
            report["github"]["expected_login"] = config.github.login
            ok = ok and login.casefold() == config.github.login.casefold()
        except GitHubError as exc:
            report["github"]["error"] = str(exc)
            ok = False
    else:
        ok = False
    return report, ok
