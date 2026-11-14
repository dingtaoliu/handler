"""Per-tenant limits on stored files, checked when something writes.

Only what a tenant's users and agent choose to write is refused: mail and attachments
arriving at the door are always stored, and then count against the total.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

Limit = Callable[[], "int | None"]
RECOUNT_SECONDS = 60.0  # how stale a cached total may get before it is recounted


class UsedBytes:
    """A root's total size, counted once and then kept current on each write.

    Writes add their size, so the check is O(1). Deletions outside these paths
    aren't seen, so the total is recounted in a worker thread once it is a minute
    old; until then it can only overstate what is used.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._bytes: int | None = None
        self._counted_at = 0.0
        self._recounting = False

    def count(self) -> int:
        """Walk the tree now (blocking); call off the event loop."""
        total = directory_size(self.root)
        with self._lock:
            self._bytes, self._counted_at = total, time.monotonic()
        return total

    async def prime(self) -> None:
        await asyncio.to_thread(self.count)

    def used(self) -> int:
        with self._lock:
            known, stale = self._bytes, time.monotonic() - self._counted_at > RECOUNT_SECONDS
        if known is None:
            return self.count()  # not primed: one walk, then cached
        if stale:
            self._recount_later()
        return known

    def add(self, delta: int) -> None:
        with self._lock:
            if self._bytes is not None:
                self._bytes = max(0, self._bytes + delta)

    def _recount_later(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # a worker thread: the next caller on the loop recounts
            return
        with self._lock:
            if self._recounting:
                return
            self._recounting = True

        def recount() -> None:
            try:
                self.count()
            finally:
                with self._lock:
                    self._recounting = False

        loop.run_in_executor(None, recount)


_workspace_limits: dict[Path, tuple[Limit, UsedBytes]] = {}


class StorageLimitReached(ValueError):
    def __init__(self, scope: str, limit_bytes: int) -> None:
        super().__init__(f"This workspace has used its {limit_bytes // (1024 * 1024)} MB of "
                         f"{scope} storage. Delete some files or upgrade the plan.")
        self.scope, self.limit_bytes = scope, limit_bytes

    def payload(self) -> dict[str, Any]:
        """The stable body of a 413."""
        return {"error": "storage_limit_reached", "scope": self.scope,
                "limit_bytes": self.limit_bytes, "message": str(self)}


def directory_size(root: Path) -> int:
    total = 0
    for directory, _, names in os.walk(root):
        for name in names:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                continue
    return total


def register_workspace_limit(root: Path, limit: Limit) -> UsedBytes:
    """Limit writes under `root`. Await `prime()` on the result to count it off the loop."""
    used = UsedBytes(Path(root).resolve())
    _workspace_limits[used.root] = (limit, used)
    return used


def unregister_workspace_limit(root: Path) -> None:
    _workspace_limits.pop(Path(root).resolve(), None)


def ensure_workspace_room(target: Path, adding: int) -> None:
    """Refuse a write of `adding` bytes to `target` if its tenant has no room left."""
    target = Path(target).resolve()
    for root, (limit, used) in _workspace_limits.items():
        if target.is_relative_to(root):
            allowed = limit()
            if allowed is None:
                return
            replaced = target.stat().st_size if target.is_file() else 0
            if used.used() - replaced + adding > allowed:
                raise StorageLimitReached("workspace", allowed)
            used.add(adding - replaced)  # the caller writes it next
            return
