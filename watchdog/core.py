"""Watchdog core: PID checks, process restart, import testing, rollback."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("handler.watchdog.core")

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _PACKAGE_DIR.parent
_DATA_DIR = _PACKAGE_DIR / "data"
_DB_PATH = _DATA_DIR / "handler.db"
_PID_PATH = _DATA_DIR / "handler.pid"
_LOG_PATH = _DATA_DIR / "handler.log"


# ---------------------------------------------------------------------------
# PID / process helpers
# ---------------------------------------------------------------------------


def _read_pid() -> int | None:
    try:
        return int(_PID_PATH.read_text().strip())
    except Exception:
        return None


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def handler_running() -> bool:
    pid = _read_pid()
    return pid is not None and _is_alive(pid)


def _start_handler() -> None:
    _pip_install()
    log = open(_LOG_PATH, "a")
    subprocess.Popen(
        [sys.executable, "-m", "handler"],
        cwd=str(_PROJECT_ROOT),
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    logger.info("handler started")


def _import_ok() -> bool:
    result = subprocess.run(
        [sys.executable, "-c", "import handler.__main__"],
        capture_output=True,
        cwd=str(_PROJECT_ROOT),
    )
    return result.returncode == 0


def _stable_tag_exists() -> bool:
    result = subprocess.run(
        ["git", "tag", "-l", "handler-stable"],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and "handler-stable" in result.stdout


def _rollback() -> None:
    """Roll back ALL tracked handler source files to the last known-stable version.

    Uses the 'handler-stable' git tag if it exists, otherwise falls back to
    HEAD (the last checkpoint commit). Also runs 'pip install' so installed
    packages match the reverted state.
    """
    ref = "handler-stable" if _stable_tag_exists() else "HEAD"
    logger.warning(f"rolling back to {ref}")
    subprocess.run(
        ["git", "checkout", ref, "--", "handler/", "pyproject.toml"],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
    )
    subprocess.run(
        ["git", "clean", "-fd", "handler/actions_custom/"],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
    )
    _pip_install()


def _pip_install() -> None:
    """Run pip install to sync packages. Best-effort, never raises."""
    cmd = [sys.executable, "-m", "pip", "install", "-e", ".", "-q"]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info("pip install: ok")
        else:
            err = (result.stderr or result.stdout).strip()[:300]
            logger.warning(f"pip install failed (non-fatal): {err}")
    except Exception as e:
        logger.warning(f"pip install failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


def watchdog() -> bool:
    """Restart handler if not running. Returns True if a restart was attempted."""
    if handler_running():
        return False

    logger.warning("handler is not running — attempting restart")

    if not _import_ok():
        logger.warning("import test failed, rolling back")
        _rollback()

    _start_handler()
    return True
