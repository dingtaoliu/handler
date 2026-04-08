"""Scheduler backend installers: launchd, systemd, crontab, windows."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .core import _DATA_DIR, _PROJECT_ROOT, _LOG_PATH

logger = logging.getLogger("handler.watchdog.backends")

_SCHEDULER_CONFIG = _DATA_DIR / "scheduler.json"
_LAUNCH_LABEL = "com.handler.cron_runner"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCH_LABEL}.plist"
_SYSTEMD_SERVICE = Path.home() / ".config" / "systemd" / "user" / "handler-cron.service"
_SYSTEMD_TIMER = Path.home() / ".config" / "systemd" / "user" / "handler-cron.timer"

_DEFAULT_AUTO_UPDATE = {
    "enabled": True,
    "mode": "release-tag",
    "remote": "origin",
    "check_interval_seconds": 6 * 60 * 60,
    "last_checked_at": "",
    "last_applied_at": "",
    "current_version": "",
    "latest_version": "",
    "last_result": "",
}


def _normalize_auto_update_config(config: dict | None) -> dict:
    merged = dict(_DEFAULT_AUTO_UPDATE)
    if isinstance(config, dict):
        merged.update(config)

    try:
        merged["check_interval_seconds"] = max(
            60,
            int(
                merged.get(
                    "check_interval_seconds",
                    _DEFAULT_AUTO_UPDATE["check_interval_seconds"],
                )
            ),
        )
    except (TypeError, ValueError):
        merged["check_interval_seconds"] = _DEFAULT_AUTO_UPDATE[
            "check_interval_seconds"
        ]

    merged["enabled"] = bool(merged.get("enabled", True))
    merged["mode"] = str(merged.get("mode", "release-tag") or "release-tag")
    merged["remote"] = str(merged.get("remote", "origin") or "origin")

    for key in (
        "last_checked_at",
        "last_applied_at",
        "current_version",
        "latest_version",
        "last_result",
    ):
        merged[key] = str(merged.get(key, "") or "")

    return merged


def _normalize_scheduler_config(data: dict) -> dict:
    return {
        "backend": str(data.get("backend", "none") or "none"),
        "python": str(data.get("python", sys.executable) or sys.executable),
        "installed_at": str(data.get("installed_at", "") or ""),
        "auto_update": _normalize_auto_update_config(data.get("auto_update")),
    }


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def load_scheduler_config() -> dict | None:
    """Load the persisted watchdog backend config, or None if not yet configured."""
    try:
        data = json.loads(_SCHEDULER_CONFIG.read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return _normalize_scheduler_config(data)


def save_scheduler_config(
    backend: str,
    python: str,
    extra: dict | None = None,
) -> None:
    """Persist the chosen watchdog backend to handler/data/scheduler.json."""
    existing = load_scheduler_config() or {}
    auto_update = _normalize_auto_update_config(existing.get("auto_update"))
    if isinstance(extra, dict) and isinstance(extra.get("auto_update"), dict):
        auto_update = _normalize_auto_update_config(
            {
                **auto_update,
                **extra["auto_update"],
            }
        )

    installed_at = (
        existing.get("installed_at") or datetime.now(timezone.utc).isoformat()
    )
    _SCHEDULER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    _SCHEDULER_CONFIG.write_text(
        json.dumps(
            _normalize_scheduler_config(
                {
                    "backend": backend,
                    "python": python,
                    "installed_at": installed_at,
                    "auto_update": auto_update,
                }
            ),
            indent=2,
        )
    )


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------


def detect_scheduler_backends() -> dict:
    """Probe available watchdog scheduler backends on this machine.

    Read-only — no side effects. Returns a dict with:
      platform     — OS name/version
      python       — current interpreter path
      backends     — availability + notes for each backend
      current_config — contents of scheduler.json (or None)
      recommendation — best backend to use on this machine
    """
    import platform

    system = platform.system()
    backends: dict = {}

    def _probe(cmd: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            return subprocess.CompletedProcess(
                cmd, returncode=127, stdout="", stderr="not found"
            )

    # launchd (macOS)
    r = _probe(["launchctl", "version"])
    backends["launchd"] = {
        "available": r.returncode == 0,
        "notes": r.stdout.strip().split("\n")[0]
        if r.returncode == 0
        else "launchctl not found",
    }

    # systemd user (Linux)
    r = _probe(["systemctl", "--user", "--version"])
    backends["systemd"] = {
        "available": r.returncode == 0,
        "notes": r.stdout.strip().split("\n")[0]
        if r.returncode == 0
        else "systemctl not found",
    }

    # crontab (Unix — read test only)
    r = _probe(["crontab", "-l"])
    ct_present = r.returncode in (0, 1)
    backends["crontab"] = {
        "available": ct_present,
        "notes": (
            "crontab present (write not tested — may fail with 'Operation not permitted' on restricted macOS)"
            if ct_present
            else "crontab not found"
        ),
    }

    # Windows Task Scheduler
    r = _probe(["schtasks", "/?"])
    backends["windows"] = {
        "available": r.returncode == 0,
        "notes": "schtasks available" if r.returncode == 0 else "schtasks not found",
    }

    # Recommendation
    if system == "Darwin" and backends["launchd"]["available"]:
        rec = "launchd"
    elif system == "Linux" and backends["systemd"]["available"]:
        rec = "systemd"
    elif system == "Windows" and backends["windows"]["available"]:
        rec = "windows"
    elif backends["crontab"]["available"]:
        rec = "crontab"
    else:
        rec = "none"

    return {
        "platform": f"{system} {platform.release()}",
        "python": sys.executable,
        "backends": backends,
        "current_config": load_scheduler_config(),
        "recommendation": rec,
    }


# ---------------------------------------------------------------------------
# Backend: launchd (macOS)
# ---------------------------------------------------------------------------


def _install_launchd(python: str) -> None:
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCH_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>handler.watchdog</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{_PROJECT_ROOT}</string>
    <key>StartInterval</key>
    <integer>60</integer>
    <key>StandardOutPath</key>
    <string>{_LOG_PATH}</string>
    <key>StandardErrorPath</key>
    <string>{_LOG_PATH}</string>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
"""
    _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _PLIST_PATH.exists() and _PLIST_PATH.read_text() == plist:
        return
    _PLIST_PATH.write_text(plist)
    subprocess.run(["launchctl", "unload", str(_PLIST_PATH)], capture_output=True)
    r = subprocess.run(
        ["launchctl", "load", str(_PLIST_PATH)], capture_output=True, text=True
    )
    if r.returncode != 0:
        raise RuntimeError(f"launchctl load failed: {(r.stderr or r.stdout).strip()}")


def _remove_launchd() -> None:
    if _PLIST_PATH.exists():
        subprocess.run(["launchctl", "unload", str(_PLIST_PATH)], capture_output=True)
        _PLIST_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Backend: systemd user timer (Linux)
# ---------------------------------------------------------------------------


def _install_systemd(python: str) -> None:
    _SYSTEMD_SERVICE.parent.mkdir(parents=True, exist_ok=True)
    _SYSTEMD_SERVICE.write_text(
        f"[Unit]\nDescription=Handler cron runner\n\n"
        f"[Service]\nType=oneshot\n"
        f"ExecStart={python} -m handler.watchdog\n"
        f"WorkingDirectory={_PROJECT_ROOT}\n"
        f"StandardOutput=append:{_LOG_PATH}\n"
        f"StandardError=append:{_LOG_PATH}\n"
    )
    _SYSTEMD_TIMER.write_text(
        f"[Unit]\nDescription=Handler cron runner (every 60s)\nAfter=network.target\n\n"
        f"[Timer]\nOnBootSec=60\nOnUnitActiveSec=60\nUnit=handler-cron.service\n\n"
        f"[Install]\nWantedBy=timers.target\n"
    )
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], capture_output=True, check=True
    )
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "handler-cron.timer"],
        capture_output=True,
        check=True,
    )


def _remove_systemd() -> None:
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", "handler-cron.timer"],
        capture_output=True,
    )
    for p in (_SYSTEMD_SERVICE, _SYSTEMD_TIMER):
        p.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


# ---------------------------------------------------------------------------
# Backend: crontab (Unix fallback)
# ---------------------------------------------------------------------------


def _install_crontab_backend(python: str) -> None:
    entry = f"* * * * * cd '{_PROJECT_ROOT}' && '{python}' -m handler.watchdog >> '{_LOG_PATH}' 2>&1"
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = r.stdout if r.returncode == 0 else ""
    if "handler.watchdog" in existing or "handler.cron_runner" in existing:
        return
    new_ct = existing.rstrip("\n") + ("\n" if existing else "") + entry + "\n"
    result = subprocess.run(
        ["crontab", "-"], input=new_ct, text=True, capture_output=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"crontab write failed: {result.stderr.strip()}")


def _remove_crontab_backend() -> None:
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if r.returncode != 0:
        return
    if "handler.watchdog" not in r.stdout and "handler.cron_runner" not in r.stdout:
        return
    filtered = (
        "\n".join(
            line
            for line in r.stdout.splitlines()
            if "handler.watchdog" not in line and "handler.cron_runner" not in line
        )
        + "\n"
    )
    subprocess.run(["crontab", "-"], input=filtered, text=True, capture_output=True)


# ---------------------------------------------------------------------------
# Backend: Windows Task Scheduler
# ---------------------------------------------------------------------------


def _install_windows(python: str) -> None:
    r = subprocess.run(
        [
            "schtasks",
            "/Create",
            "/SC",
            "MINUTE",
            "/MO",
            "1",
            "/TN",
            "HandlerCronRunner",
            "/TR",
            f'"{python}" -m handler.watchdog',
            "/F",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"schtasks create failed: {r.stderr.strip()}")


def _remove_windows() -> None:
    subprocess.run(
        ["schtasks", "/Delete", "/TN", "HandlerCronRunner", "/F"], capture_output=True
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install_watchdog(backend: str, python: str | None = None) -> bool:
    """Install the watchdog for the given backend and persist config.

    Always runs the backend installer (installers are idempotent).
    Returns True on success.
    """
    python = python or sys.executable

    dispatchers = {
        "launchd": _install_launchd,
        "systemd": _install_systemd,
        "crontab": _install_crontab_backend,
        "windows": _install_windows,
        "none": lambda _: None,
    }
    if backend not in dispatchers:
        raise ValueError(
            f"Unknown backend '{backend}'. Choose: {', '.join(dispatchers)}."
        )

    dispatchers[backend](python)
    save_scheduler_config(backend, python)
    logger.info(f"watchdog installed: backend={backend}, python={python}")
    return True


def remove_watchdog() -> bool:
    """Remove the currently configured watchdog and delete scheduler.json."""
    config = load_scheduler_config()
    if not config:
        return False
    removers = {
        "launchd": _remove_launchd,
        "systemd": _remove_systemd,
        "crontab": _remove_crontab_backend,
        "windows": _remove_windows,
    }
    backend = config.get("backend", "none")
    if backend in removers:
        removers[backend]()
    _SCHEDULER_CONFIG.unlink(missing_ok=True)
    logger.info(f"watchdog removed: backend={backend}")
    return True


def suspend_watchdog() -> bool:
    """Unload the watchdog scheduler without deleting config.

    Used during handler stop/restart so the watchdog doesn't spawn a
    duplicate process during the gap. Config is preserved so
    install_watchdog() can reload it on the next start.
    """
    config = load_scheduler_config()
    if not config:
        return False
    removers = {
        "launchd": _remove_launchd,
        "systemd": _remove_systemd,
        "crontab": _remove_crontab_backend,
        "windows": _remove_windows,
    }
    backend = config.get("backend", "none")
    if backend in removers:
        removers[backend]()
    logger.info(f"watchdog suspended: backend={backend}")
    return True
