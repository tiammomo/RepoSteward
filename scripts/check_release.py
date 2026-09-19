"""Check locally built distributions without importing package code or extracting archives."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import tarfile
import tomllib
import zipfile
from datetime import date
from email.parser import BytesParser
from pathlib import Path


def source_version(root: Path) -> str:
    config = tomllib.loads((root / "pyproject.toml").read_text())
    if "version" in config["project"] or "version" not in config["project"].get(
        "dynamic", []
    ):
        raise ValueError("project version must use the single dynamic source")
    path = config["tool"]["hatch"]["version"]["path"]
    if path != "src/reposteward/__init__.py":
        raise ValueError("unexpected version source")
    tree = ast.parse((root / path).read_text())
    values = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
    ]
    if (
        len(values) != 1
        or not isinstance(values[0], str)
        or not re.fullmatch(
            r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", values[0]
        )
    ):
        raise ValueError("release policy requires one MAJOR.MINOR.PATCH source version")
    return values[0]


def check(root: Path, dist_dir: Path, tag: str | None = None) -> dict:
    version = source_version(root)
    if tag is not None:
        if tag != f"v{version}":
            raise ValueError("tag does not match source version")
        heading = re.search(
            rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$",
            (root / "CHANGELOG.md").read_text(),
            re.MULTILINE,
        )
        if heading is None:
            raise ValueError("tagged release requires a dated changelog entry")
        date.fromisoformat(heading[1])
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("use a clean directory containing exactly one wheel and sdist")
    with zipfile.ZipFile(wheels[0]) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("duplicate wheel members")
        wheel = {name: archive.read(name) for name in archive.namelist()}
    prefix = f"reposteward-{version}/"
    with tarfile.open(sdists[0], "r:gz") as archive:
        source = {}
        for item in archive.getmembers():
            if not item.name.startswith(prefix):
                raise ValueError("unexpected sdist root")
            if item.isdir():
                continue
            if not item.isfile() or item.name in source:
                raise ValueError("non-regular or duplicate sdist member")
            stream = archive.extractfile(item)
            if stream is None:
                raise ValueError("unreadable sdist member")
            with stream:
                source[item.name] = stream.read()
    metadata = [
        data for name, data in wheel.items() if name.endswith(".dist-info/METADATA")
    ]
    if len(metadata) != 1 or prefix + "PKG-INFO" not in source:
        raise ValueError("missing or ambiguous package metadata")
    for data in [metadata[0], source[prefix + "PKG-INFO"]]:
        parsed = BytesParser().parsebytes(data)
        if parsed["Name"] != "reposteward" or parsed["Version"] != version:
            raise ValueError("distribution metadata does not match source version")
    package = root / "src/reposteward"
    expected = {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in package.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }
    for name, data in expected.items():
        if wheel.get(name.removeprefix("src/")) != data:
            raise ValueError(f"wheel missing or stale resource: {name}")
        if source.get(prefix + name) != data:
            raise ValueError(f"sdist missing or stale resource: {name}")
    for name in (
        "README.md",
        "README.zh-CN.md",
        "CHANGELOG.md",
        "pyproject.toml",
        "uv.lock",
        "scripts/check_release.py",
        "docs/agent-plugin.zh-CN.md",
        "docs/releases.md",
    ):
        if source.get(prefix + name) != (root / name).read_bytes():
            raise ValueError(
                f"sdist missing or stale documentation/configuration: {name}"
            )
    runner = (root / "docker/Dockerfile.runner").read_bytes()
    if wheel.get("reposteward/data/Dockerfile.runner") != runner:
        raise ValueError("wheel missing or stale runner")
    if source.get(prefix + "docker/Dockerfile.runner") != runner:
        raise ValueError("sdist missing or stale runner")
    return {
        "version": version,
        "tag": tag,
        "release_published": False,
        "package_files_checked": len(expected),
        "artifacts": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [wheels[0], sdists[0]]
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument(
        "--tag", help="also require matching vMAJOR.MINOR.PATCH and dated changelog"
    )
    args = parser.parse_args()
    try:
        result = check(args.root, args.dist_dir, args.tag)
    except (
        ValueError,
        OSError,
        KeyError,
        SyntaxError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        parser.exit(1, f"release check failed: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
