from __future__ import annotations

import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from reposteward.config import ConfigError, load_config
from reposteward.setup import add_repository, initialize_user_config


class SetupTests(unittest.TestCase):
    def test_init_writes_versioned_user_configuration(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"

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
        self.assertNotIn("github_token =", content.casefold())

    def test_init_does_not_overwrite_existing_configuration_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("existing", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "already exists"):
                initialize_user_config(path=path, login="alice")

            self.assertEqual(path.read_text(encoding="utf-8"), "existing")

    def test_repo_add_creates_project_config_that_layers_over_user_config(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            user = root / "user.toml"
            project = root / "project" / ".reposteward.toml"
            project.parent.mkdir()
            initialize_user_config(path=user, login="alice")

            result = add_repository("Owner/Repo", path=project)
            config = load_config(project, user_path=user)

        self.assertEqual(result["repository"], "Owner/Repo")
        self.assertEqual(config.github.login, "alice")
        self.assertIn("owner/repo", config.repositories)
        self.assertEqual(
            config.state_dir,
            project.parent / ".reposteward" / "api.github.com" / "alice",
        )

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


if __name__ == "__main__":
    unittest.main()
