"""Build real distributions with Node and web imports unavailable."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = runpy.run_path(str(ROOT / "scripts/build_frontend.py"))


class BuildModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "source"
        self.root.mkdir()
        for name in ("src", "docker", "scripts"):
            shutil.copytree(
                ROOT / name,
                self.root / name,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        for name in (
            "pyproject.toml",
            "hatch_build.py",
            "uv.lock",
            "README.md",
            "LICENSE",
        ):
            shutil.copyfile(ROOT / name, self.root / name)
        self.env = {
            "PATH": str(Path(temporary.name) / "no-node"),
            "HOME": temporary.name,
            "PYTHONDONTWRITEBYTECODE": "1",
            "REPOSTEWARD_BUILD_WEB": "0",
        }
        blocker = Path(temporary.name) / "blocker"
        blocker.mkdir()
        (blocker / "sitecustomize.py").write_text(
            "import sys\n"
            "class BlockWeb:\n"
            " def find_spec(self, fullname, *args):\n"
            "  if fullname.split('.')[0] in {'fastapi', 'pydantic', 'uvicorn'}:\n"
            "   raise AssertionError('web import during core build: ' + fullname)\n"
            "sys.meta_path.insert(0, BlockWeb())\n"
        )
        self.env["PYTHONPATH"] = str(blocker)

    def build(self, kind="wheel"):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                f"import hatchling.build as b; b.build_{kind}('dist/{kind}')",
            ],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def assets(self):
        dist = self.root / "frontend/dist"
        (dist / "assets").mkdir(parents=True)
        records = []
        for name, body, mime in (
            ("index.html", b"<html></html>", "text/html"),
            ("assets/app.js", b"console.log(1)", "text/javascript"),
            ("assets/app.css", b"body{}", "text/css"),
        ):
            (dist / name).write_bytes(body)
            records.append(
                {"path": name, "sha256": hashlib.sha256(body).hexdigest(), "mime": mime}
            )
        (dist / "assets.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "inputs_digest": SUPPORT["frontend_digest"](self.root),
                    "entry_js": "assets/app.js",
                    "entry_css": "assets/app.css",
                    "files": records,
                }
            )
        )
        return dist

    def test_core_wheel_sdist_and_editable_need_neither_node_nor_web(self):
        for kind in ("wheel", "sdist", "editable"):
            with self.subTest(kind=kind):
                result = self.build(kind)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        wheel = next((self.root / "dist/wheel").glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            self.assertFalse(any("web_dist/" in name for name in archive.namelist()))
            metadata = archive.read(
                next(name for name in archive.namelist() if name.endswith("/METADATA"))
            ).decode()
            self.assertIn("Provides-Extra: web", metadata)
            for line in metadata.splitlines():
                if line.startswith("Requires-Dist:") and any(
                    dep in line for dep in ("fastapi", "pydantic", "uvicorn")
                ):
                    self.assertIn("extra ==", line)

    def test_core_cli_help_works_without_web_dependencies(self):
        env = dict(
            self.env,
            PYTHONPATH=self.env["PYTHONPATH"] + os.pathsep + str(self.root / "src"),
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from reposteward.cli import main; raise SystemExit(main(['--help']))",
            ],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Local-first", result.stdout)

    def test_web_wheel_requires_fresh_complete_untampered_assets(self):
        self.env["REPOSTEWARD_BUILD_WEB"] = "1"
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("assets missing, stale or invalid", result.stderr)
        dist = self.assets()
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with zipfile.ZipFile(next((self.root / "dist/wheel").glob("*.whl"))) as archive:
            self.assertEqual(
                archive.read("reposteward/web_dist/assets/app.js"), b"console.log(1)"
            )
        for change in ("tampered", "missing", "stale", "path", "duplicate"):
            with self.subTest(change=change):
                manifest = json.loads((dist / "assets.json").read_text())
                if change == "tampered":
                    (dist / "assets/app.js").write_bytes(b"changed")
                elif change == "missing":
                    (dist / "assets/app.js").unlink()
                elif change == "stale":
                    manifest["inputs_digest"] = "0" * 64
                elif change == "path":
                    manifest["files"][0]["path"] = "../outside"
                else:
                    manifest["files"].append(manifest["files"][0])
                (dist / "assets.json").write_text(json.dumps(manifest))
                self.assertNotEqual(self.build().returncode, 0)
                shutil.rmtree(dist)
                self.assets()

    def test_core_does_not_package_prepared_web_assets(self):
        self.assets()
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with zipfile.ZipFile(next((self.root / "dist/wheel").glob("*.whl"))) as archive:
            self.assertFalse(any("web_dist/" in name for name in archive.namelist()))
        result = self.build("sdist")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with tarfile.open(next((self.root / "dist/sdist").glob("*.tar.gz"))) as archive:
            self.assertFalse(
                any("frontend/dist/" in name for name in archive.getnames())
            )

    def test_web_sdist_preserves_assets_for_a_node_free_wheel_rebuild(self):
        self.assets()
        self.env["REPOSTEWARD_BUILD_WEB"] = "1"
        result = self.build("sdist")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        extracted = self.root.parent / "extracted"
        with tarfile.open(next((self.root / "dist/sdist").glob("*.tar.gz"))) as archive:
            archive.extractall(extracted, filter="data")
        self.root = next(extracted.iterdir())
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with zipfile.ZipFile(next((self.root / "dist/wheel").glob("*.whl"))) as archive:
            self.assertIn("reposteward/web_dist/assets.json", archive.namelist())

    def test_build_environment_excludes_credentials(self):
        from unittest.mock import patch

        with patch.dict(
            os.environ, {"GITHUB_TOKEN": "secret", "OPENAI_API_KEY": "secret"}
        ):
            env = SUPPORT["build_environment"]()
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
