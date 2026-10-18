"""Durable copies of thread workspaces and isolated harness homes."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from .files import FileStore

logger = logging.getLogger(__name__)


_CREDENTIAL_NAMES = (
    "auth.json", "auth.json.*", "auth.*.json", "auth.toml", "auth.yaml", "auth.yml",
    "*credentials*", "*secret*",
    "token", "tokens", "token.*", "tokens.*", "*-token*", "*_token*", "*.token",
    "api_key*", "api-key*", "*oauth*", "client_secret*", "service-account*.json", "service_account*.json",
    "mcp-auth.*", "mcp_auth.*", ".env", ".env.*", "*.env", ".envrc",
    "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
    "id_ecdsa*", "id_dsa*", ".netrc", ".npmrc", ".pypirc",
    ".aws", ".ssh", ".gnupg", ".azure", ".docker", "gcloud",
)


# Regenerable harness caches. Codex keeps a ~100 MB, ~5,000-file plugin
# catalog in .tmp/; syncing it made every run spend minutes on S3 transfers.
_HOME_CACHE_DIRS = {".tmp", "tmp", ".cache", "cache", "log", "logs"}


def _cache_path(path: Path | str, *, home: bool) -> bool:
    parts = Path(path).parts
    return home and bool(parts) and parts[0] in _HOME_CACHE_DIRS


def _credential_path(path: Path, *, home: bool) -> bool:
    parts = tuple(part.lower() for part in path.parts)
    if any(fnmatch.fnmatchcase(part.lstrip("."), pattern.lstrip("."))
           for part in parts for pattern in _CREDENTIAL_NAMES):
        return True
    # Codex MCP configuration can contain bearer tokens and stdio secrets.
    return parts[-1].startswith("config.toml") and (home or ".codex" in parts)


def _copy_durable(source: Path, target: Path, *, home: bool) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for directory, dirs, files in os.walk(source, followlinks=False):
        base = Path(directory)
        dirs[:] = [name for name in dirs
                   if not (base / name).is_symlink()
                   and not _cache_path((base / name).relative_to(source), home=home)
                   and not _credential_path((base / name).relative_to(source), home=home)]
        for name in files:
            path = base / name
            relative = path.relative_to(source)
            if (path.is_symlink() or not path.is_file() or _cache_path(relative, home=home)
                    or _credential_path(relative, home=home)):
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)


def _digests(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class WorkspaceSync:
    """Remote homes/ maps to the runtime's existing local harness_homes/ layout."""

    def __init__(self, file_store: FileStore | None, local_root: Path) -> None:
        self.file_store = file_store
        self.local_root = Path(local_root)
        # Threads whose local files changed since their last upload. Runs only
        # mark threads dirty; uploads happen off the request path, on a timer
        # and at shutdown (Heroku sends SIGTERM ~30 s before killing a dyno).
        self._dirty: set[str] = set()
        self._flush_lock = asyncio.Lock()

    def mark_dirty(self, thread_slug: str) -> None:
        if self.file_store is not None:
            self._dirty.add(thread_slug)

    @property
    def dirty(self) -> frozenset[str]:
        return frozenset(self._dirty)

    async def flush(self) -> int:
        """Upload every dirty thread; failures stay dirty for the next flush."""
        async with self._flush_lock:
            pending, self._dirty = self._dirty, set()
            done = 0
            for slug in sorted(pending):
                try:
                    await self.persist(slug)
                    done += 1
                except Exception:
                    logger.exception("workspace sync failed", extra={"thread_slug": slug})
                    self._dirty.add(slug)
            return done

    async def run_periodic(self, stop: asyncio.Event, interval_seconds: float) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
            if not stop.is_set() and self._dirty:
                synced = await self.flush()
                logger.info("workspaces synced", extra={"threads": synced})

    def _locations(self, slug: str) -> list[tuple[str, Path, bool]]:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", slug) or slug in (".", ".."):
            raise ValueError(f"invalid thread slug: {slug!r}")
        return [
            (f"workspaces/{slug}/", self.local_root / "workspaces" / slug, False),
            (f"homes/{slug}/", self.local_root / "harness_homes" / slug, True),
        ]

    def _manifest_path(self, prefix: str) -> Path:
        return self.local_root / ".sync" / (prefix.strip("/").replace("/", "__") + ".json")

    def _read_manifest(self, prefix: str) -> dict[str, str]:
        try:
            return json.loads(self._manifest_path(prefix).read_text())
        except (OSError, ValueError):
            return {}

    def _write_manifest(self, prefix: str, digests: dict[str, str]) -> None:
        path = self._manifest_path(prefix)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(digests))
        tmp.replace(path)

    async def restore(self, thread_slug: str) -> None:
        if self.file_store is None:
            return
        for prefix, local, home in self._locations(thread_slug):
            if local.exists():
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            # Publish only a complete download; a failed restore stays retryable.
            with TemporaryDirectory(dir=local.parent, prefix=".restore-") as staging:
                downloaded, ready = Path(staging) / "download", Path(staging) / "ready"
                await self.file_store.sync_down(
                    prefix, downloaded, skip=lambda rel, home=home: _cache_path(rel, home=home))
                await asyncio.to_thread(_copy_durable, downloaded, ready, home=home)
                digests = await asyncio.to_thread(_digests, ready)
                ready.replace(local)
                # What is local now matches the remote copy; don't upload it again.
                self._write_manifest(prefix, digests)

    async def persist(self, thread_slug: str) -> None:
        if self.file_store is None:
            return
        for prefix, local, home in self._locations(thread_slug):
            if not local.is_dir() or local.is_symlink():
                continue
            # Upload from a sanitized snapshot, and only files that changed since
            # the last successful sync (tracked by content hash).
            with TemporaryDirectory(prefix="handler-workspace-") as staging:
                snapshot = Path(staging) / "snapshot"
                await asyncio.to_thread(_copy_durable, local, snapshot, home=home)
                digests = await asyncio.to_thread(_digests, snapshot)
                previous = self._read_manifest(prefix)
                changed = {rel for rel, digest in digests.items() if previous.get(rel) != digest}
                if changed:
                    await self.file_store.sync_up(snapshot, prefix, only=changed)
                self._write_manifest(prefix, digests)
