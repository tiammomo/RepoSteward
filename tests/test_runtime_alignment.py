from __future__ import annotations

import io
import json
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from unittest.mock import patch

import test_plugin_diagnostics

from reposteward.cli import main
from reposteward.core.runtime_alignment import alignment_report
from reposteward.plugins.diagnostics import PluginDiagnostics
from reposteward.storage.store import SCHEMA_VERSION, Store


class RuntimeAlignmentTests(unittest.TestCase):
    register = test_plugin_diagnostics.PluginDiagnosticsTests.register

    def setUp(self):
        test_plugin_diagnostics.PluginDiagnosticsTests.setUp(self)
        self.register()

    def report(self, **kwargs):
        return alignment_report(
            self.config, workspace=self.repo, **self.kwargs, **kwargs
        )

    def test_matching_runtime_and_bundle_preserve_state_without_claiming_health(self):
        before = {
            p: p.read_bytes() for p in self.config.state_dir.iterdir() if p.is_file()
        }
        with patch(
            "reposteward.github.client.resolve_authentication",
            side_effect=AssertionError("credentials"),
        ):
            report, ok = self.report()
        self.assertTrue(ok)
        self.assertEqual(report["compatibility"]["status"], "static_match")
        self.assertEqual(report["compatibility"]["client_health"], "not_probed")
        self.assertEqual(
            before,
            {p: p.read_bytes() for p in self.config.state_dir.iterdir() if p.is_file()},
        )

    def test_older_schema_and_missing_bundle_have_actionable_results(self):
        path = self.config.state_dir / "reposteward.sqlite3"
        Store(path)
        with closing(sqlite3.connect(path)) as db:
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
            db.commit()
        before = path.read_bytes()
        report, ok = self.report()
        self.assertFalse(ok)
        self.assertEqual(report["databases"]["tasks"]["status"], "migration_required")
        self.assertTrue(report["plugin"]["bundle_compatible"])
        self.assertEqual(path.read_bytes(), before)
        self.kwargs["bundle"] = self.root / "missing"
        report, ok = self.report()
        self.assertFalse(ok)
        self.assertFalse(report["compatibility"]["bundle_compatible"])
        self.assertTrue(report["next_actions"])

    def test_state_scope_mismatch_skips_all_plugin_reads(self):
        with patch.object(
            PluginDiagnostics, "doctor", side_effect=AssertionError("plugin")
        ):
            report, ok = self.report(expected_state_dir=self.root / "wrong-state")
        self.assertFalse(ok)
        self.assertIsNone(report["plugin"])
        self.assertEqual(report["compatibility"]["status"], "not_checked")

    def test_missing_config_skips_plugin(self):
        with patch.object(
            PluginDiagnostics, "doctor", side_effect=AssertionError("plugin")
        ):
            report, ok = alignment_report(None, workspace=self.repo, bundle=self.bundle)
        self.assertFalse(ok)
        self.assertEqual(report["configuration"]["status"], "unavailable")

    def test_disabled_client_is_reported_separately_from_static_compatibility(self):
        (self.home / "config.toml").write_text(
            '[plugins."reposteward-test@personal"]\nenabled = false\n'
        )
        report, ok = self.report()
        self.assertTrue(ok)
        self.assertFalse(report["compatibility"]["client_enabled"])
        self.assertTrue(report["next_actions"])

    def test_cli_combines_machine_output_and_refuses_ambiguous_selection(self):
        output = io.StringIO()
        args = [
            "--json-envelope",
            "doctor",
            "--local",
            "--workspace",
            str(self.repo),
            "--bundle",
            str(self.bundle),
            "--marketplace",
            str(self.market),
            "--codex-home",
            str(self.home),
        ]
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            redirect_stdout(output),
        ):
            code = main(args)
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(output.getvalue())["data"]["compatibility"]["status"],
            "static_match",
        )
        for tail in (
            ("--workspace", str(self.repo)),
            ("--bundle", str(self.bundle)),
            ("--marketplace", str(self.market)),
        ):
            with (
                patch(
                    "reposteward.cli.load_config", side_effect=AssertionError("config")
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    main(["--json-envelope", "doctor", "--local", *tail]), 2
                )
