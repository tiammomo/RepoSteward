from __future__ import annotations

import hashlib
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
MAX_VERIFICATION_COMMANDS = 12


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
        *,
        run_dir: Path | None = None,
    ) -> VerificationResult:
        if not self.image_available():
            raise VerificationError(
                f"runner image {self.config.runner.image!r} is missing; "
                "run 'reposteward image build'"
            )
        commands = agent_result.verification_commands
        if len(commands) > MAX_VERIFICATION_COMMANDS:
            raise VerificationError(
                f"at most {MAX_VERIFICATION_COMMANDS} verification commands are allowed"
            )
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
        verification_dir = run_dir / "verification" if run_dir is not None else None
        if policy.bootstrap_commands:
            bootstrap = " && ".join(policy.bootstrap_commands)
            result = self._run_container(
                worktree,
                bootstrap,
                network=True,
                log_path=(
                    verification_dir / "00-bootstrap.log"
                    if verification_dir is not None
                    else None
                ),
            )
            results.append(result)
            if result.exit_code:
                return VerificationResult(
                    False, tuple(results), "dependency bootstrap failed"
                )
        for index, command in enumerate(commands, start=1):
            result = self._run_container(
                worktree,
                command,
                network=False,
                log_path=(
                    verification_dir / f"{index:02d}-command.log"
                    if verification_dir is not None
                    else None
                ),
            )
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
        self,
        worktree: Path,
        command: str,
        *,
        network: bool,
        log_path: Path | None = None,
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
            "/tmp:rw,exec,nosuid,nodev,size=2g",
            "-e",
            "HOME=/tmp/reposteward-home",
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
            full_output = result.stdout + result.stderr
            exit_code = result.returncode
        except subprocess.TimeoutExpired as exc:
            full_output = _output_text(exc.stdout) + _output_text(exc.stderr)
            exit_code = 124
        encoded_output = full_output.encode("utf-8", errors="replace")
        log_truncated = len(full_output) > runner.max_log_chars
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            stored_output = full_output[-runner.max_log_chars :]
            if log_truncated:
                omitted = len(full_output) - len(stored_output)
                stored_output = (
                    f"[reposteward omitted {omitted} earlier characters; "
                    "the retained log is the configured tail]\n"
                    f"{stored_output}"
                )
            log_path.write_text(stored_output, encoding="utf-8")
        output_limit = (
            runner.passed_output_chars if exit_code == 0 else runner.max_output_chars
        )
        output = full_output[-output_limit:]
        return CommandResult(
            command=command,
            exit_code=exit_code,
            output=output,
            duration_seconds=round(time.monotonic() - start, 3),
            log_path=str(log_path or ""),
            output_chars=len(full_output),
            output_bytes=len(encoded_output),
            output_sha256=hashlib.sha256(encoded_output).hexdigest(),
            output_truncated=len(full_output) > output_limit,
            log_truncated=log_truncated,
        )
