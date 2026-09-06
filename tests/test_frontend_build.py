from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from reposteward.web_api.assets import load_assets
from reposteward.workspace import sanitized_environment


class FrontendBuildTests(unittest.TestCase):
    def test_packaged_manifest_verifies_every_asset(self):
        assets, digest = load_assets()
        self.assertEqual(len(digest), 64)
        self.assertIn("/", assets)
        self.assertTrue(any(path.startswith("/assets/") for path in assets))
        self.assertIn(b"/assets/", assets["/"][0])
        self.assertNotIn(b"/src/", assets["/"][0])

    @unittest.skipUnless(
        shutil.which("npm"), "Frontend checks require the verifier Node toolchain"
    )
    def test_frontend_types_and_user_interactions(self):
        frontend = Path(__file__).resolve().parents[1] / "frontend"
        lock = json.loads((frontend / "package-lock.json").read_text())
        package = json.loads((frontend / "package.json").read_text())
        self.assertEqual(lock["packages"][""]["dependencies"], package["dependencies"])
        self.assertEqual(
            lock["packages"][""]["devDependencies"], package["devDependencies"]
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "schema.ts"
            generated = subprocess.run(
                [
                    str(frontend / "node_modules/.bin/openapi-typescript"),
                    "openapi.json",
                    "-o",
                    str(output),
                ],
                cwd=frontend,
                capture_output=True,
                text=True,
                timeout=30,
                env=sanitized_environment(keep_codex_credentials=False),
                check=False,
            )
            self.assertEqual(
                generated.returncode, 0, generated.stdout + generated.stderr
            )
            self.assertEqual(
                output.read_bytes(),
                (frontend / "src/api/generated/schema.ts").read_bytes(),
            )
        result = subprocess.run(
            ["npm", "run", "check"],
            cwd=frontend,
            capture_output=True,
            text=True,
            timeout=120,
            env=sanitized_environment(keep_codex_credentials=False),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
