"""Explicitly prepare and validate workbench assets; core packaging never calls Node."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def frontend_digest(root, *, include_backend=True, include_frontend=True):
    paths = [
        root / "hatch_build.py",
        root / "scripts/build_frontend.py",
        root / "pyproject.toml",
        root / "uv.lock",
    ]
    excluded = {
        "node_modules",
        "dist",
        "__pycache__",
        "coverage",
        "test-results",
        "playwright-report",
    }
    folders = ["frontend"] if include_frontend else []
    if include_backend:
        folders.append("src/reposteward/web/api")
    for folder in folders:
        for parent, directories, files in os.walk(root / folder):
            directories[:] = sorted(
                name for name in directories if name not in excluded
            )
            paths.extend(Path(parent) / name for name in files if name != ".DS_Store")
    records = [
        (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(set(paths))
    ]
    return hashlib.sha256(
        json.dumps(records, separators=(",", ":")).encode()
    ).hexdigest()


def build_environment():
    allowed = {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "UV_PROJECT_ENVIRONMENT",
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_LINK_MODE",
        "NPM_CONFIG_CACHE",
        "npm_config_cache",
    }
    result = {k: v for k, v in os.environ.items() if k in allowed}
    result.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    return result


def validate_assets(root, *, require_fresh=True):
    """Reject stale, malformed or tampered prebuilt assets without running Node."""
    frontend = root / "frontend"
    dist = frontend / "dist"
    try:
        if any(
            path.is_symlink() for path in (dist, dist / "assets", dist / "assets.json")
        ):
            raise ValueError("symbolic link in asset bundle")
        record = json.loads((dist / "assets.json").read_text())
        if record["version"] != 1 or (
            require_fresh and record["inputs_digest"] != frontend_digest(root)
        ):
            raise ValueError("stale manifest")
        if require_fresh:
            for field, options in (
                ("backend_digest", {"include_frontend": False}),
                ("bundle_digest", {"include_backend": False}),
            ):
                if field in record and record[field] != frontend_digest(
                    root, **options
                ):
                    raise ValueError("stale build inputs")
        entries = record["files"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("invalid file list")
        paths = set()
        for entry in entries:
            name = entry["path"]
            if name != "index.html" and not re.fullmatch(
                r"assets/[A-Za-z0-9_.-]+", name
            ):
                raise ValueError("invalid asset path")
            path = dist / name
            if (
                name in paths
                or path.is_symlink()
                or path.resolve().parent
                not in {dist.resolve(), (dist / "assets").resolve()}
            ):
                raise ValueError("invalid or duplicate asset")
            data = path.read_bytes()
            mime = {
                ".html": "text/html",
                ".js": "text/javascript",
                ".css": "text/css",
            }.get(path.suffix)
            if (
                len(data) > 2_000_000
                or mime is None
                or entry["mime"] != mime
                or hashlib.sha256(data).hexdigest() != entry["sha256"]
            ):
                raise ValueError("asset digest or type mismatch")
            paths.add(name)
        if not {"index.html", record["entry_js"], record["entry_css"]} <= paths:
            raise ValueError("missing entry asset")
        return record
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "Workbench assets missing, stale or invalid; run "
            "uv run --extra web python scripts/build_frontend.py before a web build."
        ) from exc


def export_schema(root):
    """Export the real API contract before considering reuse after a backend edit."""
    sys.path.insert(0, str(root / "src"))
    try:
        from reposteward.web.api.app import create_app

        schema = create_app().openapi()
    finally:
        sys.path.pop(0)
    path = root / "frontend/openapi.json"
    content = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if not path.is_file() or path.read_text() != content:
        path.write_text(content)


def api_types_digest(frontend):
    try:
        return {
            "schema": hashlib.sha256(
                (frontend / "openapi.json").read_bytes()
            ).hexdigest(),
            "types": hashlib.sha256(
                (frontend / "src/api/generated/schema.ts").read_bytes()
            ).hexdigest(),
            "lock": hashlib.sha256(
                (frontend / "package-lock.json").read_bytes()
            ).hexdigest(),
            "generator": hashlib.sha256(
                (frontend / "package.json").read_bytes()
            ).hexdigest(),
        }
    except OSError:
        return None


def write_manifest(path, record):
    # An interrupted preparation must not destroy the last complete manifest.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=".assets-", suffix=".json", delete=False
        ) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o644)
            stream.write(json.dumps(record, indent=2) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_frontend(root):
    frontend = root / "frontend"
    manifest = frontend / "dist/assets.json"
    try:
        validate_assets(root)
        return "cached"
    except RuntimeError:
        pass
    try:
        previous = validate_assets(root, require_fresh=False)
    except RuntimeError:
        previous = {}
    backend_digest = frontend_digest(root, include_frontend=False)
    previous_types = previous.get("api_types")
    try:
        schema_digest = hashlib.sha256(
            (frontend / "openapi.json").read_bytes()
        ).hexdigest()
    except OSError:
        schema_digest = None
    schema_current = (
        previous.get("backend_digest") == backend_digest
        and isinstance(previous_types, dict)
        and schema_digest is not None
        and previous_types.get("schema") == schema_digest
    )
    if not schema_current:
        export_schema(root)
    if frontend_digest(root, include_frontend=False) != backend_digest:
        raise RuntimeError(
            "Backend inputs changed during API export; retry preparation."
        )
    # Rebind unchanged compiled content to the freshly exported backend contract.
    # Tests and every frontend compiler input remain part of the bundle digest.
    bundle_digest = frontend_digest(root, include_backend=False)
    if previous.get("bundle_digest") == bundle_digest:
        previous["inputs_digest"] = frontend_digest(root)
        previous["backend_digest"] = backend_digest
        write_manifest(manifest, previous)
        validate_assets(root)
        return "contract-unchanged"
    if not shutil.which("npm"):
        raise RuntimeError("Frontend preparation requires Node 22.12+ and npm.")
    env = build_environment()
    lock = frontend / "package-lock.json"
    if not lock.is_file():
        raise RuntimeError(
            "A reviewed frontend package-lock.json is required before building."
        )
    stamp = frontend / "node_modules/.reposteward-lock"
    lock_digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    tools = [
        frontend / "node_modules/.bin" / name
        for name in ("tsc", "vite", "openapi-typescript")
    ]
    if (
        not stamp.is_file()
        or stamp.read_text() != lock_digest
        or not all(path.is_file() for path in tools)
    ):
        subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=frontend,
            env=env,
            check=True,
            timeout=300,
        )
        stamp.write_text(lock_digest)
    types_digest = api_types_digest(frontend)
    if types_digest is None or previous.get("api_types") != types_digest:
        subprocess.run(
            ["npm", "run", "generate"], cwd=frontend, env=env, check=True, timeout=60
        )
    if frontend_digest(root, include_frontend=False) != backend_digest:
        raise RuntimeError(
            "Backend inputs changed during type generation; retry preparation."
        )
    compiled_inputs = frontend_digest(root)
    compiled_bundle = frontend_digest(root, include_backend=False)
    subprocess.run(
        ["npm", "run", "bundle"], cwd=frontend, env=env, check=True, timeout=180
    )
    if frontend_digest(root) != compiled_inputs:
        raise RuntimeError(
            "Build inputs changed during compilation; retry preparation."
        )
    vite = json.loads((frontend / "dist/.vite/manifest.json").read_text())
    entry = vite["index.html"]
    records = []
    for path in sorted((frontend / "dist").rglob("*")):
        name = str(path.relative_to(frontend / "dist"))
        if path.is_file() and (name == "index.html" or name.startswith("assets/")):
            mime = {
                ".html": "text/html",
                ".js": "text/javascript",
                ".css": "text/css",
            }.get(path.suffix)
            if mime is None:
                raise RuntimeError("Unexpected frontend asset type")
            records.append(
                {
                    "path": name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "mime": mime,
                }
            )
    write_manifest(
        manifest,
        {
            "version": 1,
            "inputs_digest": frontend_digest(root),
            "bundle_digest": compiled_bundle,
            "backend_digest": backend_digest,
            "api_types": api_types_digest(frontend),
            "entry_js": entry["file"],
            "entry_css": entry["css"][0],
            "files": records,
        },
    )
    validate_assets(root)
    return "built"


def main():
    parser = argparse.ArgumentParser(
        description="Explicitly prepare and verify workbench assets."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only validate existing assets; never run npm",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.check:
        validate_assets(root)
    else:
        result = prepare_frontend(root)
        print(
            {
                "cached": "Workbench assets are current; no Node commands needed.",
                "contract-unchanged": "API contract unchanged; verified assets reused.",
                "built": "Workbench assets prepared and verified.",
            }[result]
        )


if __name__ == "__main__":
    main()
