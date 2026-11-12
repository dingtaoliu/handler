"""Per-tenant limits on stored files, checked when something writes.

Only what a tenant's users and agent choose to write is refused: mail and attachments
arriving at the door are always stored, and then count against the total.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

Limit = Callable[[], "int | None"]
_workspace_limits: dict[Path, Limit] = {}


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


def register_workspace_limit(root: Path, limit: Limit) -> None:
    _workspace_limits[Path(root).resolve()] = limit


def unregister_workspace_limit(root: Path) -> None:
    _workspace_limits.pop(Path(root).resolve(), None)


def ensure_workspace_room(target: Path, adding: int) -> None:
    """Refuse a write of `adding` bytes to `target` if its tenant has no room left."""
    target = Path(target).resolve()
    for root, limit in _workspace_limits.items():
        if target.is_relative_to(root):
            allowed = limit()
            if allowed is None:
                return
            replaced = target.stat().st_size if target.is_file() else 0
            if directory_size(root) - replaced + adding > allowed:
                raise StorageLimitReached("workspace", allowed)
            return
