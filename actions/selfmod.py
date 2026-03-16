"""Self-modification tools: write_core_file, mark_stable, restart_self."""

from __future__ import annotations

import logging
import signal
import subprocess
from pathlib import Path

from agents import function_tool

logger = logging.getLogger("handler.actions.selfmod")

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _PACKAGE_DIR.parent


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


@function_tool
def mark_stable() -> str:
    """Tag the current git HEAD as the known-stable version. The watchdog rolls back to this tag on import failure. Call after verifying code changes work."""
    project_root = str(_PROJECT_ROOT)

    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0:
        return "Error: could not read current git HEAD."

    tag = subprocess.run(
        ["git", "tag", "-f", "handler-stable", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if tag.returncode != 0:
        return f"Error creating tag: {tag.stderr.strip()}"

    commit = head.stdout.strip()
    logger.info(f"mark_stable: tagged {commit} as handler-stable")
    return f"Marked commit {commit} as stable (tag: handler-stable)."


@function_tool
def restart_self() -> str:
    """Restart the handler process. Sends SIGTERM after a 3-second delay so the response is delivered first. The watchdog will bring the handler back up within ~1 minute."""
    import os
    import threading

    pid = os.getpid()

    def _kill():
        os.kill(pid, signal.SIGTERM)

    threading.Timer(3.0, _kill).start()
    logger.info(f"restart_self: SIGTERM in 3s (pid={pid})")
    return f"Restarting (PID {pid}). The watchdog will bring the handler back within ~1 minute."


@function_tool
def write_core_file(path: str, content: str) -> str:
    """Rewrite an entire handler source file (.py). Creates a git checkpoint first. Cannot modify handler/watchdog/. Call restart_self() after to reload.

    Args:
        path:    Path relative to project root (e.g. "handler/channels/web.py") or absolute.
        content: Full new content for the file.
    """
    package_dir = _PACKAGE_DIR
    project_root = _PROJECT_ROOT

    p = Path(path)
    if not p.is_absolute():
        p = (project_root / path).resolve()
    else:
        p = p.resolve()

    try:
        p.relative_to(package_dir)
    except ValueError:
        return (
            f"Blocked: '{path}' is outside the handler package. "
            "Use write_file() for non-handler files."
        )

    if p.suffix != ".py":
        return f"Blocked: only .py files allowed. Got: {p.suffix}"

    try:
        p.relative_to(package_dir / "watchdog")
        return "Blocked: watchdog/ is the recovery engine and cannot be modified here."
    except ValueError:
        pass

    _git_checkpoint(
        str(project_root), f"checkpoint: before editing {p.relative_to(project_root)}"
    )

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    except Exception as e:
        return f"Error writing {path}: {e}"

    rel = str(p.relative_to(project_root))
    logger.info(f"write_core_file: wrote {rel} ({len(content)} chars)")
    return f"Written {len(content)} chars to {rel}. Call restart_self() to reload."
