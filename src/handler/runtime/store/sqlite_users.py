"""Users and API tokens (mixin for SQLiteStore).

Tokens are random, shown once and stored only as SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ..types import new_id, utcnow
from .sqlite import _insert, _timestamp, _transaction

ROLES = ("admin", "member")
# No ":" so that "chat:<user>:<conversation>" keys can be matched by prefix.
USER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")


@dataclass
class User:
    id: str
    display_name: str
    role: str = "member"
    created_at: datetime = field(default_factory=utcnow)
    disabled: bool = False


@dataclass
class UserToken:
    """Token metadata. The token itself and its hash are never exposed."""

    id: str
    user_id: str
    label: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class UsersStore(Protocol):
    async def create_user(self, user: User) -> bool:
        """Insert the user; False if the id already exists."""
        ...

    async def get_user(self, user_id: str) -> User | None: ...

    async def list_users(self) -> list[User]: ...

    async def update_user(
        self, user_id: str, *, display_name: str | None = None,
        role: str | None = None, disabled: bool | None = None,
    ) -> User | None:
        """Change the given fields; None if the user doesn't exist."""
        ...

    async def create_token(self, user_id: str, label: str = "") -> tuple[str, str]:
        """Return (token_id, token). The token is only available here."""
        ...

    async def resolve_token(self, token: str) -> User | None:
        """The enabled user owning a live token; records last_used_at."""
        ...

    async def revoke_token(self, token_id: str) -> bool: ...

    async def list_tokens(self, user_id: str) -> list[UserToken]: ...


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def check_user(user: User) -> None:
    if not USER_ID.fullmatch(user.id):
        raise ValueError("user id must match " + USER_ID.pattern)
    if user.role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _user(row: sqlite3.Row) -> User:
    return User(row["id"], row["display_name"], row["role"],
                datetime.fromisoformat(row["created_at"]), bool(row["disabled"]))


def _token(row: sqlite3.Row) -> UserToken:
    return UserToken(row["id"], row["user_id"], row["label"],
                     datetime.fromisoformat(row["created_at"]),
                     _time(row["last_used_at"]), _time(row["revoked_at"]))


class UsersMixin:
    async def create_user(self, user: User) -> bool:
        check_user(user)

        def write(db: sqlite3.Connection) -> bool:
            with _transaction(db):
                if db.execute("SELECT 1 FROM users WHERE id = ?", (user.id,)).fetchone():
                    return False
                _insert(db, "users", {
                    "id": user.id, "display_name": user.display_name, "role": user.role,
                    "created_at": _timestamp(user.created_at), "disabled": int(user.disabled),
                })
                return True

        return await self._call(write)

    async def get_user(self, user_id: str) -> User | None:
        def read(db: sqlite3.Connection) -> User | None:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return _user(row) if row else None

        return await self._call(read)

    async def list_users(self) -> list[User]:
        return await self._call(lambda db: [
            _user(row) for row in db.execute("SELECT * FROM users ORDER BY created_at, rowid")
        ])

    async def update_user(
        self, user_id: str, *, display_name: str | None = None,
        role: str | None = None, disabled: bool | None = None,
    ) -> User | None:
        if role is not None and role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        changes = {"display_name": display_name, "role": role,
                   "disabled": None if disabled is None else int(disabled)}
        changes = {name: value for name, value in changes.items() if value is not None}

        def write(db: sqlite3.Connection) -> User | None:
            with _transaction(db):
                if changes:
                    assignments = ", ".join(f"{name} = ?" for name in changes)
                    db.execute(f"UPDATE users SET {assignments} WHERE id = ?",
                               (*changes.values(), user_id))
                row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                return _user(row) if row else None

        return await self._call(write)

    async def create_token(self, user_id: str, label: str = "") -> tuple[str, str]:
        token, token_id, now = new_token(), new_id("tok"), self._clock()

        def write(db: sqlite3.Connection) -> None:
            with _transaction(db):
                _insert(db, "user_tokens", {
                    "id": token_id, "user_id": user_id, "token_hash": hash_token(token),
                    "label": label, "created_at": _timestamp(now),
                })

        await self._call(write)
        return token_id, token

    async def resolve_token(self, token: str) -> User | None:
        digest, now = hash_token(token), _timestamp(self._clock())

        def resolve(db: sqlite3.Connection) -> User | None:
            with _transaction(db):
                row = db.execute(
                    "SELECT u.* FROM user_tokens t JOIN users u ON u.id = t.user_id "
                    "WHERE t.token_hash = ? AND t.revoked_at IS NULL", (digest,),
                ).fetchone()
                if row is None or row["disabled"]:
                    return None
                db.execute("UPDATE user_tokens SET last_used_at = ? WHERE token_hash = ?",
                           (now, digest))
                return _user(row)

        return await self._call(resolve)

    async def revoke_token(self, token_id: str) -> bool:
        now = _timestamp(self._clock())
        return await self._call(lambda db: bool(db.execute(
            "UPDATE user_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now, token_id),
        ).rowcount))

    async def list_tokens(self, user_id: str) -> list[UserToken]:
        return await self._call(lambda db: [_token(row) for row in db.execute(
            "SELECT * FROM user_tokens WHERE user_id = ? ORDER BY created_at, rowid",
            (user_id,),
        )])
