"""SQLite storage for upstream connections (mixin for SQLiteStore)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from ..types import Connection
from .sqlite import _values


def _connection(row: sqlite3.Row) -> Connection:
    values = dict(row)
    values["scopes"] = json.loads(values["scopes"])
    for name in ("last_used_at", "created_at"):
        if values[name] is not None:
            values[name] = datetime.fromisoformat(values[name])
    return Connection(**values)


class ConnectionMixin:
    async def put_connection(self, connection: Connection) -> None:
        values = _values(connection)
        values["scopes"] = json.dumps(connection.scopes)
        assignments = ", ".join(f"{name} = excluded.{name}" for name in values if name != "id")
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        await self._call(lambda db: db.execute(
            f"INSERT INTO connections ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}", values,
        ))

    async def get_connection(self, connection_id: str) -> Connection | None:
        def read(db: sqlite3.Connection) -> Connection | None:
            row = db.execute(
                "SELECT * FROM connections WHERE id = ?", (connection_id,)
            ).fetchone()
            return _connection(row) if row else None

        return await self._call(read)

    async def find_connection(self, connector: str, owner: str) -> Connection | None:
        def read(db: sqlite3.Connection) -> Connection | None:
            row = db.execute(
                "SELECT * FROM connections WHERE connector = ? AND owner = ? "
                "AND status = 'active' ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (connector, owner),
            ).fetchone()
            return _connection(row) if row else None

        return await self._call(read)

    async def list_connections(self, owner: str | None = None) -> list[Connection]:
        def read(db: sqlite3.Connection) -> list[Connection]:
            where, params = ("WHERE owner = ?", (owner,)) if owner is not None else ("", ())
            return [_connection(row) for row in db.execute(
                f"SELECT * FROM connections {where} ORDER BY created_at, rowid", params,
            )]

        return await self._call(read)
