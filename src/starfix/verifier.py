from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from .config import AppConfig, RepositoryPolicy
from .models import AgentResult, CommandResult, VerificationResult

DANGEROUS_COMMAND = re.compile(
    r"(?:\brm\s+-|\bcurl\b|\bwget\b|\bssh\b|\bgit\s+push\b|\bdocker\b|"
    r"\bprintenv\b|/proc/|/run/secrets|`|\$\()",
    re.IGNORECASE,
)


class VerificationError(RuntimeError):
    """Verification could not be run safely."""


def _output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class DockerVerifier:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def image_available(self) -> bool:
        result = subprocess.run(
            ["docker", "image", "inspect", self.config.runner.image],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    def verify(
        self,
        worktree: Path,
        policy: RepositoryPolicy,
        agent_result: AgentResult,
    ) -> VerificationResult:
        if not self.image_available():
            raise VerificationError(
                f"runner image {self.config.runner.image!r} is missing; "
                "run 'starfix image build'"
            )
        commands = agent_result.verification_commands
        if self.config.safety.require_verification and not commands:
            return VerificationResult(
                False, (), "agent supplied no verification commands"
            )
        for command in commands:
            self._validate_command(command, policy)
        missing_markers = [
            marker
            for marker in policy.required_verification_markers
            if not any(marker in command for command in commands)
        ]
        if missing_markers:
            return VerificationResult(
                False,
                (),
                "missing required verification: " + ", ".join(missing_markers),
            )

        results: list[CommandResult] = []
        if policy.bootstrap_commands:
            bootstrap = " && ".join(policy.bootstrap_commands)
            result = self._run_container(worktree, bootstrap, network=True)
            results.append(result)
            if result.exit_code:
                return VerificationResult(
                    False, tuple(results), "dependency bootstrap failed"
                )
        for command in commands:
            result = self._run_container(worktree, command, network=False)
            results.append(result)
            if result.exit_code:
                return VerificationResult(
                    False, tuple(results), f"verification failed: {command}"
                )
        return VerificationResult(True, tuple(results))

    @staticmethod
    def _validate_command(command: str, policy: RepositoryPolicy) -> None:
        if not command.strip() or "\n" in command or "\r" in command:
            raise VerificationError(
                "verification commands must be single non-empty lines"
            )
        if not any(
            command.startswith(prefix) for prefix in policy.verification_prefixes
        ):
            raise VerificationError(
                f"verification command is not allowlisted: {command}"
            )
        if DANGEROUS_COMMAND.search(command):
            raise VerificationError(
                f"verification command contains a blocked operation: {command}"
            )

    def _run_container(
        self, worktree: Path, command: str, *, network: bool
    ) -> CommandResult:
        runner = self.config.runner
        shell_command = f'mkdir -p "$HOME" && {command}'
        docker_command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "bridge" if network else "none",
            "--cpus",
            str(runner.cpus),
            "--memory",
            runner.memory,
            "--pids-limit",
            str(runner.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=2g",
            "-e",
            "HOME=/tmp/starfix-home",
            "-e",
            "CI=1",
            "-v",
            f"{worktree.resolve()}:/workspace:rw",
            "-w",
            "/workspace",
            runner.image,
            "bash",
            "-lc",
            shell_command,
        ]
        start = time.monotonic()
        try:
            result = subprocess.run(
                docker_command,
                check=False,
                capture_output=True,
                text=True,
                timeout=runner.timeout_seconds,
                env={"PATH": os.environ.get("PATH", "")},
            )
            output = (result.stdout + result.stderr)[-runner.max_output_chars :]
            exit_code = result.returncode
        except subprocess.TimeoutExpired as exc:
            output = (_output_text(exc.stdout) + _output_text(exc.stderr))[
                -runner.max_output_chars :
            ]
            exit_code = 124
        return CommandResult(
            command=command,
            exit_code=exit_code,
            output=output,
            duration_seconds=round(time.monotonic() - start, 3),
        )
