"""Mailbox tables on the instance's own store (SQLite or Postgres).

The tables live next to the core tables of the instance (same SQLite file,
or the instance's Postgres schema), so two instances never see each other's
mailboxes. They are created on first use (`CREATE TABLE IF NOT EXISTS`), so
an instance without the integration has none of them.

Timestamps are ISO-8601 UTC strings on both backends, so ordering and range
filters compare as text. JSON columns are TEXT.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ...types import utcnow
from .config import Mailbox, mailbox_from_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_accounts (
    id TEXT PRIMARY KEY,
    config TEXT NOT NULL,
    secret_ref TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_sync_at TEXT,
    last_error TEXT,
    last_error_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_folders (
    account_id TEXT NOT NULL,
    folder TEXT NOT NULL,
    uidvalidity BIGINT,
    last_uid BIGINT NOT NULL DEFAULT 0,
    backfill_until_uid BIGINT NOT NULL DEFAULT 0,
    backfill_since TEXT,
    wake_after TEXT,
    synced_at TEXT,
    PRIMARY KEY (account_id, folder)
);
CREATE TABLE IF NOT EXISTS mail_conversations (
    account_id TEXT NOT NULL,
    id TEXT NOT NULL,
    subject TEXT,
    norm_subject TEXT,
    participants TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_message_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, id)
);
CREATE INDEX IF NOT EXISTS mail_conversations_subject
    ON mail_conversations (account_id, norm_subject);
CREATE TABLE IF NOT EXISTS mail_messages (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    folder TEXT,
    uidvalidity BIGINT,
    uid BIGINT,
    message_id TEXT NOT NULL,
    subject TEXT,
    from_addr TEXT,
    from_name TEXT,
    to_addrs TEXT,
    cc_addrs TEXT,
    reply_to TEXT,
    in_reply_to TEXT,
    refs TEXT,
    date TEXT,
    direction TEXT NOT NULL,
    body_text TEXT,
    body_truncated INTEGER NOT NULL DEFAULT 0,
    attachments TEXT,
    size BIGINT,
    saved_dir TEXT,
    woke INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, message_id)
);
CREATE INDEX IF NOT EXISTS mail_messages_conversation
    ON mail_messages (account_id, conversation_id, date);
CREATE INDEX IF NOT EXISTS mail_messages_date ON mail_messages (account_id, date);
CREATE TABLE IF NOT EXISTS mail_refs (
    account_id TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    PRIMARY KEY (account_id, ref_id)
);
CREATE TABLE IF NOT EXISTS mail_drafts (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    conversation_id TEXT,
    fields TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    thread_key TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

MESSAGE_COLUMNS = (
    "id", "account_id", "conversation_id", "folder", "uidvalidity", "uid", "message_id",
    "subject", "from_addr", "from_name", "to_addrs", "cc_addrs", "reply_to", "in_reply_to",
    "refs", "date", "direction", "body_text", "body_truncated", "attachments", "size",
    "saved_dir", "woke", "created_at")
JSON_COLUMNS = {"to_addrs", "cc_addrs", "reply_to", "in_reply_to", "refs", "attachments",
                "participants", "fields"}
SUMMARY_COLUMNS = tuple(c for c in MESSAGE_COLUMNS if c != "body_text")


def _now() -> str:
    return utcnow().isoformat()


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in JSON_COLUMNS & out.keys():
        if isinstance(out[key], str):
            try:
                out[key] = json.loads(out[key])
            except ValueError:
                pass
    if "body_truncated" in out:
        out["body_truncated"] = bool(out["body_truncated"])
    if "woke" in out:
        out["woke"] = bool(out["woke"])
    return out


def _encode(values: dict[str, Any]) -> dict[str, Any]:
    return {k: (json.dumps(v, ensure_ascii=False) if k in JSON_COLUMNS and v is not None
                and not isinstance(v, str) else v) for k, v in values.items()}


def _like(text: str) -> str:
    escaped = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class MailStore:
    """Async access to the mail_* tables through the instance store's connection."""

    def __init__(self, store: Any) -> None:
        self._store = store
        if hasattr(store, "_call"):
            self._kind = "sqlite"
        elif hasattr(store, "_conn"):
            self._kind = "postgres"
        else:
            raise TypeError("MailStore needs a SQLiteStore or PostgresStore")
        self._ready = False
        self._lock = asyncio.Lock()

    # ── plumbing ───────────────────────────────────────────────────────────

    async def _ensure(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            if self._kind == "sqlite":
                await self._store._call(lambda db: db.executescript(SCHEMA))
            else:
                async with self._store._conn() as conn:
                    await conn.execute(SCHEMA)
            self._ready = True

    async def _query(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        await self._ensure()
        if self._kind == "sqlite":
            def run(db):
                cursor = db.execute(query, params)
                return [dict(row) for row in cursor.fetchall()] if cursor.description else []
            return await self._store._call(run)
        async with self._store._conn() as conn:
            cursor = await conn.execute(query.replace("?", "%s"), params)
            return list(await cursor.fetchall()) if cursor.description else []

    async def _execute(self, query: str, params: tuple = ()) -> int:
        await self._ensure()
        if self._kind == "sqlite":
            return await self._store._call(lambda db: db.execute(query, params).rowcount)
        async with self._store._conn() as conn:
            cursor = await conn.execute(query.replace("?", "%s"), params)
            return cursor.rowcount

    async def _script(self, statements: list[tuple[str, tuple]]) -> None:
        """Several writes in one transaction."""
        await self._ensure()
        if self._kind == "sqlite":
            def run(db):
                db.execute("BEGIN IMMEDIATE")
                try:
                    for query, params in statements:
                        db.execute(query, params)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            await self._store._call(run)
            return
        async with self._store._conn() as conn:
            async with conn.transaction():
                for query, params in statements:
                    await conn.execute(query.replace("?", "%s"), params)

    # ── accounts ───────────────────────────────────────────────────────────

    async def put_mailbox(self, mailbox: Mailbox) -> None:
        mailbox.updated_at = _now()
        await self._execute(
            "INSERT INTO mail_accounts (id, config, secret_ref, status, last_sync_at, "
            "last_error, last_error_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (id) DO UPDATE SET config=excluded.config, "
            "secret_ref=excluded.secret_ref, status=excluded.status, "
            "last_sync_at=excluded.last_sync_at, last_error=excluded.last_error, "
            "last_error_at=excluded.last_error_at, updated_at=excluded.updated_at",
            (mailbox.id, json.dumps(mailbox.config(), ensure_ascii=False), mailbox.secret_ref,
             mailbox.status, mailbox.last_sync_at, mailbox.last_error, mailbox.last_error_at,
             mailbox.created_at, mailbox.updated_at))

    async def get_mailbox(self, mailbox_id: str) -> Mailbox | None:
        rows = await self._query("SELECT * FROM mail_accounts WHERE id = ?", (mailbox_id,))
        return mailbox_from_row(rows[0], json.loads(rows[0]["config"])) if rows else None

    async def list_mailboxes(self) -> list[Mailbox]:
        rows = await self._query("SELECT * FROM mail_accounts ORDER BY created_at, id")
        return [mailbox_from_row(row, json.loads(row["config"])) for row in rows]

    async def record_sync(self, mailbox_id: str, *, status: str, error: str | None,
                          synced: bool) -> None:
        now = _now()
        if error:
            await self._execute(
                "UPDATE mail_accounts SET status=?, last_error=?, last_error_at=? WHERE id=?",
                (status, error[:1000], now, mailbox_id))
        else:
            await self._execute(
                "UPDATE mail_accounts SET status=?, last_error=NULL, "
                "last_sync_at=CASE WHEN ? THEN ? ELSE last_sync_at END WHERE id=?",
                (status, 1 if synced else 0, now, mailbox_id) if self._kind == "sqlite"
                else (status, synced, now, mailbox_id))

    async def delete_mailbox(self, mailbox_id: str) -> None:
        await self._script([(f"DELETE FROM {table} WHERE account_id = ?", (mailbox_id,))
                            for table in ("mail_messages", "mail_refs", "mail_conversations",
                                          "mail_folders", "mail_drafts")]
                           + [("DELETE FROM mail_accounts WHERE id = ?", (mailbox_id,))])

    # ── folder sync state ──────────────────────────────────────────────────

    async def get_folder(self, mailbox_id: str, folder: str) -> dict[str, Any] | None:
        rows = await self._query(
            "SELECT * FROM mail_folders WHERE account_id = ? AND folder = ?",
            (mailbox_id, folder))
        return rows[0] if rows else None

    async def list_folders(self, mailbox_id: str) -> list[dict[str, Any]]:
        return await self._query(
            "SELECT * FROM mail_folders WHERE account_id = ? ORDER BY folder", (mailbox_id,))

    async def put_folder(self, mailbox_id: str, folder: str, *, uidvalidity: int,
                         last_uid: int, backfill_until_uid: int, backfill_since: str | None,
                         wake_after: str | None) -> None:
        await self._execute(
            "INSERT INTO mail_folders (account_id, folder, uidvalidity, last_uid, "
            "backfill_until_uid, backfill_since, wake_after, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (account_id, folder) DO UPDATE SET "
            "uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid, "
            "backfill_until_uid=excluded.backfill_until_uid, "
            "backfill_since=excluded.backfill_since, wake_after=excluded.wake_after, "
            "synced_at=excluded.synced_at",
            (mailbox_id, folder, uidvalidity, last_uid, backfill_until_uid, backfill_since,
             wake_after, _now()))

    async def advance_folder(self, mailbox_id: str, folder: str, last_uid: int) -> None:
        await self._execute(
            "UPDATE mail_folders SET last_uid = ?, synced_at = ? "
            "WHERE account_id = ? AND folder = ? AND last_uid < ?",
            (last_uid, _now(), mailbox_id, folder, last_uid))

    # ── conversations ──────────────────────────────────────────────────────

    async def conversation_for_refs(self, mailbox_id: str, ids: list[str]) -> str | None:
        """The conversation of the first id (in order) already known, if any."""
        if not ids:
            return None
        marks = ",".join("?" for _ in ids)
        rows = await self._query(
            f"SELECT ref_id, conversation_id FROM mail_refs WHERE account_id = ? "
            f"AND ref_id IN ({marks})", (mailbox_id, *ids))
        found = {row["ref_id"]: row["conversation_id"] for row in rows}
        return next((found[i] for i in ids if i in found), None)

    async def conversations_by_subject(self, mailbox_id: str, norm_subject: str,
                                       since: str) -> list[dict[str, Any]]:
        rows = await self._query(
            "SELECT * FROM mail_conversations WHERE account_id = ? AND norm_subject = ? "
            "AND last_message_at >= ? ORDER BY last_message_at DESC LIMIT 20",
            (mailbox_id, norm_subject, since))
        return [_decode(r) for r in rows]

    async def get_conversation(self, mailbox_id: str, conversation_id: str) -> dict | None:
        rows = await self._query(
            "SELECT * FROM mail_conversations WHERE account_id = ? AND id = ?",
            (mailbox_id, conversation_id))
        return _decode(rows[0]) if rows else None

    # ── messages ───────────────────────────────────────────────────────────

    async def find_message(self, mailbox_id: str, message_id: str) -> dict[str, Any] | None:
        rows = await self._query(
            f"SELECT {', '.join(SUMMARY_COLUMNS)} FROM mail_messages "
            "WHERE account_id = ? AND message_id = ?", (mailbox_id, message_id))
        return _decode(rows[0]) if rows else None

    async def relocate_message(self, row_id: str, folder: str, uidvalidity: int,
                               uid: int) -> None:
        await self._execute(
            "UPDATE mail_messages SET folder = ?, uidvalidity = ?, uid = ? WHERE id = ?",
            (folder, uidvalidity, uid, row_id))

    async def add_message(self, row: dict[str, Any], *, conversation: dict[str, Any],
                          refs: list[str]) -> None:
        """Insert the message, its refs and the conversation summary atomically."""
        values = _encode({**row, "created_at": row.get("created_at") or _now()})
        values["body_truncated"] = 1 if values.get("body_truncated") else 0
        values["woke"] = 1 if values.get("woke") else 0
        columns = [c for c in MESSAGE_COLUMNS if c in values]
        statements: list[tuple[str, tuple]] = [(
            f"INSERT INTO mail_messages ({', '.join(columns)}) VALUES "
            f"({', '.join('?' for _ in columns)}) ON CONFLICT (account_id, message_id) "
            "DO NOTHING", tuple(values[c] for c in columns))]
        for ref in dict.fromkeys(refs):
            statements.append((
                "INSERT INTO mail_refs (account_id, ref_id, conversation_id) VALUES (?,?,?) "
                "ON CONFLICT (account_id, ref_id) DO NOTHING",
                (row["account_id"], ref, row["conversation_id"])))
        conv = _encode(conversation)
        statements.append((
            "INSERT INTO mail_conversations (account_id, id, subject, norm_subject, "
            "participants, message_count, last_message_at, created_at) "
            "VALUES (?,?,?,?,?,1,?,?) ON CONFLICT (account_id, id) DO UPDATE SET "
            "participants = excluded.participants, "
            "message_count = mail_conversations.message_count + 1, "
            "last_message_at = CASE WHEN excluded.last_message_at > "
            "COALESCE(mail_conversations.last_message_at, '') "
            "THEN excluded.last_message_at ELSE mail_conversations.last_message_at END",
            (row["account_id"], row["conversation_id"], conv.get("subject"),
             conv.get("norm_subject"), conv.get("participants"), conv.get("last_message_at"),
             _now())))
        await self._script(statements)

    async def set_saved_dir(self, row_id: str, saved_dir: str) -> None:
        await self._execute("UPDATE mail_messages SET saved_dir = ? WHERE id = ?",
                            (saved_dir, row_id))

    async def get_message(self, row_id: str) -> dict[str, Any] | None:
        rows = await self._query("SELECT * FROM mail_messages WHERE id = ?", (row_id,))
        return _decode(rows[0]) if rows else None

    async def conversation_messages(self, mailbox_id: str, conversation_id: str,
                                    limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._query(
            f"SELECT {', '.join(SUMMARY_COLUMNS)} FROM mail_messages "
            "WHERE account_id = ? AND conversation_id = ? ORDER BY date DESC, id LIMIT ?",
            (mailbox_id, conversation_id, limit))
        return [_decode(r) for r in reversed(rows)]

    async def search(self, mailbox_ids: list[str], *, folder: str | None = None,
                     sender: str | None = None, recipient: str | None = None,
                     subject: str | None = None, text: str | None = None,
                     since: str | None = None, until: str | None = None,
                     direction: str | None = None, conversation_id: str | None = None,
                     limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        if not mailbox_ids:
            return []
        where = [f"account_id IN ({','.join('?' for _ in mailbox_ids)})"]
        params: list[Any] = list(mailbox_ids)
        like = " LIKE ? ESCAPE '\\'"
        if folder:
            where.append("folder = ?")
            params.append(folder)
        if sender:
            where.append(f"(lower(from_addr){like} OR lower(from_name){like})")
            params += [_like(sender)] * 2
        if recipient:
            where.append(f"(lower(to_addrs){like} OR lower(cc_addrs){like})")
            params += [_like(recipient)] * 2
        if subject:
            where.append(f"lower(subject){like}")
            params.append(_like(subject))
        if text:
            where.append(f"(lower(subject){like} OR lower(body_text){like} "
                         f"OR lower(attachments){like})")
            params += [_like(text)] * 3
        if since:
            where.append("date >= ?")
            params.append(since)
        if until:
            where.append("date < ?")
            params.append(until)
        if direction:
            where.append("direction = ?")
            params.append(direction)
        if conversation_id:
            where.append("conversation_id = ?")
            params.append(conversation_id)
        rows = await self._query(
            f"SELECT {', '.join(SUMMARY_COLUMNS)}, substr(body_text, 1, 240) AS snippet "
            f"FROM mail_messages WHERE {' AND '.join(where)} "
            "ORDER BY date DESC, id LIMIT ? OFFSET ?", (*params, limit, offset))
        return [_decode(r) for r in rows]

    async def counts(self, mailbox_id: str) -> dict[str, int]:
        rows = await self._query(
            "SELECT direction, COUNT(*) AS n FROM mail_messages WHERE account_id = ? "
            "GROUP BY direction", (mailbox_id,))
        return {row["direction"]: int(row["n"]) for row in rows}

    # ── drafts ─────────────────────────────────────────────────────────────

    async def put_draft(self, draft: dict[str, Any]) -> None:
        now = _now()
        await self._execute(
            "INSERT INTO mail_drafts (id, account_id, conversation_id, fields, status, "
            "thread_key, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (id) DO UPDATE SET fields=excluded.fields, status=excluded.status, "
            "updated_at=excluded.updated_at",
            (draft["id"], draft["account_id"], draft.get("conversation_id"),
             json.dumps(draft["fields"], ensure_ascii=False), draft.get("status", "draft"),
             draft.get("thread_key"), draft.get("created_by"),
             draft.get("created_at") or now, now))

    async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        rows = await self._query("SELECT * FROM mail_drafts WHERE id = ?", (draft_id,))
        return _decode(rows[0]) if rows else None
