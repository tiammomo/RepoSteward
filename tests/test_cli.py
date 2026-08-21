from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from reposteward.cli import main
from reposteward.config import load_config


class CliSetupTests(unittest.TestCase):
    def test_init_and_repo_add_form_a_complete_configuration(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            user = root / "user.toml"
            project = root / "project" / ".reposteward.toml"
            project.parent.mkdir()
            output = io.StringIO()

            with redirect_stdout(output):
                init_code = main(
                    [
                        "init",
                        "--path",
                        str(user),
                        "--login",
                        "alice",
                    ]
                )
                repo_code = main(
                    [
                        "repo",
                        "add",
                        "owner/repo",
                        "--path",
                        str(project),
                    ]
                )

            config = load_config(project, user_path=user)
            documents = output.getvalue().split("}\n{")

        self.assertEqual(init_code, 0)
        self.assertEqual(repo_code, 0)
        self.assertEqual(config.github.login, "alice")
        self.assertIn("owner/repo", config.repositories)
        self.assertEqual(len(documents), 2)

    def test_init_reports_machine_readable_json(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "user.toml"
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main(["init", "--path", str(path), "--login", "alice"])
            payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["github_login"], "alice")

    def test_issue_draft_is_local_only(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_home = root / "config"
            user = config_home / "reposteward" / "config.toml"
            project = root / "project" / ".reposteward.toml"
            project.parent.mkdir()
            initialize_args = [
                "init",
                "--path",
                str(user),
                "--login",
                "alice",
            ]
            output = io.StringIO()
            with patch.dict(
                "os.environ",
                {
                    "XDG_CONFIG_HOME": str(config_home),
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_DATA_HOME": str(root / "data"),
                },
            ):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(initialize_args), 0)
                    self.assertEqual(
                        main(
                            [
                                "repo",
                                "add",
                                "owner/repo",
                                "--path",
                                str(project),
                            ]
                        ),
                        0,
                    )
                with redirect_stdout(output):
                    exit_code = main(
                        [
                            "--config",
                            str(project),
                            "issue",
                            "draft",
                            "owner/repo",
                            "--title",
                            "Example bug",
                            "--summary",
                            "A summary",
                            "--actual",
                            "Actual behavior",
                            "--expected",
                            "Expected behavior",
                        ]
                    )
            payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["public_write"])
        self.assertIn("## Summary", payload["body"])

    def test_portfolio_inspect_supports_json_and_text(self) -> None:
        result = {
            "snapshot_digest": "a" * 64,
            "expected_digest": "",
            "matches_expected_digest": None,
            "snapshot": {
                "repository": "owner/repo",
                "complete": True,
                "pull_requests": [],
                "overlaps": [],
                "errors": [],
                "stats": {
                    "pull_requests": 0,
                    "draft_pull_requests": 0,
                    "overlapping_pairs": 0,
                },
            },
            "harness_invoked": False,
            "workspace_modified": False,
            "public_write": False,
        }
        pipeline = MagicMock()
        pipeline.portfolio_snapshot.return_value = result

        json_output = io.StringIO()
        text_output = io.StringIO()
        with (
            patch("reposteward.cli.load_config", return_value=object()),
            patch("reposteward.cli.Pipeline", return_value=pipeline),
        ):
            with redirect_stdout(json_output):
                json_code = main(["portfolio", "inspect", "owner/repo"])
            with redirect_stdout(text_output):
                text_code = main(
                    ["portfolio", "inspect", "owner/repo", "--format", "text"]
                )

        self.assertEqual(json_code, 0)
        self.assertEqual(text_code, 0)
        self.assertEqual(json.loads(json_output.getvalue()), result)
        self.assertIn("Portfolio: owner/repo", text_output.getvalue())
        pipeline.portfolio_snapshot.assert_called_with("owner/repo", expected_digest="")

    def test_portfolio_plan_and_dependency_attestation_are_routed(self) -> None:
        plan = {
            "plan_digest": "a" * 64,
            "expected_digest": "",
            "matches_expected_digest": None,
            "plan": {
                "repository": "owner/repo",
                "portfolio_snapshot_digest": "b" * 64,
                "complete": True,
                "suggested_merge_order": [1],
                "authoritative_edges": [],
                "ready_blockers": {},
                "suggestions": [],
            },
        }
        pipeline = MagicMock()
        pipeline.portfolio_dependency_plan.return_value = plan
        pipeline.attest_portfolio_dependency.return_value = {
            "action": "confirm",
            "public_write": False,
        }
        pipeline.portfolio_dependency_events.return_value = {
            "events": [],
            "public_write": False,
        }
        plan_output = io.StringIO()
        dependency_output = io.StringIO()
        list_output = io.StringIO()
        with (
            patch("reposteward.cli.load_config", return_value=object()),
            patch("reposteward.cli.Pipeline", return_value=pipeline),
        ):
            with redirect_stdout(plan_output):
                plan_code = main(
                    ["portfolio", "plan", "owner/repo", "--format", "text"]
                )
            with redirect_stdout(dependency_output):
                dependency_code = main(
                    [
                        "portfolio",
                        "dependency",
                        "confirm",
                        "owner/repo",
                        "2",
                        "1",
                        "--reviewed-by",
                        "alice",
                    ]
                )
            with redirect_stdout(list_output):
                list_code = main(
                    [
                        "portfolio",
                        "dependency",
                        "list",
                        "owner/repo",
                        "--pull-number",
                        "2",
                    ]
                )

        self.assertEqual(plan_code, 0)
        self.assertEqual(dependency_code, 0)
        self.assertEqual(list_code, 0)
        self.assertIn("Dependency plan: owner/repo", plan_output.getvalue())
        pipeline.attest_portfolio_dependency.assert_called_once_with(
            "owner/repo",
            pull_number=2,
            dependency_number=1,
            action="confirm",
            reviewed_by="alice",
        )
        pipeline.portfolio_dependency_events.assert_called_once_with(
            "owner/repo", pull_number=2, limit=100
        )

    def test_ci_diagnose_is_routed_as_a_read_only_command(self) -> None:
        pipeline = MagicMock()
        pipeline.ci_failure_analysis.return_value = {
            "analysis_digest": "a" * 64,
            "failures": [],
            "public_write": False,
        }
        output = io.StringIO()

        with (
            patch("reposteward.cli.load_config", return_value=object()),
            patch("reposteward.cli.Pipeline", return_value=pipeline),
            redirect_stdout(output),
        ):
            code = main(["ci", "diagnose", "owner/repo", "12"])

        self.assertEqual(code, 0)
        pipeline.ci_failure_analysis.assert_called_once_with("owner/repo", 12)
        self.assertIn('"public_write": false', output.getvalue())


if __name__ == "__main__":
    unittest.main()
