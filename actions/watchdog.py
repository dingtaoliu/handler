"""Watchdog helpers: health check for system prompt injection.

detect_watchdog / configure_watchdog are no longer tools — watchdog is
auto-configured at boot in __main__.py.  get_handler_status is no longer a
tool — it runs automatically and problems are injected into the system prompt.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger("handler.actions.watchdog")

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _PACKAGE_DIR.parent


def _check_watchdog_active(backend: str) -> str:
    """Return a short string describing whether the watchdog is actively scheduled."""
    try:
        if backend == "launchd":
            label = "com.handler.cron_runner"
            r = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True, text=True,
            )
            return "yes" if r.returncode == 0 else "no (not loaded)"
        elif backend == "systemd":
            r = subprocess.run(
                ["systemctl", "--user", "is-active", "handler-cron.timer"],
                capture_output=True, text=True,
            )
            return "yes" if r.stdout.strip() == "active" else f"no ({r.stdout.strip()})"
        elif backend == "crontab":
            r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            return "yes" if "cron_runner" in r.stdout else "no (not in crontab)"
        elif backend == "windows":
            r = subprocess.run(
                ["schtasks", "/query", "/tn", "HandlerCronRunner"],
                capture_output=True, text=True,
            )
            return "yes" if r.returncode == 0 else "no (task not found)"
        elif backend == "none":
            return "disabled"
    except Exception as e:
        return f"unknown (check failed: {e})"
    return "unknown"


def get_health_problems() -> list[str]:
    """Run a quick health check and return a list of problem descriptions.

    Returns an empty list if everything is healthy. Called automatically
    during system prompt assembly — only problems are shown to the agent.
    """
    from ..watchdog import load_scheduler_config, _PID_PATH, _LOG_PATH

    problems: list[str] = []

    # Process liveness
    pid: int | None = None
    try:
        pid = int(_PID_PATH.read_text().strip()) if _PID_PATH.exists() else None
    except Exception:
        pass

    if pid is not None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            problems.append(f"Handler process dead — PID {pid} not found. Needs manual restart: python -m handler")

    # Watchdog
    cfg = load_scheduler_config()
    if cfg is None:
        problems.append("Watchdog not configured — handler will not auto-restart if it crashes.")
    else:
        backend = cfg.get("backend", "unknown")
        active = _check_watchdog_active(backend)
        if not active.startswith("yes"):
            problems.append(f"Watchdog backend '{backend}' is not active: {active}")

    # Recent log errors
    try:
        if _LOG_PATH.exists():
            log_text = _LOG_PATH.read_text(encoding="utf-8", errors="replace")
            tail = log_text.strip().splitlines()[-30:]
            error_lines = [line for line in tail if "ERROR" in line or "Traceback" in line]
            if error_lines:
                problems.append(
                    f"Recent log errors ({len(error_lines)} in last 30 lines):\n"
                    + "\n".join(error_lines[-5:])
                )
    except Exception:
        pass

    return problems
