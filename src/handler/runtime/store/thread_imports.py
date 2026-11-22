"""Finished conversations imported from another system as chat threads (both stores).

An import writes the thread, its messages, its title and the owner's read mark in
one transaction, with no event: nothing is routed, no run starts and no plan input
is counted. The thread is created with no harness session, so the first new turn
gets the record as a recap (`RunExecutor._stream`).

`thread_imports` remembers each (source, source_id) with the thread it became and a
hash of what was sent, so a repeat is recognised and a different payload refused.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ..types import Lane, Role, ThreadType, new_id

CREATED, EXISTING, CONFLICT = "created", "existing", "conflict"


@dataclass(frozen=True)
class ImportedMessage:
    role: Role
    text: str
    created_at: datetime


@dataclass(frozen=True)
class ThreadImport:
    key: str
    owner_user_id: str
    harness: str
    source: str
    source_id: str
    title: str | None
    created_at: datetime
    messages: list[ImportedMessage]
    content_hash: str


@dataclass(frozen=True)
class ImportOutcome:
    status: Literal["created", "existing", "conflict"]
    thread_key: str
    messages: int = 0
    reason: str | None = None


def _blocks(message: ImportedMessage) -> list[dict]:
    return [{"type": "text", "text": message.text}]


def _raw(spec: ThreadImport) -> dict:
    return {"imported_from": spec.source, "source_id": spec.source_id}


def _seen(spec: ThreadImport, row) -> ImportOutcome:
    """The outcome for a (source, source_id) that was imported before."""
    if row["thread_key"] == spec.key and row["content_hash"] == spec.content_hash:
        return ImportOutcome(EXISTING, spec.key, row["messages"])
    if row["thread_key"] != spec.key:
        return ImportOutcome(CONFLICT, row["thread_key"], row["messages"],
                             f"{spec.source} {spec.source_id} was already imported "
                             f"as {row['thread_key']}")
    return ImportOutcome(CONFLICT, spec.key, row["messages"],
                         f"{spec.source} {spec.source_id} was already imported "
                         "with different content")


def _taken(spec: ThreadImport) -> ImportOutcome:
    return ImportOutcome(CONFLICT, spec.key, 0,
                         f"thread {spec.key} already exists and was not imported "
                         f"from {spec.source} {spec.source_id}")


class _Rollback(Exception):
    def __init__(self, outcome: ImportOutcome) -> None:
        self.outcome = outcome


class SQLiteThreadImports:
    async def import_thread(self, spec: ThreadImport) -> ImportOutcome:
        from .sqlite import _insert, _timestamp, _transaction

        def write(db: sqlite3.Connection) -> ImportOutcome:
            with _transaction(db):
                row = db.execute(
                    "SELECT thread_key, content_hash, messages FROM thread_imports "
                    "WHERE source = ? AND source_id = ?", (spec.source, spec.source_id)
                ).fetchone()
                if row is not None:
                    return _seen(spec, row)
                if db.execute("SELECT 1 FROM threads WHERE key = ?", (spec.key,)).fetchone():
                    return _taken(spec)
                _insert(db, "threads", {
                    "key": spec.key, "type": ThreadType.CHAT.value,
                    "lane": Lane.INTERACTIVE.value, "owner_user_id": spec.owner_user_id,
                    "harness": spec.harness, "model": None, "priority": 0,
                    "created_at": _timestamp(spec.created_at),
                })
                raw = json.dumps(_raw(spec))
                db.executemany(
                    "INSERT INTO messages (id, thread_key, role, blocks, run_id, raw, created_at) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    [(new_id("msg"), spec.key, m.role.value,
                      json.dumps(_blocks(m), ensure_ascii=False), raw,
                      _timestamp(m.created_at)) for m in spec.messages])
                if spec.title:
                    db.execute("INSERT INTO thread_names (thread_key, title, manual) "
                               "VALUES (?, ?, 1)", (spec.key, spec.title))
                db.execute("INSERT INTO thread_views (thread_key, user_id, read_at) "
                           "VALUES (?, ?, ?)", (spec.key, spec.owner_user_id,
                                                _timestamp(spec.messages[-1].created_at)))
                _insert(db, "thread_imports", {
                    "source": spec.source, "source_id": spec.source_id,
                    "thread_key": spec.key, "content_hash": spec.content_hash,
                    "messages": len(spec.messages),
                    "imported_at": _timestamp(self._clock()),
                })
                return ImportOutcome(CREATED, spec.key, len(spec.messages))

        return await self._call(write)


class PostgresThreadImports:
    async def import_thread(self, spec: ThreadImport) -> ImportOutcome:
        from psycopg.types.json import Jsonb

        from .postgres import strip_nul

        try:
            async with self._conn() as conn:
                cursor = await conn.execute(
                    "SELECT thread_key, content_hash, messages FROM thread_imports "
                    "WHERE source = %s AND source_id = %s", (spec.source, spec.source_id))
                row = await cursor.fetchone()
                if row is not None:
                    return _seen(spec, row)
                cursor = await conn.execute(
                    "INSERT INTO threads (key, type, lane, owner_user_id, harness, model, "
                    "priority, created_at) VALUES (%s, %s, %s, %s, %s, NULL, 0, %s) "
                    "ON CONFLICT (key) DO NOTHING RETURNING key",
                    (spec.key, ThreadType.CHAT.value, Lane.INTERACTIVE.value,
                     spec.owner_user_id, spec.harness, spec.created_at))
                if await cursor.fetchone() is None:
                    # Taken, possibly by the same import committing concurrently.
                    cursor = await conn.execute(
                        "SELECT thread_key, content_hash, messages FROM thread_imports "
                        "WHERE source = %s AND source_id = %s", (spec.source, spec.source_id))
                    row = await cursor.fetchone()
                    return _seen(spec, row) if row is not None else _taken(spec)
                raw = Jsonb(strip_nul(_raw(spec)))
                async with conn.cursor() as batch:
                    await batch.executemany(
                        "INSERT INTO messages (id, thread_key, role, blocks, run_id, raw, "
                        "created_at) VALUES (%s, %s, %s, %s, NULL, %s, %s)",
                        [(new_id("msg"), spec.key, m.role.value, Jsonb(strip_nul(_blocks(m))),
                          raw, m.created_at) for m in spec.messages])
                if spec.title:
                    await conn.execute("INSERT INTO thread_names (thread_key, title, manual) "
                                       "VALUES (%s, %s, 1)", (spec.key, strip_nul(spec.title)))
                await conn.execute(
                    "INSERT INTO thread_views (thread_key, user_id, read_at) VALUES (%s, %s, %s)",
                    (spec.key, spec.owner_user_id, spec.messages[-1].created_at))
                cursor = await conn.execute(
                    "INSERT INTO thread_imports (source, source_id, thread_key, content_hash, "
                    "messages, imported_at) VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (source, source_id) DO NOTHING RETURNING thread_key",
                    (spec.source, spec.source_id, spec.key, spec.content_hash,
                     len(spec.messages), self._clock()))
                if await cursor.fetchone() is None:
                    # The same source chat committed meanwhile under another key.
                    cursor = await conn.execute(
                        "SELECT thread_key, content_hash, messages FROM thread_imports "
                        "WHERE source = %s AND source_id = %s", (spec.source, spec.source_id))
                    raise _Rollback(_seen(spec, await cursor.fetchone()))
                return ImportOutcome(CREATED, spec.key, len(spec.messages))
        except _Rollback as rollback:
            return rollback.outcome
