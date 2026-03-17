"""Centralized path constants for the handler package.

Every module that needs project paths should import from here instead of
recomputing them locally. The watchdog is an exception — it runs standalone
and keeps its own path definitions.
"""

from pathlib import Path
from datetime import date

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DATA_DIR = PACKAGE_DIR / "data"
CONFIG_DIR = DATA_DIR / "config"
MEMORY_DIR = DATA_DIR / "memory"
DB_PATH = DATA_DIR / "handler.db"
PID_PATH = DATA_DIR / "handler.pid"
LOG_DIR = DATA_DIR / "logs"
UPLOAD_DIR = DATA_DIR / "uploads"

# Keep LOG_PATH as a compatibility alias pointing at today's log
LOG_PATH = LOG_DIR / f"handler-{date.today().isoformat()}.log"


def get_log_path(d: date | None = None) -> Path:
    """Return the log file path for a given date (defaults to today)."""
    return LOG_DIR / f"handler-{(d or date.today()).isoformat()}.log"
