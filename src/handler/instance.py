"""Instance metadata and workspace resolution helpers.

An instance is a self-contained Handler workspace. The legacy single-instance
layout still lives directly at ~/.handler; named instances live under
~/.handler/instances/<instance_id>/.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

INSTANCE_ENV_VAR = "HANDLER_INSTANCE"
DATA_DIR_ENV_VAR = "HANDLER_DATA_DIR"
INSTANCE_META_FILENAME = "instance.json"
DEFAULT_INSTANCE_ID = "default"
DEFAULT_INSTANCE_HOST = "0.0.0.0"
DEFAULT_INSTANCE_PORT = 8000
_BASE_DIR = Path.home() / ".handler"
_INSTANCES_SUBDIR = "instances"


def default_root_dir() -> Path:
    return _BASE_DIR


def instances_parent_dir(base_dir: Path | None = None) -> Path:
    return (base_dir or default_root_dir()) / _INSTANCES_SUBDIR


def slugify_instance_id(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("instance id cannot be empty")
    return slug


def canonical_instance_id(value: str | None) -> str:
    if not value:
        return DEFAULT_INSTANCE_ID
    slug = slugify_instance_id(value)
    return DEFAULT_INSTANCE_ID if slug in {"legacy", DEFAULT_INSTANCE_ID} else slug


def resolve_instance_dir(instance_id: str | None = None) -> Path:
    explicit_data_dir = os.environ.get(DATA_DIR_ENV_VAR, "").strip()
    if explicit_data_dir:
        return Path(explicit_data_dir).expanduser().resolve()

    resolved_instance_id = canonical_instance_id(
        instance_id or os.environ.get(INSTANCE_ENV_VAR)
    )
    if resolved_instance_id == DEFAULT_INSTANCE_ID:
        return default_root_dir()
    return (instances_parent_dir() / resolved_instance_id).resolve()


def instance_id_for_dir(data_dir: Path) -> str:
    resolved = data_dir.expanduser().resolve()
    if resolved == default_root_dir().resolve():
        return DEFAULT_INSTANCE_ID
    parent = instances_parent_dir().resolve()
    try:
        relative = resolved.relative_to(parent)
    except ValueError:
        return resolved.name
    parts = relative.parts
    return parts[0] if parts else DEFAULT_INSTANCE_ID


@dataclass(frozen=True)
class InstanceMetadata:
    id: str
    display_name: str
    host: str = DEFAULT_INSTANCE_HOST
    port: int = DEFAULT_INSTANCE_PORT
    created_at: str = ""
    enabled_channels: tuple[str, ...] = ("web", "scheduler")

    @property
    def is_default(self) -> bool:
        return self.id == DEFAULT_INSTANCE_ID


def instance_meta_path(data_dir: Path) -> Path:
    return data_dir / INSTANCE_META_FILENAME


def default_instance_metadata(instance_id: str, data_dir: Path) -> InstanceMetadata:
    display_name = "Default" if instance_id == DEFAULT_INSTANCE_ID else instance_id
    return InstanceMetadata(
        id=instance_id,
        display_name=display_name,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def load_instance_metadata(
    data_dir: Path, instance_id: str | None = None
) -> InstanceMetadata:
    resolved_dir = data_dir.expanduser().resolve()
    resolved_instance_id = canonical_instance_id(
        instance_id or instance_id_for_dir(resolved_dir)
    )
    meta_path = instance_meta_path(resolved_dir)
    if not meta_path.exists():
        return default_instance_metadata(resolved_instance_id, resolved_dir)

    try:
        raw = json.loads(meta_path.read_text())
    except Exception:
        return default_instance_metadata(resolved_instance_id, resolved_dir)

    if not isinstance(raw, dict):
        return default_instance_metadata(resolved_instance_id, resolved_dir)

    host = str(raw.get("host") or DEFAULT_INSTANCE_HOST)
    try:
        port = int(raw.get("port") or DEFAULT_INSTANCE_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_INSTANCE_PORT
    enabled_channels = raw.get("enabled_channels") or ["web", "scheduler"]
    if not isinstance(enabled_channels, list):
        enabled_channels = ["web", "scheduler"]

    return InstanceMetadata(
        id=canonical_instance_id(str(raw.get("id") or resolved_instance_id)),
        display_name=str(raw.get("display_name") or resolved_instance_id),
        host=host,
        port=port,
        created_at=str(raw.get("created_at") or ""),
        enabled_channels=tuple(
            str(channel) for channel in enabled_channels if str(channel).strip()
        )
        or ("web", "scheduler"),
    )


def write_instance_metadata(data_dir: Path, metadata: InstanceMetadata) -> None:
    resolved_dir = data_dir.expanduser().resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    payload = asdict(metadata)
    payload["enabled_channels"] = list(metadata.enabled_channels)
    instance_meta_path(resolved_dir).write_text(json.dumps(payload, indent=2) + "\n")


def ensure_instance_layout(
    instance_id: str | None = None,
    *,
    host: str = DEFAULT_INSTANCE_HOST,
    port: int = DEFAULT_INSTANCE_PORT,
    display_name: str | None = None,
) -> tuple[Path, InstanceMetadata]:
    resolved_instance_id = canonical_instance_id(instance_id)
    data_dir = resolve_instance_dir(resolved_instance_id)
    data_dir.mkdir(parents=True, exist_ok=True)
    metadata = load_instance_metadata(data_dir, resolved_instance_id)
    if not instance_meta_path(data_dir).exists():
        metadata = InstanceMetadata(
            id=resolved_instance_id,
            display_name=display_name or metadata.display_name,
            host=host,
            port=port,
            created_at=metadata.created_at or datetime.now(timezone.utc).isoformat(),
            enabled_channels=metadata.enabled_channels,
        )
        write_instance_metadata(data_dir, metadata)
    return data_dir, metadata


def discover_instance_dirs() -> list[Path]:
    discovered: list[Path] = []
    legacy_root = default_root_dir()
    if legacy_root.exists():
        discovered.append(legacy_root)
    parent = instances_parent_dir()
    if parent.exists():
        for child in sorted(path for path in parent.iterdir() if path.is_dir()):
            discovered.append(child)
    return discovered


def is_instance_dir(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    if instance_meta_path(resolved).exists():
        return True
    return any(
        (resolved / marker).exists() for marker in ("config", "memory", "handler.db")
    )


def discover_instances() -> list[tuple[Path, InstanceMetadata]]:
    results: list[tuple[Path, InstanceMetadata]] = []
    for data_dir in discover_instance_dirs():
        if not is_instance_dir(data_dir):
            continue
        instance_id = instance_id_for_dir(data_dir)
        results.append((data_dir, load_instance_metadata(data_dir, instance_id)))
    return results
