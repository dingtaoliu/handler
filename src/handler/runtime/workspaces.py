"""Durable copies of thread workspaces and isolated harness homes."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from .files import FileStore


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
                   and not _credential_path((base / name).relative_to(source), home=home)]
        for name in files:
            path = base / name
            relative = path.relative_to(source)
            if path.is_symlink() or not path.is_file() or _credential_path(relative, home=home):
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)


class WorkspaceSync:
    """Remote homes/ maps to the runtime's existing local harness_homes/ layout."""

    def __init__(self, file_store: FileStore | None, local_root: Path) -> None:
        self.file_store = file_store
        self.local_root = Path(local_root)

    def _locations(self, slug: str) -> list[tuple[str, Path, bool]]:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", slug) or slug in (".", ".."):
            raise ValueError(f"invalid thread slug: {slug!r}")
        return [
            (f"workspaces/{slug}/", self.local_root / "workspaces" / slug, False),
            (f"homes/{slug}/", self.local_root / "harness_homes" / slug, True),
        ]

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
                await self.file_store.sync_down(prefix, downloaded)
                await asyncio.to_thread(_copy_durable, downloaded, ready, home=home)
                ready.replace(local)

    async def persist(self, thread_slug: str) -> None:
        if self.file_store is None:
            return
        for prefix, local, home in self._locations(thread_slug):
            if not local.is_dir() or local.is_symlink():
                continue
            # sync_up has no filter, so it must only see a sanitized snapshot.
            with TemporaryDirectory(prefix="handler-workspace-") as staging:
                snapshot = Path(staging) / "snapshot"
                await asyncio.to_thread(_copy_durable, local, snapshot, home=home)
                await self.file_store.sync_up(snapshot, prefix)
