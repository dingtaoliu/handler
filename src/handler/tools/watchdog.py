"""Watchdog helpers: health check for system prompt injection.

detect_watchdog / configure_watchdog are no longer tools — watchdog is
auto-configured at boot in __main__.py.  get_handler_status is no longer a
tool — it runs automatically and problems are injected into the system prompt.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import subprocess

from ..paths import PID_PATH as _PID_PATH, LOG_PATH as _LOG_PATH

logger = logging.getLogger("handler.tools.watchdog")


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _check_watchdog_active(backend: str) -> str:
    """Return a short string describing whether the watchdog is actively scheduled."""
    try:
        if backend == "launchd":
            label = "com.handler.cron_runner"
            r = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True,
                text=True,
            )
            return "yes" if r.returncode == 0 else "no (not loaded)"
        elif backend == "systemd":
            r = subprocess.run(
                ["systemctl", "--user", "is-active", "handler-cron.timer"],
                capture_output=True,
                text=True,
            )
            return "yes" if r.stdout.strip() == "active" else f"no ({r.stdout.strip()})"
        elif backend == "crontab":
            r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            return "yes" if "cron_runner" in r.stdout else "no (not in crontab)"
        elif backend == "windows":
            r = subprocess.run(
                ["schtasks", "/query", "/tn", "HandlerCronRunner"],
                capture_output=True,
                text=True,
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
    from ..watchdog import load_scheduler_config

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
            problems.append(
                f"Handler process dead — PID {pid} not found. Needs manual restart: python -m handler"
            )

    # Watchdog
    cfg = load_scheduler_config()
    if cfg is None:
        problems.append(
            "Watchdog not configured — handler will not auto-restart if it crashes."
        )
    else:
        backend = cfg.get("backend", "unknown")
        active = _check_watchdog_active(backend)
        if not active.startswith("yes"):
            problems.append(f"Watchdog backend '{backend}' is not active: {active}")

        auto_update = cfg.get("auto_update", {})
        if auto_update.get("enabled", False):
            last_result = str(auto_update.get("last_result", "") or "")
            if last_result.startswith(("error:", "failed:", "skipped:")):
                problems.append(f"Watchdog auto-update issue: {last_result}")

            last_checked = _parse_timestamp(
                str(auto_update.get("last_checked_at", "") or "")
            )
            interval = int(auto_update.get("check_interval_seconds", 0) or 0)
            if last_checked is None:
                problems.append(
                    "Watchdog auto-update has never completed a release check."
                )
            elif interval > 0:
                age = (datetime.now(timezone.utc) - last_checked).total_seconds()
                if age > interval * 2:
                    problems.append(
                        "Watchdog auto-update checks appear stale "
                        f"(last checked {last_checked.isoformat()})."
                    )

    # Recent log errors
    try:
        if _LOG_PATH.exists():
            log_text = _LOG_PATH.read_text(encoding="utf-8", errors="replace")
            tail = log_text.strip().splitlines()[-30:]
            error_lines = [
                line for line in tail if "ERROR" in line or "Traceback" in line
            ]
            if error_lines:
                problems.append(
                    f"Recent log errors ({len(error_lines)} in last 30 lines):\n"
                    + "\n".join(error_lines[-5:])
                )
    except Exception:
        pass

    return problems
