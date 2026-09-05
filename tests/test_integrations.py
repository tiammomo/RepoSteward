from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_projects import repository

from reposteward.cli import main
from reposteward.integrations import (
    SHARED_PATH,
    SHARED_TEXT,
    AgentIntegration,
    IntegrationConflict,
)


class IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = repository(self.root / "checkout")
        self.state = self.root / "state"
        self.service = AgentIntegration(self.state)
        self.service.registry.link(self.repo)

    def apply(self, client: str = "codex", *, revert: bool = False) -> dict:
        plan = self.service.plan(self.repo, client=client, revert=revert)
        return self.service.apply(
            self.repo, client=client, plan_digest=plan["plan_digest"], revert=revert
        )

    def test_preview_keeps_existing_scoped_rules_and_creates_no_files(self) -> None:
        (self.repo / "AGENTS.md").write_text("Keep this root rule.\n")
        (self.repo / "subdir").mkdir()
        (self.repo / "subdir/AGENTS.md").write_text("Nested rule.\n")
        plan = self.service.plan(self.repo, client="codex")
        self.assertEqual(
            [op["path"] for op in plan["operations"]], [SHARED_PATH, "AGENTS.md"]
        )
        self.assertEqual(
            {rule["path"] for rule in plan["rules"]["files"]},
            {"AGENTS.md", "subdir/AGENTS.md"},
        )
        self.assertNotIn("Keep this root rule", json.dumps(plan))
        self.assertFalse((self.repo / SHARED_PATH).exists())
        self.assertFalse((self.state / "integrations").exists())
        self.assertEqual(
            (self.repo / "AGENTS.md").read_text(), "Keep this root rule.\n"
        )

    def test_apply_retry_and_revert_preserve_user_edits_and_permissions(self) -> None:
        agents = self.repo / "AGENTS.md"
        original = "Original instructions without final newline"
        agents.write_text(original)
        agents.chmod(0o640)
        plan = self.service.plan(self.repo, client="codex")
        result = self.service.apply(
            self.repo, client="codex", plan_digest=plan["plan_digest"]
        )
        self.assertEqual(result["health"], {"codex": "ok", "shared": "ok"})
        retry = self.service.apply(
            self.repo, client="codex", plan_digest=plan["plan_digest"]
        )
        self.assertTrue(retry["idempotent"])
        agents.write_text("New rule before\n" + agents.read_text() + "New rule after\n")
        self.apply(revert=True)
        self.assertEqual(
            agents.read_text(), "New rule before\n" + original + "New rule after\n"
        )
        self.assertEqual(agents.stat().st_mode & 0o777, 0o640)
        self.assertFalse((self.repo / SHARED_PATH).exists())
        self.assertEqual(self.apply(revert=True)["clients"], [])

    def test_multiple_clients_share_one_entry_and_revert_only_owned_files(self) -> None:
        for client in ("codex", "claude-code", "copilot-vscode"):
            self.apply(client)
        self.assertIn(
            "@.agents/reposteward-context.md", (self.repo / "CLAUDE.md").read_text()
        )
        self.assertIn(
            "#file:", (self.repo / ".github/copilot-instructions.md").read_text()
        )
        self.apply("claude-code", revert=True)
        self.assertFalse((self.repo / "CLAUDE.md").exists())
        self.assertTrue((self.repo / SHARED_PATH).exists())
        self.apply("codex", revert=True)
        self.apply("copilot-vscode", revert=True)
        self.assertFalse((self.repo / SHARED_PATH).exists())
        self.assertFalse((self.repo / "AGENTS.md").exists())

    def test_preexisting_identical_shared_file_is_not_deleted(self) -> None:
        shared = self.repo / SHARED_PATH
        shared.parent.mkdir(parents=True)
        shared.write_text(SHARED_TEXT)
        self.apply()
        self.apply(revert=True)
        self.assertEqual(shared.read_text(), SHARED_TEXT)

    def test_stale_plan_and_managed_drift_preserve_user_content(self) -> None:
        plan = self.service.plan(self.repo, client="codex")
        agents = self.repo / "AGENTS.md"
        agents.write_text("User edit after preview")
        with self.assertRaisesRegex(IntegrationConflict, "stale"):
            self.service.apply(
                self.repo, client="codex", plan_digest=plan["plan_digest"]
            )
        self.assertFalse((self.repo / SHARED_PATH).exists())
        self.apply()
        agents.write_text(agents.read_text().replace("Read `", "Edited `"))
        content = agents.read_text()
        self.assertEqual(self.service.inspect(self.repo)["health"]["codex"], "drift")
        with self.assertRaisesRegex(IntegrationConflict, "drifted"):
            self.service.plan(self.repo, client="codex", revert=True)
        self.assertEqual(agents.read_text(), content)

    def test_symlinks_unowned_markers_and_unknown_clients_are_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.repo / ".agents").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(IntegrationConflict, "symlink"):
            self.service.plan(self.repo, client="codex")
        self.assertEqual(list(outside.iterdir()), [])
        (self.repo / ".agents").unlink()
        (self.repo / "AGENTS.md").write_text("<!-- reposteward:unknown -->")
        with self.assertRaisesRegex(IntegrationConflict, "unowned"):
            self.service.plan(self.repo, client="codex")
        with self.assertRaisesRegex(IntegrationConflict, "unsupported"):
            self.service.plan(self.repo, client="imaginary-client")

    def test_interrupted_apply_resumes_exact_journal_after_a_completed_file_write(
        self,
    ) -> None:
        plan = self.service.plan(self.repo, client="codex")
        write = self.service._write_operation
        count = 0

        def interrupted(root: Path, operation: dict) -> None:
            nonlocal count
            write(root, operation)
            count += 1
            if count == 1:
                raise OSError("simulated interruption after rename")

        with (
            patch.object(self.service, "_write_operation", side_effect=interrupted),
            self.assertRaises(OSError),
        ):
            self.service.apply(
                self.repo, client="codex", plan_digest=plan["plan_digest"]
            )
        self.assertTrue((self.repo / SHARED_PATH).exists())
        self.assertEqual(
            self.service.inspect(self.repo)["pending_plan"], plan["plan_digest"]
        )
        with self.assertRaisesRegex(IntegrationConflict, "original"):
            self.service.apply(
                self.repo, client="claude-code", plan_digest=plan["plan_digest"]
            )
        result = self.service.apply(
            self.repo, client="codex", plan_digest=plan["plan_digest"]
        )
        self.assertIsNone(result["pending_plan"])
        self.assertEqual(result["clients"], ["codex"])
        self.assertEqual((self.repo / "AGENTS.md").read_text().count(":begin -->"), 1)

    def test_interrupted_apply_does_not_overwrite_new_user_edits(self) -> None:
        plan = self.service.plan(self.repo, client="codex")
        with (
            patch.object(
                self.service, "_write_operation", side_effect=OSError("interrupted")
            ),
            self.assertRaises(OSError),
        ):
            self.service.apply(
                self.repo, client="codex", plan_digest=plan["plan_digest"]
            )
        shared = self.repo / SHARED_PATH
        shared.parent.mkdir()
        shared.write_text("User content after interruption")
        with self.assertRaisesRegex(IntegrationConflict, "preserved"):
            self.service.apply(
                self.repo, client="codex", plan_digest=plan["plan_digest"]
            )
        self.assertEqual(shared.read_text(), "User content after interruption")
        self.assertIsNotNone(self.service.inspect(self.repo)["pending_plan"])

    def test_binding_scope_and_read_only_cli_require_no_pipeline(self) -> None:
        other = repository(self.root / "other", remote="git@github.com:owner/other.git")
        self.service.registry.link(other)
        plan = self.service.plan(self.repo, client="codex")
        with self.assertRaisesRegex(IntegrationConflict, "stale"):
            self.service.apply(other, client="codex", plan_digest=plan["plan_digest"])
        output = io.StringIO()
        with (
            patch(
                "reposteward.cli.load_config",
                return_value=SimpleNamespace(state_dir=self.state),
            ),
            patch(
                "reposteward.cli.Pipeline",
                side_effect=AssertionError("Pipeline started"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(["integration", "plan", str(self.repo), "--client", "codex"]), 0
            )
        report = json.loads(output.getvalue())
        self.assertFalse(report["public_write"])
        self.assertNotIn(str(self.state), output.getvalue())
        self.assertFalse((self.repo / SHARED_PATH).exists())
