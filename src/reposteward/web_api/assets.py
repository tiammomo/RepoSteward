"""Load only build-manifest assets from an installed wheel or source checkout."""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from importlib.resources import files
from pathlib import Path


@lru_cache(maxsize=1)
def load_assets() -> tuple[dict[str, tuple[bytes, str]], str]:
    root = files("reposteward").joinpath("web_dist")
    if not root.joinpath("assets.json").is_file():
        root = Path(__file__).resolve().parents[3] / "frontend/dist"
    try:
        manifest = json.loads(root.joinpath("assets.json").read_text())
        if manifest["version"] != 1 or len(manifest["files"]) > 100:
            raise ValueError("invalid manifest")
        assets = {}
        for entry in manifest["files"]:
            path = entry["path"]
            if path != "index.html" and not re.fullmatch(
                r"assets/[A-Za-z0-9_.-]+", path
            ):
                raise ValueError("invalid asset path")
            body = root.joinpath(path).read_bytes()
            if (
                len(body) > 2_000_000
                or hashlib.sha256(body).hexdigest() != entry["sha256"]
            ):
                raise ValueError("asset digest mismatch")
            assets["/" if path == "index.html" else "/" + path] = (body, entry["mime"])
        assets["/app.js"] = assets["/" + manifest["entry_js"]]
        assets["/app.css"] = assets["/" + manifest["entry_css"]]
        if "/" not in assets:
            raise ValueError("missing index")
        return assets, manifest["inputs_digest"]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "Workbench assets missing or invalid; build or reinstall RepoSteward."
        ) from exc
