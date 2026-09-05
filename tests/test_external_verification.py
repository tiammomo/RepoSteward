from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from threading import Event, Timer
from unittest.mock import patch

import test_external_tasks

from reposteward.cli import main
from reposteward.config import ConfigError, VerificationProfile, load_config
from reposteward.external_tasks import TaskConflict
from reposteward.external_verification import ExternalVerification
from reposteward.models import CommandResult
from reposteward.verifier import (
    DockerVerifier,
    VerificationCancelled,
    VerificationError,
)


class ExternalVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        test_external_tasks.ExternalTaskTests.setUp(self)
        policy = replace(
            self.config.repositories["owner/repo"],
            verification_prefixes=("python ",),
            required_verification_markers=(),
        )
        self.config = replace(
            self.config,
            repositories={"owner/repo": policy},
            verification_profiles=(
                VerificationProfile(
                    "owner/repo",
                    "test",
                    ("python -m unittest",),
                    ("python -m pip --version",),
                ),
            ),
        )
        self.service.config = self.config
        self.task = self.service.start(self.repo, issue_number=7, reviewed_by="owner")
        self.verifier = DockerVerifier(self.config)
        self.check = ExternalVerification(self.config, verifier=self.verifier)
        self.calls = []
        self.image = patch.object(self.verifier, "image_available", return_value=True)
        self.image.start()
        self.addCleanup(self.image.stop)
        self.container = patch.object(
            self.verifier, "_run_container", side_effect=self.container_run
        )
        self.mock_container = self.container.start()
        self.addCleanup(self.container.stop)

    def container_run(
        self, snapshot: Path, command: str, *, network: bool, log_path: Path, **kwargs
    ) -> CommandResult:
        self.calls.append((snapshot, command, network))
        self.assertNotEqual(snapshot, self.repo)
        self.assertEqual(
            (snapshot / "source.txt").read_text(),
            (self.repo / "source.txt").read_text(),
        )
        output = "verification output\n" * 300
        log_path.write_text(output)
        return CommandResult(
            command,
            0,
            output[-2000:],
            0.1,
            str(log_path),
            len(output),
            len(output),
            hashlib.sha256(output.encode()).hexdigest(),
            True,
            False,
        )

    def request(self, *, key: str = "one", revision: int = 0) -> dict:
        current = self.service.inspect(self.task["run_id"], live=True)
        return self.check.request(
            self.task["run_id"],
            profile="test",
            expected_revision=revision,
            expected_snapshot=current["current_snapshot"]["digest"],
            idempotency_key=key,
        )

    def test_dirty_snapshot_is_verified_in_actual_copy_and_remains_unpublishable(
        self,
    ) -> None:
        (self.repo / "source.txt").write_text("dirty code\n")
        (self.repo / "untracked.py").write_text("print('new')\n")
        result = self.request()
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(result["current_applicability"], "matches")
        self.assertTrue(result["snapshot"]["dirty"])
        self.assertFalse(result["publication_eligible"])
        self.assertEqual([call[2] for call in self.calls], [True, False])
        self.assertTrue(all(not call[0].exists() for call in self.calls))
        self.assertEqual(self.service.inspect(self.task["run_id"])["status"], "running")
        retry = self.request()
        self.assertTrue(retry["idempotent"])
        self.assertEqual(len(self.calls), 2)
        (self.repo / "source.txt").write_text("later edit\n")
        inspection = self.check.inspect(
            self.task["run_id"], result["evidence_id"].split(":")[1], live=True
        )
        self.assertEqual(inspection["outcome"], "passed")
        self.assertEqual(inspection["current_applicability"], "not_verified")
        self.assertIn("workspace_changed_since_verification", inspection["validity"])
        with self.assertRaisesRegex(TaskConflict, "different input"):
            self.request()

    def test_copy_corruption_is_detected_before_any_container_execution(self) -> None:
        original = self.verifier._copy_workspace

        def corrupted(source, target, **kwargs):
            copied = original(source, target, **kwargs)
            (target / "source.txt").write_text("corrupt copy")
            return copied

        with patch.object(self.verifier, "_copy_workspace", side_effect=corrupted):
            result = self.request()
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("differs", result["reason"])
        self.assertEqual(self.calls, [])

    def test_source_and_bootstrap_mutations_cannot_produce_success_evidence(
        self,
    ) -> None:
        def edit_source(snapshot, command, **kwargs):
            result = self.container_run(snapshot, command, **kwargs)
            (self.repo / "source.txt").write_text("edited while container ran")
            return result

        self.mock_container.side_effect = edit_source
        result = self.request()
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("changed during verification", result["reason"])
        self.assertEqual(len(self.calls), 1)
        self.calls.clear()

        def edit_copy(snapshot, command, **kwargs):
            result = self.container_run(snapshot, command, **kwargs)
            (snapshot / "source.txt").write_text("bootstrap replaced code")
            return result

        self.mock_container.side_effect = edit_copy
        result = self.request(key="two")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(len(self.calls), 1)

    def test_failures_cancellation_timeout_and_lost_terminal_record_are_distinct(
        self,
    ) -> None:
        def failed(snapshot, command, **kwargs):
            return replace(self.container_run(snapshot, command, **kwargs), exit_code=1)

        self.mock_container.side_effect = failed
        self.assertEqual(self.request()["outcome"], "failed")
        self.mock_container.side_effect = KeyboardInterrupt
        self.assertEqual(self.request(key="cancel")["outcome"], "cancelled")

        def timeout(snapshot, command, **kwargs):
            return replace(
                self.container_run(snapshot, command, **kwargs), exit_code=124
            )

        self.mock_container.side_effect = timeout
        result = self.request(key="timeout")
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("timed out", result["reason"])
        identifier = result["evidence_id"].split(":")[1]
        with self.service._store(write=True)._connection() as db:
            db.execute(
                "UPDATE external_verifications SET outcome='running' WHERE id=?",
                (identifier,),
            )
        self.assertEqual(
            self.check.inspect(self.task["run_id"], identifier)["outcome"], "unknown"
        )
        count = len(self.calls)
        self.assertEqual(self.request(key="timeout")["outcome"], "unknown")
        self.assertEqual(len(self.calls), count)

    def test_profile_revision_base_and_request_scope_are_rechecked(self) -> None:
        with self.assertRaisesRegex(TaskConflict, "revision"):
            self.request(revision=1)
        with self.assertRaisesRegex(VerificationError, "user-owned"):
            self.check.request(
                self.task["run_id"],
                profile="python -c arbitrary",
                expected_revision=0,
                expected_snapshot=self.task["snapshot"]["digest"],
                idempotency_key="bad",
            )
        result = self.request()
        identifier = result["evidence_id"].split(":")[1]
        changed = replace(
            self.config,
            verification_profiles=(
                VerificationProfile("owner/repo", "test", ("python -m unittest -v",)),
            ),
        )
        self.assertIn(
            "verification_profile_changed",
            ExternalVerification(changed).inspect(
                self.task["run_id"], identifier, live=True
            )["validity"],
        )
        other = self.service.start(self.repo, issue_number=7, reviewed_by="owner")
        with self.assertRaises(KeyError):
            self.check.inspect(other["run_id"], identifier)
        with self.assertRaises(KeyError):
            self.check.evidence(other["run_id"], f"log:{identifier}:0")
        current = self.service.inspect(self.task["run_id"], live=True)
        self.service.checkpoint(
            self.task["run_id"],
            expected_revision=0,
            expected_snapshot=current["current_snapshot"]["digest"],
            idempotency_key="cp",
            payload={"next_action": "continue"},
        )
        self.assertIn(
            "checkpoint_revision_changed",
            self.check.inspect(self.task["run_id"], identifier, live=True)["validity"],
        )

    def test_bounded_logs_source_lookup_missing_and_tampered_evidence(self) -> None:
        result = self.request()
        run_id = self.task["run_id"]
        identifier = result["evidence_id"].split(":")[1]
        report = self.check.evidence(run_id, f"log:{identifier}:0", limit=32)
        self.assertEqual(report["returned"], 32)
        self.assertEqual(report["next_offset"], 32)
        log = (
            self.check._directory(identifier)
            / result["result"]["commands"][0]["log"]["path"]
        )
        log.write_text("tampered")
        self.assertEqual(
            self.check.evidence(run_id, f"log:{identifier}:0")["availability"],
            "unknown",
        )
        log.unlink()
        self.assertEqual(
            self.check.evidence(run_id, f"log:{identifier}:0")["availability"],
            "unknown",
        )
        with self.service._store()._connection() as db:
            digest = db.execute(
                "SELECT digest FROM external_task_sources WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        self.assertEqual(
            self.check.evidence(run_id, f"source:{digest}")["availability"], "available"
        )
        self.assertEqual(
            self.check.evidence(run_id, f"checkpoint:{run_id}:0")["availability"],
            "available",
        )
        with self.assertRaises(KeyError):
            self.check.evidence(run_id, "source:" + "f" * 64)
        with self.assertRaises(ValueError):
            self.check.evidence(run_id, result["evidence_id"], limit=16001)
        self.assertEqual(self.check.list(run_id, limit=1)["omitted"], 0)

    def test_trusted_profile_config_ignores_repository_override(self) -> None:
        user = self.root / "user.toml"
        project = self.root / "repo.toml"
        stanza = '\n[[verification_profiles]]\nrepository="owner/repo"\nname="test"\ncommands=["python -m unittest"]\n'
        user.write_text(user.read_text() + stanza)
        project.write_text(
            project.read_text() + stanza.replace("python -m unittest", "python evil.py")
        )
        config = load_config(project, user_path=user)
        self.assertEqual(
            config.verification_profiles[0].commands, ("python -m unittest",)
        )
        standalone = self.root / "standalone.toml"
        standalone.write_text(project.read_text() + '\n[github]\nlogin="owner"\n')
        self.assertEqual(load_config(standalone).verification_profiles, ())
        user.write_text(user.read_text() + stanza)
        with self.assertRaisesRegex(ConfigError, "duplicate"):
            load_config(project, user_path=user)

    def test_read_only_cli_does_not_start_pipeline_or_harness(self) -> None:
        self.request()
        output = io.StringIO()
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            patch("reposteward.cli.Pipeline", side_effect=AssertionError("Pipeline")),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["verification", "list", self.task["run_id"]]), 0)
        self.assertEqual(
            json.loads(output.getvalue())["evidence"][0]["outcome"], "passed"
        )

    def test_timeout_removes_exact_docker_container_with_minimal_environment(
        self,
    ) -> None:
        self.container.stop()
        self.addCleanup(lambda: None)
        timeout = subprocess.TimeoutExpired(["docker"], 1, output=b"partial")
        with (
            patch(
                "reposteward.verifier.subprocess.run",
                side_effect=[timeout, subprocess.CompletedProcess([], 0)],
            ) as run,
            patch.dict(os.environ, {"GITHUB_TOKEN": "sentinel-never-forward"}),
        ):
            result = self.verifier._run_container(
                self.repo, "python -m unittest", network=False
            )
        self.assertEqual(result.exit_code, 124)
        first, second = run.call_args_list
        docker = first.args[0]
        self.assertEqual(
            second.args[0],
            ["docker", "rm", "--force", docker[docker.index("--name") + 1]],
        )
        self.assertNotIn("GITHUB_TOKEN", first.kwargs["env"])
        self.assertNotIn("GITHUB_TOKEN", second.kwargs["env"])

    def test_cancel_event_stops_before_execution_and_persists_cancelled_evidence(
        self,
    ) -> None:
        cancelled = Event()
        cancelled.set()
        result = self.check.request(
            self.task["run_id"],
            profile="test",
            expected_revision=0,
            expected_snapshot=self.task["snapshot"]["digest"],
            idempotency_key="client-cancel",
            cancel_event=cancelled,
        )
        self.assertEqual(result["outcome"], "cancelled")
        self.assertEqual(self.calls, [])

    def test_cancellable_process_is_reaped_when_client_disconnects(self) -> None:
        cancelled = Event()
        timer = Timer(0.1, cancelled.set)
        timer.start()
        try:
            with self.assertRaises(VerificationCancelled):
                self.verifier._cancellable_command(
                    [sys.executable, "-c", "import time; time.sleep(30)"], cancelled, 1
                )
        finally:
            timer.cancel()
            timer.join()
