"""Exercise built-archive validation, including stale and ambiguous artifacts."""

import io
import runpy
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

check = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/check_release.py")
)["check"]


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        files = {
            "pyproject.toml": '[project]\ndynamic = ["version"]\n[tool.hatch.version]\npath = "src/reposteward/__init__.py"\n',
            "src/reposteward/__init__.py": '__version__ = "0.1.0"\n',
            "src/reposteward/data/plugin-skills/understand-project/SKILL.md": "project reading skill\n",
            "CHANGELOG.md": "## [Unreleased]\n",
        }
        for name in (
            "README.md",
            "README.zh-CN.md",
            "uv.lock",
            "scripts/check_release.py",
            "docs/agent-plugin.zh-CN.md",
            "docs/releases.md",
            "docker/Dockerfile.runner",
        ):
            files[name] = name + "\n"
        for name, data in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        metadata = b"Name: reposteward\nVersion: 0.1.0\n"
        self.wheel = {
            k.removeprefix("src/"): v.encode()
            for k, v in files.items()
            if k.startswith("src/")
        }
        self.wheel["reposteward/data/Dockerfile.runner"] = files[
            "docker/Dockerfile.runner"
        ].encode()
        self.wheel["reposteward-0.1.0.dist-info/METADATA"] = metadata
        self.sdist = {"reposteward-0.1.0/" + k: v.encode() for k, v in files.items()}
        self.sdist["reposteward-0.1.0/PKG-INFO"] = metadata

    def build(self):
        with zipfile.ZipFile(
            self.dist / "reposteward-0.1.0-py3-none-any.whl", "w"
        ) as archive:
            for name, data in self.wheel.items():
                archive.writestr(name, data)
        with tarfile.open(self.dist / "reposteward-0.1.0.tar.gz", "w:gz") as archive:
            for name, data in self.sdist.items():
                item = tarfile.TarInfo(name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))

    def test_valid_archives_report_hashes_without_claiming_release(self):
        self.build()
        result = check(self.root, self.dist)
        self.assertEqual(result["version"], "0.1.0")
        self.assertEqual(len(result["artifacts"]), 2)
        self.assertFalse(result["release_published"])

    def test_stale_metadata_is_rejected(self):
        for target, name in (
            (self.wheel, "reposteward-0.1.0.dist-info/METADATA"),
            (self.sdist, "reposteward-0.1.0/PKG-INFO"),
        ):
            with self.subTest(name=name):
                original = target[name]
                target[name] = b"Name: reposteward\nVersion: 9.0.0\n"
                self.build()
                with self.assertRaisesRegex(ValueError, "metadata"):
                    check(self.root, self.dist)
                target[name] = original

    def test_missing_skill_and_stale_sdist_documentation_are_rejected(self):
        name = "reposteward/data/plugin-skills/understand-project/SKILL.md"
        original = self.wheel.pop(name)
        self.build()
        with self.assertRaisesRegex(ValueError, "wheel missing"):
            check(self.root, self.dist)
        self.wheel[name] = original
        self.sdist["reposteward-0.1.0/README.zh-CN.md"] = b"old docs"
        self.build()
        with self.assertRaisesRegex(ValueError, "sdist missing"):
            check(self.root, self.dist)

    def test_tag_requires_matching_version_and_dated_changelog(self):
        self.build()
        with self.assertRaisesRegex(ValueError, "tag does not match"):
            check(self.root, self.dist, "v0.2.0")
        with self.assertRaisesRegex(ValueError, "dated changelog"):
            check(self.root, self.dist, "v0.1.0")
        data = "## [0.1.0] - 2026-09-18\n"
        (self.root / "CHANGELOG.md").write_text(data)
        self.sdist["reposteward-0.1.0/CHANGELOG.md"] = data.encode()
        self.build()
        self.assertEqual(check(self.root, self.dist, "v0.1.0")["tag"], "v0.1.0")

    def test_stale_dist_directory_is_not_silently_selected(self):
        self.build()
        (self.dist / "old.whl").write_bytes(b"old")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            check(self.root, self.dist)

    def test_sdist_links_are_rejected_without_extraction(self):
        self.build()
        with tarfile.open(self.dist / "reposteward-0.1.0.tar.gz", "w:gz") as archive:
            item = tarfile.TarInfo("reposteward-0.1.0/link")
            item.type = tarfile.SYMTYPE
            item.linkname = "/outside"
            archive.addfile(item)
        with self.assertRaisesRegex(ValueError, "non-regular"):
            check(self.root, self.dist)


if __name__ == "__main__":
    unittest.main()
