"""Durable conversation titles and per-user read watermarks on both stores."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def clean_title(title: str) -> str:
    title = " ".join(title.split())
    if not title or len(title) > 120:
        raise ValueError("title must contain 1 to 120 characters")
    return title


def _date(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


class ThreadMetadata:
    async def thread_metadata(self, keys: list[str], user_id: str | None) -> dict[str, dict]:
        if not keys:
            return {}
        marks = ",".join("?" for _ in keys)
        # Tool output is agent activity too: notify-only automations must be unread.
        # One indexed batch includes fallback source text for older conversations.
        rows = await self._thread_query(f"""
            SELECT t.key, t.created_at, t.summary, n.title,
              v.read_at, m.last_message_at, m.last_assistant_message_at,
              (SELECT blocks FROM messages WHERE thread_key=t.key AND role='user'
                 ORDER BY created_at, id LIMIT 1) AS first_blocks,
              (SELECT payload FROM events WHERE thread_key=t.key
                 ORDER BY received_at, id LIMIT 1) AS first_payload
            FROM threads t
            LEFT JOIN thread_names n ON n.thread_key=t.key
            LEFT JOIN thread_views v ON v.thread_key=t.key AND v.user_id=?
            LEFT JOIN (SELECT thread_key, MAX(created_at) AS last_message_at,
               MAX(CASE WHEN role IN ('assistant','tool') THEN created_at END) AS last_assistant_message_at
               FROM messages WHERE thread_key IN ({marks}) GROUP BY thread_key) m
               ON m.thread_key=t.key
            WHERE t.key IN ({marks})
        """, (user_id, *keys, *keys))
        result = {}
        for row in rows:
            payload = row["first_payload"] or {}
            blocks = row["first_blocks"] or []
            if isinstance(payload, str):
                payload = json.loads(payload)
            if isinstance(blocks, str):
                blocks = json.loads(blocks)
            text = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict))
            fallback = payload.get("name") or payload.get("subject") or text or payload.get("text")
            if not fallback and isinstance(payload.get("email"), dict):
                fallback = payload["email"].get("subject")
            title = row["title"] or row["summary"] or fallback or row["key"]
            assistant = _date(row["last_assistant_message_at"])
            read_at = _date(row["read_at"])
            result[row["key"]] = dict(title=" ".join(str(title).split())[:120],
                last_message_at=_date(row["last_message_at"]) or _date(row["created_at"]),
                last_assistant_message_at=assistant, read_at=read_at,
                unread=bool(user_id and assistant and (not read_at or assistant > read_at)))
        return result

    async def set_thread_title(self, key: str, title: str, *, manual: bool = True) -> bool:
        title = clean_title(title)
        # Generated titles cannot replace either a manual or an earlier generated name.
        clause = "DO UPDATE SET title=excluded.title, manual=excluded.manual" if manual else "DO NOTHING"
        rows = await self._thread_query(
            "INSERT INTO thread_names(thread_key,title,manual) "
            "SELECT key,?,? FROM threads WHERE key=? "
            f"ON CONFLICT(thread_key) {clause} RETURNING thread_key", (title, int(manual), key))
        return bool(rows)

    async def mark_thread_read(self, key: str, user_id: str, last_message_at: datetime | None):
        if last_message_at is None:
            rows = await self._thread_query(
                "SELECT read_at FROM thread_views WHERE thread_key=? AND user_id=?", (key, user_id))
            return _date(rows[0]["read_at"]) if rows else None
        if last_message_at.tzinfo is None:
            raise ValueError("last_message_at must include a timezone")
        # Cap at the newest message actually delivered by the client, and never regress.
        stamp = self._thread_timestamp(last_message_at)
        rows = await self._thread_query("""
            INSERT INTO thread_views(thread_key,user_id,read_at)
            SELECT thread_key,?,MAX(created_at) FROM messages
              WHERE thread_key=? AND created_at<=? GROUP BY thread_key
            ON CONFLICT(thread_key,user_id) DO UPDATE SET read_at=
              CASE WHEN excluded.read_at > thread_views.read_at THEN excluded.read_at
                ELSE thread_views.read_at END RETURNING read_at
        """, (user_id, key, stamp))
        return _date(rows[0]["read_at"]) if rows else None


class SQLiteThreadMetadata(ThreadMetadata):
    def _thread_timestamp(self, value):
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    async def _thread_query(self, query, parameters):
        return await self._call(lambda db: [dict(row) for row in db.execute(query, parameters)])


class PostgresThreadMetadata(ThreadMetadata):
    def _thread_timestamp(self, value):
        return value

    async def _thread_query(self, query, parameters):
        async with self._conn() as conn:
            cursor = await conn.execute(query.replace("?", "%s"), parameters)
            return await cursor.fetchall()
