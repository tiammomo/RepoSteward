from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(config_home)}):
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


if __name__ == "__main__":
    unittest.main()
