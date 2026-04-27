"""Shared-instance user registry and per-user path helpers.

The current multi-user model is intentionally simple:
- shared config and uploads remain global under ~/.handler/
- each conversation is bound to exactly one user
- each user gets their own profile, memory, and credentials directories
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .paths import DATA_DIR, USERS_DIR, LEGACY_CREDENTIALS_DIR, LEGACY_MEMORY_DIR

logger = logging.getLogger("handler.users")

_USERS_FILE = DATA_DIR / "users.json"
_DEFAULT_USER_ID = "danny"
_DEFAULT_USERS = [
    {"id": "danny", "display_name": "Danny Liu"},
    {
        "id": "zhijian-zhu",
        "display_name": "Zhijian Zhu",
        "aliases": ["zhijian"],
    },
]

DEFAULT_USER_ID = _DEFAULT_USER_ID


def slugify_user_id(value: str) -> str:
    """Normalize a user id for paths and persistence."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("user id cannot be empty")
    return slug


@dataclass(frozen=True)
class InstanceUser:
    id: str
    display_name: str
    telegram_user_ids: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        return slugify_user_id(self.id)

    @property
    def base_dir(self) -> Path:
        return USERS_DIR / self.slug

    @property
    def memory_dir(self) -> Path:
        return self.base_dir / "memory"

    @property
    def credentials_dir(self) -> Path:
        return self.base_dir / "credentials"

    @property
    def profile_path(self) -> Path:
        return self.base_dir / "profile.md"


def _write_default_users_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _USERS_FILE.write_text(json.dumps(_DEFAULT_USERS, indent=2) + "\n")


def _load_users_file() -> list[dict]:
    if not _USERS_FILE.exists():
        _write_default_users_file()
    try:
        data = json.loads(_USERS_FILE.read_text())
    except Exception:
        logger.warning("users.json invalid, rewriting defaults", exc_info=True)
        _write_default_users_file()
        data = json.loads(_USERS_FILE.read_text())
    if not isinstance(data, list):
        raise ValueError("users.json must contain a list")
    return data


def list_users() -> list[InstanceUser]:
    users: list[InstanceUser] = []
    for raw in _load_users_file():
        if not isinstance(raw, dict):
            continue
        user_id = slugify_user_id(str(raw.get("id", "")))
        display_name = str(raw.get("display_name", "")).strip() or user_id
        aliases = {
            slugify_user_id(str(value))
            for value in raw.get("aliases", [])
            if str(value).strip()
        }
        name_parts = [part for part in display_name.split() if part.strip()]
        if name_parts:
            aliases.add(slugify_user_id(name_parts[0]))
        telegram_user_ids = tuple(
            str(value).strip()
            for value in raw.get("telegram_user_ids", [])
            if str(value).strip()
        )
        users.append(
            InstanceUser(
                id=user_id,
                display_name=display_name,
                telegram_user_ids=telegram_user_ids,
                aliases=tuple(sorted(alias for alias in aliases if alias != user_id)),
            )
        )
    if not users:
        users = [
            InstanceUser(id=item["id"], display_name=item["display_name"])
            for item in _DEFAULT_USERS
        ]
    return users


def get_default_user() -> InstanceUser:
    for user in list_users():
        if user.id == _DEFAULT_USER_ID:
            return user
    return list_users()[0]


def get_user(user_id: str | None) -> InstanceUser:
    if not user_id:
        return get_default_user()
    slug = slugify_user_id(user_id)
    for user in list_users():
        if slug == user.id or slug in user.aliases:
            return user
    raise KeyError(slug)


def resolve_user_from_telegram(
    telegram_user_id: int | str | None,
    *,
    username: str | None = None,
    first_name: str | None = None,
) -> InstanceUser | None:
    """Resolve a user from Telegram sender metadata.

    Resolution order:
    - explicit telegram_user_ids entries in users.json
    - exact normalized username/first-name matches against user ids/names
    """

    sender_id = str(telegram_user_id).strip() if telegram_user_id is not None else ""
    for user in list_users():
        if sender_id and sender_id in user.telegram_user_ids:
            return user

    candidates: list[str] = []
    if username:
        candidates.append(slugify_user_id(username))
    if first_name:
        try:
            candidates.append(slugify_user_id(first_name))
        except ValueError:
            pass

    if not candidates:
        return None

    for user in list_users():
        aliases = {user.id}
        aliases.update(user.aliases)
        aliases.update(
            slugify_user_id(part) for part in user.display_name.split() if part.strip()
        )
        if any(candidate in aliases for candidate in candidates):
            return user

    return None


def bootstrap_user_layout() -> None:
    """Ensure the users registry and directories exist.

    Existing single-user data is not moved here aggressively. The migration path is:
    - shared config/uploads stay in their legacy locations
    - Danny's memory and credentials are copied lazily on startup for compatibility
    """

    users = list_users()
    for user in users:
        user.memory_dir.mkdir(parents=True, exist_ok=True)
        user.credentials_dir.mkdir(parents=True, exist_ok=True)
        user.profile_path.parent.mkdir(parents=True, exist_ok=True)
        if not user.profile_path.exists():
            user.profile_path.write_text(f"Name: {user.display_name}\n")

    default_user = get_default_user()
    if LEGACY_MEMORY_DIR.exists() and not any(default_user.memory_dir.glob("*.md")):
        for path in LEGACY_MEMORY_DIR.glob("*.md"):
            target = default_user.memory_dir / path.name
            if not target.exists():
                target.write_text(path.read_text())

    if LEGACY_CREDENTIALS_DIR.exists():
        for filename in ("desktop.json", "token.json", "drive_token.json"):
            src = LEGACY_CREDENTIALS_DIR / filename
            dst = default_user.credentials_dir / filename
            if src.exists() and not dst.exists():
                dst.write_text(src.read_text())


def serialize_users() -> list[dict[str, str]]:
    return [{"id": user.id, "display_name": user.display_name} for user in list_users()]
