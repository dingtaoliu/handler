"""Watchdog core: PID checks, process restart, import testing, rollback."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("handler.watchdog.core")

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _PACKAGE_DIR.parent.parent  # src/handler -> src -> repo root
# Match paths.py: ~/.handler by default, overridable via HANDLER_DATA_DIR
_DATA_DIR = Path(os.environ.get("HANDLER_DATA_DIR") or Path.home() / ".handler")
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


def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
    )


def _head_commit(short: bool = False) -> str:
    args = ["rev-parse"]
    if short:
        args.append("--short")
    args.append("HEAD")
    result = _run_git(*args)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _is_release_tag(tag: str) -> bool:
    return bool(tag) and tag != "handler-stable"


def _release_tags() -> list[str]:
    result = _run_git("tag", "--sort=-version:refname")
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "git tag failed")
    return [tag for tag in result.stdout.splitlines() if _is_release_tag(tag.strip())]


def _current_release_tag() -> str:
    result = _run_git("tag", "--points-at", "HEAD", "--sort=-version:refname")
    if result.returncode != 0:
        return ""
    for tag in result.stdout.splitlines():
        if _is_release_tag(tag.strip()):
            return tag.strip()
    return ""


def _fetch_remote_release_tags(remote: str) -> None:
    result = _run_git("fetch", "--tags", remote)
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout).strip()
            or f"git fetch --tags {remote} failed"
        )


def _checkout_ref(ref: str) -> None:
    result = _run_git("checkout", ref)
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout).strip() or f"git checkout {ref} failed"
        )


def _worktree_dirty() -> bool:
    result = _run_git("status", "--porcelain")
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout).strip() or "git status failed"
        )
    return bool(result.stdout.strip())


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _save_auto_update_state(config: dict, **fields: str | bool | int) -> None:
    from .backends import save_scheduler_config

    auto_update = dict(config.get("auto_update", {}))
    auto_update.update(fields)
    save_scheduler_config(
        config["backend"],
        config["python"],
        extra={"auto_update": auto_update},
    )
    config["auto_update"] = auto_update


def _auto_update_due(auto_update: dict) -> bool:
    last_checked = _parse_timestamp(auto_update.get("last_checked_at", ""))
    if last_checked is None:
        return True
    interval = int(auto_update.get("check_interval_seconds", 6 * 60 * 60))
    elapsed = (datetime.now(timezone.utc) - last_checked).total_seconds()
    return elapsed >= interval


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


def _stop_handler(timeout_seconds: float = 15.0) -> None:
    pid = _read_pid()
    if pid is None:
        _PID_PATH.unlink(missing_ok=True)
        return

    if not _is_alive(pid):
        _PID_PATH.unlink(missing_ok=True)
        return

    logger.info(f"stopping handler for restart (pid={pid})")
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _is_alive(pid):
            _PID_PATH.unlink(missing_ok=True)
            return
        time.sleep(0.5)

    logger.warning(f"handler did not stop after {timeout_seconds}s, forcing kill")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _PID_PATH.unlink(missing_ok=True)


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


def _maybe_apply_release_update() -> bool:
    from .backends import load_scheduler_config

    config = load_scheduler_config()
    if not config:
        return False

    auto_update = config.get("auto_update", {})
    if not auto_update.get("enabled", True):
        return False
    if auto_update.get("mode") != "release-tag":
        return False
    if not _auto_update_due(auto_update):
        return False

    checked_at = datetime.now(timezone.utc).isoformat()
    current_version = _current_release_tag() or _head_commit(short=True) or "unknown"

    try:
        if _worktree_dirty():
            logger.warning("auto-update skipped: git worktree is dirty")
            _save_auto_update_state(
                config,
                last_checked_at=checked_at,
                current_version=current_version,
                last_result="skipped: dirty worktree",
            )
            return False

        remote = str(auto_update.get("remote", "origin") or "origin")
        logger.info(f"auto-update: checking {remote} for newer release tags")
        _fetch_remote_release_tags(remote)
        tags = _release_tags()
        if not tags:
            _save_auto_update_state(
                config,
                last_checked_at=checked_at,
                current_version=current_version,
                latest_version="",
                last_result=f"no release tags found on {remote}",
            )
            return False

        latest_tag = tags[0]
        if _current_release_tag() == latest_tag:
            _save_auto_update_state(
                config,
                last_checked_at=checked_at,
                current_version=latest_tag,
                latest_version=latest_tag,
                last_result="up-to-date",
            )
            return False

        previous_head = _head_commit()
        if not previous_head:
            raise RuntimeError("could not resolve current HEAD")

        logger.info(
            f"auto-update: applying release {latest_tag} (current={current_version})"
        )
        _checkout_ref(latest_tag)
        _pip_install()

        if not _import_ok():
            logger.warning(
                f"auto-update import test failed for {latest_tag}, restoring previous checkout"
            )
            try:
                _checkout_ref(previous_head)
                _pip_install()
            except Exception as restore_error:
                logger.warning(
                    f"auto-update restore to previous HEAD failed: {restore_error}"
                )
            if not _import_ok():
                logger.warning(
                    "previous checkout still unhealthy after restore, rolling back"
                )
                _rollback()

            _save_auto_update_state(
                config,
                last_checked_at=checked_at,
                current_version=current_version,
                latest_version=latest_tag,
                last_result=f"failed: import test failed for {latest_tag}",
            )
            return False

        if handler_running():
            _stop_handler()
        _start_handler()

        applied_at = datetime.now(timezone.utc).isoformat()
        _save_auto_update_state(
            config,
            last_checked_at=checked_at,
            last_applied_at=applied_at,
            current_version=latest_tag,
            latest_version=latest_tag,
            last_result=f"updated to {latest_tag}",
        )
        logger.info(f"auto-update: handler restarted on release {latest_tag}")
        return True
    except Exception as e:
        logger.warning(f"auto-update check failed: {e}", exc_info=True)
        _save_auto_update_state(
            config,
            last_checked_at=checked_at,
            current_version=current_version,
            last_result=f"error: {e}",
        )
        return False


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


def watchdog() -> bool:
    """Apply release updates when due, then restart handler if not running.

    Returns True if an update or restart was attempted.
    """
    if _maybe_apply_release_update():
        return True

    if handler_running():
        return False

    logger.warning("handler is not running — attempting restart")

    if not _import_ok():
        logger.warning("import test failed, rolling back")
        _rollback()

    _start_handler()
    return True
