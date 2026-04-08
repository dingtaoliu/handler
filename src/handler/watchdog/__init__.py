"""Watchdog: process liveness probe + rollback.

Runs standalone every 60s via a platform scheduler (launchd/systemd/crontab/windows).
Only checks if the handler is alive and restarts it if dead. Rolls back on import
failure. Job execution happens in-process via SchedulerChannel.
"""

from .core import (
    _DATA_DIR,
    _PID_PATH,
    _LOG_PATH,
    _PROJECT_ROOT,
)
from .backends import (
    load_scheduler_config,
    save_scheduler_config,
    detect_scheduler_backends,
    install_watchdog,
    remove_watchdog,
    suspend_watchdog,
)

__all__ = [
    "_DATA_DIR",
    "_PID_PATH",
    "_LOG_PATH",
    "_PROJECT_ROOT",
    "load_scheduler_config",
    "save_scheduler_config",
    "detect_scheduler_backends",
    "install_watchdog",
    "remove_watchdog",
    "suspend_watchdog",
]


def main() -> None:
    """Entry point when run as a standalone liveness probe."""
    import logging

    from .core import watchdog

    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )

    try:
        watchdog()
    except Exception as e:
        logging.getLogger("handler.watchdog").error(
            f"watchdog error (non-fatal): {e}", exc_info=True
        )
