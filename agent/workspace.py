"""
Workspace: per-session temporary directory for agent file caching.

Files are downloaded from FileStorageService on first access and reused
within the same session, avoiding repeated fetches from storage.

Cleanup:
- Explicitly via workspace.cleanup() after a response is returned
- On server restart (stale directories are swept at startup)
- Via periodic TTL sweep (directories older than WORKSPACE_TTL_HOURS are deleted)
"""

import logging
import shutil
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

WORKSPACE_BASE_DIR = Path(tempfile.gettempdir()) / "agent_workspaces"
WORKSPACE_TTL_HOURS = 24


def sweep_stale_workspaces() -> int:
    """
    Delete workspace directories older than WORKSPACE_TTL_HOURS.

    Should be called once at server startup. Returns the number of
    directories removed.
    """
    if not WORKSPACE_BASE_DIR.exists():
        return 0

    cutoff = time.time() - (WORKSPACE_TTL_HOURS * 3600)
    removed = 0
    for entry in WORKSPACE_BASE_DIR.iterdir():
        if entry.is_dir() and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            logger.info(f"Swept stale workspace: {entry.name}")
            removed += 1

    return removed


class Workspace:
    """
    Temporary local directory that caches files for one agent session.

    Usage:
        workspace = Workspace.create(session_id="conv_42")
        path = workspace.get_file(file_id=7, file_storage_service=svc)
        workspace.cleanup()
    """

    def __init__(self, session_id: str, path: Path) -> None:
        self.session_id = session_id
        self.path = path
        # Maps file_id -> local Path for quick cache lookups
        self._cache: dict[int, Path] = {}

    @classmethod
    def create(cls, session_id: str) -> "Workspace":
        """Create a new workspace directory for the given session."""
        WORKSPACE_BASE_DIR.mkdir(parents=True, exist_ok=True)
        workspace_path = WORKSPACE_BASE_DIR / session_id
        workspace_path.mkdir(exist_ok=True)
        logger.debug(f"Created workspace at {workspace_path}")
        return cls(session_id=session_id, path=workspace_path)

    def get_file(self, file_id: int, file_storage_service, file_record) -> Path:
        """
        Return the local path for a file, downloading it if not cached.

        Args:
            file_id: ID of the File record
            file_storage_service: FileStorageService instance
            file_record: File model instance (provides stored_filename and filename)

        Returns:
            Path to the local cached file
        """
        if file_id in self._cache:
            return self._cache[file_id]

        dest = self.path / f"{file_id}_{file_record.filename}"
        if not dest.exists():
            logger.debug(f"Copying file {file_id} into workspace {self.session_id}")
            source = file_storage_service.get_file_path(file_id)
            dest.write_bytes(source.read_bytes())

        self._cache[file_id] = dest
        return dest

    def list_files(self) -> list[Path]:
        """Return all files currently in this workspace directory."""
        return [p for p in self.path.iterdir() if p.is_file()]

    def put_file(self, filename: str, content: bytes) -> Path:
        """Write arbitrary content to the workspace (e.g. agent-generated output)."""
        dest = self.path / filename
        dest.write_bytes(content)
        return dest

    def cleanup(self) -> None:
        """Delete the workspace directory and all its contents."""
        if self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)
            logger.debug(f"Cleaned up workspace {self.session_id}")
        self._cache.clear()
