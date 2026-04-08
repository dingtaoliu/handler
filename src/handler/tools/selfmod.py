"""Git checkpoint helper used by guarded file-edit tools."""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger("handler.tools.selfmod")


def _git_checkpoint(project_root: str, message: str) -> None:
    """Commit tracked handler files before a code change. Non-fatal if git is unavailable."""
    try:
        subprocess.run(
            ["git", "add", "-A", "handler/"],
            cwd=project_root,
            capture_output=True,
            check=True,
        )
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info(f"git checkpoint: {message}")
        else:
            logger.info("git checkpoint: nothing new to commit, skipped")
    except Exception as e:
        logger.warning(f"git checkpoint failed (non-fatal): {e}")
