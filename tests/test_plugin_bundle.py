from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_projects import git, repository

from reposteward.cli import main
from reposteward.core.config import load_config
from reposteward.integrations.mcp import SCHEMAS, ScopedBridge, create_server
from reposteward.plugins.bundle import MANIFEST, SKILLS, PluginBundle
from reposteward.projects.registry import ProjectError, ProjectRegistry
from reposteward.storage.workspace import sanitized_environment


class PluginBundleTests(unittest.TestCase):
    def setUp(self) -> None:
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
        self.service = PluginBundle(self.config)
        self.output = self.root / "reposteward-test"

    def export(self) -> dict:
        plan = self.service.plan(self.repo, output=self.output)
        return self.service.export(
            self.repo, output=self.output, plan_digest=plan["plan_digest"]
        )

    def test_plan_is_read_only_and_export_contains_exact_reviewed_content(self) -> None:
        before = self.registry.path.read_bytes()
        with patch(
            "reposteward.github.client.GitHubClient",
            side_effect=AssertionError("offline"),
        ):
            plan = self.service.plan(self.repo, output=self.output)
            self.assertEqual(plan, self.service.plan(self.repo, output=self.output))
            self.assertFalse(self.output.exists())
            result = self.service.export(
                self.repo, output=self.output, plan_digest=plan["plan_digest"]
            )
        self.assertEqual(before, self.registry.path.read_bytes())
        self.assertFalse((self.config.state_dir / "reposteward.sqlite3").exists())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertTrue(result["exported"])
        self.assertEqual(result["client_installation"], "not_attempted")
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for name, item in plan["files"].items():
            self.assertEqual((self.output / name).read_text(), item["content"])
            self.assertEqual(
                hashlib.sha256((self.output / name).read_bytes()).hexdigest(),
                item["sha256"],
            )
            self.assertEqual((self.output / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            len(list((self.output / "skills").glob("*/SKILL.md"))), len(SKILLS)
        )
        manifest = json.loads((self.output / MANIFEST).read_text())
        self.assertEqual(manifest["name"], self.output.name)
        self.assertNotIn(
            "env",
            json.loads((self.output / ".mcp.json").read_text())["mcpServers"][
                self.output.name
            ],
        )

    def test_config_runtime_and_output_changes_invalidate_plan(self) -> None:
        plan = self.service.plan(self.repo, output=self.output)
        with (
            patch("reposteward.plugins.bundle._runtime_digest", return_value="changed"),
            self.assertRaisesRegex(ProjectError, "plan changed"),
        ):
            self.service.export(
                self.repo, output=self.output, plan_digest=plan["plan_digest"]
            )
        with self.assertRaisesRegex(ProjectError, "plan changed"):
            self.service.export(
                self.repo,
                output=self.root / "other-plugin",
                plan_digest=plan["plan_digest"],
            )
        with self.config.path.open("a") as stream:
            stream.write("\n# configuration changed after review\n")
        with self.assertRaisesRegex(ProjectError, "plan changed"):
            self.service.export(
                self.repo, output=self.output, plan_digest=plan["plan_digest"]
            )
        self.assertFalse(self.output.exists())

    def test_existing_paths_links_and_workspace_outputs_are_never_overwritten(
        self,
    ) -> None:
        with self.assertRaisesRegex(ProjectError, "outside"):
            self.service.plan(self.repo, output=self.repo / "plugin")
        for target in (self.repo, self.root / "absent"):
            self.output.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ProjectError, "already exists"):
                self.service.plan(self.repo, output=self.output)
            self.output.unlink()
        self.output.mkdir()
        sentinel = self.output / "user-content"
        sentinel.write_text("preserve")
        with self.assertRaisesRegex(ProjectError, "already exists"):
            self.service.plan(self.repo, output=self.output)
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_late_destination_creation_preserves_other_writer(self) -> None:
        plan = self.service.plan(self.repo, output=self.output)
        real_mkdir = os.mkdir

        def racing_mkdir(path, *args, **kwargs):
            if path == self.output.name:
                real_mkdir(path, *args, **kwargs)
                (self.output / "other-writer").write_text("keep")
            return real_mkdir(path, *args, **kwargs)

        with (
            patch("reposteward.plugins.bundle.os.mkdir", side_effect=racing_mkdir),
            self.assertRaises(FileExistsError),
        ):
            self.service.export(
                self.repo, output=self.output, plan_digest=plan["plan_digest"]
            )
        self.assertEqual([p.name for p in self.output.iterdir()], ["other-writer"])

    def test_incomplete_export_has_no_client_manifest_and_is_not_overwritten(
        self,
    ) -> None:
        plan = self.service.plan(self.repo, output=self.output)
        real_write = self.service._write

        def interrupted(fd, name, content):
            if name == "connection.json":
                raise OSError("disk failure")
            return real_write(fd, name, content)

        with (
            patch.object(self.service, "_write", side_effect=interrupted),
            self.assertRaisesRegex(OSError, "disk failure"),
        ):
            self.service.export(
                self.repo, output=self.output, plan_digest=plan["plan_digest"]
            )
        self.assertFalse((self.output / MANIFEST).exists())
        self.assertTrue((self.output / ".mcp.json").is_file())
        with self.assertRaisesRegex(ProjectError, "already exists"):
            self.export()

    def test_missing_mcp_is_diagnosed_before_any_write(self) -> None:
        with patch(
            "reposteward.plugins.bundle.importlib.util.find_spec", return_value=None
        ):
            plan = self.service.plan(self.repo, output=self.output)
            self.assertFalse(plan["diagnostics"]["mcp_available"])
            with self.assertRaisesRegex(ProjectError, "reposteward\\[mcp\\]"):
                self.service.export(
                    self.repo, output=self.output, plan_digest=plan["plan_digest"]
                )
        self.assertFalse(self.output.exists())

    def test_saved_scope_rejects_rebinding_another_workspace_or_account(self) -> None:
        running_bridge = ScopedBridge(self.config, self.repo)
        expected = running_bridge.scope_digest()
        create_server(self.config, self.repo, expected_scope=expected)
        other = repository(self.root / "other")
        self.registry.link(other)
        with self.assertRaisesRegex(ProjectError, "scope changed"):
            create_server(self.config, other, expected_scope=expected)
        account = replace(
            self.config, github=replace(self.config.github, login="someone")
        )
        with self.assertRaisesRegex(ProjectError, "scope changed"):
            create_server(account, self.repo, expected_scope=expected)
        linked = self.registry.inspect(self.repo)
        self.registry.unlink(linked["binding"]["id"])
        with self.assertRaises(ProjectError):
            create_server(self.config, self.repo, expected_scope=expected)
        self.registry.link(self.repo)
        with self.assertRaisesRegex(ProjectError, "binding changed"):
            running_bridge.call("project", {})
        with self.assertRaisesRegex(ProjectError, "scope changed"):
            create_server(self.config, self.repo, expected_scope=expected)
        # Existing unpinned MCP configuration stays usable after explicit rebinding.
        create_server(self.config, self.repo)

    def test_cli_export_and_generated_stdio_configuration(self) -> None:
        from mcp import Client, StdioServerParameters

        environment = sanitized_environment(keep_codex_credentials=False)
        environment["XDG_CONFIG_HOME"] = str(self.root / "no-user-config")
        with patch.dict(os.environ, environment, clear=True):
            arguments = ["--config", str(self.config.path), "plugin"]
            with redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(
                    main(
                        [
                            *arguments,
                            "plan",
                            str(self.repo),
                            "--output",
                            str(self.output),
                        ]
                    ),
                    0,
                )
            plan = json.loads(stdout.getvalue())
            with redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(
                    main(
                        [
                            *arguments,
                            "export",
                            str(self.repo),
                            "--output",
                            str(self.output),
                            "--plan-digest",
                            plan["plan_digest"],
                        ]
                    ),
                    0,
                )
            entry = json.loads((self.output / ".mcp.json").read_text())["mcpServers"][
                self.output.name
            ]

            async def run():
                async with Client(
                    StdioServerParameters(**entry, env=environment)
                ) as client:
                    available = await client.list_tools()
                    self.assertEqual({t.name for t in available.tools}, set(SCHEMAS))
                    result = await client.call_tool("project", {})
                    self.assertEqual(
                        result.structured_content["project"]["repository"], "owner/repo"
                    )
                    guide = await client.call_tool("understanding", {"action": "guide"})
                    self.assertEqual(guide.structured_content["status"], "not_scanned")
                    unrelated = await client.call_tool(
                        "project", {"path": str(self.root / "other")}
                    )
                    self.assertTrue(unrelated.is_error)

            asyncio.run(asyncio.wait_for(run(), timeout=20))
