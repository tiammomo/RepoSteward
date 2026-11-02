"""Exercise build invalidation against content changes and corrupt artifacts."""

from __future__ import annotations

import hashlib
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SUPPORT = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/build_frontend.py")
)
GLOBALS = SUPPORT["prepare_frontend"].__globals__


class IncrementalBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.frontend = self.root / "frontend"
        self.contract = '{"paths":{}}\n'
        self.commands = []
        self.exports = 0
        for name, content in {
            "hatch_build.py": "# build hook\n",
            "scripts/build_frontend.py": "# frontend preparation\n",
            "pyproject.toml": "# metadata\n",
            "uv.lock": "# dependency lock\n",
            "src/reposteward/web/api/app.py": "# API implementation\n",
            "frontend/package.json": "{}\n",
            "frontend/package-lock.json": "{}\n",
            "frontend/src/app.ts": "export const value = 1;\n",
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.install_tools()
        self.addCleanup(patch.stopall)
        patch.dict(GLOBALS, {"export_schema": self.export}).start()
        patch.object(SUPPORT["subprocess"], "run", side_effect=self.command).start()
        patch.object(SUPPORT["shutil"], "which", return_value="/tools/npm").start()

    def install_tools(self):
        tools = self.frontend / "node_modules/.bin"
        tools.mkdir(parents=True, exist_ok=True)
        for name in ("tsc", "vite", "openapi-typescript"):
            (tools / name).write_text("fixture executable")
        (tools.parent / ".reposteward-lock").write_text(
            hashlib.sha256(
                (self.frontend / "package-lock.json").read_bytes()
            ).hexdigest()
        )

    def export(self, _root):
        self.exports += 1
        (self.frontend / "openapi.json").write_text(self.contract)

    def command(self, args, **_kwargs):
        action = args[-1] if args[1] == "run" else "ci"
        self.commands.append(action)
        if action == "ci":
            self.install_tools()
        elif action == "generate":
            generated = self.frontend / "src/api/generated/schema.ts"
            generated.parent.mkdir(parents=True, exist_ok=True)
            generated.write_text("// " + self.contract)
        elif action == "bundle":
            dist = self.frontend / "dist"
            (dist / "assets").mkdir(parents=True, exist_ok=True)
            (dist / ".vite").mkdir(exist_ok=True)
            (dist / "index.html").write_text("<html></html>")
            (dist / "assets/app.js").write_bytes(
                (self.frontend / "src/app.ts").read_bytes()
            )
            (dist / "assets/app.css").write_text("body{}")
            (dist / ".vite/manifest.json").write_text(
                json.dumps(
                    {
                        "index.html": {
                            "file": "assets/app.js",
                            "css": ["assets/app.css"],
                        },
                    }
                )
            )
        else:
            self.fail(f"unexpected command: {args}")

    def prepare(self):
        return SUPPORT["prepare_frontend"](self.root)

    def prime(self):
        self.assertEqual(self.prepare(), "built")
        self.assertEqual(self.commands, ["generate", "bundle"])
        self.commands.clear()
        self.exports = 0

    def test_unchanged_build_needs_neither_schema_export_nor_node(self):
        self.prime()
        with patch.object(SUPPORT["shutil"], "which", return_value=None):
            self.assertEqual(self.prepare(), "cached")
        self.assertEqual(self.exports, 0)
        self.assertEqual(self.commands, [])

    def test_backend_implementation_edit_reuses_verified_bundle_after_real_export(self):
        self.prime()
        (self.root / "src/reposteward/web/api/app.py").write_text(
            "# internal refactor\n"
        )
        with self.assertRaises(RuntimeError):
            SUPPORT["validate_assets"](self.root)
        with patch.object(SUPPORT["shutil"], "which", return_value=None):
            self.assertEqual(self.prepare(), "contract-unchanged")
        self.assertEqual(self.exports, 1)
        self.assertEqual(self.commands, [])
        SUPPORT["validate_assets"](self.root)

    def test_frontend_edit_recompiles_without_repeating_unchanged_type_generation(self):
        self.prime()
        (self.frontend / "src/app.ts").write_text("export const value = 2;\n")
        self.assertEqual(self.prepare(), "built")
        self.assertEqual(self.commands, ["bundle"])
        self.assertEqual(self.exports, 0)
        self.assertIn(b"2", (self.frontend / "dist/assets/app.js").read_bytes())

    def test_backend_contract_change_regenerates_types_and_bundle(self):
        self.prime()
        (self.root / "src/reposteward/web/api/app.py").write_text("# new route\n")
        self.contract = '{"paths":{"/new":{}}}\n'
        self.assertEqual(self.prepare(), "built")
        self.assertEqual(self.commands, ["generate", "bundle"])
        self.assertIn(
            "/new", (self.frontend / "src/api/generated/schema.ts").read_text()
        )

    def test_dependency_lock_change_reinstalls_and_regenerates(self):
        self.prime()
        (self.frontend / "package-lock.json").write_text('{"lockfileVersion":3}\n')
        self.assertEqual(self.prepare(), "built")
        self.assertEqual(self.commands, ["ci", "generate", "bundle"])

    def test_changed_generated_types_cannot_be_reused(self):
        self.prime()
        (self.frontend / "src/api/generated/schema.ts").write_text("corrupt types")
        self.assertEqual(self.prepare(), "built")
        self.assertEqual(self.commands, ["generate", "bundle"])

    def test_tampered_asset_cannot_be_reused_after_a_backend_only_edit(self):
        self.prime()
        (self.root / "src/reposteward/web/api/app.py").write_text(
            "# internal refactor\n"
        )
        (self.frontend / "dist/assets/app.js").write_text("corrupt bundle")
        self.assertEqual(self.prepare(), "built")
        self.assertEqual(self.commands, ["generate", "bundle"])

    def test_missing_compiler_is_restored_when_a_rebuild_is_required(self):
        self.prime()
        (self.frontend / "src/app.ts").write_text("export const value = 2;\n")
        (self.frontend / "node_modules/.bin/tsc").unlink()
        self.assertEqual(self.prepare(), "built")
        self.assertEqual(self.commands, ["ci", "bundle"])

    def test_frontend_test_edit_is_still_a_compiler_input(self):
        self.prime()
        (self.frontend / "src/app.test.ts").write_text("// changed test type input\n")
        self.assertEqual(self.prepare(), "built")
        self.assertEqual(self.commands, ["bundle"])

    def test_generator_configuration_change_invalidates_generated_types(self):
        self.prime()
        (self.frontend / "package.json").write_text(
            '{"scripts":{"generate":"changed"}}'
        )
        self.assertEqual(self.prepare(), "built")
        self.assertEqual(self.commands, ["generate", "bundle"])

    def test_edit_during_schema_export_cannot_rebind_the_previous_bundle(self):
        self.prime()
        backend = self.root / "src/reposteward/web/api/app.py"
        backend.write_text("# before export")

        def edit_during_export(root):
            self.export(root)
            backend.write_text("# changed during export")

        with (
            patch.dict(GLOBALS, {"export_schema": edit_during_export}),
            self.assertRaisesRegex(RuntimeError, "changed during API export"),
        ):
            self.prepare()
        with self.assertRaises(RuntimeError):
            SUPPORT["validate_assets"](self.root)
        self.assertEqual(self.commands, [])

    def test_edit_during_compilation_cannot_receive_a_valid_manifest(self):
        self.prime()
        source = self.frontend / "src/app.ts"
        source.write_text("export const value = 2;")

        def edit_during_bundle(args, **kwargs):
            self.command(args, **kwargs)
            if args == ["npm", "run", "bundle"]:
                source.write_text("export const value = 3;")

        with (
            patch.object(SUPPORT["subprocess"], "run", side_effect=edit_during_bundle),
            self.assertRaisesRegex(RuntimeError, "changed during compilation"),
        ):
            self.prepare()
        with self.assertRaises(RuntimeError):
            SUPPORT["validate_assets"](self.root)

    def test_changed_exported_schema_is_restored_from_backend_before_reuse(self):
        self.prime()
        (self.frontend / "openapi.json").write_text("corrupt schema")
        self.assertEqual(self.prepare(), "contract-unchanged")
        self.assertEqual(self.exports, 1)
        self.assertEqual(self.commands, [])
        self.assertEqual((self.frontend / "openapi.json").read_text(), self.contract)
        SUPPORT["validate_assets"](self.root)
