"""Build and verify the frontend before packaging; wheels need no Node runtime."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def frontend_digest(root):
    paths = [root / "hatch_build.py", root / "pyproject.toml", root / "uv.lock"]
    excluded = {
        "node_modules",
        "dist",
        "__pycache__",
        "generated",
        "coverage",
        "test-results",
        "playwright-report",
    }
    for folder in ("frontend", "src/reposteward/web_api"):
        for parent, directories, files in os.walk(root / folder):
            directories[:] = sorted(
                name for name in directories if name not in excluded
            )
            paths.extend(
                Path(parent) / name
                for name in files
                if name not in {"openapi.json", ".DS_Store"}
            )
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


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        frontend = root / "frontend"
        manifest = frontend / "dist/assets.json"
        digest = frontend_digest(root)
        if manifest.is_file():
            record = json.loads(manifest.read_text())
            if record.get("inputs_digest") == digest and all(
                (frontend / "dist" / f["path"]).is_file()
                and hashlib.sha256(
                    (frontend / "dist" / f["path"]).read_bytes()
                ).hexdigest()
                == f["sha256"]
                for f in record["files"]
            ):
                build_data.setdefault("force_include", {})[str(frontend / "dist")] = (
                    "frontend/dist"
                    if self.target_name == "sdist"
                    else "reposteward/web_dist"
                )
                return
        if not shutil.which("npm"):
            raise RuntimeError(
                "Source builds require Node/npm; install a release wheel for daily use."
            )
        env = build_environment()
        sys.path.insert(0, str(root / "src"))
        try:
            from reposteward.web_api.app import create_app

            schema = create_app().openapi()
        finally:
            sys.path.pop(0)
        (frontend / "openapi.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        lock = frontend / "package-lock.json"
        if not lock.is_file():
            raise RuntimeError(
                "A reviewed frontend package-lock.json is required before building."
            )
        stamp = frontend / "node_modules/.reposteward-lock"
        lock_digest = hashlib.sha256(lock.read_bytes()).hexdigest()
        if not stamp.is_file() or stamp.read_text() != lock_digest:
            subprocess.run(
                ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                cwd=frontend,
                env=env,
                check=True,
                timeout=300,
            )
            stamp.write_text(lock_digest)
        subprocess.run(
            ["npm", "run", "build"], cwd=frontend, env=env, check=True, timeout=180
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
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "inputs_digest": frontend_digest(root),
                    "entry_js": entry["file"],
                    "entry_css": entry["css"][0],
                    "files": records,
                },
                indent=2,
            )
            + "\n"
        )
        build_data.setdefault("force_include", {})[str(frontend / "dist")] = (
            "frontend/dist" if self.target_name == "sdist" else "reposteward/web_dist"
        )
