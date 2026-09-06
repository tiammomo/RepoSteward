from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from reposteward.cli import main
from reposteward.config import load_config
from reposteward.state_upgrade import (
    StateUpgradeError,
    _write_manifest,
    inspect_backup,
    upgrade_plan,
    upgrade_state,
)
from reposteward.store import MIGRATIONS, SCHEMA_VERSION, apply_migration


class StateUpgradeTests(unittest.TestCase):
    def fixture(self, root: Path, *, version: int | None = 17):
        config_path = root / "config.toml"
        config_path.write_text(
            "config_version = 1\n[project]\nnamespace_state = false\n"
            f'state_dir = "{root.as_posix()}/state"\n[github]\nlogin = "alice"\n'
        )
        config = load_config(config_path)
        if version is not None:
            config.state_dir.mkdir()
            with closing(
                sqlite3.connect(config.state_dir / "reposteward.sqlite3")
            ) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("BEGIN IMMEDIATE")
                for number in range(1, version + 1):
                    apply_migration(db, number)
                db.execute(
                    "INSERT INTO issue_drafts(id,repository,title,body,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (
                        "retained",
                        "owner/repo",
                        "A task",
                        "original context",
                        "2026-01-01",
                        "2026-01-01",
                    ),
                )
                db.commit()
        return config

    def apply(self, config):
        plan = upgrade_plan(config)
        self.assertTrue(plan["eligible"])
        return upgrade_state(
            config, expected_state_dir=config.state_dir, plan_digest=plan["plan_digest"]
        )

    def schema_and_body(self, path):
        with closing(sqlite3.connect(path)) as db:
            return db.execute("PRAGMA user_version").fetchone()[0], db.execute(
                "SELECT body FROM issue_drafts WHERE id='retained'"
            ).fetchone()[0]

    def test_plan_does_not_initialize_missing_state_or_authenticate(self):
        with TemporaryDirectory() as directory:
            config = self.fixture(Path(directory), version=None)
            with (
                patch(
                    "reposteward.store.Store.__init__",
                    side_effect=AssertionError("no Store"),
                ),
                patch(
                    "reposteward.github.resolve_token",
                    side_effect=AssertionError("no auth"),
                ),
            ):
                plan = upgrade_plan(config)
            self.assertFalse(plan["eligible"])
            self.assertEqual(plan["database"]["status"], "missing")
            self.assertFalse(config.state_dir.exists())

    def test_upgrade_keeps_data_and_backup_can_restore_a_separate_copy(self):
        with TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            result = self.apply(config)
            self.assertTrue(result["upgraded"])
            self.assertEqual(
                self.schema_and_body(config.state_dir / "reposteward.sqlite3"),
                (SCHEMA_VERSION, "original context"),
            )
            backup = Path(result["backup"]["backup_directory"])
            self.assertTrue(inspect_backup(backup)["backup_verified"])
            self.assertEqual(
                self.schema_and_body(backup / "reposteward.sqlite3"),
                (17, "original context"),
            )
            restored = Path(directory) / "restored.sqlite3"
            shutil.copyfile(backup / "reposteward.sqlite3", restored)
            self.assertEqual(self.schema_and_body(restored), (17, "original context"))
            if os.name != "nt":
                self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
                self.assertEqual(
                    (backup / "reposteward.sqlite3").stat().st_mode & 0o777, 0o600
                )
            self.assertFalse(upgrade_plan(config)["eligible"])

    def test_wrong_scope_and_changed_contents_do_not_write(self):
        with TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            plan = upgrade_plan(config)
            with self.assertRaisesRegex(StateUpgradeError, "expected directory"):
                upgrade_state(
                    config,
                    expected_state_dir=Path(directory) / "wrong",
                    plan_digest=plan["plan_digest"],
                )
            path = config.state_dir / "reposteward.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.execute("UPDATE issue_drafts SET title='changed'")
                db.commit()
            with self.assertRaisesRegex(StateUpgradeError, "stale"):
                upgrade_state(
                    config,
                    expected_state_dir=config.state_dir,
                    plan_digest=plan["plan_digest"],
                )
            self.assertFalse((config.state_dir / "backups").exists())
            self.assertEqual(self.schema_and_body(path)[0], 17)

    def test_future_schema_and_changed_migration_are_rejected(self):
        with TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            plan = upgrade_plan(config)
            with (
                patch.dict(
                    MIGRATIONS, {18: (*MIGRATIONS[18], "CREATE TABLE changed(id)")}
                ),
                self.assertRaisesRegex(StateUpgradeError, "stale"),
            ):
                upgrade_state(
                    config,
                    expected_state_dir=config.state_dir,
                    plan_digest=plan["plan_digest"],
                )
            with closing(
                sqlite3.connect(config.state_dir / "reposteward.sqlite3")
            ) as db:
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
            self.assertEqual(
                upgrade_plan(config)["database"]["status"], "newer_than_supported"
            )

    def test_active_lease_prevents_upgrade(self):
        with TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            plan = upgrade_plan(config)
            now = datetime.now(UTC)
            with closing(
                sqlite3.connect(config.state_dir / "reposteward.sqlite3")
            ) as db:
                db.execute(
                    "INSERT INTO run_leases VALUES (?,?,?,?,?)",
                    (
                        "scope",
                        "worker",
                        1,
                        (now + timedelta(minutes=5)).isoformat(),
                        now.isoformat(),
                    ),
                )
                db.commit()
            self.assertEqual(upgrade_plan(config)["active_leases"], 1)
            with self.assertRaises(StateUpgradeError):
                upgrade_state(
                    config,
                    expected_state_dir=config.state_dir,
                    plan_digest=plan["plan_digest"],
                )
            self.assertFalse((config.state_dir / "backups").exists())

    def test_backup_failure_leaves_original_schema(self):
        with TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            with (
                patch(
                    "reposteward.state_upgrade._backup",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaisesRegex(StateUpgradeError, "did not commit"),
            ):
                self.apply(config)
            self.assertEqual(
                self.schema_and_body(config.state_dir / "reposteward.sqlite3"),
                (17, "original context"),
            )
            manifest = next((config.state_dir / "backups").glob("*/manifest.json"))
            self.assertEqual(json.loads(manifest.read_text())["phase"], "rolled_back")

    def test_migration_failure_rolls_back_every_version(self):
        with TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))

            def fail_after_first(db, version):
                if version == 19:
                    raise sqlite3.OperationalError("injected migration failure")
                apply_migration(db, version)

            with (
                patch(
                    "reposteward.state_upgrade.apply_migration",
                    side_effect=fail_after_first,
                ),
                self.assertRaisesRegex(StateUpgradeError, "did not commit"),
            ):
                self.apply(config)
            path = config.state_dir / "reposteward.sqlite3"
            self.assertEqual(self.schema_and_body(path), (17, "original context"))
            with closing(sqlite3.connect(path)) as db:
                self.assertFalse(
                    db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='feedback_items'"
                    ).fetchall()
                )
            backup = next((config.state_dir / "backups").iterdir())
            self.assertTrue(inspect_backup(backup)["backup_verified"])

    def test_lost_commit_acknowledgment_is_reported_as_unknown(self):
        class LostAck(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError("lost acknowledgment")

        connect = sqlite3.connect

        def intercepted(database, **kwargs):
            if "?mode=rw" in str(database):
                kwargs["factory"] = LostAck
            return connect(database, **kwargs)

        with TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            with (
                patch(
                    "reposteward.state_upgrade.sqlite3.connect", side_effect=intercepted
                ),
                self.assertRaisesRegex(StateUpgradeError, "outcome is unknown"),
            ):
                self.apply(config)
            self.assertEqual(
                self.schema_and_body(config.state_dir / "reposteward.sqlite3")[0],
                SCHEMA_VERSION,
            )
            manifest = next((config.state_dir / "backups").glob("*/manifest.json"))
            self.assertEqual(
                json.loads(manifest.read_text())["phase"], "commit_outcome_unknown"
            )

    def test_recording_failure_after_commit_does_not_claim_rollback(self):
        def fail_final_record(directory, record):
            if record["phase"] == "upgraded":
                raise OSError("disk full after commit")
            _write_manifest(directory, record)

        with TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            with (
                patch(
                    "reposteward.state_upgrade._write_manifest",
                    side_effect=fail_final_record,
                ),
                self.assertRaisesRegex(StateUpgradeError, "upgrade committed"),
            ):
                self.apply(config)
            self.assertEqual(
                self.schema_and_body(config.state_dir / "reposteward.sqlite3"),
                (SCHEMA_VERSION, "original context"),
            )
            backup = next((config.state_dir / "backups").iterdir())
            result = inspect_backup(backup)
            self.assertTrue(result["backup_verified"])
            self.assertEqual(result["phase"], "upgraded_record_incomplete")

    def test_tampered_backup_is_not_verified(self):
        with TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            result = self.apply(config)
            backup = Path(result["backup"]["backup_directory"])
            with closing(sqlite3.connect(backup / "reposteward.sqlite3")) as db:
                db.execute("UPDATE issue_drafts SET body='different'")
                db.commit()
            with self.assertRaisesRegex(StateUpgradeError, "checksum"):
                inspect_backup(backup)

    def test_cli_plan_does_not_construct_pipeline(self):
        with TemporaryDirectory() as directory:
            config = self.fixture(Path(directory))
            with (
                patch("reposteward.cli.load_config", return_value=config),
                patch(
                    "reposteward.cli.Pipeline",
                    side_effect=AssertionError("no Pipeline"),
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(main(["state", "plan"]), 0)
            self.assertTrue(json.loads(output.getvalue())["eligible"])
