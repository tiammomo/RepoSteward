"""Copy locked npm CAS archives through a host-only, content-verified cache."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

MAX_FILES = 4096
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_BYTES = 512 * 1024 * 1024
MAX_LOCK_BYTES = 16 * 1024 * 1024


def _safe(path: Path) -> None:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("cache paths must not contain symlinks")


class NpmDownloadCache:
    def __init__(self, directory: Path, key: str, digests: set[str]):
        self.directory = directory / key
        self.digests = digests
        self.report = {
            "key": key,
            "restored_files": 0,
            "saved_files": 0,
            "invalid_files": 0,
        }

    @classmethod
    def for_workspace(cls, config, policy, worktree: Path):
        root = config.runner.npm_download_cache_dir
        image = config.runner.image
        if (
            root is None
            or not policy.bootstrap_commands
            or not re.fullmatch(
                r"(?:sha256:[0-9a-f]{64}|[^\s]+@sha256:[0-9a-f]{64})", image
            )
        ):
            return None
        try:
            _safe(root)
            if root.resolve().is_relative_to(worktree.resolve()):
                return None
            locks, digests, size = [], set(), 0
            for parent, directories, files in os.walk(worktree, followlinks=False):
                directories[:] = sorted(
                    name
                    for name in directories
                    if name
                    not in {
                        ".git",
                        "node_modules",
                        ".venv",
                        "venv",
                        "dist",
                        "__pycache__",
                        ".reposteward",
                        ".starfix",
                    }
                    and not (Path(parent) / name).is_symlink()
                )
                if "package-lock.json" not in files:
                    continue
                path = Path(parent) / "package-lock.json"
                _safe(path)
                if len(locks) >= 64 or path.stat().st_size + size > MAX_LOCK_BYTES:
                    return None
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        return None
                    data = stream.read(MAX_LOCK_BYTES - size + 1)
                if len(data) + size > MAX_LOCK_BYTES:
                    return None
                size += len(data)
                lock = json.loads(data)
                if lock.get("lockfileVersion") not in (2, 3) or not isinstance(
                    lock.get("packages"), dict
                ):
                    return None
                locks.append(
                    (str(path.relative_to(worktree)), hashlib.sha256(data).hexdigest())
                )
                for package in lock["packages"].values():
                    for integrity in str(package.get("integrity", "")).split():
                        if not integrity.startswith("sha512-"):
                            continue
                        digest = base64.b64decode(integrity[7:], validate=True)
                        if len(digest) == 64:
                            digests.add(digest.hex())
                if len(digests) > MAX_FILES:
                    return None
            if not locks or not digests:
                return None
            material = {
                "version": 1,
                "repository": policy.name.casefold(),
                "account": config.github.login.casefold(),
                "api": config.github.api_url,
                "image": image,
                "bootstrap": policy.bootstrap_commands,
                "locks": sorted(locks),
            }
            key = hashlib.sha256(
                json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return cls(root, key, digests)
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def _npm_path(environment: Path, digest: str) -> Path:
        return (
            environment
            / "npm-cache/_cacache/content-v2/sha512"
            / digest[:2]
            / digest[2:4]
            / digest[4:]
        )

    def _copy(self, source: Path, target: Path, digest: str, remaining: int) -> int:
        temporary = None
        try:
            _safe(source)
            _safe(target)
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > min(
                    MAX_FILE_BYTES, remaining
                ):
                    raise ValueError("archive exceeds cache limits")
                target.parent.mkdir(parents=True, exist_ok=True)
                _safe(target)
                hasher, total = hashlib.sha512(), 0
                with tempfile.NamedTemporaryFile(
                    dir=target.parent, prefix=".archive-", delete=False
                ) as output:
                    temporary = Path(output.name)
                    while chunk := stream.read(1024 * 1024):
                        total += len(chunk)
                        if total > min(MAX_FILE_BYTES, remaining):
                            raise ValueError("archive changed beyond cache limits")
                        hasher.update(chunk)
                        output.write(chunk)
                if total != info.st_size or hasher.hexdigest() != digest:
                    raise ValueError("archive does not match locked integrity")
                _safe(target)
                temporary.replace(target)
                return total
        except FileNotFoundError:
            return -1
        except (OSError, ValueError):
            self.report["invalid_files"] += 1
            return -1
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def restore(self, environment: Path) -> dict:
        total = 0
        for digest in sorted(self.digests):
            size = self._copy(
                self.directory / digest,
                self._npm_path(environment, digest),
                digest,
                MAX_BYTES - total,
            )
            if size >= 0:
                total += size
                self.report["restored_files"] += 1
        return dict(self.report)

    def save(self, environment: Path) -> dict:
        total = 0
        for digest in sorted(self.digests):
            size = self._copy(
                self._npm_path(environment, digest),
                self.directory / digest,
                digest,
                MAX_BYTES - total,
            )
            if size >= 0:
                total += size
                self.report["saved_files"] += 1
        return dict(self.report)
