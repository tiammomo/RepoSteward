from __future__ import annotations

import subprocess
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from reposteward.config import ConfigError, load_config
from reposteward.setup import add_repository, initialize_user_config


class SetupTests(unittest.TestCase):
    def test_init_writes_versioned_user_configuration(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"

            with patch.dict(
                "os.environ",
                {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_DATA_HOME": str(root / "data"),
                },
            ):
                result = initialize_user_config(
                    path=path,
                    login="alice",
                    git_name="Alice",
                    git_email="alice@example.com",
                )
            content = path.read_text(encoding="utf-8")
            parsed = tomllib.loads(content)

        self.assertEqual(result["github_login"], "alice")
        self.assertEqual(parsed["config_version"], 1)
        self.assertEqual(parsed["github"]["login"], "alice")
        self.assertEqual(parsed["issue_review"]["project_owner"], "")
        self.assertEqual(parsed["issue_review"]["project_number"], 0)
        self.assertTrue(parsed["issue_review"]["require_distinct_reviewer"])
        self.assertEqual(
            parsed["project"]["state_dir"], str(root / "state" / "reposteward")
        )
        self.assertEqual(
            parsed["project"]["workspace_dir"],
            str(root / "data" / "reposteward" / "workspaces"),
        )
        self.assertNotIn("github_token =", content.casefold())

    def test_init_does_not_overwrite_existing_configuration_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("existing", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "already exists"):
                initialize_user_config(path=path, login="alice")

            self.assertEqual(path.read_text(encoding="utf-8"), "existing")

    def test_init_can_configure_a_shared_issue_review_project(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"

            initialize_user_config(
                path=path,
                login="alice",
                issue_project_owner="team",
                issue_project_number=7,
                issue_project_owner_type="organization",
            )
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(parsed["issue_review"]["project_owner"], "team")
        self.assertEqual(parsed["issue_review"]["project_number"], 7)
        self.assertEqual(parsed["issue_review"]["project_owner_type"], "organization")

    def test_repo_add_creates_project_config_that_layers_over_user_config(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            user = root / "user.toml"
            project = root / "project" / ".reposteward.toml"
            project.parent.mkdir()
            with patch.dict(
                "os.environ",
                {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_DATA_HOME": str(root / "data"),
                },
            ):
                initialize_user_config(path=user, login="alice")

            result = add_repository("Owner/Repo", path=project)
            config = load_config(project, user_path=user)
            project_config = tomllib.loads(project.read_text(encoding="utf-8"))

        self.assertEqual(result["repository"], "Owner/Repo")
        self.assertEqual(config.github.login, "alice")
        self.assertIn("owner/repo", config.repositories)
        self.assertEqual(
            config.state_dir,
            root / "state" / "reposteward" / "api.github.com" / "alice",
        )
        self.assertEqual(
            config.workspace_dir,
            root / "data" / "reposteward" / "workspaces" / "api.github.com" / "alice",
        )
        self.assertNotIn("project", project_config)

    def test_repo_add_rejects_duplicate_case_insensitively(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory) / ".reposteward.toml"
            add_repository("Owner/Repo", path=project)

            with self.assertRaisesRegex(ConfigError, "already configured"):
                add_repository("owner/repo", path=project)

    def test_repo_add_uses_same_repository_strategy_for_maintainers(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            user = root / "user.toml"
            project = root / ".reposteward.toml"
            initialize_user_config(path=user, login="alice")

            add_repository("owner/repo", path=project, mode="maintainer")
            config = load_config(project, user_path=user)

        policy = config.repositories["owner/repo"]
        self.assertEqual(policy.mode, "maintainer")
        self.assertEqual(policy.submission_strategy, "same-repository")

    def test_repo_add_excludes_local_config_without_changing_gitignore(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)

            result = add_repository("owner/repo", path=root / ".reposteward.toml")

            ignored = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "-q", ".reposteward.toml"],
                check=False,
            )
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--short"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            gitignore_exists = (root / ".gitignore").exists()

        self.assertTrue(result["git_exclude_added"])
        self.assertEqual(ignored.returncode, 0)
        self.assertEqual(status, "")
        self.assertFalse(gitignore_exists)

    def test_repo_add_does_not_hide_an_explicitly_tracked_config(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            config_path = root / ".reposteward.toml"
            config_path.write_text("config_version = 1\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "add", "-f", ".reposteward.toml"],
                check=True,
            )

            result = add_repository("owner/repo", path=config_path)
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--short"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        self.assertFalse(result["git_exclude_added"])
        self.assertIn("AM .reposteward.toml", status)


if __name__ == "__main__":
    unittest.main()
