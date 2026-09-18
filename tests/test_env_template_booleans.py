from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from test_projects import git, repository

from reposteward.config import ConfigError, load_config
from reposteward.context import repository_policy_digest
from reposteward.models import AgentResult, CommandResult
from reposteward.projects import ProjectError, canonical_digest
from reposteward.snapshots import verify_snapshot_copy, workspace_snapshot
from reposteward.verifier import (
    MAX_ENV_TEMPLATE_BYTES,
    DockerVerifier,
    VerificationError,
)

DECLARATION = (
    'env_template_booleans = { ROUTEPILOT_V1_DEV_AUTH = ["0"], '
    'ROUTEPILOT_BFF_DEV_AUTH = ["0"] }\n'
)
TEMPLATE = (
    "ROUTEPILOT_V1_DEV_AUTH=0\n"
    "ROUTEPILOT_BFF_DEV_AUTH=0\n"
    "ROUTEPILOT_V1_DEV_BFF_SECRET=\n"
)


def configuration(root: Path, trusted: str = "", untrusted: str = ""):
    user = root / "user.toml"
    project = root / "project.toml"
    user.write_text(
        'config_version = 1\n[github]\nlogin = "owner"\n'
        '[repositories."owner/repo"]\n' + trusted
    )
    project.write_text(
        'config_version = 1\n[github]\nlogin = "owner"\n'
        '[repositories."owner/repo"]\n' + untrusted
    )
    return load_config(project, user_path=user)


class EnvTemplateBooleanConfigTests(unittest.TestCase):
    def test_only_user_configuration_can_declare_or_expand_boolean_fields(self):
        injected = (
            'env_template_booleans = { ROUTEPILOT_V1_DEV_AUTH = ["0", "1"], '
            'API_KEY = ["0"] }\n'
            '[repositories."other/repo"]\n' + DECLARATION
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = configuration(root, DECLARATION)
            expected = trusted.repositories["owner/repo"].env_template_booleans
            self.assertEqual(
                dict(expected),
                {"ROUTEPILOT_BFF_DEV_AUTH": ("0",), "ROUTEPILOT_V1_DEV_AUTH": ("0",)},
            )
            config = configuration(root, DECLARATION, injected)
            self.assertEqual(
                config.repositories["owner/repo"].env_template_booleans, expected
            )
            self.assertEqual(
                config.repositories["other/repo"].env_template_booleans, ()
            )
            self.assertEqual(
                configuration(root, "", injected)
                .repositories["owner/repo"]
                .env_template_booleans,
                (),
            )
            self.assertEqual(
                load_config(root / "project.toml", include_user=False)
                .repositories["owner/repo"]
                .env_template_booleans,
                (),
            )

    def test_invalid_declarations_fail_without_echoing_configured_values(self):
        invalid = (
            "[]",
            '{ "*_AUTH" = ["0"] }',
            '{ "A.B" = ["0"] }',
            '{ "1AUTH" = ["0"] }',
            '{ " AUTH" = ["0"] }',
            '{ "Å_AUTH" = ["0"] }',
            '{ "' + "A" * 129 + '" = ["0"] }',
            "{ AUTH = [] }",
            '{ AUTH = "0" }',
            "{ AUTH = [false] }",
            "{ AUTH = [0] }",
            '{ AUTH = [["0"]] }',
            '{ AUTH = ["0", "0"] }',
            '{ AUTH = ["FALSE"] }',
            '{ AUTH = [" 0"] }',
            '{ AUTH = ["fake-sensitive-value"] }',
            "{ " + ", ".join(f'A{i} = ["0"]' for i in range(65)) + " }",
        )
        with tempfile.TemporaryDirectory() as directory:
            for declaration in invalid:
                with self.subTest(declaration=declaration):
                    with self.assertRaises(ConfigError) as error:
                        configuration(
                            Path(directory), "env_template_booleans = " + declaration
                        )
                    self.assertNotIn("fake-sensitive-value", str(error.exception))

    def test_configuration_is_canonical_and_repository_names_are_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = configuration(
                root,
                'env_template_booleans = { B_AUTH = ["yes", "0"], A_AUTH = ["no"] }',
            )
            self.assertEqual(
                config.repositories["owner/repo"].env_template_booleans,
                (("A_AUTH", ("no",)), ("B_AUTH", ("0", "yes"))),
            )
            with self.assertRaises(ConfigError):
                configuration(root, '[repositories."owner/*"]\n' + DECLARATION)

    def test_default_preserves_policy_identity_and_declared_values_change_it(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = configuration(Path(directory)).repositories["owner/repo"]
            original = asdict(policy)
            for name in (
                "owner_attestation",
                "branch_cleanup",
                "max_active_pull_requests",
                "unlimited_diff_lines",
                "env_template_booleans",
            ):
                original.pop(name)
            self.assertEqual(
                repository_policy_digest(policy), canonical_digest(original)
            )
            enabled = replace(policy, env_template_booleans=(("DEV_AUTH", ("0",)),))
            expanded = replace(
                policy, env_template_booleans=(("DEV_AUTH", ("0", "1")),)
            )
            self.assertNotEqual(
                repository_policy_digest(policy), repository_policy_digest(enabled)
            )
            self.assertNotEqual(
                repository_policy_digest(enabled), repository_policy_digest(expanded)
            )


class EnvTemplateBooleanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = repository(self.root / "checkout")
        (self.repo / ".env.example").write_text(TEMPLATE)
        git(self.repo, "add", ".env.example")

    def test_boolean_auth_switches_still_need_trusted_declarations(self):
        verifier = DockerVerifier(configuration(self.root))
        with self.assertRaisesRegex(VerificationError, "non-empty sensitive"):
            verifier._copy_workspace(self.repo, self.root / "copy")

    def test_reviewed_disabled_switches_enter_verification_unchanged(self):
        config = configuration(self.root, DECLARATION)
        verifier = DockerVerifier(config)
        with verifier._verification_sandbox(
            self.repo, self.root / "run", policy=config.repositories["owner/repo"]
        ) as (sandbox, _, _):
            self.assertEqual((sandbox / ".env.example").read_text(), TEMPLATE)

    def test_declared_values_and_existing_empty_forms_are_accepted(self):
        path = self.repo / ".env.example"
        fields = (("DEV_AUTH", ("0", "1", "true", "false", "yes", "no")),)
        for value in (
            "0",
            "1",
            "true",
            "false",
            "yes",
            "no",
            "",
            "''",
            '""',
            "'' # comment",
        ):
            with self.subTest(value=value):
                path.write_text(f"DEV_AUTH={value}\n")
                DockerVerifier._validate_env_template(
                    path, Path(path.name), env_template_booleans=fields
                )

    def test_undeclared_names_and_unapproved_values_fail_in_both_paths(self):
        config = configuration(self.root, DECLARATION)
        fields = config.repositories["owner/repo"].env_template_booleans
        invalid = (
            [
                "ROUTEPILOT_V1_DEV_AUTH=" + value
                for value in (
                    "1",
                    "false",
                    "FALSE",
                    "01",
                    '"0"',
                    "0 # comment",
                    "replace-with-value",
                    "your-provider-api-key",
                    "fake-sensitive-value",
                    "0" * 129,
                )
            ]
            + [
                name + "=0"
                for name in (
                    "AUTH",
                    "DEV_AUTH",
                    "API_KEY",
                    "TOKEN",
                    "SECRET",
                    "PASSWORD",
                    "OTHER_ROUTEPILOT_V1_DEV_AUTH",
                    "ROUTEPILOT_V1_DEV_AUTH_TOKEN",
                    "routepilot_v1_dev_auth",
                    "ROUTEPILOT_V1_DEV_BFF_SECRET",
                )
            ]
            + [
                "export ROUTEPILOT_V1_DEV_AUTH",
                "ROUTEPILOT_V1_DEV_AUTH=0\nAUTH=fake-sensitive-value",
            ]
        )
        for index, line in enumerate(invalid):
            with self.subTest(line=line):
                (self.repo / ".env.example").write_text(line + "\n")
                for operation in (
                    lambda: workspace_snapshot(self.repo, env_template_booleans=fields),
                    lambda index=index: DockerVerifier._copy_workspace(
                        self.repo,
                        self.root / f"copy-{index}",
                        env_template_booleans=fields,
                    ),
                ):
                    with self.assertRaises(VerificationError) as error:
                        operation()
                    self.assertNotIn("fake-sensitive-value", str(error.exception))

    def test_allowance_cannot_smuggle_non_boolean_literals_through_library_calls(self):
        path = self.repo / ".env.example"
        path.write_text("DEV_AUTH=fake-sensitive-value\n")
        with self.assertRaisesRegex(VerificationError, "unapproved boolean"):
            DockerVerifier._validate_env_template(
                path,
                Path(path.name),
                env_template_booleans=(("DEV_AUTH", ("fake-sensitive-value",)),),
            )

    def test_template_size_encoding_and_regular_file_checks_still_apply(self):
        path = self.repo / ".env.example"
        fields = (("ROUTEPILOT_V1_DEV_AUTH", ("0",)),)
        for content in (b"\xff", b"#" * (MAX_ENV_TEMPLATE_BYTES + 1)):
            path.write_bytes(content)
            with self.assertRaises(VerificationError):
                workspace_snapshot(self.repo, env_template_booleans=fields)
        path.unlink()
        target = self.repo / "template.txt"
        target.write_text("ROUTEPILOT_V1_DEV_AUTH=0\n")
        path.symlink_to(target)
        with self.assertRaisesRegex(VerificationError, "regular file"):
            workspace_snapshot(self.repo, env_template_booleans=fields)
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(VerificationError, "regular file"):
            workspace_snapshot(self.repo, env_template_booleans=fields)

    def test_untracked_templates_and_real_environment_files_are_not_allowed(self):
        fields = (
            configuration(self.root, DECLARATION)
            .repositories["owner/repo"]
            .env_template_booleans
        )
        git(self.repo, "reset", "--", ".env.example")
        snapshot = workspace_snapshot(self.repo, env_template_booleans=fields)
        self.assertNotIn(".env.example", [entry["path"] for entry in snapshot["files"]])
        (self.repo / ".env").write_text(TEMPLATE)
        git(self.repo, "add", ".env")
        with self.assertRaisesRegex(ProjectError, "tracked sensitive path"):
            workspace_snapshot(self.repo, env_template_booleans=fields)

    def test_snapshot_guard_detects_mutation_to_another_approved_boolean(self):
        fields = (
            ("ROUTEPILOT_BFF_DEV_AUTH", ("0",)),
            ("ROUTEPILOT_V1_DEV_AUTH", ("0", "1")),
        )
        snapshot = workspace_snapshot(self.repo, env_template_booleans=fields)
        copy = self.root / "copy"
        DockerVerifier._copy_workspace(self.repo, copy, env_template_booleans=fields)
        verify_snapshot_copy(copy, snapshot, exact=True, env_template_booleans=fields)
        (copy / ".env.example").write_text(
            TEMPLATE.replace("V1_DEV_AUTH=0", "V1_DEV_AUTH=1")
        )
        with self.assertRaisesRegex(ProjectError, "copy differs"):
            verify_snapshot_copy(
                copy, snapshot, exact=False, env_template_booleans=fields
            )

    def test_both_container_phases_and_manifest_use_the_reviewed_policy(self):
        config = configuration(self.root, DECLARATION)
        policy = replace(
            config.repositories["owner/repo"],
            bootstrap_commands=("setup",),
            verification_prefixes=("check",),
        )
        verifier = DockerVerifier(config)
        networks = []

        def run(sandbox, command, **kwargs):
            networks.append(kwargs["network"])
            self.assertEqual((sandbox / ".env.example").read_text(), TEMPLATE)
            return CommandResult(command, 0, "ok", 0.01)

        with (
            patch.object(verifier, "image_available", return_value=True),
            patch.object(verifier, "_run_container", side_effect=run),
        ):
            result = verifier.verify(
                self.repo,
                policy,
                AgentResult("test", "test", "test", ("check",)),
                run_dir=self.root / "run",
            )
        self.assertTrue(result.passed)
        self.assertEqual(networks, [True, False])
        manifest = json.loads((self.root / "run/verification/sandbox.json").read_text())
        self.assertEqual(
            manifest["trusted_env_template_booleans"],
            {name: list(values) for name, values in policy.env_template_booleans},
        )
        self.assertTrue(manifest["cleaned"])
