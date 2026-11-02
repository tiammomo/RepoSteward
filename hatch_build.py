"""Package core without Node; explicitly selected web builds require prepared assets."""

import os
import runpy
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        selection = os.environ.get("REPOSTEWARD_BUILD_WEB", "0")
        if selection not in {"0", "1"}:
            raise RuntimeError("REPOSTEWARD_BUILD_WEB must be 0 or 1")
        if selection == "0":
            return
        root = Path(self.root)
        support = runpy.run_path(str(root / "scripts/build_frontend.py"))
        support["validate_assets"](root)
        build_data.setdefault("force_include", {})[str(root / "frontend/dist")] = (
            "frontend/dist" if self.target_name == "sdist" else "reposteward/web_dist"
        )
