"""Shared memory, versioned settings and durable notifications."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from ..types import MemoryTopic, SettingsVersion, new_id
from .sqlite import _insert, _timestamp, _transaction, _values


def _topic(row: sqlite3.Row) -> MemoryTopic:
    return MemoryTopic(**{**dict(row), "updated_at": datetime.fromisoformat(row["updated_at"])})


def _settings(row: sqlite3.Row) -> SettingsVersion:
    return SettingsVersion(**{
        **dict(row), "value": json.loads(row["value"]),
        "updated_at": datetime.fromisoformat(row["updated_at"]),
    })


class MemoryMixin:
    async def list_topics(self) -> list[MemoryTopic]:
        return await self._call(lambda db: [
            _topic(row) for row in db.execute("SELECT * FROM memory_topics ORDER BY name")
        ])

    async def get_topic(self, name: str) -> MemoryTopic | None:
        def read(db: sqlite3.Connection) -> MemoryTopic | None:
            row = db.execute("SELECT * FROM memory_topics WHERE name = ?", (name,)).fetchone()
            return _topic(row) if row else None
        return await self._call(read)

    async def put_topic(
        self, topic: MemoryTopic, expected_version: int | None
    ) -> MemoryTopic | None:
        def write(db: sqlite3.Connection) -> MemoryTopic | None:
            with _transaction(db):
                row = db.execute(
                    "SELECT version FROM memory_topics WHERE name = ?", (topic.name,)
                ).fetchone()
                if (row["version"] if row else None) != expected_version:
                    return None
                stored = replace(topic, version=(expected_version or 0) + 1,
                                 updated_at=self._clock())
                db.execute("DELETE FROM memory_topics WHERE name = ?", (topic.name,))
                _insert(db, "memory_topics", _values(stored))
                return stored
        return await self._call(write)

    async def delete_topic(self, name: str) -> bool:
        return await self._call(lambda db: bool(db.execute(
            "DELETE FROM memory_topics WHERE name = ?", (name,)
        ).rowcount))


class SettingsMixin:
    async def get_settings(self, section: str) -> SettingsVersion | None:
        rows = await self.settings_history(section, limit=1)
        return rows[0] if rows else None

    async def put_settings(
        self, section: str, value: dict, *, updated_by: str | None,
        expected_version: int | None, schema_version: int = 1,
    ) -> SettingsVersion | None:
        def write(db: sqlite3.Connection) -> SettingsVersion | None:
            with _transaction(db):
                latest = db.execute(
                    "SELECT MAX(version) FROM settings_versions WHERE section = ?", (section,)
                ).fetchone()[0]
                if latest != expected_version:
                    return None
                stored = SettingsVersion(section, (latest or 0) + 1, value,
                                         schema_version, updated_by, self._clock())
                values = _values(stored)
                values["value"] = json.dumps(value)
                _insert(db, "settings_versions", values)
                return stored
        return await self._call(write)

    async def settings_history(self, section: str, limit: int = 50) -> list[SettingsVersion]:
        return await self._call(lambda db: [
            _settings(row) for row in db.execute(
                "SELECT * FROM settings_versions WHERE section = ? ORDER BY version DESC LIMIT ?",
                (section, limit),
            )
        ])


@dataclass(frozen=True)
class Notification:
    id: str
    user_id: str
    thread_key: str
    run_id: str | None
    message: str
    created_at: datetime
    read_at: datetime | None = None


class NotificationStore(Protocol):
    async def add_notification(
        self, user_id: str, thread_key: str, run_id: str | None, message: str
    ) -> Notification: ...

    async def list_notifications(
        self, user_id: str, unread_only: bool = False
    ) -> list[Notification]: ...

    async def mark_read(self, id: str) -> bool: ...


class NotificationMixin:
    async def add_notification(
        self, user_id: str, thread_key: str, run_id: str | None, message: str
    ) -> Notification:
        notification = Notification(new_id("ntf"), user_id, thread_key, run_id,
                                    message, self._clock())
        await self._call(lambda db: _insert(db, "notifications", _values(notification)))
        return notification

    async def list_notifications(
        self, user_id: str, unread_only: bool = False
    ) -> list[Notification]:
        def read(db: sqlite3.Connection) -> list[Notification]:
            rows = db.execute(
                "SELECT * FROM notifications WHERE user_id = ? "
                + ("AND read_at IS NULL " if unread_only else "")
                + "ORDER BY created_at DESC, rowid DESC", (user_id,),
            )
            return [Notification(**{
                **dict(row), "created_at": datetime.fromisoformat(row["created_at"]),
                "read_at": datetime.fromisoformat(row["read_at"]) if row["read_at"] else None,
            }) for row in rows]
        return await self._call(read)

    async def mark_read(self, id: str) -> bool:
        return await self._call(lambda db: bool(db.execute(
            "UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE id = ?",
            (_timestamp(self._clock()), id),
        ).rowcount))
