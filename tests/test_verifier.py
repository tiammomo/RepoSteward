from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reposteward.config import RepositoryPolicy, RunnerConfig
from reposteward.models import AgentResult, CommandResult
from reposteward.verifier import DockerVerifier, VerificationError


def _repository(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)


class VerificationSandboxTests(unittest.TestCase):
    def _verifier(self) -> DockerVerifier:
        verifier = DockerVerifier(
            SimpleNamespace(
                runner=RunnerConfig(),
                safety=SimpleNamespace(require_verification=True),
            )
        )
        verifier.image_available = lambda: True  # type: ignore[method-assign]
        return verifier

    def test_bootstrap_and_offline_commands_share_only_the_ephemeral_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            _repository(worktree)
            (worktree / ".venv").mkdir()
            (worktree / ".venv" / "host-marker").write_text("host")
            (worktree / "node_modules").mkdir()
            (worktree / ".env").write_text("SECRET=value\n")
            run_dir = root / "run"
            observed: list[tuple[Path, Path, bool]] = []

            def run_container(
                sandbox: Path,
                command: str,
                *,
                network: bool,
                log_path: Path | None = None,
                environment_dir: Path | None = None,
                git_dir: Path | None = None,
            ) -> CommandResult:
                assert environment_dir is not None
                assert git_dir is not None
                observed.append((sandbox, environment_dir, network))
                self.assertNotEqual(sandbox, worktree)
                self.assertFalse((sandbox / "node_modules").exists())
                self.assertFalse((sandbox / ".env").exists())
                if network:
                    (sandbox / ".venv").mkdir()
                    (environment_dir / "cache-marker").write_text("cached")
                else:
                    self.assertTrue((sandbox / ".venv").is_dir())
                    self.assertTrue((environment_dir / "cache-marker").is_file())
                return CommandResult(command, 0, "", 0.01)

            verifier = self._verifier()
            verifier._run_container = run_container  # type: ignore[method-assign]
            policy = RepositoryPolicy(
                name="owner/repo",
                bootstrap_commands=("uv sync",),
                verification_prefixes=("uv run ",),
            )
            result = verifier.verify(
                worktree,
                policy,
                AgentResult("summary", "fix(repo): test", "notes", ("uv run test",)),
                run_dir=run_dir,
            )
            manifest = json.loads(
                (run_dir / "verification" / "sandbox.json").read_text()
            )
            host_marker = (worktree / ".venv" / "host-marker").read_text()

        self.assertTrue(result.passed)
        self.assertEqual([value[2] for value in observed], [True, False])
        self.assertEqual(observed[0][0], observed[1][0])
        self.assertEqual(observed[0][1], observed[1][1])
        self.assertFalse(observed[0][0].exists())
        self.assertEqual(host_marker, "host")
        self.assertTrue(manifest["cleaned"])
        self.assertFalse(manifest["host_workspace_writable"])

    def test_failed_bootstrap_still_removes_the_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            _repository(worktree)
            run_dir = root / "run"
            sandbox_paths: list[Path] = []

            def fail(sandbox: Path, command: str, **_kwargs: object) -> CommandResult:
                sandbox_paths.append(sandbox)
                return CommandResult(command, 1, "failed", 0.01)

            verifier = self._verifier()
            verifier._run_container = fail  # type: ignore[method-assign]
            policy = RepositoryPolicy(
                name="owner/repo",
                bootstrap_commands=("uv sync",),
                verification_prefixes=("uv run ",),
            )
            result = verifier.verify(
                worktree,
                policy,
                AgentResult("summary", "fix(repo): test", "notes", ("uv run test",)),
                run_dir=run_dir,
            )

            manifest = json.loads(
                (run_dir / "verification" / "sandbox.json").read_text()
            )
            sandbox_exists = sandbox_paths[0].exists()

        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "dependency bootstrap failed")
        self.assertFalse(sandbox_exists)
        self.assertTrue(manifest["cleaned"])

    def test_tracked_dependency_named_path_is_copied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            _repository(worktree)
            tracked_target = worktree / "target" / "source.txt"
            tracked_target.parent.mkdir()
            tracked_target.write_text("tracked source\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "target/source.txt"], cwd=worktree, check=True
            )
            destination = root / "snapshot"

            copied = DockerVerifier._copy_workspace(worktree, destination)

            self.assertGreaterEqual(copied, 2)
            self.assertEqual(
                (destination / "target" / "source.txt").read_text(encoding="utf-8"),
                "tracked source\n",
            )

    def test_tracked_sensitive_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            _repository(worktree)
            (worktree / ".ENV.test").write_text("SECRET=value\n", encoding="utf-8")
            subprocess.run(["git", "add", ".ENV.test"], cwd=worktree, check=True)

            with self.assertRaisesRegex(
                VerificationError, "tracked sensitive path cannot enter verification"
            ):
                DockerVerifier._copy_workspace(worktree, root / "snapshot")

    def test_tracked_empty_environment_templates_are_copied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            _repository(worktree)
            names = (".env.example", ".env.sample", ".env.template")
            for name in names:
                (worktree / name).write_text(
                    "# Safe defaults for local development\nAPI_KEY=\nLOG_LEVEL=info\n",
                    encoding="utf-8",
                )
            subprocess.run(["git", "add", *names], cwd=worktree, check=True)

            DockerVerifier._copy_workspace(worktree, root / "snapshot")

            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(
                        (root / "snapshot" / name).read_text(encoding="utf-8"),
                        (worktree / name).read_text(encoding="utf-8"),
                    )

    def test_untracked_environment_template_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            _repository(worktree)
            (worktree / ".env.example").write_text("API_KEY=\n", encoding="utf-8")

            DockerVerifier._copy_workspace(worktree, root / "snapshot")

            self.assertFalse((root / "snapshot" / ".env.example").exists())

    def test_tracked_environment_template_with_secret_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            _repository(worktree)
            (worktree / ".env.example").write_text(
                "API_KEY=real-value\n", encoding="utf-8"
            )
            subprocess.run(["git", "add", ".env.example"], cwd=worktree, check=True)

            with self.assertRaisesRegex(VerificationError, "non-empty sensitive field"):
                DockerVerifier._copy_workspace(worktree, root / "snapshot")

    def test_tracked_environment_template_symlink_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            _repository(worktree)
            (worktree / "template-source").write_text("API_KEY=\n", encoding="utf-8")
            (worktree / ".env.example").symlink_to("template-source")
            subprocess.run(["git", "add", ".env.example"], cwd=worktree, check=True)

            with self.assertRaisesRegex(VerificationError, "must be a regular file"):
                DockerVerifier._copy_workspace(worktree, root / "snapshot")

    def test_tracked_environment_template_in_sensitive_directory_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            _repository(worktree)
            template = worktree / "secrets" / ".env.example"
            template.parent.mkdir()
            template.write_text("API_KEY=\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "secrets/.env.example"], cwd=worktree, check=True
            )

            with self.assertRaisesRegex(
                VerificationError, "tracked sensitive path cannot enter verification"
            ):
                DockerVerifier._copy_workspace(worktree, root / "snapshot")

    def test_environment_template_must_be_bounded_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content, message in (
                ("oversized", b"#" * (64 * 1024 + 1), "exceeds 65536 bytes"),
                ("non-utf8", b"API_KEY=\xff\n", "must be UTF-8"),
            ):
                with self.subTest(name=name):
                    worktree = root / name
                    worktree.mkdir()
                    _repository(worktree)
                    (worktree / ".env.example").write_bytes(content)
                    subprocess.run(
                        ["git", "add", ".env.example"], cwd=worktree, check=True
                    )

                    with self.assertRaisesRegex(VerificationError, message):
                        DockerVerifier._copy_workspace(
                            worktree, root / f"{name}-snapshot"
                        )

    def test_container_receives_shared_cache_and_read_only_git_mounts(self) -> None:
        verifier = self._verifier()
        completed = subprocess.CompletedProcess(
            args=["docker"], returncode=0, stdout="", stderr=""
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            environment = root / "environment"
            git_dir = root / "git"
            for path in (workspace, environment, git_dir):
                path.mkdir()
            with patch(
                "reposteward.verifier.subprocess.run", return_value=completed
            ) as run:
                verifier._run_container(
                    workspace,
                    "uv run test",
                    network=False,
                    environment_dir=environment,
                    git_dir=git_dir,
                )

        command = run.call_args.args[0]
        self.assertIn(f"{workspace.resolve()}:/workspace:rw", command)
        self.assertIn(f"{environment.resolve()}:/reposteward-env:rw", command)
        self.assertIn(f"{git_dir.resolve()}:/reposteward-git:ro", command)
        self.assertIn("none", command)


if __name__ == "__main__":
    unittest.main()
