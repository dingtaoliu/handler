"""Centralized path constants for the handler package.

Every module that needs project paths should import from here instead of
recomputing them locally. The watchdog is an exception — it runs standalone
and keeps its own path definitions.
"""

from datetime import date
import os
from pathlib import Path
import sysconfig

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent  # src/handler -> src -> repo root
SCRIPTS_DIR = Path(sysconfig.get_path("scripts"))

# Workspace: ~/.handler by default, overridable via HANDLER_DATA_DIR env var.
# Kept separate from the package so handler works when installed via pip.
DATA_DIR = Path(os.environ.get("HANDLER_DATA_DIR") or Path.home() / ".handler")
CONFIG_DIR = DATA_DIR / "config"
MEMORY_DIR = DATA_DIR / "memory"
DB_PATH = DATA_DIR / "handler.db"
MODELS_CONFIG_PATH = CONFIG_DIR / "models.json"
PID_PATH = DATA_DIR / "handler.pid"
LOG_DIR = DATA_DIR / "logs"
SHELL_LOG_DIR = DATA_DIR / "shell_logs"
UPLOAD_DIR = DATA_DIR / "uploads"
GMAIL_UPLOAD_DIR = UPLOAD_DIR / "gmail"
GDRIVE_UPLOAD_DIR = UPLOAD_DIR / "gdrive"
USERS_DIR = DATA_DIR / "users"
LEGACY_MEMORY_DIR = MEMORY_DIR
LEGACY_CREDENTIALS_DIR = DATA_DIR / "credentials"

# Keep LOG_PATH as a compatibility alias pointing at today's log
LOG_PATH = LOG_DIR / f"handler-{date.today().isoformat()}.log"


def get_log_path(d: date | None = None) -> Path:
    """Return the log file path for a given date (defaults to today)."""
    return LOG_DIR / f"handler-{(d or date.today()).isoformat()}.log"


def with_scripts_dir_on_path(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment with the active Python scripts dir prepended to PATH."""
    source = dict(os.environ if env is None else env)
    scripts_dir = str(SCRIPTS_DIR.resolve())
    path_parts = [part for part in source.get("PATH", "").split(os.pathsep) if part]
    if scripts_dir not in path_parts:
        source["PATH"] = os.pathsep.join([scripts_dir, *path_parts]) if path_parts else scripts_dir
    return source


def ensure_scripts_dir_on_path() -> None:
    """Mutate the current process environment so console scripts are discoverable."""
    os.environ.update(with_scripts_dir_on_path())
