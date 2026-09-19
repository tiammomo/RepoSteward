from __future__ import annotations

import fcntl
import io
import json
import sqlite3
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from dataclasses import replace
from unittest.mock import patch

import test_external_verification

from reposteward.cli import main
from reposteward.storage.store import Store, StoreError
from reposteward.tasks.external import TaskConflict
from reposteward.tasks.lifecycle import TaskLifecycle
from reposteward.verification.execution import (
    observe,
    read_record,
    record_container,
    write_record,
)
from reposteward.verification.recovery import VerificationRecovery
from reposteward.verification.verifier import DockerVerifier
from reposteward.workflows.policy import PolicyError


class RecoveryTests(unittest.TestCase):
    container_run = test_external_verification.ExternalVerificationTests.container_run
    request = test_external_verification.ExternalVerificationTests.request

    def setUp(self):
        test_external_verification.ExternalVerificationTests.setUp(self)
        value = self.request()
        self.identifier = value["evidence_id"].split(":")[1]
        self.directory = self.check._directory(self.identifier)
        self.run = self.task["run_id"]
        self.recovery = VerificationRecovery(self.config)
        self.store = self.service._store(write=True)
        # Crash injection: the completed worker's last SQL update never committed.
        with self.store._connection() as db:
            db.execute(
                "UPDATE external_verifications SET outcome='running',reason='',result='{}' WHERE id=?",
                (self.identifier,),
            )

    def plan(self):
        return self.recovery.plan(
            self.run, self.identifier, reason="Recover interrupted verifier"
        )

    def apply(self, plan):
        return self.recovery.reconcile(
            self.run,
            self.identifier,
            reason="Recover interrupted verifier",
            plan_digest=plan["plan_digest"],
            reviewed_by="owner",
            idempotency_key="recover-one",
        )

    def test_reconciliation_preserves_snapshot_and_unblocks_terminal_disposition(self):
        before = self.check.inspect(self.run, self.identifier)
        lifecycle = TaskLifecycle(self.config)
        self.assertFalse(
            lifecycle.plan(self.run, outcome="cancelled", reason="Stop")["eligible"]
        )
        result = self.apply(self.plan())
        after = self.check.inspect(self.run, self.identifier)
        self.assertEqual(after["outcome"], "unknown")
        self.assertEqual(after["snapshot"], before["snapshot"])
        self.assertFalse(after["publication_eligible"])
        self.assertIsNotNone(after["reconciliation"])
        self.assertTrue(
            lifecycle.plan(self.run, outcome="cancelled", reason="Stop")["eligible"]
        )
        replay = self.apply(result["reconciliation"]["payload"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["reconciliation"], result["reconciliation"])

    def test_active_lock_blocks_plan_and_apply(self):
        plan = self.plan()
        with (self.directory / "lease").open("r+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn("verification_active", self.plan()["reasons"])
            with self.assertRaises(TaskConflict):
                self.apply(plan)

    def test_concurrent_reconciliation_produces_only_one_receipt(self):
        plan = self.plan()

        def attempt(_):
            try:
                return self.apply(plan)
            except TaskConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(attempt, range(2)))
        self.assertTrue(any(value is not None for value in values))
        self.assertTrue(self.apply(plan)["idempotent"])
        with self.store._connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM verification_reconciliations"
                ).fetchone()[0],
                1,
            )

    def test_old_schema_reads_do_not_migrate_and_upgrade_preserves_verification(self):
        before = self.check._row(self.store, self.run, self.identifier)
        with self.store._connection() as db:
            db.execute("DROP TABLE verification_reconciliations")
            db.execute("PRAGMA user_version=23")
        with self.assertRaises(StoreError):
            self.plan()
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 23)
        upgraded = Store(self.store.path)
        self.assertEqual(self.check._row(upgraded, self.run, self.identifier), before)
        self.assertTrue(self.plan()["eligible"])

    def test_cli_uses_reviewed_plan_without_starting_pipeline(self):
        output = io.StringIO()
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            patch("reposteward.cli.Pipeline", side_effect=AssertionError("pipeline")),
            redirect_stdout(output),
        ):
            status = main(
                [
                    "verification",
                    "reconcile-plan",
                    self.run,
                    self.identifier,
                    "--reason",
                    "Recover interrupted verifier",
                ]
            )
        self.assertEqual(status, 0)
        plan = json.loads(output.getvalue())
        output = io.StringIO()
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            redirect_stdout(output),
        ):
            status = main(
                [
                    "verification",
                    "reconcile",
                    self.run,
                    self.identifier,
                    "--reason",
                    "Recover interrupted verifier",
                    "--plan-digest",
                    plan["plan_digest"],
                    "--reviewed-by",
                    "owner",
                    "--idempotency-key",
                    "cli-recovery",
                ]
            )
        self.assertEqual(status, 0)
        self.assertFalse(json.loads(output.getvalue())["idempotent"])

    def test_stale_row_and_wrong_reviewer_do_not_mutate(self):
        plan = self.plan()
        with self.store._connection() as db:
            db.execute(
                "UPDATE external_verifications SET reason='new observation' WHERE id=?",
                (self.identifier,),
            )
        with self.assertRaises(TaskConflict):
            self.apply(plan)
        with self.assertRaises(PolicyError):
            self.recovery.reconcile(
                self.run,
                self.identifier,
                reason="x",
                plan_digest=plan["plan_digest"],
                reviewed_by="other",
                idempotency_key="x",
            )
        self.assertIsNone(self.recovery._existing(self.store, self.identifier))

    def test_account_and_host_change_block(self):
        config = replace(self.config, github=replace(self.config.github, login="other"))
        self.assertIn(
            "task_account_changed",
            VerificationRecovery(config).plan(self.run, self.identifier, reason="x")[
                "reasons"
            ],
        )
        config = replace(
            self.config,
            github=replace(
                self.config.github, api_url="https://enterprise.invalid/api/v3"
            ),
        )
        self.assertIn(
            "task_host_changed",
            VerificationRecovery(config).plan(self.run, self.identifier, reason="x")[
                "reasons"
            ],
        )

    def test_missing_legacy_identity_and_unavailable_docker_remain_blocked(self):
        (self.directory / "execution.json").unlink()
        self.assertIn("execution_unconfirmed", self.plan()["reasons"])
        with patch(
            "reposteward.verification.recovery.observe",
            side_effect=RuntimeError("Docker unavailable"),
        ):
            self.assertFalse(self.plan()["eligible"])

    def test_different_retry_cannot_replace_receipt(self):
        plan = self.plan()
        self.apply(plan)
        with self.assertRaises(TaskConflict):
            self.recovery.reconcile(
                self.run,
                self.identifier,
                reason="changed",
                plan_digest=plan["plan_digest"],
                reviewed_by="owner",
                idempotency_key="recover-one",
            )
        config = replace(
            self.config,
            github=replace(self.config.github, api_url="https://different.invalid"),
        )
        with self.assertRaises(TaskConflict):
            VerificationRecovery(config).reconcile(
                self.run,
                self.identifier,
                reason="Recover interrupted verifier",
                plan_digest=plan["plan_digest"],
                reviewed_by="owner",
                idempotency_key="recover-one",
            )

    def test_transaction_failure_rolls_back_receipt_and_outcome(self):
        with self.store._connection() as db:
            db.execute(
                "CREATE TRIGGER fail_recovery BEFORE UPDATE ON external_verifications BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply(self.plan())
        self.assertIsNone(self.recovery._existing(self.store, self.identifier))
        self.assertEqual(
            self.check._row(self.store, self.run, self.identifier)["outcome"], "running"
        )

    def receipt(self):
        name = "reposteward-verify-" + "a" * 32
        with patch(
            "reposteward.verification.execution.docker", return_value="daemon-one\n"
        ):
            token = record_container(
                self.directory / "verification" / "00-test.log", name
            )
        return name, token

    def test_docker_absence_is_bound_to_same_daemon(self):
        self.receipt()
        with patch(
            "reposteward.verification.execution.docker", side_effect=["daemon-one", ""]
        ):
            self.assertEqual(
                observe(self.directory)["containers"][0]["state"], "absent"
            )
        with (
            patch(
                "reposteward.verification.execution.docker",
                return_value="different-daemon",
            ),
            self.assertRaises(ValueError),
        ):
            observe(self.directory)

    def test_live_or_replaced_container_is_not_reconciled(self):
        name, token = self.receipt()
        container = {
            "Id": "b" * 64,
            "Name": "/" + name,
            "Config": {"Labels": {"reposteward.execution": token}},
            "State": {"Status": "running"},
        }
        for status, label in (
            ("running", token),
            ("created", token),
            ("exited", "wrong"),
        ):
            container["State"]["Status"] = status
            container["Config"]["Labels"]["reposteward.execution"] = label
            with patch(
                "reposteward.verification.execution.docker",
                side_effect=["daemon-one", "b" * 64, json.dumps([container])],
            ):
                self.assertFalse(self.plan()["eligible"])
        container["State"]["Status"] = "exited"
        container["Config"]["Labels"]["reposteward.execution"] = token
        with patch(
            "reposteward.verification.execution.docker",
            side_effect=["daemon-one", "b" * 64, json.dumps([container])],
        ):
            self.assertTrue(self.plan()["eligible"])

    def test_container_intent_is_durable_before_start_and_labels_match(self):
        log = self.directory / "verification" / "intent.log"
        calls = []

        def run(args, **kwargs):
            if args[1] == "info":
                return subprocess.CompletedProcess(args, 0, "daemon-one", "")
            self.assertEqual(args[:2], ["docker", "run"])
            receipt = read_record(log.with_suffix(".container.json"))
            self.assertEqual(args[args.index("--name") + 1], receipt["name"])
            self.assertEqual(
                args[args.index("--label") + 1],
                "reposteward.execution=" + receipt["token"],
            )
            self.assertEqual(set(kwargs["env"]), {"PATH"})
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "ok", "")

        with patch("subprocess.run", side_effect=run):
            result = DockerVerifier._run_container(
                self.verifier, self.repo, "true", network=False, log_path=log
            )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(FileExistsError):
            write_record(self.directory / "execution.json", {"token": "overwrite"})
