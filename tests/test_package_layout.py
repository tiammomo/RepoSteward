from __future__ import annotations

import importlib
import pkgutil
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

import reposteward
from reposteward.core.runtime import installation_info
from reposteward.plugins import bundle


class PackageLayoutTests(unittest.TestCase):
    def test_all_shipped_modules_import_without_credentials_or_state(self):
        with (
            patch(
                "reposteward.github.client.resolve_token",
                side_effect=AssertionError("offline import"),
            ),
            patch(
                "reposteward.storage.store.Store.__init__",
                side_effect=AssertionError("no state on import"),
            ),
        ):
            for module in pkgutil.walk_packages(reposteward.__path__, "reposteward."):
                with self.subTest(module=module.name):
                    importlib.import_module(module.name)

    def test_runtime_location_and_bundled_resources_use_package_root(self):
        self.assertEqual(
            Path(installation_info()["module_path"]),
            Path(reposteward.__file__).resolve().parent,
        )
        root = files("reposteward")
        self.assertTrue(root.joinpath("schemas").is_dir())
        self.assertTrue(
            root.joinpath("data", "plugin-skills", "resume-task", "SKILL.md").is_file()
        )
        self.assertTrue(root.joinpath("web", "index.html").is_file())

    def test_plugin_digest_covers_nested_modules_and_ignores_bytecode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plugins").mkdir()
            (root / "verification").mkdir()
            (root / "__init__.py").write_text('"""fixture"""\n')
            module = root / "verification" / "recovery.py"
            module.write_text("RESULT = 'first'\n")
            with patch.object(bundle, "__file__", str(root / "plugins" / "bundle.py")):
                original = bundle._runtime_digest()
                (root / "verification" / "cache.pyc").write_bytes(b"ignored")
                self.assertEqual(bundle._runtime_digest(), original)
                module.write_text("RESULT = 'changed'\n")
                changed = bundle._runtime_digest()
                self.assertNotEqual(changed, original)
                module.rename(root / "plugins" / "recovery.py")
                self.assertNotEqual(bundle._runtime_digest(), changed)
