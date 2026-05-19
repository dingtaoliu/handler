"""Centralized path constants for the handler package.

Every module that needs project paths should import from here instead of
recomputing them locally. The selected instance workspace is resolved from the
active process environment and can be reconfigured before the rest of the
runtime is imported.
"""

from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path
import sysconfig

from .instance import (
    DATA_DIR_ENV_VAR,
    INSTANCE_ENV_VAR,
    DEFAULT_INSTANCE_ID,
    InstanceMetadata,
    load_instance_metadata,
    resolve_instance_dir,
)

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent  # src/handler -> src -> repo root
SCRIPTS_DIR = Path(sysconfig.get_path("scripts"))


@dataclass(frozen=True)
class InstancePaths:
    instance_id: str
    metadata: InstanceMetadata
    data_dir: Path
    config_dir: Path
    memory_dir: Path
    db_path: Path
    models_config_path: Path
    pid_path: Path
    log_dir: Path
    shell_log_dir: Path
    upload_dir: Path
    gmail_upload_dir: Path
    gdrive_upload_dir: Path
    users_dir: Path
    credentials_dir: Path
    instance_meta_path: Path
    tasks_dir: Path

    @property
    def legacy_memory_dir(self) -> Path:
        return self.memory_dir

    @property
    def legacy_credentials_dir(self) -> Path:
        return self.credentials_dir

    def get_log_path(self, d: date | None = None) -> Path:
        return self.log_dir / f"handler-{(d or date.today()).isoformat()}.log"


def resolve_instance_paths(instance_id: str | None = None) -> InstancePaths:
    data_dir = resolve_instance_dir(instance_id)
    metadata = load_instance_metadata(
        data_dir,
        instance_id or os.environ.get(INSTANCE_ENV_VAR) or DEFAULT_INSTANCE_ID,
    )
    config_dir = data_dir / "config"
    upload_dir = data_dir / "uploads"
    return InstancePaths(
        instance_id=metadata.id,
        metadata=metadata,
        data_dir=data_dir,
        config_dir=config_dir,
        memory_dir=data_dir / "memory",
        db_path=data_dir / "handler.db",
        models_config_path=config_dir / "models.json",
        pid_path=data_dir / "handler.pid",
        log_dir=data_dir / "logs",
        shell_log_dir=data_dir / "shell_logs",
        upload_dir=upload_dir,
        gmail_upload_dir=upload_dir / "gmail",
        gdrive_upload_dir=upload_dir / "gdrive",
        users_dir=data_dir / "users",
        credentials_dir=data_dir / "credentials",
        instance_meta_path=data_dir / "instance.json",
        tasks_dir=data_dir / "tasks",
    )


_ACTIVE_PATHS = resolve_instance_paths()


def _sync_globals(paths: InstancePaths) -> None:
    globals().update(
        {
            "INSTANCE_ID": paths.instance_id,
            "INSTANCE_METADATA": paths.metadata,
            "DATA_DIR": paths.data_dir,
            "CONFIG_DIR": paths.config_dir,
            "MEMORY_DIR": paths.memory_dir,
            "DB_PATH": paths.db_path,
            "MODELS_CONFIG_PATH": paths.models_config_path,
            "PID_PATH": paths.pid_path,
            "LOG_DIR": paths.log_dir,
            "SHELL_LOG_DIR": paths.shell_log_dir,
            "UPLOAD_DIR": paths.upload_dir,
            "GMAIL_UPLOAD_DIR": paths.gmail_upload_dir,
            "GDRIVE_UPLOAD_DIR": paths.gdrive_upload_dir,
            "USERS_DIR": paths.users_dir,
            "LEGACY_MEMORY_DIR": paths.legacy_memory_dir,
            "LEGACY_CREDENTIALS_DIR": paths.legacy_credentials_dir,
            "INSTANCE_META_PATH": paths.instance_meta_path,
            "TASKS_DIR": paths.tasks_dir,
            "LOG_PATH": paths.get_log_path(),
        }
    )


def configure_instance(
    instance_id: str | None = None, *, data_dir: str | Path | None = None
) -> InstancePaths:
    if data_dir is not None:
        os.environ[DATA_DIR_ENV_VAR] = str(Path(data_dir).expanduser().resolve())
        os.environ.pop(INSTANCE_ENV_VAR, None)
    elif instance_id is not None:
        os.environ[INSTANCE_ENV_VAR] = instance_id
        os.environ.pop(DATA_DIR_ENV_VAR, None)

    paths = resolve_instance_paths(instance_id)
    globals()["_ACTIVE_PATHS"] = paths
    _sync_globals(paths)
    return paths


def current_instance_paths() -> InstancePaths:
    return _ACTIVE_PATHS


_sync_globals(_ACTIVE_PATHS)


def get_log_path(d: date | None = None) -> Path:
    """Return the log file path for a given date (defaults to today)."""
    return current_instance_paths().get_log_path(d)


def with_scripts_dir_on_path(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment with the active Python scripts dir prepended to PATH."""
    source = dict(os.environ if env is None else env)
    scripts_dir = str(SCRIPTS_DIR.resolve())
    path_parts = [part for part in source.get("PATH", "").split(os.pathsep) if part]
    if scripts_dir not in path_parts:
        source["PATH"] = (
            os.pathsep.join([scripts_dir, *path_parts]) if path_parts else scripts_dir
        )
    return source


def ensure_scripts_dir_on_path() -> None:
    """Mutate the current process environment so console scripts are discoverable."""
    os.environ.update(with_scripts_dir_on_path())
