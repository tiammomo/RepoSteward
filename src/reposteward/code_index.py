"""Bounded, workspace-bound static index; only explicit scans persist a cache."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .code_facts import PARSER_VERSION, parse_code
from .issues import SENSITIVE_DETAIL
from .projects import (
    ProjectError,
    canonical_digest,
    local_git,
    workspace_metadata,
    workspace_state,
)
from .verifier import UNTRACKED_SANDBOX_EXCLUDED_NAMES, DockerVerifier

MAX_FILES = 2000
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_CACHE_BYTES = 16 * 1024 * 1024
INDEX_VERSION = 1
EXCLUDED = UNTRACKED_SANDBOX_EXCLUDED_NAMES | {
    ".idea",
    ".vscode",
    "dist",
    "build",
    "vendor",
    ".next",
}


class Unreadable(ValueError):
    def __init__(self, reason: str, read_bytes: int = 0):
        super().__init__(reason)
        self.read_bytes = read_bytes


def skip_path(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != name
        or len(name) > 512
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        return "unsafe_path"
    if DockerVerifier._sensitive_path(Path(name)) or any(
        p.casefold()
        in {
            ".ssh",
            ".aws",
            ".gnupg",
            ".npmrc",
            ".pypirc",
            ".netrc",
            "id_rsa",
            "id_ed25519",
        }
        or p.casefold().endswith((".pem", ".key", ".p12", ".pfx"))
        for p in path.parts
    ):
        return "sensitive_path"
    if any(p.casefold() in EXCLUDED for p in path.parts):
        return "excluded_directory"
    return ""


def read_source(root: Path, name: str, *, byte_limit: int = MAX_FILE_BYTES) -> bytes:
    """Open every path component without following links; never open devices/FIFOs."""
    if skip_path(name):
        raise Unreadable("excluded_path")
    descriptors = []
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        parts = PurePosixPath(name).parts
        for part in parts[:-1]:
            descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            descriptors.append(descriptor)
        before = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise Unreadable("non_regular_or_link")
        if before.st_size > min(MAX_FILE_BYTES, byte_limit):
            raise Unreadable(
                "total_bytes_limit"
                if byte_limit < MAX_FILE_BYTES
                else "file_bytes_limit"
            )
        file_fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        descriptors.append(file_fd)
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise Unreadable("source_changed_during_read")
        chunks = []
        size = 0
        while size <= min(MAX_FILE_BYTES, byte_limit):
            chunk = os.read(
                file_fd, min(65536, min(MAX_FILE_BYTES, byte_limit) + 1 - size)
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(file_fd)
        if (after.st_mtime_ns, after.st_ctime_ns, after.st_size) != (
            opened.st_mtime_ns,
            opened.st_ctime_ns,
            opened.st_size,
        ):
            raise Unreadable("source_changed_during_read")
        if size > min(MAX_FILE_BYTES, byte_limit):
            raise Unreadable(
                "total_bytes_limit"
                if byte_limit < MAX_FILE_BYTES
                else "file_bytes_limit"
            )
        data = b"".join(chunks)
        if b"\0" in data:
            raise Unreadable("non_text", size)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Unreadable("non_utf8", size) from exc
        if SENSITIVE_DETAIL.search(text):
            raise Unreadable("sensitive_content", size)
        return data
    except FileNotFoundError as exc:
        raise Unreadable("missing") from exc
    except OSError as exc:
        raise Unreadable("unavailable_or_link") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def inventory(metadata: dict) -> tuple[dict, dict[str, bytes]]:
    root = Path(metadata["root"])
    names = sorted(
        set(
            filter(
                None,
                local_git(
                    root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
                ).split("\0"),
            )
        )
    )
    records, contents = {}, {}
    counts = Counter()
    size = 0
    visited = 0
    for name in names:
        reason = skip_path(name)
        if reason:
            counts[reason] += 1
            continue
        visited += 1
        if visited > MAX_FILES:
            counts["file_count_limit"] += 1
            continue
        try:
            data = read_source(root, name, byte_limit=MAX_TOTAL_BYTES - size)
            if size + len(data) > MAX_TOTAL_BYTES:
                reason = "total_bytes_limit"
            else:
                size += len(data)
                contents[name] = data
                records[name] = hashlib.sha256(data).hexdigest()
                continue
        except Unreadable as exc:
            size += exc.read_bytes
            reason = str(exc)
        records[name] = "excluded:" + reason
        counts[reason] += 1
    summary = {
        "manifest_digest": canonical_digest(names),
        "observed_paths": len(names),
        "indexed_files": len(contents),
        "read_bytes": size,
        "excluded": dict(sorted(counts.items())),
        "records": records,
    }
    return summary, contents


class CodeIndex:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir.expanduser().absolute()

    @staticmethod
    def binding(metadata: dict) -> str:
        return canonical_digest(
            {key: metadata[key] for key in ("identity", "root", "fingerprint")}
        )

    @contextmanager
    def directory(self, root: Path, *, create: bool = False):
        if self.cache_dir.resolve().is_relative_to(root):
            raise ProjectError(
                "understanding cache must be outside the inspected workspace"
            )
        if create:
            self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif not self.cache_dir.exists():
            yield None
            return
        descriptor = os.open(
            self.cache_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ProjectError(
                    "understanding cache must be owned and writable only by the user"
                )
            yield descriptor
        finally:
            os.close(descriptor)

    def load(self, metadata: dict) -> dict | None:
        with self.directory(Path(metadata["root"])) as directory:
            if directory is None:
                return None
            try:
                descriptor = os.open(
                    self.binding(metadata) + ".json",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory,
                )
            except FileNotFoundError:
                return None
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o022
                ):
                    raise ProjectError("unsafe understanding cache file")
                if info.st_size > MAX_CACHE_BYTES:
                    raise ProjectError("understanding cache exceeds the size limit")
                raw = stream.read(MAX_CACHE_BYTES + 1)
            try:
                data = json.loads(raw)
                if (
                    len(raw) > MAX_CACHE_BYTES
                    or data["version"] != INDEX_VERSION
                    or data["parser_version"] != PARSER_VERSION
                    or data["binding"] != self.binding(metadata)
                    or data["digest"]
                    != canonical_digest(
                        {k: v for k, v in data.items() if k != "digest"}
                    )
                ):
                    raise ValueError
                return data
            except (
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                RecursionError,
            ) as exc:
                raise ProjectError(
                    "understanding cache is incompatible or damaged; run scan --rebuild"
                ) from exc

    def scan(self, path: Path, *, rebuild: bool = False) -> dict:
        metadata = workspace_metadata(path)
        root = Path(metadata["root"])
        with self.directory(root, create=True) as directory:
            # Serialize scans; read-only queries see either complete generation.
            fcntl.flock(directory, fcntl.LOCK_EX)
            previous = None if rebuild else self.load(metadata)
            state = workspace_state(root)
            snapshot, contents = inventory(metadata)
            files = {}
            reused = 0
            for name, data in contents.items():
                digest = snapshot["records"][name]
                old = previous["files"].get(name) if previous else None
                if old and old["digest"] == digest:
                    facts = old["facts"]
                    reused += 1
                else:
                    facts = parse_code(name, data.decode("utf-8"))
                files[name] = {"digest": digest, "facts": facts}
            # File changes while parsing must not publish mixed-version observations.
            check, _ = inventory(metadata)
            if (
                check != snapshot
                or workspace_metadata(root) != metadata
                or workspace_state(root) != state
            ):
                raise ProjectError(
                    "workspace changed during scan; retry after edits finish"
                )
            result = {
                "version": INDEX_VERSION,
                "parser_version": PARSER_VERSION,
                "binding": self.binding(metadata),
                "repository": metadata["repository"],
                "host": metadata["host"],
                "state": state,
                "scanned_at": datetime.now(UTC).isoformat(),
                "snapshot": snapshot,
                "files": files,
                "cache": {"reused_files": reused, "parsed_files": len(files) - reused},
                "changes": changes(
                    previous["snapshot"] if previous else None, snapshot
                ),
            }
            result["digest"] = canonical_digest(result)
            raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
            if len(raw) > MAX_CACHE_BYTES:
                raise ProjectError(
                    "parsed index exceeds the cache size limit; previous cache retained"
                )
            temporary = ".scan-" + uuid.uuid4().hex
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(
                    temporary,
                    self.binding(metadata) + ".json",
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                )
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return {
            key: value
            for key, value in result.items()
            if key != "files" and key != "snapshot"
        } | {"coverage": coverage(result)}


def changes(previous: dict | None, current: dict) -> dict:
    before = (
        {p: d for p, d in previous["records"].items() if d != "excluded:missing"}
        if previous
        else {}
    )
    after = {p: d for p, d in current["records"].items() if d != "excluded:missing"}
    paths = {
        "added": sorted(after.keys() - before.keys()),
        "deleted": sorted(before.keys() - after.keys()),
        "changed": sorted(
            name for name in before.keys() & after.keys() if before[name] != after[name]
        ),
    }
    return {
        "counts": {kind: len(names) for kind, names in paths.items()},
        "paths": {kind: names[:12] for kind, names in paths.items()},
        "path_samples_truncated": any(len(names) > 12 for names in paths.values()),
    }


def coverage(index: dict) -> dict:
    languages, capabilities, directories = Counter(), Counter(), Counter()
    for name, record in index["files"].items():
        facts = record["facts"]
        languages[facts["language"]] += 1
        capabilities[facts["status"]] += 1
        directories[name.split("/")[0] if "/" in name else "."] += 1
    return {
        key: value for key, value in index["snapshot"].items() if key != "records"
    } | {
        "languages": dict(languages.most_common()),
        "capabilities": dict(capabilities.most_common()),
        "directories": dict(directories.most_common(30)),
        "omitted_syntax_items": sum(
            r["facts"]["omitted_nodes"] for r in index["files"].values()
        ),
        "limits": {
            "files": MAX_FILES,
            "file_bytes": MAX_FILE_BYTES,
            "total_bytes": MAX_TOTAL_BYTES,
        },
    }
