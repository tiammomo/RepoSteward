from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_projects import git, repository

from reposteward.cli import main
from reposteward.code_facts import parse_code
from reposteward.code_index import Unreadable, read_source
from reposteward.projects import ProjectError, workspace_metadata
from reposteward.understanding import Understanding, render_guide


class UnderstandingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = repository(self.root / "repo")
        self.cache = self.root / "cache"
        self.service = Understanding(self.cache)
        self.write("README.md", "# Demo\n\nA parcel delivery tool.\n")
        self.write(
            "CONTRIBUTING.md", "# Contribute\n\nReview an Issue before changing code.\n"
        )
        self.write(
            "pyproject.toml",
            '[project]\nname = "parcel"\ndescription = "Deliver parcels"\n[project.scripts]\nparcel = "parcel.cli:main"\n',
        )
        self.write("src/parcel/__init__.py", '"""Parcel package."""\n')
        self.write(
            "src/parcel/cli.py",
            '"""Command entry."""\nfrom .delivery import deliver\ndef main():\n    return deliver()\n',
        )
        self.write(
            "src/parcel/delivery.py",
            '"""Parcel delivery."""\nfrom pathlib import Path\ndef deliver():\n    """Ship a parcel."""\n    return "delivered"\n',
        )
        self.write(
            "src/parcel/invoices.py",
            '"""Invoice settlement."""\ndef settle():\n    return 7\n',
        )
        self.write(
            "tests/test_delivery.py",
            'from parcel.delivery import deliver\ndef test_delivery():\n    assert deliver() == "delivered"\nif __name__ == "__main__":\n    test_delivery()\n',
        )
        self.write(
            "docs/architecture.md", "# Architecture\n\nParcels pass through delivery.\n"
        )
        self.commit()

    def write(self, name: str, value: str):
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)

    def commit(self):
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Fixture")

    def record(self, path: str):
        return self.service.index.load(workspace_metadata(self.repo))["files"][path]

    def evidence_id(self, path: str):
        guide = self.service.guide(self.repo, focus=path)
        return next(
            item["source"]["evidence_id"]
            for item in guide["reading_path"]
            if item["path"] == path
        )

    def test_overview_has_declarations_entry_implementation_tests_and_citations(self):
        result = self.service.scan(self.repo)
        guide = self.service.guide(self.repo, mode="contributor")
        self.assertEqual(guide["status"], "current")
        self.assertEqual(result["coverage"]["indexed_files"], 10)
        self.assertEqual(guide["entries"][0]["target_path"], "src/parcel/cli.py")
        self.assertEqual(guide["entries"][0]["source"]["line"], 1)
        self.assertEqual(guide["entries"][0]["source"]["end_line"], 5)
        self.assertIn("Deliver parcels", guide["project_declarations"][0]["text"])
        paths = [item["path"] for item in guide["reading_path"]]
        self.assertEqual(paths[:3], ["README.md", "CONTRIBUTING.md", "pyproject.toml"])
        self.assertIn("tests/test_delivery.py", paths)
        self.assertFalse(
            any(entry["target_path"].startswith("tests/") for entry in guide["entries"])
        )
        self.assertEqual(guide["reading_path"][0]["summary_source"]["line"], 3)
        self.assertTrue(
            any(
                e["from"] == "src/parcel/cli.py" and e["to"] == "src/parcel/delivery.py"
                for e in guide["static_imports"]
            )
        )
        item = next(
            i for i in guide["reading_path"] if i["path"] == "src/parcel/delivery.py"
        )
        symbol = item["symbols"][0]
        self.assertEqual((symbol["name"], symbol["line"]), ("deliver", 3))
        source = symbol["source"]
        self.assertEqual(
            source["digest"],
            hashlib.sha256((self.repo / item["path"]).read_bytes()).hexdigest(),
        )
        self.assertIn(f"/blob/{guide['head']}/", source["url"])
        evidence = self.service.evidence(
            self.repo, source["evidence_id"], start_line=3, limit=3
        )
        self.assertEqual(
            evidence["text"],
            'def deliver():\n    """Ship a parcel."""\n    return "delivered"',
        )
        self.assertIn("不是运行时调用图", render_guide(guide))

    def test_question_changes_route_and_includes_importer_and_test_hints(self):
        self.service.scan(self.repo)
        delivery = self.service.guide(self.repo, focus="deliver", limit=6)
        invoice = self.service.guide(self.repo, focus="invoices settle", limit=6)
        delivery_paths = {item["path"] for item in delivery["reading_path"]}
        invoice_paths = {item["path"] for item in invoice["reading_path"]}
        self.assertIn("src/parcel/cli.py", delivery_paths)
        self.assertIn("tests/test_delivery.py", delivery_paths)
        self.assertIn("src/parcel/invoices.py", invoice_paths)
        self.assertNotIn("src/parcel/invoices.py", delivery_paths)
        self.assertEqual(
            self.service.guide(self.repo, focus="nonexistent_unique_term")[
                "reading_path"
            ],
            [],
        )

    def test_incremental_cache_reuses_unchanged_parses_and_queries_do_not_write(self):
        first = self.service.scan(self.repo)
        original = {
            p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.cache.iterdir()
        }
        with patch(
            "reposteward.code_index.parse_code",
            side_effect=AssertionError("query parsed source"),
        ):
            self.service.guide(self.repo)
            self.service.evidence(self.repo, self.evidence_id("src/parcel/cli.py"))
            self.assertEqual(
                original,
                {
                    p: (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.cache.iterdir()
                },
            )
            second = self.service.scan(self.repo)
        self.assertEqual(
            second["cache"],
            {"reused_files": first["coverage"]["indexed_files"], "parsed_files": 0},
        )
        self.write("src/parcel/invoices.py", "def settle():\n    return 8\n")
        with patch("reposteward.code_index.parse_code", wraps=parse_code) as parse:
            third = self.service.scan(self.repo)
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(third["changes"]["counts"]["changed"], 1)

    def test_dirty_boolean_staying_true_does_not_hide_changed_source(self):
        self.write("src/parcel/delivery.py", "def deliver():\n    return 1\n")
        self.service.scan(self.repo)
        evidence_id = self.evidence_id("src/parcel/delivery.py")
        self.assertNotIn(
            "url", self.service.guide(self.repo)["reading_path"][0]["source"]
        )
        self.write("src/parcel/delivery.py", "def deliver():\n    return 2\n")
        guide = self.service.guide(self.repo)
        self.assertEqual(guide["status"], "stale")
        self.assertTrue(guide["dirty"] and guide["current_state"]["dirty"])
        self.assertEqual(guide["reading_path"], [])
        self.assertEqual(self.service.evidence(self.repo, evidence_id)["text"], "")
        self.assertEqual(
            guide["changes_since_scan"]["paths"]["changed"], ["src/parcel/delivery.py"]
        )

    def test_delete_rename_add_and_head_changes_invalidate(self):
        self.service.scan(self.repo)
        old_id = self.evidence_id("src/parcel/delivery.py")
        (self.repo / "src/parcel/delivery.py").rename(
            self.repo / "src/parcel/shipping.py"
        )
        guide = self.service.guide(self.repo)
        self.assertEqual(
            guide["changes_since_scan"]["counts"],
            {"added": 1, "deleted": 1, "changed": 0},
        )
        self.assertEqual(self.service.evidence(self.repo, old_id)["status"], "stale")
        self.service.scan(self.repo)
        with self.assertRaisesRegex(ProjectError, "outside"):
            self.service.evidence(self.repo, old_id)
        self.commit()
        self.assertEqual(self.service.guide(self.repo)["status"], "stale")

    def test_workspace_and_repository_identity_scope_include_clones_and_worktrees(self):
        self.service.scan(self.repo)
        evidence_id = self.evidence_id("src/parcel/delivery.py")
        other = repository(self.root / "clone")
        self.assertEqual(self.service.guide(other)["status"], "not_scanned")
        self.service.scan(other)
        with self.assertRaises(ProjectError):
            self.service.evidence(other, evidence_id)
        worktree = self.root / "worktree"
        git(self.repo, "worktree", "add", "--detach", str(worktree))
        self.assertEqual(self.service.guide(worktree)["status"], "not_scanned")
        self.service.scan(worktree)
        self.assertEqual(self.service.guide(worktree)["status"], "current")
        git(
            self.repo, "remote", "set-url", "origin", "git@github.com:owner/another.git"
        )
        self.assertEqual(self.service.guide(self.repo)["status"], "not_scanned")

    def test_unregistered_no_config_no_auth_cli_read_and_scan(self):
        output = io.StringIO()
        with (
            patch(
                "reposteward.cli.load_config",
                side_effect=AssertionError("configuration required"),
            ),
            patch(
                "reposteward.github.GitHubClient.__init__",
                side_effect=AssertionError("GitHub initialized"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(
                    [
                        "understand",
                        "guide",
                        str(self.repo),
                        "--cache-dir",
                        str(self.cache),
                        "--format",
                        "json",
                    ]
                ),
                0,
            )
            self.assertFalse(self.cache.exists())
            self.assertEqual(json.loads(output.getvalue())["status"], "not_scanned")
            output.truncate(0)
            output.seek(0)
            self.assertEqual(
                main(
                    [
                        "understand",
                        "scan",
                        str(self.repo),
                        "--cache-dir",
                        str(self.cache),
                    ]
                ),
                0,
            )
        self.assertFalse((self.cache.parent / "reposteward.sqlite3").exists())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_default_cache_without_any_configuration(self):
        output = io.StringIO()
        with (
            patch("reposteward.config.discover_project_config", return_value=None),
            patch(
                "reposteward.config.default_user_config_path",
                return_value=self.root / "absent.toml",
            ),
            patch(
                "reposteward.config.default_state_dir", return_value=self.root / "state"
            ),
            patch(
                "reposteward.cli.load_config",
                side_effect=AssertionError("configuration required"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["understand", "scan", str(self.repo)]), 0)
        self.assertTrue((self.root / "state/understanding").is_dir())

    def test_sensitive_binary_links_generated_files_and_code_never_execute(self):
        trap = self.root / "executed"
        self.write(
            "trap.py", f"from pathlib import Path\nPath({str(trap)!r}).touch()\n"
        )
        self.write(".env", "TOKEN=private\n")
        self.write("private.key", "private material\n")
        self.write("node_modules/pkg/index.js", "ignored tracked dependency\n")
        self.write("secret.py", 'value = "' + "ghp_" + "z" * 30 + '"\n')
        (self.repo / "image.png").write_bytes(b"\x00\xff")
        (self.repo / "link.py").symlink_to(self.repo / "trap.py")
        self.commit()
        git(self.repo, "config", "core.fsmonitor", f"touch {trap}")
        result = self.service.scan(self.repo)
        self.assertFalse(trap.exists())
        excluded = result["coverage"]["excluded"]
        self.assertEqual(excluded["sensitive_path"], 2)
        self.assertEqual(excluded["sensitive_content"], 1)
        self.assertEqual(excluded["non_text"], 1)
        self.assertEqual(excluded["non_regular_or_link"], 1)
        cache_text = next(self.cache.glob("*.json")).read_text()
        self.assertNotIn("z" * 30, cache_text)
        self.assertNotIn("private material", cache_text)
        self.assertNotIn(str(trap), cache_text)

    def test_parent_symlink_and_fifo_never_opened(self):
        self.service.scan(self.repo)
        evidence_id = self.evidence_id("src/parcel/delivery.py")
        parent = self.repo / "src/parcel"
        parent.rename(self.repo / "src/saved")
        parent.symlink_to(self.repo / "src/saved", target_is_directory=True)
        with self.assertRaises(Unreadable):
            read_source(self.repo, "src/parcel/delivery.py")
        self.assertEqual(self.service.evidence(self.repo, evidence_id)["text"], "")
        os.mkfifo(self.repo / "pipe.py")
        with self.assertRaisesRegex(Unreadable, "non_regular"):
            read_source(self.repo, "pipe.py")

    def test_budgets_are_explicit_and_non_python_is_not_claimed_understood(self):
        self.write("src/new.ts", "export function launch() {}\n")
        self.write("bad.py", "def broken(:\n")
        self.write("large.txt", "x" * 600000)
        result = self.service.scan(self.repo)
        self.assertEqual(result["coverage"]["excluded"]["file_bytes_limit"], 1)
        self.assertEqual(result["coverage"]["languages"]["typescript"], 1)
        self.assertEqual(result["coverage"]["capabilities"]["parse_failed"], 1)
        self.assertEqual(self.record("src/new.ts")["facts"]["status"], "inventory_only")
        with patch("reposteward.code_index.MAX_FILES", 2):
            limited = self.service.scan(self.repo)
        self.assertGreater(limited["coverage"]["excluded"]["file_count_limit"], 0)
        with patch("reposteward.code_index.MAX_TOTAL_BYTES", 60):
            small = self.service.scan(self.repo)
        self.assertLessEqual(small["coverage"]["read_bytes"], 60)
        self.assertGreater(small["coverage"]["excluded"]["total_bytes_limit"], 0)

    def test_failed_scan_preserves_previous_generation_and_rebuild_repairs_damage(self):
        self.service.scan(self.repo)
        cache = next(self.cache.glob("*.json"))
        original = cache.read_bytes()
        with (
            patch("reposteward.code_index.MAX_CACHE_BYTES", 10),
            self.assertRaises(ProjectError),
        ):
            self.service.scan(self.repo, rebuild=True)
        self.assertEqual(cache.read_bytes(), original)
        cache.write_text('{"version": 999}')
        with self.assertRaisesRegex(ProjectError, "rebuild"):
            self.service.guide(self.repo)
        self.service.scan(self.repo, rebuild=True)
        self.assertEqual(self.service.guide(self.repo)["status"], "current")

    def test_cache_cannot_be_placed_in_repository_or_follow_a_symlink(self):
        with self.assertRaisesRegex(ProjectError, "outside"):
            Understanding(self.repo / "cache").scan(self.repo)
        self.service.scan(self.repo)
        cache = next(self.cache.glob("*.json"))
        cache.unlink()
        victim = self.root / "victim.json"
        victim.write_text("private")
        cache.symlink_to(victim)
        with self.assertRaises(OSError):
            self.service.guide(self.repo)
        self.assertEqual(victim.read_text(), "private")

    def test_markdown_escapes_repository_markup_and_source_output_is_bounded(self):
        self.write(
            "README.md",
            '# Demo\n\n<script>alert("x")</script>\n\n[click](javascript:bad) and ![image](https://bad.invalid)\n\n**untrusted** <img src="bad">\n',
        )
        self.write("src/parcel/huge_line.py", '"' + "a" * 17000 + '"\n')
        self.service.scan(self.repo)
        markdown = render_guide(self.service.guide(self.repo))
        self.assertNotIn("<img src=", markdown)
        self.assertNotIn("![image]", markdown)
        self.assertIn("\\*\\*untrusted\\*\\*", markdown)
        evidence = self.service.evidence(
            self.repo, self.evidence_id("src/parcel/huge_line.py")
        )
        self.assertEqual(len(evidence["text"]), 16000)
        self.assertTrue(evidence["truncated"])
        for options in ({"limit": True}, {"start_line": 0}, {"limit": 121}):
            with self.assertRaises(ValueError):
                self.service.evidence(self.repo, evidence["evidence_id"], **options)
        with self.assertRaises(ValueError):
            self.service.evidence(self.repo, "../../etc/passwd")

    def test_changed_during_scan_does_not_replace_previous_facts(self):
        self.service.scan(self.repo)
        original = next(self.cache.glob("*.json")).read_bytes()
        self.write("new.py", "value = 1\n")

        def modifying_parser(path, text):
            self.write("new.py", "value = 2\n")
            return parse_code(path, text)

        with (
            patch("reposteward.code_index.parse_code", side_effect=modifying_parser),
            self.assertRaisesRegex(ProjectError, "changed during scan"),
        ):
            self.service.scan(self.repo)
        self.assertEqual(next(self.cache.glob("*.json")).read_bytes(), original)

    def test_ast_relative_imports_and_manifest_source_spans(self):
        facts = parse_code(
            "src/pkg/inner/run.py",
            "from ..core import work\nfrom . import helper\nfrom ....escape import no\n",
        )
        self.assertEqual(
            [item["target"] for item in facts["imports"]],
            ["pkg.core", "pkg.core.work", "pkg.inner.helper"],
        )
        npm = parse_code(
            "package.json",
            '{"name":"parcel", "bin": {"parcel":\n"cli.js"}, "scripts": {"test": "node test.js"}}',
        )
        self.assertEqual(npm["entries"][0]["end_line"], 2)
        self.assertEqual(npm["commands"][0]["execution"], "not_run")
        self.assertEqual(
            parse_code("pyproject.toml", "not toml")["status"], "parse_failed"
        )
        bounded = parse_code(
            "many.py", "\n".join(f"def f{i}(): pass" for i in range(150))
        )
        self.assertEqual(len(bounded["symbols"]), 128)
        self.assertEqual(bounded["omitted_nodes"], 22)

    def test_ambiguous_module_names_do_not_create_invented_relationships(self):
        self.write("parcel/delivery.py", "def competing(): pass\n")
        self.service.scan(self.repo)
        guide = self.service.guide(self.repo, focus="delivery")
        self.assertEqual(guide["relations"]["ambiguous_module_names"], 1)
        self.assertFalse(
            any(e["from"] == "src/parcel/cli.py" for e in guide["static_imports"])
        )

    def test_long_unicode_paths_still_fit_the_shared_response_byte_budget(self):
        directory = "/".join(["包" * 50] * 8)
        for i in range(20):
            self.write(
                f"{directory}/unit{i}.py",
                "\n".join(f"def operation{j}(): pass" for j in range(8)),
            )
        self.commit()
        self.service.scan(self.repo)
        guide = self.service.guide(self.repo, focus="unit", limit=20)
        self.assertTrue(guide["output_truncated"])
        self.assertLessEqual(
            len(json.dumps(guide, ensure_ascii=False).encode()), 240000
        )
        self.assertLess(len(guide["reading_path"]), 20)

    def test_focus_prioritizes_matching_symbol_over_earlier_definitions(self):
        self.write(
            "src/parcel/invoices.py",
            "\n".join(f"def unrelated{i}(): pass" for i in range(10))
            + "\ndef settle(): return 3\n",
        )
        self.service.scan(self.repo)
        guide = self.service.guide(self.repo, focus="settle")
        item = next(
            item
            for item in guide["reading_path"]
            if item["path"] == "src/parcel/invoices.py"
        )
        self.assertEqual(item["symbols"][0]["name"], "settle")
        self.assertEqual(item["symbols"][0]["source"]["line"], 11)
        script = parse_code(
            ".tools/run-script.py", 'if __name__ == "__main__":\n    pass\n'
        )
        self.assertEqual(script["module"], "")
        self.assertEqual(script["entries"][0]["target"], ".tools/run-script.py")
