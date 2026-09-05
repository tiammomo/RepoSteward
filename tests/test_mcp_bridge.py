from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import unittest
from dataclasses import asdict
from threading import Event
from unittest.mock import patch

import test_external_verification
from test_projects import git, repository

from reposteward.mcp_bridge import SCHEMAS, ScopedBridge, create_server
from reposteward.mcp_config import client_config
from reposteward.projects import ProjectError
from reposteward.verifier import DockerVerifier
from reposteward.workspace import sanitized_environment

HAS_MCP = importlib.util.find_spec("mcp") is not None


class BridgeTests(unittest.TestCase):
    container_run = test_external_verification.ExternalVerificationTests.container_run

    def setUp(self) -> None:
        test_external_verification.ExternalVerificationTests.setUp(self)
        self.bridge = ScopedBridge(self.config, self.repo)

    def test_understanding_is_read_only_and_matches_shared_service(self):
        from reposteward.understanding import Understanding

        service = Understanding(self.config.state_dir / "understanding")
        self.assertEqual(
            self.bridge.call("understanding", {"action": "guide"})["status"],
            "not_scanned",
        )
        service.scan(self.repo)
        self.assertEqual(
            self.bridge.call("understanding", {"action": "query", "focus": "source"}),
            service.guide(self.repo, focus="source"),
        )
        item = service.guide(self.repo, focus="source")["reading_path"][0]
        arguments = {
            "action": "evidence",
            "evidence_id": item["source"]["evidence_id"],
            "limit": 4,
        }
        self.assertEqual(
            self.bridge.call("understanding", arguments),
            service.evidence(self.repo, arguments["evidence_id"], limit=4),
        )
        with self.assertRaises(ValueError):
            self.bridge.call("understanding", {"action": "scan"})
        self.service.registry.unlink(self.task["binding_id"])
        with self.assertRaises(ProjectError):
            self.bridge.call("understanding", {"action": "guide"})

    def checkpoint_arguments(self) -> dict:
        current = self.service.inspect(self.task["run_id"], live=True)
        return {
            "run_id": self.task["run_id"],
            "expected_revision": current["revision"],
            "expected_snapshot": current["current_snapshot"]["digest"],
            "idempotency_key": "mcp-cp",
            "payload": {
                "remaining": ["Check the boundary"],
                "next_action": "inspect boundary",
                "notes": "Agent observation only",
            },
        }

    def test_application_facts_match_cli_services_and_preserve_open_work(self) -> None:
        run_id = self.task["run_id"]
        self.assertEqual(
            self.bridge.call("context", {"run_id": run_id, "live": True}),
            self.service.context(run_id, live=True),
        )
        saved = self.bridge.call("checkpoint", self.checkpoint_arguments())
        self.assertEqual(saved["revision"], 1)
        context = self.bridge.call("context", {})
        self.assertEqual(context["open_work"], ["Check the boundary"])
        self.assertEqual(context["agent_claims"]["trust"], "agent_unverified")
        evidence = self.bridge.call("evidence", {"run_id": run_id, "action": "list"})
        source = self.bridge.call(
            "evidence",
            {
                "run_id": run_id,
                "action": "get",
                "evidence_id": evidence["sources"][0]["evidence_id"],
                "limit": 20,
            },
        )
        self.assertEqual(source["returned"], 20)
        self.assertEqual(
            self.bridge.call("verification", {"run_id": run_id, "action": "profiles"})[
                "profiles"
            ][0]["name"],
            "test",
        )

    def test_all_tools_reject_arbitrary_paths_commands_and_authority_claims(
        self,
    ) -> None:
        for name in SCHEMAS:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.bridge.call(
                    name, {"path": "/etc/passwd", "command": "echo unsafe"}
                )
        args = self.checkpoint_arguments()
        args["payload"]["status"] = "ready"
        with self.assertRaises(ValueError):
            self.bridge.call("checkpoint", args)
        with self.assertRaises(ValueError):
            self.bridge.call("context", {"budget": True})
        with self.assertRaises(ValueError):
            self.bridge.call("project", {"value": "x" * 120001})

    def test_same_project_other_workspace_is_out_of_scope_and_unlink_revokes_access(
        self,
    ) -> None:
        other = repository(self.root / "other", remote="git@github.com:owner/repo.git")
        git(other, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(other, "switch", "-c", "owner/other")
        self.service.registry.link(other)
        self.github.branch_head_sha.return_value = git(other, "rev-parse", "HEAD")
        second = self.service.start(other, issue_number=7, reviewed_by="owner")
        for name, args in [
            ("context", {"run_id": second["run_id"]}),
            ("evidence", {"run_id": second["run_id"], "action": "list"}),
            ("verification", {"run_id": second["run_id"], "action": "profiles"}),
            ("checkpoint", {**self.checkpoint_arguments(), "run_id": second["run_id"]}),
        ]:
            with self.subTest(name=name), self.assertRaisesRegex(ProjectError, "scope"):
                self.bridge.call(name, args)
        self.service.registry.unlink(self.task["binding_id"])
        with self.assertRaises(ProjectError):
            self.bridge.call("project", {})

    def test_client_configuration_previews_are_distinct_and_do_not_write(self) -> None:
        before = set(self.repo.rglob("*"))
        for client, marker in (
            ("codex", "mcp_servers"),
            ("claude-code", "mcpServers"),
            ("copilot-vscode", '"servers"'),
        ):
            result = client_config(self.config, self.repo, client=client)
            self.assertIn(marker, result["fragment"])
            self.assertFalse(result["writes_files"])
            self.assertEqual(result["actual_session_validation"], "not_run")
        self.assertEqual(before, set(self.repo.rglob("*")))
        with self.assertRaises(ValueError):
            client_config(self.config, self.repo, client="unknown")

    @unittest.skipUnless(HAS_MCP, "install reposteward[mcp] for SDK protocol tests")
    def test_official_sdk_calls_all_capabilities(self) -> None:
        from mcp import Client

        async def run():
            async with Client(create_server(self.config, self.repo)) as client:
                listing = await client.list_tools()
                self.assertEqual({tool.name for tool in listing.tools}, set(SCHEMAS))
                project = await client.call_tool("project", {})
                self.assertEqual(
                    project.structured_content["project"]["id"], self.task["project_id"]
                )
                context = await client.call_tool(
                    "context", {"run_id": self.task["run_id"]}
                )
                self.assertEqual(
                    context.structured_content["run_id"], self.task["run_id"]
                )
                evidence = await client.call_tool(
                    "evidence", {"run_id": self.task["run_id"], "action": "list"}
                )
                self.assertTrue(evidence.structured_content["sources"])
                understanding = await client.call_tool(
                    "understanding", {"action": "guide"}
                )
                self.assertEqual(
                    understanding.structured_content["status"], "not_scanned"
                )
                cp = await client.call_tool("checkpoint", self.checkpoint_arguments())
                self.assertEqual(cp.structured_content["revision"], 1)
                current = self.service.inspect(self.task["run_id"], live=True)
                verified = await client.call_tool(
                    "verification",
                    {
                        "run_id": self.task["run_id"],
                        "action": "request",
                        "profile": "test",
                        "expected_revision": 1,
                        "expected_snapshot": current["current_snapshot"]["digest"],
                        "idempotency_key": "sdk-test",
                    },
                )
                self.assertEqual(verified.structured_content["outcome"], "passed")
                self.assertFalse(verified.structured_content["publication_eligible"])

        with (
            patch.object(DockerVerifier, "image_available", return_value=True),
            patch.object(
                DockerVerifier, "_run_container", side_effect=self.container_run
            ),
        ):
            asyncio.run(run())

    def stdio_environment(self) -> tuple[dict, list[str]]:
        user_root = self.root / "client-config"
        user_file = user_root / "reposteward/config.toml"
        user_file.parent.mkdir(parents=True)
        lines = [
            "config_version = 1",
            "[project]",
            f"state_dir = {json.dumps(str(self.config.state_dir))}",
            "namespace_state = false",
            "[github]",
            'login = "owner"',
            '[repositories."owner/repo"]',
        ]
        for key, value in asdict(self.config.repositories["owner/repo"]).items():
            if key != "name" and value is not None:
                lines.append(f"{key} = {json.dumps(value)}")
        user_file.write_text("\n".join(lines) + "\n")
        empty_project = self.root / "client-project.toml"
        empty_project.write_text("config_version = 1\n")
        environment = sanitized_environment(keep_codex_credentials=False)
        environment["XDG_CONFIG_HOME"] = str(user_root)
        arguments = [
            "-m",
            "reposteward.cli",
            "--config",
            str(empty_project),
            "mcp",
            "serve",
            str(self.repo),
        ]
        return environment, arguments

    @unittest.skipUnless(
        HAS_MCP, "install reposteward[mcp] for real STDIO protocol tests"
    )
    def test_real_stdio_legacy_and_modern_clients_and_clean_eof(self) -> None:
        from mcp import Client, StdioServerParameters

        environment, arguments = self.stdio_environment()

        async def run():
            for mode in ("legacy", "2026-07-28"):
                async with Client(
                    StdioServerParameters(
                        command=sys.executable, args=arguments, env=environment
                    ),
                    mode=mode,
                    read_timeout_seconds=10,
                ) as client:
                    listing = await client.list_tools()
                    self.assertEqual(
                        {tool.name for tool in listing.tools}, set(SCHEMAS)
                    )
                    result = await client.call_tool(
                        "context", {"run_id": self.task["run_id"]}
                    )
                    self.assertEqual(
                        result.structured_content["run_id"], self.task["run_id"]
                    )
                    self.assertEqual(
                        result.structured_content["contract"]["source_digest"],
                        self.task["context_pack"]["task_contract"]["source_digest"],
                    )

        asyncio.run(asyncio.wait_for(run(), timeout=35))

    @unittest.skipUnless(
        HAS_MCP, "install reposteward[mcp] for cancellation protocol tests"
    )
    def test_sdk_cancellation_reaches_worker_and_waits_for_cleanup(self) -> None:
        from mcp import Client

        from reposteward.external_verification import ExternalVerification

        started, cancelled, finished = Event(), Event(), Event()

        def waiting(*args, cancel_event, **kwargs):
            started.set()
            if cancel_event.wait(5):
                cancelled.set()
            finished.set()
            return {"outcome": "cancelled", "public_write": False}

        async def run():
            async with Client(create_server(self.config, self.repo)) as client:
                task = asyncio.create_task(
                    client.call_tool(
                        "verification",
                        {
                            "run_id": self.task["run_id"],
                            "action": "request",
                            "profile": "test",
                            "expected_revision": 0,
                            "expected_snapshot": self.task["snapshot"]["digest"],
                            "idempotency_key": "cancel-test",
                        },
                    )
                )
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        with patch.object(ExternalVerification, "request", side_effect=waiting):
            asyncio.run(asyncio.wait_for(run(), timeout=10))
        self.assertTrue(cancelled.is_set())
        self.assertTrue(finished.is_set())

    @unittest.skipUnless(HAS_MCP, "install reposteward[mcp] for strict transport tests")
    def test_unknown_version_and_oversized_wire_frame_fail_without_nonprotocol_stdout(
        self,
    ) -> None:
        environment, arguments = self.stdio_environment()

        async def run():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                *arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {"io.modelcontextprotocol/protocolVersion": "2999-01-01"}
                },
            }
            process.stdin.write((json.dumps(request) + "\n").encode())
            await process.stdin.drain()
            result = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
            self.assertIn("error", result)
            process.stdin.close()
            await asyncio.wait_for(process.communicate(), 5)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                *arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(b"x" * 120001 + b"\n"), 10
            )
            self.assertEqual(stdout, b"")
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"request limit", stderr)

        asyncio.run(asyncio.wait_for(run(), timeout=25))
