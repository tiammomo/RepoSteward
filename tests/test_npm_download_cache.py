from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from reposteward.core.config import (
    RepositoryPolicy,
    RunnerConfig,
    load_config,
)
from reposteward.verification.npm_cache import NpmDownloadCache


class NpmDownloadCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.archive = b"locked download bytes"
        self.digest = hashlib.sha512(self.archive).hexdigest()
        self.lock = self.workspace / "package-lock.json"
        self.lock.write_text(
            json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "node_modules/fixture": {
                            "integrity": "sha512-"
                            + base64.b64encode(
                                hashlib.sha512(self.archive).digest()
                            ).decode()
                        }
                    },
                }
            )
        )
        self.policy = RepositoryPolicy(
            name="owner/repo", bootstrap_commands=("npm ci --ignore-scripts",)
        )
        user = self.root / "base.toml"
        user.write_text('[github]\nlogin="owner"\n')
        self.config = replace(
            load_config(user),
            repositories={"owner/repo": self.policy},
            runner=RunnerConfig(
                image="sha256:" + "a" * 64,
                npm_download_cache_dir=self.root / "downloads",
            ),
        )

    def cache(self, config=None, policy=None):
        return NpmDownloadCache.for_workspace(
            config or self.config, policy or self.policy, self.workspace
        )

    def seed(self, environment):
        path = self.cache()._npm_path(environment, self.digest)
        path.parent.mkdir(parents=True)
        path.write_bytes(self.archive)
        return path

    def test_verified_archive_round_trip_excludes_mutable_indexes(self):
        env = self.root / "first"
        self.seed(env)
        (env / "npm-cache/_cacache/index-v5").mkdir()
        (env / "npm-cache/_cacache/index-v5/untrusted").write_text("mutable metadata")
        cache = self.cache()
        self.assertEqual(cache.save(env)["saved_files"], 1)
        self.assertEqual([p.name for p in cache.directory.iterdir()], [self.digest])
        second = self.root / "second"
        self.assertEqual(self.cache().restore(second)["restored_files"], 1)
        self.assertEqual(
            cache._npm_path(second, self.digest).read_bytes(), self.archive
        )

    def test_different_lock_image_bootstrap_repository_and_account_use_new_keys(self):
        first = self.cache().directory
        self.assertNotEqual(
            first, self.cache(policy=replace(self.policy, name="owner/other")).directory
        )
        self.assertNotEqual(
            first,
            self.cache(
                policy=replace(self.policy, bootstrap_commands=("npm ci",))
            ).directory,
        )
        self.assertNotEqual(
            first,
            self.cache(
                config=replace(
                    self.config,
                    runner=replace(self.config.runner, image="sha256:" + "b" * 64),
                )
            ).directory,
        )
        self.assertNotEqual(
            first,
            self.cache(
                config=replace(
                    self.config, github=replace(self.config.github, login="another")
                )
            ).directory,
        )
        self.lock.write_text(self.lock.read_text() + "\n")
        self.assertNotEqual(first, self.cache().directory)

    def test_corrupt_archives_are_not_restored(self):
        cache = self.cache()
        cache.directory.mkdir(parents=True)
        (cache.directory / self.digest).write_bytes(b"corrupt")
        target = self.root / "restored"
        report = cache.restore(target)
        self.assertEqual(report["restored_files"], 0)
        self.assertEqual(report["invalid_files"], 1)
        self.assertFalse(cache._npm_path(target, self.digest).exists())

    def test_symlink_archive_and_parent_are_never_followed(self):
        cache = self.cache()
        cache.directory.mkdir(parents=True)
        outside = self.root / "outside"
        outside.write_bytes(self.archive)
        (cache.directory / self.digest).symlink_to(outside)
        self.assertEqual(cache.restore(self.root / "restored")["restored_files"], 0)
        environment = self.root / "environment"
        environment.mkdir()
        (environment / "npm-cache").symlink_to(self.root)
        self.assertEqual(cache.save(environment)["saved_files"], 0)
        self.assertEqual(outside.read_bytes(), self.archive)

    def test_unsupported_image_missing_lock_and_disabled_setting_fall_back(self):
        self.assertIsNone(
            self.cache(config=replace(self.config, runner=RunnerConfig()))
        )
        self.assertIsNone(
            self.cache(
                config=replace(
                    self.config,
                    runner=replace(self.config.runner, image="runner:latest"),
                )
            )
        )
        self.lock.unlink()
        self.assertIsNone(self.cache())

    def test_limits_and_failed_publication_leave_no_visible_archive(self):
        env = self.root / "environment"
        self.seed(env)
        cache = self.cache()
        with patch("reposteward.verification.npm_cache.MAX_FILE_BYTES", 1):
            self.assertEqual(cache.save(env)["saved_files"], 0)
        with patch.object(Path, "replace", side_effect=OSError("interrupted")):
            self.assertEqual(cache.save(env)["saved_files"], 0)
        self.assertEqual(list(cache.directory.glob("*")), [])

    def test_only_user_configuration_can_choose_the_cache_path(self):
        user = self.root / "user.toml"
        project = self.root / "project.toml"
        user.write_text(
            '[github]\nlogin="owner"\n[runner]\nnpm_download_cache_dir="downloads"\n'
        )
        project.write_text('[runner]\nnpm_download_cache_dir="/project/chosen"\n')
        config = load_config(project, user_path=user)
        self.assertEqual(config.runner.npm_download_cache_dir, self.root / "downloads")
        user.write_text('[github]\nlogin="owner"\n')
        self.assertIsNone(
            load_config(project, user_path=user).runner.npm_download_cache_dir
        )

    @unittest.skipUnless(shutil.which("npm"), "npm archive smoke requires Node")
    def test_real_npm_ci_installs_offline_from_restored_content_only(self):
        package = json.dumps({"name": "fixture", "version": "1.0.0"}).encode()
        archive = self.workspace / "fixture.tgz"
        with tarfile.open(archive, "w:gz") as bundle:
            entry = tarfile.TarInfo("package/package.json")
            entry.size = len(package)
            bundle.addfile(entry, io.BytesIO(package))
        integrity = (
            "sha512-"
            + base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()
        )
        (self.workspace / "package.json").write_text(
            json.dumps(
                {
                    "name": "consumer",
                    "version": "1.0.0",
                    "dependencies": {"fixture": "1.0.0"},
                }
            )
        )
        self.lock.write_text(
            json.dumps(
                {
                    "name": "consumer",
                    "version": "1.0.0",
                    "lockfileVersion": 3,
                    "packages": {
                        "": {
                            "name": "consumer",
                            "version": "1.0.0",
                            "dependencies": {"fixture": "1.0.0"},
                        },
                        "node_modules/fixture": {
                            "version": "1.0.0",
                            "resolved": "https://registry.example.invalid/fixture/-/fixture-1.0.0.tgz",
                            "integrity": integrity,
                        },
                    },
                }
            )
        )
        first = self.root / "first"
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "CI": "1",
        }
        seeded = subprocess.run(
            [
                "npm",
                "cache",
                "add",
                str(archive),
                "--cache",
                str(first / "npm-cache"),
                "--ignore-scripts",
                "--offline",
            ],
            env=env,
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(seeded.returncode, 0, seeded.stdout + seeded.stderr)
        cache = self.cache()
        self.assertEqual(cache.save(first)["saved_files"], 1)
        second = self.root / "second"
        self.assertEqual(self.cache().restore(second)["restored_files"], 1)
        archive.unlink()
        installed = subprocess.run(
            [
                "npm",
                "ci",
                "--cache",
                str(second / "npm-cache"),
                "--ignore-scripts",
                "--offline",
                "--no-audit",
                "--no-fund",
            ],
            env=env,
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertEqual(
            json.loads(
                (self.workspace / "node_modules/fixture/package.json").read_text()
            )["version"],
            "1.0.0",
        )

    def test_test_stage_mutations_are_not_saved_to_the_host_cache(self):
        import test_verifier

        from reposteward.core.models import AgentResult, CommandResult
        from reposteward.verification.verifier import DockerVerifier

        test_verifier._repository(self.workspace)
        policy = replace(self.policy, verification_prefixes=("python ",))
        verifier = DockerVerifier(self.config)

        def run(_workspace, command, *, network, environment_dir, **kwargs):
            archive = self.cache()._npm_path(environment_dir, self.digest)
            if network:
                archive.parent.mkdir(parents=True, exist_ok=True)
                archive.write_bytes(self.archive)
            else:
                archive.write_bytes(b"test-stage mutation")
            return CommandResult(command, 0, "", 0.01)

        with (
            patch.object(verifier, "image_available", return_value=True),
            patch.object(verifier, "_run_container", side_effect=run),
        ):
            result = verifier.verify(
                self.workspace,
                policy,
                AgentResult("summary", "fix: fixture", "notes", ("python check",)),
                run_dir=self.root / "run",
            )
        self.assertTrue(result.passed)
        cache = self.cache(policy=policy)
        self.assertEqual((cache.directory / self.digest).read_bytes(), self.archive)
        manifest = json.loads((self.root / "run/verification/sandbox.json").read_text())
        self.assertEqual(manifest["npm_download_cache"]["saved_files"], 1)

    def test_failed_bootstrap_does_not_publish_archives(self):
        import test_verifier

        from reposteward.core.models import AgentResult, CommandResult
        from reposteward.verification.verifier import DockerVerifier

        test_verifier._repository(self.workspace)
        policy = replace(self.policy, verification_prefixes=("python ",))
        verifier = DockerVerifier(self.config)

        def fail(_workspace, command, *, environment_dir, **kwargs):
            path = self.cache()._npm_path(environment_dir, self.digest)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.archive)
            return CommandResult(command, 1, "failed", 0.01)

        with (
            patch.object(verifier, "image_available", return_value=True),
            patch.object(verifier, "_run_container", side_effect=fail),
        ):
            self.assertFalse(
                verifier.verify(
                    self.workspace,
                    policy,
                    AgentResult("summary", "fix: fixture", "notes", ("python check",)),
                ).passed
            )
        self.assertFalse((self.cache(policy=policy).directory / self.digest).exists())

    def test_concurrent_publications_leave_one_complete_verified_archive(self):
        first, second = self.root / "first", self.root / "second"
        self.seed(first)
        self.seed(second)
        with ThreadPoolExecutor(max_workers=2) as pool:
            left = pool.submit(self.cache().save, first)
            right = pool.submit(self.cache().save, second)
            self.assertEqual(left.result()["saved_files"], 1)
            self.assertEqual(right.result()["saved_files"], 1)
        cache = self.cache()
        self.assertEqual(
            [path.name for path in cache.directory.iterdir()], [self.digest]
        )
        self.assertEqual(cache.restore(self.root / "third")["restored_files"], 1)

    def test_cache_location_does_not_change_the_verification_profile_identity(self):
        from reposteward.core.config import VerificationProfile
        from reposteward.verification.external import ExternalVerification

        profile = VerificationProfile("owner/repo", "unit", ("python check",))
        without_cache = replace(
            self.config, runner=replace(self.config.runner, npm_download_cache_dir=None)
        )
        original = ExternalVerification(without_cache)._profile_digest(profile)
        self.assertEqual(
            ExternalVerification(self.config)._profile_digest(profile), original
        )
        relocated = replace(
            self.config,
            runner=replace(
                self.config.runner, npm_download_cache_dir=self.root / "elsewhere"
            ),
        )
        self.assertEqual(
            ExternalVerification(relocated)._profile_digest(profile), original
        )
