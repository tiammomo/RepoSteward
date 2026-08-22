from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import chain
from pathlib import Path

from .config import AppConfig, RepositoryPolicy
from .models import AgentResult, CommandResult, VerificationResult

DANGEROUS_COMMAND = re.compile(
    r"(?:\brm\s+-|\bcurl\b|\bwget\b|\bssh\b|\bgit\s+push\b|\bdocker\b|"
    r"\bprintenv\b|/proc/|/run/secrets|`|\$\()",
    re.IGNORECASE,
)
MAX_VERIFICATION_COMMANDS = 12
UNTRACKED_SANDBOX_EXCLUDED_NAMES = frozenset(
    {
        ".git",
        ".gradle",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "credentials",
        "node_modules",
        "secrets",
        "target",
        "venv",
    }
)
SENSITIVE_SANDBOX_NAMES = frozenset({"credentials", "secrets"})


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

        verification_dir = run_dir / "verification" if run_dir is not None else None
        results: list[CommandResult] = []
        with self._verification_sandbox(worktree, verification_dir) as sandbox:
            sandbox_worktree, environment_dir, git_dir = sandbox
            if policy.bootstrap_commands:
                bootstrap = " && ".join(policy.bootstrap_commands)
                result = self._run_container(
                    sandbox_worktree,
                    bootstrap,
                    network=True,
                    log_path=(
                        verification_dir / "00-bootstrap.log"
                        if verification_dir is not None
                        else None
                    ),
                    environment_dir=environment_dir,
                    git_dir=git_dir,
                )
                results.append(result)
                if result.exit_code:
                    return VerificationResult(
                        False, tuple(results), "dependency bootstrap failed"
                    )
            for index, command in enumerate(commands, start=1):
                result = self._run_container(
                    sandbox_worktree,
                    command,
                    network=False,
                    log_path=(
                        verification_dir / f"{index:02d}-command.log"
                        if verification_dir is not None
                        else None
                    ),
                    environment_dir=environment_dir,
                    git_dir=git_dir,
                )
                results.append(result)
                if result.exit_code:
                    return VerificationResult(
                        False, tuple(results), f"verification failed: {command}"
                    )
            return VerificationResult(True, tuple(results))

    @staticmethod
    def _sandbox_ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in UNTRACKED_SANDBOX_EXCLUDED_NAMES
            or name == ".env"
            or name.startswith(".env.")
        }

    @staticmethod
    def _git_file_list(worktree: Path, *arguments: str) -> tuple[Path, ...]:
        listing = subprocess.run(
            ["git", "ls-files", *arguments, "-z"],
            cwd=worktree,
            check=True,
            capture_output=True,
            env={"PATH": os.environ.get("PATH", "")},
        ).stdout
        return tuple(Path(os.fsdecode(raw)) for raw in listing.split(b"\0") if raw)

    @staticmethod
    def _sensitive_path(relative: Path) -> bool:
        return any(
            part.casefold() in SENSITIVE_SANDBOX_NAMES
            or part.casefold() == ".env"
            or part.casefold().startswith(".env.")
            for part in relative.parts
        )

    @classmethod
    def _copy_workspace(cls, worktree: Path, target: Path) -> int:
        """Copy tracked and non-ignored untracked files without scanning caches."""
        tracked = cls._git_file_list(worktree, "--cached")
        untracked = cls._git_file_list(worktree, "--others", "--exclude-standard")
        target.mkdir(parents=True)
        copied = 0
        entries = chain(
            ((value, True) for value in tracked),
            ((value, False) for value in untracked),
        )
        for relative, is_tracked in entries:
            if relative.is_absolute() or ".." in relative.parts:
                raise VerificationError("Git returned an unsafe workspace path")
            if cls._sensitive_path(relative):
                if is_tracked:
                    raise VerificationError(
                        f"tracked sensitive path cannot enter verification: {relative}"
                    )
                continue
            if not is_tracked and any(
                part in UNTRACKED_SANDBOX_EXCLUDED_NAMES for part in relative.parts
            ):
                continue
            source = worktree / relative
            destination = target / relative
            if source.is_symlink():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(os.readlink(source))
            elif source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination, follow_symlinks=False)
            elif source.is_dir():
                shutil.copytree(
                    source,
                    destination,
                    symlinks=True,
                    ignore=cls._sandbox_ignore,
                    ignore_dangling_symlinks=True,
                )
            else:
                continue
            copied += 1
        return copied

    @staticmethod
    def _git_paths(worktree: Path) -> tuple[Path, str]:
        def resolve(value: str) -> Path:
            path = Path(value)
            return (
                (worktree / path).resolve()
                if not path.is_absolute()
                else path.resolve()
            )

        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "")},
        ).stdout.strip()
        git_dir = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "")},
        ).stdout.strip()
        common_path = resolve(common)
        git_path = resolve(git_dir)
        relative = git_path.relative_to(common_path)
        container_git_dir = "/reposteward-git"
        if relative.parts:
            container_git_dir += "/" + relative.as_posix()
        return common_path, container_git_dir

    @contextmanager
    def _verification_sandbox(
        self, worktree: Path, verification_dir: Path | None
    ) -> Iterator[tuple[Path, Path, Path]]:
        temporary = None
        if verification_dir is None:
            temporary = tempfile.TemporaryDirectory(prefix="reposteward-verify-")
            parent = Path(temporary.name)
        else:
            verification_dir.mkdir(parents=True, exist_ok=True)
            parent = verification_dir
        sandbox_root = parent / f"sandbox-{uuid.uuid4().hex}"
        sandbox_worktree = sandbox_root / "workspace"
        environment_dir = sandbox_root / "environment"
        manifest_path = verification_dir / "sandbox.json" if verification_dir else None
        common_git_dir: Path | None = None
        try:
            common_git_dir, container_git_dir = self._git_paths(worktree)
            copied_files = self._copy_workspace(worktree, sandbox_worktree)
            environment_dir.mkdir(parents=True)
            (sandbox_worktree / ".git").write_text(
                f"gitdir: {container_git_dir}\n", encoding="utf-8"
            )
            if manifest_path is not None:
                manifest_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "sandbox": "ephemeral_copy",
                            "excluded_untracked_names": sorted(
                                UNTRACKED_SANDBOX_EXCLUDED_NAMES
                            ),
                            "host_workspace_writable": False,
                            "shared_dependency_environment": True,
                            "copied_entries": copied_files,
                            "cleaned": False,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            yield sandbox_worktree, environment_dir, common_git_dir
        finally:
            if sandbox_root.exists():
                shutil.rmtree(sandbox_root)
            if manifest_path is not None and manifest_path.exists():
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                payload["cleaned"] = True
                manifest_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            if temporary is not None:
                temporary.cleanup()

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
        environment_dir: Path | None = None,
        git_dir: Path | None = None,
    ) -> CommandResult:
        runner = self.config.runner
        environment_dir = environment_dir or worktree
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
            "HOME=/reposteward-env/home",
            "-e",
            "CI=1",
            "-e",
            "XDG_CACHE_HOME=/reposteward-env/cache",
            "-e",
            "UV_CACHE_DIR=/reposteward-env/uv-cache",
            "-e",
            "UV_TOOL_DIR=/reposteward-env/uv-tools",
            "-e",
            "UV_PYTHON_INSTALL_DIR=/reposteward-env/uv-python",
            "-e",
            "PIP_CACHE_DIR=/reposteward-env/pip-cache",
            "-e",
            "npm_config_cache=/reposteward-env/npm-cache",
            "-e",
            "PNPM_HOME=/reposteward-env/pnpm-home",
            "-e",
            "GRADLE_USER_HOME=/reposteward-env/gradle",
            "-v",
            f"{worktree.resolve()}:/workspace:rw",
            "-v",
            f"{environment_dir.resolve()}:/reposteward-env:rw",
            "-w",
            "/workspace",
        ]
        if git_dir is not None:
            docker_command.extend(["-v", f"{git_dir.resolve()}:/reposteward-git:ro"])
        docker_command.extend([runner.image, "bash", "-lc", shell_command])
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
