from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from reposteward.config import ConfigError, load_config
from reposteward.models import AgentResult, CommandResult
from reposteward.verifier import DockerVerifier


class VerificationHostsTests(unittest.TestCase):
    def config(self, root: Path, hosts: str, project_hosts: str = ""):
        user = root / "user.toml"
        project = root / "project.toml"
        user.write_text(
            'config_version = 1\n[github]\nlogin = "alice"\n'
            '[repositories."owner/repo"]\n' + hosts,
            encoding="utf-8",
        )
        project.write_text(
            'config_version = 1\n[github]\nlogin = "alice"\n'
            '[repositories."owner/repo"]\n' + project_hosts,
            encoding="utf-8",
        )
        return load_config(project, user_path=user)

    def test_aliases_are_trusted_and_repository_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hosts = 'verification_hosts = { "EXAMPLE.com" = "93.184.216.34" }\n'
            injected = (
                'verification_hosts = { "example.com" = "127.0.0.1", '
                '"injected.example" = "10.0.0.1" }\n'
                '[repositories."other/repo"]\n'
                'verification_hosts = { "example.com" = "10.0.0.1" }\n'
            )
            config = self.config(root, hosts, injected)
            self.assertEqual(
                config.repositories["owner/repo"].verification_hosts,
                (("example.com", "93.184.216.34"),),
            )
            self.assertEqual(config.repositories["other/repo"].verification_hosts, ())
            self.assertEqual(
                self.config(root, "", injected)
                .repositories["owner/repo"]
                .verification_hosts,
                (),
            )
            self.assertEqual(
                load_config(root / "project.toml", include_user=False)
                .repositories["owner/repo"]
                .verification_hosts,
                (),
            )

    def test_invalid_host_aliases_are_rejected(self):
        invalid = (
            "[]",
            '{ "example.com" = "host-gateway" }',
            '{ "example.com" = "::1" }',
            '{ "example.com" = "999.0.0.1" }',
            '{ "example.com" = 123 }',
            '{ "bad host" = "1.1.1.1" }',
            '{ "-bad.example" = "1.1.1.1" }',
            '{ "bad..example" = "1.1.1.1" }',
            '{ "bad:80" = "1.1.1.1" }',
            '{ "EXAMPLE.com" = "1.1.1.1", "example.com" = "8.8.8.8" }',
            '{ "' + "a" * 64 + '.example" = "1.1.1.1" }',
            '{ "' + ".".join(["a" * 63] * 4) + '" = "1.1.1.1" }',
        )
        with tempfile.TemporaryDirectory() as directory:
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(ConfigError):
                    self.config(Path(directory), "verification_hosts = " + value)

    def test_aliases_reach_both_phases_and_the_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(
                root, 'verification_hosts = { "example.com" = "93.184.216.34" }'
            )
            worktree = root / "source"
            worktree.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
            (worktree / "source.txt").write_text("unchanged")
            subprocess.run(["git", "add", "source.txt"], cwd=worktree, check=True)
            verifier = DockerVerifier(config)
            policy = config.repositories["owner/repo"]
            policy = replace(
                policy, bootstrap_commands=("setup",), verification_prefixes=("check",)
            )
            result = AgentResult("test", "test", "test", ("check",))
            calls = []

            def run(_worktree, command, **kwargs):
                calls.append(kwargs)
                return CommandResult(command, 0, "ok", 0.01)

            with (
                patch.object(verifier, "image_available", return_value=True),
                patch.object(verifier, "_run_container", side_effect=run),
            ):
                observed = verifier.verify(
                    worktree, policy, result, run_dir=root / "run"
                )
            self.assertTrue(observed.passed)
            self.assertEqual([value["network"] for value in calls], [True, False])
            self.assertTrue(
                all(
                    value["host_aliases"] == policy.verification_hosts
                    for value in calls
                )
            )
            manifest = json.loads((root / "run/verification/sandbox.json").read_text())
            self.assertEqual(
                manifest["trusted_host_aliases"], [["example.com", "93.184.216.34"]]
            )
            self.assertTrue(manifest["cleaned"])

    def test_docker_alias_arguments_preserve_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verifier = DockerVerifier(self.config(root, ""))
            completed = subprocess.CompletedProcess([], 0, "", "")
            with patch(
                "reposteward.verifier.subprocess.run", return_value=completed
            ) as run:
                verifier._run_container(
                    root,
                    "check",
                    network=False,
                    host_aliases=(("example.com", "93.184.216.34"),),
                )
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertEqual(
                command[command.index("--add-host") + 1], "example.com:93.184.216.34"
            )
            self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
            self.assertIn("no-new-privileges", command)
            self.assertIn("--user", command)
