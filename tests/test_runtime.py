from __future__ import annotations

import io
import json
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from reposteward.cli import main
from reposteward.config import load_config
from reposteward.runtime import inspect_database, installation_info, local_diagnostics
from reposteward.store import SCHEMA_VERSION, Store


class RuntimeTests(unittest.TestCase):
    def config(self, root: Path):
        config = root / "config.toml"
        config.write_text(
            "config_version = 1\n[project]\nnamespace_state = false\n"
            f'state_dir = "{root.as_posix()}/state"\n[github]\nlogin = "alice"\n'
        )
        return load_config(config)

    def test_missing_state_is_not_created_and_no_execution_is_attempted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            before = sorted(root.rglob("*"))
            with (
                patch("subprocess.run", side_effect=AssertionError("no processes")),
                patch(
                    "reposteward.store.Store.__init__",
                    side_effect=AssertionError("no Store"),
                ),
                patch(
                    "reposteward.github.resolve_token",
                    side_effect=AssertionError("no credentials"),
                ),
            ):
                report, ok = local_diagnostics(config)
            self.assertTrue(ok)
            self.assertEqual(report["databases"]["tasks"]["status"], "missing")
            self.assertEqual(sorted(root.rglob("*")), before)

    def test_expected_directory_mismatch_does_not_open_a_database(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("sqlite3.connect", side_effect=AssertionError("no DB read")):
                report, ok = local_diagnostics(
                    self.config(root), expected_state_dir=root / "other"
                )
            self.assertFalse(ok)
            self.assertFalse(report["configuration"]["state_dir_matches"])
            self.assertEqual(report["databases"], {})

    def test_compatible_and_older_ledgers_remain_unchanged(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            path = config.state_dir / "reposteward.sqlite3"
            Store(path)
            for version, expected in (
                (SCHEMA_VERSION, "compatible"),
                (17, "migration_required"),
            ):
                with closing(sqlite3.connect(path)) as db:
                    db.execute(f"PRAGMA user_version={version}")
                    db.commit()
                before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
                report, ok = local_diagnostics(config)
                self.assertEqual(report["databases"]["tasks"]["status"], expected)
                self.assertEqual(ok, version == SCHEMA_VERSION)
                self.assertEqual(
                    {p.name: p.read_bytes() for p in path.parent.iterdir()}, before
                )

    def test_future_corrupt_unrecognized_and_uninitialized_database(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "data.sqlite3"
            path.write_bytes(b"not a database")
            self.assertEqual(
                inspect_database(path, supported=22, required_tables=())["status"],
                "invalid_database",
            )
            path.unlink()
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA user_version=999")
            before = path.read_bytes()
            self.assertEqual(
                inspect_database(path, supported=22, required_tables=())["status"],
                "newer_than_supported",
            )
            self.assertEqual(path.read_bytes(), before)
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA user_version=22")
            self.assertEqual(
                inspect_database(path, supported=22, required_tables=("runs",))[
                    "status"
                ],
                "unrecognized",
            )
            path.write_bytes(b"")
            self.assertEqual(
                inspect_database(path, supported=22, required_tables=())["status"],
                "uninitialized",
            )
            self.assertEqual(path.read_bytes(), b"")

    def test_live_wal_is_unknown_and_no_sidecars_are_created(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "data.sqlite3"
            with closing(sqlite3.connect(path)) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE sample(value)")
                writer.commit()
                before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
                with patch(
                    "reposteward.runtime.sqlite3.connect",
                    side_effect=AssertionError("no stale immutable read"),
                ):
                    result = inspect_database(path, supported=22, required_tables=())
                self.assertEqual(result["status"], "snapshot_required")
                self.assertIsNone(result["schema"])
                self.assertEqual(
                    {p.name: p.read_bytes() for p in path.parent.iterdir()}, before
                )

    def test_project_registry_compatibility_is_reported_separately(self):
        with TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            config.state_dir.mkdir()
            with closing(sqlite3.connect(config.state_dir / "projects.sqlite3")) as db:
                for table in ("projects", "workspace_bindings", "project_events"):
                    db.execute(f"CREATE TABLE {table}(id TEXT)")
                db.execute("PRAGMA user_version=1")
                db.commit()
            report, ok = local_diagnostics(config)
            self.assertTrue(ok)
            self.assertEqual(report["databases"]["projects"]["status"], "compatible")
            self.assertEqual(report["databases"]["tasks"]["status"], "missing")

    def test_layer_sources_match_trusted_merge_without_exposing_config(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            user = root / "user.toml"
            user.write_text(
                'config_version = 1\n[project]\nstate_dir = "/trusted/state"\n'
                'namespace_state = false\n[github]\nlogin = "alice"\n'
                'api_url = "https://example.test:8443/api/v3"\n'
                'gh_auth_command = ["SECRET_COMMAND"]\n'
            )
            project = root / "project.toml"
            project.write_text(
                'config_version = 1\n[project]\nstate_dir = "/untrusted/state"\n'
                '[github]\nlogin = "mallory"\n[agent]\nharness = "codex-sdk"\n'
                '[runner]\nimage = "untrusted-image"\n'
            )
            config = load_config(project, user_path=user)
            # Deliberately require a different directory: provenance is still visible,
            # but this test never inspects the configured absolute state path.
            report, _ = local_diagnostics(config, expected_state_dir=root / "expected")
            settings = report["configuration"]["settings"]
            self.assertEqual(settings["project.state_dir"]["source"], "user")
            self.assertEqual(
                settings["project.state_dir"]["effective_value"], "/trusted/state"
            )
            self.assertEqual(settings["github.login"]["source"], "user")
            self.assertEqual(settings["agent.harness"]["source"], "default")
            self.assertEqual(settings["runner.image"]["source"], "default")
            encoded = json.dumps(report)
            for secret in ("SECRET", "HIDDEN", "mallory", "untrusted-image"):
                self.assertNotIn(secret, encoded)
            self.assertEqual(
                dict(load_config(project).setting_sources)["github.login"], "project"
            )
            self.assertEqual(config.github.gh_auth_command, ("SECRET_COMMAND",))

    def test_url_userinfo_is_not_exposed_through_derived_state_paths(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                'config_version = 1\n[github]\nlogin = "alice"\n'
                'api_url = "https://alice:SECRET@example.test/api/v3"\n'
            )
            config = load_config(path)
            with patch("sqlite3.connect", side_effect=AssertionError("no DB read")):
                report, ok = local_diagnostics(config)
            self.assertFalse(ok)
            self.assertEqual(report["configuration"]["status"], "unsupported_api_url")
            self.assertNotIn("secret", json.dumps(report).lower())

    def test_version_redacts_direct_url_and_reports_metadata_absence(self):
        distribution = Mock(version="0.1.0")
        distribution.locate_file.return_value = Path("/installed")
        distribution.read_text.return_value = json.dumps(
            {
                "url": "https://token:SECRET@example.test/repo",
                "vcs_info": {"commit_id": "a" * 40},
            }
        )
        with patch(
            "reposteward.runtime.metadata.distribution", return_value=distribution
        ):
            report = installation_info()
        self.assertEqual(report["source_revision"], "a" * 40)
        self.assertNotIn("SECRET", json.dumps(report))
        with patch(
            "reposteward.runtime.metadata.distribution",
            side_effect=metadata.PackageNotFoundError,
        ):
            self.assertFalse(installation_info()["metadata_available"])

    def test_version_cli_needs_no_config_and_local_doctor_handles_missing_config(self):
        with patch(
            "reposteward.cli.load_config", side_effect=AssertionError("no config")
        ):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["version"]), 0)
            self.assertIn("version", json.loads(output.getvalue()))
            with (
                redirect_stdout(io.StringIO()) as output,
                self.assertRaises(SystemExit) as stopped,
            ):
                main(["--version"])
            self.assertEqual(stopped.exception.code, 0)
            self.assertTrue(output.getvalue().startswith("reposteward "))
        with (
            TemporaryDirectory() as directory,
            patch(
                "reposteward.config.default_user_config_path",
                return_value=Path(directory) / "user.toml",
            ),
        ):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    main(
                        [
                            "--config",
                            str(Path(directory) / "missing.toml"),
                            "doctor",
                            "--local",
                        ]
                    ),
                    1,
                )
            self.assertEqual(
                json.loads(output.getvalue())["configuration"]["status"],
                "unavailable",
            )

    def test_full_doctor_remains_a_separate_explicit_path(self):
        with TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            with (
                patch("reposteward.cli.load_config", return_value=config),
                patch(
                    "reposteward.cli.run_doctor", return_value=({"tools": {}}, True)
                ) as doctor,
            ):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["doctor"]), 0)
                doctor.assert_called_once_with(config)
