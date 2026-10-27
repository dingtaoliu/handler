"""Postgres storage for the phase 2 domains (mixins for PostgresStore).

Same semantics as the sqlite_<domain> mixins; uses self._conn(), self._clock,
self._wake and PostgresCore's event routing from postgres.py.
"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, TypeVar

from psycopg.types.json import Jsonb

from .tasks import announce_routed
from ..types import (
    Approval,
    ApprovalStatus,
    AuditEntry,
    Connection,
    Decision,
    Event,
    MemoryTopic,
    Question,
    QuestionStatus,
    Schedule,
    SettingsVersion,
    ThreadSpec,
    new_id,
)
from .postgres import _insert, _notify, _put_event
from . import sqlite as _sqlite  # noqa: F401  (sqlite_memory needs sqlite imported first)
from .sqlite_memory import Notification
from .sqlite_users import ROLES, User, UserToken, check_user, hash_token, new_token

T = TypeVar("T")
_JSON = {"args", "result", "notify", "scopes", "options", "answer"}


def _row_values(record: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in fields(record):
        value = getattr(record, field.name)
        if value is not None and field.name in _JSON:
            value = Jsonb(value)
        elif isinstance(value, Enum):
            value = value.value
        values[field.name] = value
    return values


def _columns(cls: type) -> str:
    return ", ".join(field.name for field in fields(cls))


def _from_row(cls: type[T], row: dict[str, Any]) -> T:
    values = {field.name: row[field.name] for field in fields(cls)}
    if cls is Approval:
        values["status"] = ApprovalStatus(values["status"])
    elif cls is Question:
        values["status"] = QuestionStatus(values["status"])
    elif cls is AuditEntry:
        values["decision"] = Decision(values["decision"])
    return cls(**values)


class _Rows:
    async def _fetch(self, cls: type[T], query: str, parameters: Any = ()) -> list[T]:
        async with self._conn() as conn:
            cursor = await conn.execute(query, parameters)
            return [_from_row(cls, row) for row in await cursor.fetchall()]

    async def _fetch_one(self, cls: type[T], query: str, parameters: Any = ()) -> T | None:
        rows = await self._fetch(cls, query, parameters)
        return rows[0] if rows else None


class ScheduleMixin(_Rows):
    async def create_schedule(self, schedule: Schedule) -> None:
        async with self._conn() as conn:
            await _insert(conn, "schedules", _row_values(schedule))

    async def get_schedule(self, schedule_id: str) -> Schedule | None:
        return await self._fetch_one(
            Schedule, f"SELECT {_columns(Schedule)} FROM schedules WHERE id = %s",
            (schedule_id,),
        )

    async def list_schedules(self, include_disabled: bool = False) -> list[Schedule]:
        where = "" if include_disabled else "WHERE enabled "
        return await self._fetch(
            Schedule, f"SELECT {_columns(Schedule)} FROM schedules {where}"
            "ORDER BY created_at, id",
        )

    async def update_schedule(self, schedule: Schedule) -> None:
        values = _row_values(schedule)
        assignments = ", ".join(f"{name} = %({name})s" for name in values if name != "id")
        async with self._conn() as conn:
            await conn.execute(f"UPDATE schedules SET {assignments} WHERE id = %(id)s", values)

    async def delete_schedule(self, schedule_id: str) -> bool:
        async with self._conn() as conn:
            cursor = await conn.execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
            return bool(cursor.rowcount)

    async def next_due_at(self) -> datetime | None:
        async with self._conn() as conn:
            cursor = await conn.execute(
                "SELECT MIN(next_run_at) AS due FROM schedules WHERE enabled"
            )
            return (await cursor.fetchone())["due"]

    async def due_schedules(self, now: datetime, limit: int = 50) -> list[Schedule]:
        return await self._fetch(
            Schedule, f"SELECT {_columns(Schedule)} FROM schedules "
            "WHERE enabled AND next_run_at <= %s ORDER BY next_run_at, id LIMIT %s",
            (now, max(0, limit)),
        )

    async def fire_schedule(
        self,
        schedule: Schedule,
        expected_next_run_at: datetime,
        next_run_at: datetime | None,
        event: Event,
        thread: ThreadSpec,
    ) -> bool:
        async with self._conn() as conn:
            # The UPDATE takes the row lock: a concurrent fire waits, then
            # re-evaluates next_run_at against the new value and loses.
            cursor = await conn.execute(
                "UPDATE schedules SET last_run_at = %s, next_run_at = %s, "
                "enabled = CASE WHEN %s THEN FALSE ELSE enabled END "
                "WHERE id = %s AND enabled AND next_run_at = %s",
                (expected_next_run_at, next_run_at, next_run_at is None,
                 schedule.id, expected_next_run_at),
            )
            if not cursor.rowcount:
                return False
            routed = await _put_event(conn, event, thread, self._clock())
            if routed:
                await _notify(conn)
        self._wake.set()
        if routed:
            announce_routed(self, event, thread)
        return True


class ApprovalMixin(_Rows):
    async def create_approval(self, approval: Approval) -> None:
        async with self._conn() as conn:
            await _insert(conn, "approvals", _row_values(approval))

    async def get_approval(self, approval_id: str) -> Approval | None:
        return await self._fetch_one(
            Approval, f"SELECT {_columns(Approval)} FROM approvals WHERE id = %s",
            (approval_id,),
        )

    async def list_approvals(
        self,
        status: ApprovalStatus | None = None,
        limit: int = 100,
        thread_key: str | None = None,
    ) -> list[Approval]:
        clauses, params = [], []
        if status:
            clauses.append("status = %s")
            params.append(status.value)
        if thread_key:
            clauses.append("thread_key = %s")
            params.append(thread_key)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        return await self._fetch(
            Approval, f"SELECT {_columns(Approval)} FROM approvals {where}"
            "ORDER BY created_at DESC, seq DESC LIMIT %s", (*params, limit),
        )

    async def transition_approval(
        self,
        approval_id: str,
        expected: ApprovalStatus,
        new: ApprovalStatus,
        *,
        decided_by: str | None = None,
        result: dict | None = None,
    ) -> bool:
        sets = ["status = %s"]
        params: list[Any] = [new.value]
        if decided_by is not None:
            sets += ["decided_by = %s", "decided_at = %s"]
            params += [decided_by, self._clock()]
        if result is not None:
            sets.append("result = %s")
            params.append(Jsonb(result))
        async with self._conn() as conn:
            cursor = await conn.execute(
                f"UPDATE approvals SET {', '.join(sets)} WHERE id = %s AND status = %s",
                (*params, approval_id, expected.value),
            )
            return cursor.rowcount == 1


class QuestionMixin(_Rows):
    async def create_question(self, question: Question) -> None:
        async with self._conn() as conn:
            await _insert(conn, "questions", _row_values(question))

    async def get_question(self, question_id: str) -> Question | None:
        return await self._fetch_one(
            Question, f"SELECT {_columns(Question)} FROM questions WHERE id = %s",
            (question_id,),
        )

    async def list_questions(
        self,
        status: QuestionStatus | None = None,
        limit: int = 100,
        thread_key: str | None = None,
        run_ids: list[str] | None = None,
    ) -> list[Question]:
        clauses, params = [], []
        if status:
            clauses.append("status = %s")
            params.append(status.value)
        if thread_key:
            clauses.append("thread_key = %s")
            params.append(thread_key)
        if run_ids is not None:
            if not run_ids:
                return []
            clauses.append("run_id = ANY(%s)")
            params.append(list(run_ids))
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        return await self._fetch(
            Question, f"SELECT {_columns(Question)} FROM questions {where}"
            "ORDER BY created_at DESC, seq DESC LIMIT %s", (*params, limit),
        )

    async def transition_question(
        self,
        question_id: str,
        expected: QuestionStatus,
        new: QuestionStatus,
        *,
        answer: dict | None = None,
        answered_by: str | None = None,
    ) -> bool:
        sets = ["status = %s"]
        params: list[Any] = [new.value]
        if answer is not None:
            sets += ["answer = %s", "answered_by = %s", "answered_at = %s"]
            params += [Jsonb(answer), answered_by, self._clock()]
        async with self._conn() as conn:
            cursor = await conn.execute(
                f"UPDATE questions SET {', '.join(sets)} WHERE id = %s AND status = %s",
                (*params, question_id, expected.value),
            )
            return cursor.rowcount == 1


class AuditMixin(_Rows):
    async def add_audit(self, entry: AuditEntry) -> None:
        async with self._conn() as conn:
            await _insert(conn, "audit_log", _row_values(entry))

    async def list_audit(
        self, run_id: str | None = None, limit: int = 200
    ) -> list[AuditEntry]:
        where, params = ("WHERE run_id = %s ", (run_id,)) if run_id else ("", ())
        return await self._fetch(
            AuditEntry, f"SELECT {_columns(AuditEntry)} FROM audit_log {where}"
            "ORDER BY at DESC, seq DESC LIMIT %s", (*params, limit),
        )


class MemoryMixin(_Rows):
    async def list_topics(self) -> list[MemoryTopic]:
        return await self._fetch(
            MemoryTopic, f"SELECT {_columns(MemoryTopic)} FROM memory_topics ORDER BY name",
        )

    async def get_topic(self, name: str) -> MemoryTopic | None:
        return await self._fetch_one(
            MemoryTopic, f"SELECT {_columns(MemoryTopic)} FROM memory_topics WHERE name = %s",
            (name,),
        )

    async def put_topic(
        self, topic: MemoryTopic, expected_version: int | None
    ) -> MemoryTopic | None:
        stored = replace(topic, version=(expected_version or 0) + 1, updated_at=self._clock())
        values = _row_values(stored)
        async with self._conn() as conn:
            if expected_version is None:
                columns = ", ".join(values)
                placeholders = ", ".join(f"%({name})s" for name in values)
                cursor = await conn.execute(
                    f"INSERT INTO memory_topics ({columns}) VALUES ({placeholders}) "
                    "ON CONFLICT (name) DO NOTHING", values,
                )
            else:
                values["expected"] = expected_version
                cursor = await conn.execute(
                    "UPDATE memory_topics SET description = %(description)s, "
                    "content = %(content)s, version = %(version)s, "
                    "updated_by = %(updated_by)s, updated_at = %(updated_at)s "
                    "WHERE name = %(name)s AND version = %(expected)s", values,
                )
            return stored if cursor.rowcount else None

    async def delete_topic(self, name: str) -> bool:
        async with self._conn() as conn:
            cursor = await conn.execute("DELETE FROM memory_topics WHERE name = %s", (name,))
            return bool(cursor.rowcount)


class SettingsMixin(_Rows):
    async def get_settings(self, section: str) -> SettingsVersion | None:
        history = await self.settings_history(section, limit=1)
        return history[0] if history else None

    async def put_settings(
        self, section: str, value: dict, *, updated_by: str | None,
        expected_version: int | None, schema_version: int = 1,
    ) -> SettingsVersion | None:
        async with self._conn() as conn:
            cursor = await conn.execute(
                "SELECT MAX(version) AS latest FROM settings_versions WHERE section = %s",
                (section,),
            )
            latest = (await cursor.fetchone())["latest"]
            if latest != expected_version:
                return None
            stored = SettingsVersion(section, (latest or 0) + 1, value, schema_version,
                                     updated_by, self._clock())
            # A concurrent writer of the same version hits the primary key.
            cursor = await conn.execute(
                "INSERT INTO settings_versions (section, version, value, schema_version, "
                "updated_by, updated_at) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (section, version) DO NOTHING",
                (section, stored.version, Jsonb(value), schema_version, updated_by,
                 stored.updated_at),
            )
            return stored if cursor.rowcount else None

    async def settings_history(self, section: str, limit: int = 50) -> list[SettingsVersion]:
        return await self._fetch(
            SettingsVersion, f"SELECT {_columns(SettingsVersion)} FROM settings_versions "
            "WHERE section = %s ORDER BY version DESC LIMIT %s", (section, limit),
        )


class NotificationMixin(_Rows):
    async def add_notification(
        self, user_id: str, thread_key: str, run_id: str | None, message: str
    ) -> Notification:
        notification = Notification(new_id("ntf"), user_id, thread_key, run_id,
                                    message, self._clock())
        async with self._conn() as conn:
            await _insert(conn, "notifications", _row_values(notification))
        return notification

    async def list_notifications(
        self, user_id: str, unread_only: bool = False
    ) -> list[Notification]:
        return await self._fetch(
            Notification, f"SELECT {_columns(Notification)} FROM notifications "
            "WHERE user_id = %s " + ("AND read_at IS NULL " if unread_only else "")
            + "ORDER BY created_at DESC, seq DESC", (user_id,),
        )

    async def mark_read(self, id: str) -> bool:
        async with self._conn() as conn:
            cursor = await conn.execute(
                "UPDATE notifications SET read_at = COALESCE(read_at, %s) WHERE id = %s",
                (self._clock(), id),
            )
            return bool(cursor.rowcount)


class ConnectionMixin(_Rows):
    async def put_connection(self, connection: Connection) -> None:
        values = _row_values(connection)
        columns = ", ".join(values)
        placeholders = ", ".join(f"%({name})s" for name in values)
        assignments = ", ".join(f"{name} = excluded.{name}" for name in values if name != "id")
        async with self._conn() as conn:
            await conn.execute(
                f"INSERT INTO connections ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT (id) DO UPDATE SET {assignments}", values,
            )

    async def get_connection(self, connection_id: str) -> Connection | None:
        return await self._fetch_one(
            Connection, f"SELECT {_columns(Connection)} FROM connections WHERE id = %s",
            (connection_id,),
        )

    async def find_connection(self, connector: str, owner: str) -> Connection | None:
        return await self._fetch_one(
            Connection, f"SELECT {_columns(Connection)} FROM connections "
            "WHERE connector = %s AND owner = %s AND status = 'active' "
            "ORDER BY created_at DESC, seq DESC LIMIT 1", (connector, owner),
        )

    async def list_connections(self, owner: str | None = None) -> list[Connection]:
        where, params = ("WHERE owner = %s ", (owner,)) if owner is not None else ("", ())
        return await self._fetch(
            Connection, f"SELECT {_columns(Connection)} FROM connections {where}"
            "ORDER BY created_at, seq", params,
        )

    async def delete_connection(self, connection_id: str) -> bool:
        async with self._conn() as conn:
            cursor = await conn.execute(
                "DELETE FROM connections WHERE id = %s", (connection_id,))
            return bool(cursor.rowcount)

    # ---- secrets (values arrive already encrypted) -------------------------

    async def put_secret(self, key: str, value: str) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO secrets (key, value, updated_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at", (key, value, self._clock()))

    async def get_secret(self, key: str) -> str | None:
        async with self._conn() as conn:
            cursor = await conn.execute("SELECT value FROM secrets WHERE key = %s", (key,))
            row = await cursor.fetchone()
        return row["value"] if row else None

    async def delete_secret(self, key: str) -> bool:
        async with self._conn() as conn:
            cursor = await conn.execute("DELETE FROM secrets WHERE key = %s", (key,))
            return bool(cursor.rowcount)


class UsersMixin(_Rows):
    async def create_user(self, user: User) -> bool:
        check_user(user)
        values = _row_values(user)
        columns = ", ".join(values)
        placeholders = ", ".join(f"%({name})s" for name in values)
        async with self._conn() as conn:
            cursor = await conn.execute(
                f"INSERT INTO users ({columns}) VALUES ({placeholders}) "
                "ON CONFLICT (id) DO NOTHING", values,
            )
            return bool(cursor.rowcount)

    async def get_user(self, user_id: str) -> User | None:
        return await self._fetch_one(
            User, f"SELECT {_columns(User)} FROM users WHERE id = %s", (user_id,)
        )

    async def list_users(self) -> list[User]:
        return await self._fetch(
            User, f"SELECT {_columns(User)} FROM users ORDER BY created_at, seq"
        )

    async def update_user(
        self, user_id: str, *, display_name: str | None = None,
        role: str | None = None, disabled: bool | None = None,
    ) -> User | None:
        if role is not None and role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        changes = {"display_name": display_name, "role": role, "disabled": disabled}
        changes = {name: value for name, value in changes.items() if value is not None}
        if not changes:
            return await self.get_user(user_id)
        assignments = ", ".join(f"{name} = %({name})s" for name in changes)
        async with self._conn() as conn:
            cursor = await conn.execute(
                f"UPDATE users SET {assignments} WHERE id = %(id)s "
                f"RETURNING {_columns(User)}", {**changes, "id": user_id},
            )
            row = await cursor.fetchone()
        return _from_row(User, row) if row else None

    async def create_token(
        self, user_id: str, label: str = "", *, ttl_seconds: float | None = None,
    ) -> tuple[str, str]:
        token, token_id, now = new_token(), new_id("tok"), self._clock()
        async with self._conn() as conn:
            await _insert(conn, "user_tokens", {
                "id": token_id, "user_id": user_id, "token_hash": hash_token(token),
                "label": label, "created_at": now,
            })
            if ttl_seconds is not None:
                await _insert(conn, "user_token_expiry", {
                    "token_id": token_id,
                    "expires_at": now + timedelta(seconds=ttl_seconds),
                })
        return token_id, token

    async def resolve_token(self, token: str) -> User | None:
        now = self._clock()
        async with self._conn() as conn:
            cursor = await conn.execute(
                "UPDATE user_tokens t SET last_used_at = %(now)s FROM users u "
                "WHERE u.id = t.user_id AND t.token_hash = %(hash)s "
                "AND t.revoked_at IS NULL AND NOT u.disabled "
                "AND NOT EXISTS (SELECT 1 FROM user_token_expiry e "
                "WHERE e.token_id = t.id AND e.expires_at <= %(now)s) RETURNING "
                + ", ".join(f"u.{name}" for name in User.__dataclass_fields__),
                {"now": now, "hash": hash_token(token)},
            )
            row = await cursor.fetchone()
        return _from_row(User, row) if row else None

    async def revoke_token(self, token_id: str) -> bool:
        async with self._conn() as conn:
            cursor = await conn.execute(
                "UPDATE user_tokens SET revoked_at = %s WHERE id = %s AND revoked_at IS NULL",
                (self._clock(), token_id),
            )
            return bool(cursor.rowcount)

    async def list_tokens(self, user_id: str) -> list[UserToken]:
        return await self._fetch(
            UserToken, f"SELECT {_columns(UserToken)} FROM user_tokens "
            "WHERE user_id = %s ORDER BY created_at, seq", (user_id,),
        )

    async def create_sign_in_ticket(
        self, user_id: str, ticket_hash: str, ttl_seconds: float,
    ) -> datetime:
        now = self._clock()
        expires_at = now + timedelta(seconds=ttl_seconds)
        async with self._conn() as conn:
            await conn.execute("DELETE FROM sign_in_tickets WHERE expires_at <= %s", (now,))
            await _insert(conn, "sign_in_tickets", {
                "ticket_hash": ticket_hash, "user_id": user_id,
                "created_at": now, "expires_at": expires_at,
            })
        return expires_at

    async def redeem_sign_in_ticket(self, ticket_hash: str) -> str | None:
        now = self._clock()
        async with self._conn() as conn:
            cursor = await conn.execute(
                "UPDATE sign_in_tickets SET used_at = %(now)s WHERE ticket_hash = %(hash)s "
                "AND used_at IS NULL AND expires_at > %(now)s RETURNING user_id",
                {"now": now, "hash": ticket_hash},
            )
            row = await cursor.fetchone()
        return row["user_id"] if row else None
