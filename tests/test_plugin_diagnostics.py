from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_projects import repository

from reposteward.cli import main
from reposteward.core.config import load_config
from reposteward.plugins.bundle import MANIFEST, PluginBundle
from reposteward.plugins.diagnostics import FILES, MAX_FILE_BYTES, PluginDiagnostics
from reposteward.projects.registry import ProjectRegistry, canonical_digest


class PluginDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = repository(self.root / "repo")
        path = self.root / "config.toml"
        path.write_text(
            "config_version = 1\n[project]\nnamespace_state = false\n"
            f"state_dir = {json.dumps(str(self.root / 'state'))}\n"
            '[github]\nlogin = "owner"\n[repositories."owner/repo"]\nenabled = true\n'
        )
        self.config = load_config(path)
        self.registry = ProjectRegistry(self.config.state_dir / "projects.sqlite3")
        self.registry.link(self.repo)
        self.bundle = self.root / "reposteward-test"
        service = PluginBundle(self.config)
        plan = service.plan(self.repo, output=self.bundle)
        service.export(self.repo, output=self.bundle, plan_digest=plan["plan_digest"])
        self.home = self.root / "codex"
        self.market = self.root / ".agents/plugins/marketplace.json"
        self.kwargs = {
            "bundle": self.bundle,
            "codex_home": self.home,
            "marketplace": self.market,
        }
        self.service = PluginDiagnostics(self.config)

    def doctor(self):
        return self.service.doctor(self.repo, **self.kwargs)

    def status(self, report, code):
        return next(item["status"] for item in report["checks"] if item["code"] == code)

    def register(self, *, enabled=True, installation="AVAILABLE"):
        self.market.parent.mkdir(parents=True, exist_ok=True)
        self.market.write_text(
            json.dumps(
                {
                    "name": "personal",
                    "plugins": [
                        {
                            "name": self.bundle.name,
                            "source": {"source": "local", "path": "./reposteward-test"},
                            "policy": {
                                "installation": installation,
                                "authentication": "ON_INSTALL",
                            },
                            "category": "Developer Tools",
                        }
                    ],
                }
            )
        )
        self.home.mkdir(exist_ok=True)
        (self.home / "config.toml").write_text(
            '[plugins."reposteward-test@personal"]\n'
            f"enabled = {str(enabled).lower()}\n"
            '[plugins.other]\nprivate_setting = "DO-NOT-EMIT"\n'
        )

    def test_offline_read_only_plan_and_exact_cache_do_not_claim_client_health(self):
        self.register()
        version = json.loads((self.bundle / MANIFEST).read_text())["version"]
        cache = self.home / "plugins/cache/personal/reposteward-test" / version
        shutil.copytree(self.bundle, cache)
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch(
            "reposteward.github.client.GitHubClient",
            side_effect=AssertionError("offline"),
        ):
            report = self.doctor()
            plan = self.service.install_plan(self.repo, **self.kwargs)
        self.assertTrue(report["bundle_compatible"])
        self.assertTrue(plan["ready_for_client_install"])
        self.assertEqual(report["client"]["cache"], "matches")
        self.assertEqual(report["client"]["effective_installation"], "not_probed")
        self.assertEqual(report["client"]["mcp_health"], "not_probed")
        self.assertEqual(report["actual_session_validation"], "not_run")
        self.assertEqual(
            plan["steps"][1]["argv"],
            ["codex", "plugin", "add", "reposteward-test@personal"],
        )
        self.assertNotIn("DO-NOT-EMIT", json.dumps(plan))
        self.assertEqual(
            before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        )
        self.assertEqual(plan, self.service.install_plan(self.repo, **self.kwargs))
        (cache / "README.md").write_text("stale")
        self.assertEqual(self.doctor()["client"]["cache"], "different_contents")

    def test_default_personal_marketplace_is_not_explicitly_added(self):
        self.register()
        with patch("reposteward.plugins.diagnostics.Path.home", return_value=self.root):
            plan = self.service.install_plan(self.repo, **self.kwargs)
        self.assertEqual(plan["steps"][0]["action"], "install")

    def test_missing_registration_or_disabled_policy_has_no_install_command(self):
        plan = self.service.install_plan(self.repo, **self.kwargs)
        self.assertFalse(plan["ready_for_client_install"])
        self.assertEqual(plan["steps"], [])
        self.register(installation="NOT_AVAILABLE")
        plan = self.service.install_plan(self.repo, **self.kwargs)
        self.assertFalse(plan["ready_for_client_install"])
        self.assertEqual(
            self.status(plan["observation"], "marketplace_installation_policy"),
            "warning",
        )
        self.register(enabled=False)
        plan = self.service.install_plan(self.repo, **self.kwargs)
        self.assertFalse(plan["ready_for_client_install"])
        self.assertEqual(plan["steps"][0]["action"], "review_disabled_plugin")
        self.assertFalse(any("argv" in step for step in plan["steps"]))

    def test_config_runtime_dependency_and_legacy_receipts(self):
        with self.config.path.open("a") as stream:
            stream.write("\n# reviewed configuration changed\n")
        report = self.doctor()
        self.assertFalse(report["bundle_compatible"])
        self.assertEqual(self.status(report, "configuration_snapshot"), "error")
        receipt_path = self.bundle / "export.json"
        receipt = json.loads(receipt_path.read_text())
        del receipt["schema_version"], receipt["config_digests"]
        receipt_path.write_text(json.dumps(receipt))
        report = self.doctor()
        self.assertTrue(report["bundle_compatible"])
        self.assertEqual(self.status(report, "configuration_snapshot"), "warning")
        with patch(
            "reposteward.plugins.bundle._runtime_digest", return_value="changed"
        ):
            report = self.doctor()
        self.assertEqual(self.status(report, "runtime_matches"), "error")
        with patch(
            "reposteward.plugins.diagnostics.importlib.util.find_spec",
            return_value=None,
        ):
            report = self.doctor()
        self.assertEqual(self.status(report, "mcp_available"), "error")

    def test_account_workspace_and_relink_require_new_export(self):
        account = replace(
            self.config, github=replace(self.config.github, login="someone")
        )
        report = PluginDiagnostics(account).doctor(self.repo, **self.kwargs)
        self.assertEqual(self.status(report, "workspace_binding"), "error")
        other = repository(self.root / "other")
        self.registry.link(other)
        report = self.service.doctor(other, **self.kwargs)
        self.assertEqual(self.status(report, "workspace_binding"), "error")
        self.registry.unlink(self.registry.inspect(self.repo)["binding"]["id"])
        self.assertFalse(self.doctor()["bundle_compatible"])
        self.registry.link(self.repo)
        self.assertEqual(self.status(self.doctor(), "workspace_binding"), "error")

    def test_untrusted_self_consistent_mcp_command_is_not_accepted_or_executed(self):
        sentinel = self.root / "executed"
        path = self.bundle / ".mcp.json"
        value = json.loads(path.read_text())
        value["mcpServers"][self.bundle.name] = {
            "command": "touch",
            "args": [str(sentinel)],
        }
        path.write_text(json.dumps(value))
        # An attacker can rewrite all hashes; compare against trusted templates too.
        content = {
            name: (self.bundle / name).read_text() for name in FILES if name != MANIFEST
        }
        path = self.bundle / MANIFEST
        manifest = json.loads(path.read_text())
        runtime = json.loads(content["connection.json"])["runtime"]["version"]
        manifest["version"] = f"{runtime}+bundle.{canonical_digest(content)[:12]}"
        path.write_text(json.dumps(manifest))
        path = self.bundle / "export.json"
        receipt = json.loads(path.read_text())
        receipt["files"] = {
            name: hashlib.sha256((self.bundle / name).read_bytes()).hexdigest()
            for name in FILES
        }
        path.write_text(json.dumps(receipt))
        report = self.doctor()
        self.assertEqual(self.status(report, "bundle_integrity"), "pass")
        self.assertEqual(self.status(report, "trusted_contents"), "error")
        self.assertFalse(report["bundle_compatible"])
        self.assertFalse(sentinel.exists())

    def test_unsafe_and_incomplete_files_fail_without_following_or_blocking(self):
        path = self.bundle / "README.md"
        original = path.read_bytes()
        for kind in ("symlink", "fifo", "large", "missing", "changed"):
            with self.subTest(kind=kind):
                path.unlink()
                if kind == "symlink":
                    path.symlink_to(self.config.path)
                elif kind == "fifo":
                    os.mkfifo(path)
                elif kind == "large":
                    path.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
                elif kind == "changed":
                    path.write_text("modified")
                self.assertEqual(
                    self.status(self.doctor(), "bundle_integrity"), "error"
                )
                path.unlink(missing_ok=True)
                path.write_bytes(original)
        extra = self.bundle / "hooks"
        extra.mkdir()
        self.assertFalse(self.doctor()["bundle_compatible"])
        extra.rmdir()
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        report = self.service.doctor(self.repo, bundle=alias / self.bundle.name)
        self.assertFalse(report["bundle_compatible"])

    def test_malformed_receipts_cannot_inject_paths(self):
        path = self.bundle / "export.json"
        original = json.loads(path.read_text())
        malformed = [
            "[]",
            '{"schema_version": 2, "schema_version": 2}',
            "[" * 2000 + "]" * 2000,
        ]
        original["files"]["../config.toml"] = "0" * 64
        malformed.append(json.dumps(original))
        for content in malformed:
            with self.subTest(content=content[:60]):
                path.write_text(content)
                self.assertFalse(self.doctor()["bundle_compatible"])

    def test_marketplace_ambiguity_and_invalid_client_setting_block_plan(self):
        self.register()
        catalog = json.loads(self.market.read_text())
        entry = catalog["plugins"][0]
        for change in ("duplicate", "traversal", "wrong_root", "bad_name", "bad_shape"):
            with self.subTest(change=change):
                value = json.loads(json.dumps(catalog))
                if change == "duplicate":
                    value["plugins"].append(entry)
                elif change == "traversal":
                    value["plugins"][0]["source"]["path"] = "./../reposteward-test"
                elif change == "wrong_root":
                    value["plugins"][0]["source"]["path"] = "./repo"
                elif change == "bad_name":
                    value["name"] = "../../elsewhere"
                else:
                    value["plugins"][0]["policy"] = []
                self.market.write_text(json.dumps(value))
                self.assertFalse(
                    self.service.install_plan(self.repo, **self.kwargs)[
                        "ready_for_client_install"
                    ]
                )
        self.register()
        (self.home / "config.toml").write_text("plugins = []\n")
        plan = self.service.install_plan(self.repo, **self.kwargs)
        self.assertFalse(plan["ready_for_client_install"])
        self.assertEqual(
            plan["observation"]["client"]["config_observation"], "unreadable"
        )

    def test_cli_exit_codes_distinguish_bundle_compatibility_from_install_readiness(
        self,
    ):
        arguments = ["--config", str(self.config.path), "plugin"]
        options = [
            str(self.repo),
            "--bundle",
            str(self.bundle),
            "--codex-home",
            str(self.home),
            "--marketplace",
            str(self.market),
        ]
        with patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(self.root / "no-user-config")}
        ):
            for action, expected in (("doctor", 0), ("install-plan", 2)):
                with redirect_stdout(io.StringIO()) as stdout:
                    code = main([*arguments, action, *options])
                self.assertEqual(code, expected)
                self.assertFalse(json.loads(stdout.getvalue())["writes_files"])
            (self.bundle / "README.md").write_text("broken")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([*arguments, "doctor", *options]), 2)
