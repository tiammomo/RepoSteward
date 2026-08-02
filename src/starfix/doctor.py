from __future__ import annotations

import shutil
import subprocess
from typing import Any

from .config import AppConfig
from .github import GitHubClient, GitHubError, resolve_token
from .verifier import DockerVerifier


def run_doctor(config: AppConfig) -> tuple[dict[str, Any], bool]:
    report: dict[str, Any] = {"tools": {}, "github": {}, "runner": {}}
    ok = True
    for tool in ("git", config.agent.executable, "docker"):
        path = shutil.which(tool)
        report["tools"][tool] = path or "missing"
        ok = ok and path is not None
    if shutil.which(config.agent.executable):
        codex = subprocess.run(
            [config.agent.executable, "login", "status"],
            check=False,
            capture_output=True,
            text=True,
        )
        report["tools"]["codex_auth"] = (
            (codex.stdout + codex.stderr).strip()
            if codex.returncode == 0
            else "not authenticated"
        )
        ok = ok and codex.returncode == 0
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
    token = resolve_token(config.github)
    report["github"]["token"] = "present" if token else "missing"
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
