"""EventStore: SQLite persistence layer for handler system state.

Tables:
  events          — unified audit log (every significant thing that happens)
  conversations   — conversation metadata
  messages        — chat messages (optimized for conversation read path)
  summaries       — compaction summaries
  cron_jobs       — scheduled job state machine
  token_usage     — token accounting and cost tracking

Agent-controlled memory lives on disk as data/memory/*.md files,
managed by AgentContext — not in this class.
"""

import json
import logging
import sqlite3
from pathlib import Path

from .users import DEFAULT_USER_ID

logger = logging.getLogger("handler.event_store")

_MULTIMODAL_PREFIX = "__handler_multimodal__:"


class EventStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    ts TEXT DEFAULT (datetime('now')),
                    event_type TEXT NOT NULL,
                    conversation_id TEXT DEFAULT '',
                    user_id TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    data TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_events_type_ts
                    ON events(event_type, ts);
                CREATE INDEX IF NOT EXISTS idx_events_conversation
                    ON events(conversation_id, ts);

                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT DEFAULT '',
                    channel TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    conversation_id TEXT,
                    role TEXT,
                    content TEXT,
                    ts TEXT DEFAULT (datetime('now')),
                    compacted_at TEXT DEFAULT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                );

                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY,
                    conversation_id TEXT,
                    ts TEXT DEFAULT (datetime('now')),
                    content TEXT,
                    message_count INTEGER
                );

                CREATE TABLE IF NOT EXISTS cron_jobs (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    schedule TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '',
                    conversation_id TEXT DEFAULT '',
                    user_id TEXT DEFAULT '',
                    notify_channel TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    one_shot INTEGER DEFAULT 0,
                    last_run TEXT DEFAULT NULL,
                    next_run TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY,
                    conversation_id TEXT,
                    user_id TEXT DEFAULT '',
                    ts TEXT DEFAULT (datetime('now')),
                    model TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    estimated_cost_usd REAL,
                    trigger TEXT
                );
            """)
            self._migrate(conn)

    def _migrate(self, conn):
        """Run migrations for existing databases."""
        # Add one_shot column to cron_jobs if missing
        try:
            conn.execute("ALTER TABLE cron_jobs ADD COLUMN one_shot INTEGER DEFAULT 0")
        except Exception:
            pass

        # Add notify_channel column to cron_jobs if missing
        try:
            conn.execute(
                "ALTER TABLE cron_jobs ADD COLUMN notify_channel TEXT DEFAULT ''"
            )
        except Exception:
            pass

        for table in ("events", "conversations", "cron_jobs", "token_usage"):
            try:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN user_id TEXT DEFAULT ''"
                )
            except Exception:
                pass

        conn.execute(
            "UPDATE conversations SET user_id = ? WHERE user_id IS NULL OR user_id = ''",
            (DEFAULT_USER_ID,),
        )
        conn.execute(
            "UPDATE events SET user_id = ? WHERE user_id IS NULL OR user_id = ''",
            (DEFAULT_USER_ID,),
        )
        conn.execute(
            "UPDATE cron_jobs SET user_id = ? WHERE user_id IS NULL OR user_id = ''",
            (DEFAULT_USER_ID,),
        )
        conn.execute(
            "UPDATE token_usage SET user_id = ? WHERE user_id IS NULL OR user_id = ''",
            (DEFAULT_USER_ID,),
        )

        # Migrate old event_log → events table
        try:
            conn.execute("SELECT 1 FROM event_log LIMIT 1")
        except Exception:
            return  # No old event_log table, nothing to migrate

        # event_log exists — migrate its rows into events
        count = conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0]
        if count > 0:
            conn.execute("""
                INSERT INTO events (ts, event_type, source, data)
                SELECT ts, type, source, data FROM event_log
            """)
            logger.info(f"migrated {count} rows from event_log → events")

        # Migrate existing messages into events too
        msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if msg_count > 0:
            rows = conn.execute(
                "SELECT ts, conversation_id, role, content FROM messages ORDER BY ts"
            ).fetchall()
            for r in rows:
                event_type = (
                    "agent_message" if r["role"] == "assistant" else "user_message"
                )
                conn.execute(
                    "INSERT INTO events (ts, event_type, conversation_id, data) VALUES (?, ?, ?, ?)",
                    (
                        r["ts"],
                        event_type,
                        r["conversation_id"],
                        json.dumps({"role": r["role"], "content": r["content"][:500]}),
                    ),
                )
            logger.info(f"migrated {msg_count} messages → events")

        # Migrate summaries into events
        sum_count = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        if sum_count > 0:
            rows = conn.execute(
                "SELECT ts, conversation_id, content, message_count FROM summaries"
            ).fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO events (ts, event_type, conversation_id, data) VALUES (?, ?, ?, ?)",
                    (
                        r["ts"],
                        "compaction",
                        r["conversation_id"],
                        json.dumps({"message_count": r["message_count"]}),
                    ),
                )
            logger.info(f"migrated {sum_count} summaries → events")

        # Drop old event_log table
        conn.execute("DROP TABLE event_log")
        logger.info("dropped old event_log table")

    # ------------------------------------------------------------------
    # Events (unified audit log)
    # ------------------------------------------------------------------

    def log_event(
        self,
        event_type: str,
        source: str,
        data: dict,
        conversation_id: str = "",
        user_id: str = "",
    ):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (event_type, source, conversation_id, user_id, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    event_type,
                    source,
                    conversation_id,
                    user_id,
                    json.dumps(data, default=str),
                ),
            )

    # ------------------------------------------------------------------
    # Conversations & Messages
    # ------------------------------------------------------------------

    def ensure_conversation(
        self,
        conversation_id: str,
        channel: str = "",
        user_id: str = "",
    ):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversations (id, user_id, channel) VALUES (?, ?, ?)",
                (conversation_id, user_id or DEFAULT_USER_ID, channel),
            )
            if channel:
                conn.execute(
                    "UPDATE conversations SET channel = ? WHERE id = ? AND (channel = '' OR channel IS NULL)",
                    (channel, conversation_id),
                )
            if user_id:
                conn.execute(
                    "UPDATE conversations SET user_id = ? WHERE id = ?",
                    (user_id, conversation_id),
                )

    def get_conversation_user(self, conversation_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if row and row["user_id"]:
                return row["user_id"]
            return DEFAULT_USER_ID

    @staticmethod
    def _is_multimodal_blocks(value: object) -> bool:
        return isinstance(value, list) and all(
            isinstance(item, dict) and isinstance(item.get("type"), str)
            for item in value
        )

    def _serialize_message_content(self, content: str | list) -> str:
        if isinstance(content, list):
            return _MULTIMODAL_PREFIX + json.dumps(content)
        return content

    def _deserialize_message_content(self, stored: str | None) -> str | list | None:
        if stored is None:
            return None
        if stored.startswith(_MULTIMODAL_PREFIX):
            try:
                return json.loads(stored[len(_MULTIMODAL_PREFIX) :])
            except (json.JSONDecodeError, TypeError):
                return stored
        if stored.startswith("["):
            try:
                parsed = json.loads(stored)
            except (json.JSONDecodeError, TypeError):
                return stored
            if self._is_multimodal_blocks(parsed):
                return parsed
        return stored

    @staticmethod
    def _preview_message_content(content: str | list) -> str:
        if isinstance(content, str):
            return content

        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif block.get("type") == "image":
                parts.append("[Image]")
        return " ".join(parts) if parts else "[Non-text message]"

    def add_message(self, conversation_id: str, role: str, content: str | list):
        event_type = "agent_message" if role == "assistant" else "user_message"
        stored = self._serialize_message_content(content)
        preview = self._preview_message_content(content)[:500]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
                (conversation_id, role, stored),
            )
            # Also record as an event
            user_id = self.get_conversation_user(conversation_id)
            conn.execute(
                "INSERT INTO events (event_type, conversation_id, user_id, data) VALUES (?, ?, ?, ?)",
                (
                    event_type,
                    conversation_id,
                    user_id,
                    json.dumps({"role": role, "content": preview}),
                ),
            )

    def get_messages(
        self,
        conversation_id: str,
        limit: int | None = None,
        include_compacted: bool = False,
    ) -> list[dict]:
        if include_compacted:
            sql = (
                "SELECT role, content FROM messages "
                "WHERE conversation_id = ? "
                "ORDER BY ts ASC"
            )
        else:
            sql = (
                "SELECT role, content FROM messages "
                "WHERE conversation_id = ? AND compacted_at IS NULL "
                "ORDER BY ts ASC"
            )
        params: list = [conversation_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            results = []
            for r in rows:
                content = self._deserialize_message_content(r["content"])
                results.append({"role": r["role"], "content": content})
            return results

    def get_active_conversations(self) -> list[str]:
        """Return conversation IDs that have at least one non-compacted message."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT conversation_id FROM messages "
                "WHERE compacted_at IS NULL"
            ).fetchall()
            return [r["conversation_id"] for r in rows]

    def list_web_conversations(self) -> list[dict]:
        """Return web conversations with metadata, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    c.id,
                    c.user_id,
                    c.created_at,
                    COUNT(m.id) AS message_count,
                    MAX(m.ts) AS last_ts,
                    (SELECT content FROM messages
                     WHERE conversation_id = c.id
                     ORDER BY ts DESC LIMIT 1) AS last_content,
                    (SELECT role FROM messages
                     WHERE conversation_id = c.id
                     ORDER BY ts DESC LIMIT 1) AS last_role
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                WHERE c.channel = 'web' OR c.id = 'web' OR c.id LIKE 'web-%'
                GROUP BY c.id
                ORDER BY last_ts DESC NULLS LAST
                """
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "user_id": r["user_id"] or DEFAULT_USER_ID,
                    "created_at": r["created_at"],
                    "message_count": r["message_count"] or 0,
                    "last_ts": r["last_ts"],
                    "last_content": self._preview_message_content(
                        self._deserialize_message_content(r["last_content"]) or ""
                    )[:120],
                    "last_role": r["last_role"],
                }
                for r in rows
            ]

    def list_all_conversations(self) -> list[dict]:
        """Return all conversations across all channels with metadata, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    c.id,
                    c.user_id,
                    c.channel,
                    c.created_at,
                    COUNT(m.id) AS message_count,
                    MAX(m.ts) AS last_ts,
                    (SELECT content FROM messages
                     WHERE conversation_id = c.id
                     ORDER BY ts DESC LIMIT 1) AS last_content,
                    (SELECT role FROM messages
                     WHERE conversation_id = c.id
                     ORDER BY ts DESC LIMIT 1) AS last_role
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                GROUP BY c.id
                ORDER BY last_ts DESC NULLS LAST
                """
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "user_id": r["user_id"] or DEFAULT_USER_ID,
                    "channel": r["channel"] or "",
                    "created_at": r["created_at"],
                    "message_count": r["message_count"] or 0,
                    "last_ts": r["last_ts"],
                    "last_content": self._preview_message_content(
                        self._deserialize_message_content(r["last_content"]) or ""
                    )[:120],
                    "last_role": r["last_role"],
                }
                for r in rows
            ]

    def get_last_message_ts(self, conversation_id: str) -> str | None:
        """Return the timestamp of the most recent active message, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ts FROM messages "
                "WHERE conversation_id = ? AND compacted_at IS NULL "
                "ORDER BY ts DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            return row["ts"] if row else None

    def compact_all(self, conversation_id: str) -> int:
        """Mark all active messages as compacted. Returns count."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE messages SET compacted_at = datetime('now') "
                "WHERE conversation_id = ? AND compacted_at IS NULL",
                (conversation_id,),
            )
            return cursor.rowcount

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------

    def store_compaction(
        self, conversation_id: str, summary: str, n_to_compact: int
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE messages SET compacted_at = datetime('now')
                WHERE id IN (
                    SELECT id FROM messages
                    WHERE conversation_id = ? AND compacted_at IS NULL
                    ORDER BY ts ASC
                    LIMIT ?
                )
                """,
                (conversation_id, n_to_compact),
            )
            conn.execute(
                "INSERT INTO summaries (conversation_id, content, message_count) VALUES (?, ?, ?)",
                (conversation_id, summary, n_to_compact),
            )
            # Record compaction event
            conn.execute(
                "INSERT INTO events (event_type, conversation_id, data) VALUES (?, ?, ?)",
                (
                    "compaction",
                    conversation_id,
                    json.dumps({"message_count": n_to_compact}),
                ),
            )

    def get_latest_summary(self, conversation_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content FROM summaries WHERE conversation_id = ? ORDER BY ts DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            return row["content"] if row else None

    # ------------------------------------------------------------------
    # Cron Jobs
    # ------------------------------------------------------------------

    def add_cron_job(
        self,
        name: str,
        type: str,
        schedule: str,
        next_run: str,
        payload: str = "",
        conversation_id: str = "",
        user_id: str = "",
        one_shot: bool = False,
        notify_channel: str = "",
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO cron_jobs (name, type, schedule, next_run, payload, conversation_id, user_id, one_shot, notify_channel) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    type,
                    schedule,
                    next_run,
                    payload,
                    conversation_id,
                    user_id or DEFAULT_USER_ID,
                    1 if one_shot else 0,
                    notify_channel,
                ),
            )
            row_id = cursor.lastrowid
            if row_id is None:
                raise RuntimeError("failed to create cron job")
            return row_id

    def get_due_jobs(self) -> list[dict]:
        """Return enabled jobs whose next_run <= now."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, type, schedule, payload, conversation_id, user_id, one_shot, notify_channel "
                "FROM cron_jobs WHERE enabled = 1 AND next_run <= datetime('now')"
            ).fetchall()
            return [dict(r) for r in rows]

    def update_job_run(self, job_id: int, next_run: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE cron_jobs SET last_run = datetime('now'), next_run = ? WHERE id = ?",
                (next_run, job_id),
            )

    def list_cron_jobs(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, type, schedule, payload, conversation_id, user_id, notify_channel, enabled, last_run, next_run "
                "FROM cron_jobs ORDER BY next_run ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_cron_job(self, job_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Token Usage
    # ------------------------------------------------------------------

    # Cost per 1M tokens (input, output) — update as models change
    MODEL_COSTS: dict[str, tuple[float, float]] = {
        # OpenAI
        "gpt-5.4": (2.50, 15.00),
        "gpt-5.4-pro": (30.00, 180.00),
        "gpt-5.4-mini": (0.75, 4.50),
        "gpt-5.4-nano": (0.20, 1.25),
        "gpt-4.1": (3.00, 12.00),
        "gpt-4.1-mini": (0.80, 3.20),
        "gpt-4.1-nano": (0.20, 0.80),
        "gpt-4o": (3.75, 15.00),
        "gpt-4o-mini": (0.30, 1.20),
        "o4-mini": (4.00, 16.00),
        # Claude
        "claude-opus-4-6": (5.00, 25.00),
        "claude-opus-4-5": (5.00, 25.00),
        "claude-opus-4-1": (15.00, 75.00),
        "claude-opus-4-0": (15.00, 75.00),
        "claude-sonnet-4-6": (3.00, 15.00),
        "claude-sonnet-4-5": (3.00, 15.00),
        "claude-sonnet-4-0": (3.00, 15.00),
        "claude-haiku-4-5": (1.00, 5.00),
        "claude-haiku-3-5": (0.80, 4.00),
        "claude-3-haiku": (0.25, 1.25),
    }

    def _estimate_cost(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        """Estimate USD cost based on model pricing per 1M tokens."""
        costs = self.MODEL_COSTS.get(model)
        if not costs:
            for prefix, c in self.MODEL_COSTS.items():
                if model.startswith(prefix):
                    costs = c
                    break
        if not costs:
            costs = (2.00, 8.00)  # default fallback
        input_cost, output_cost = costs
        return (input_tokens * input_cost + output_tokens * output_cost) / 1_000_000

    def record_token_usage(
        self,
        conversation_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        trigger: str = "chat",
    ) -> None:
        total = input_tokens + output_tokens
        cost = self._estimate_cost(model, input_tokens, output_tokens)
        user_id = self.get_conversation_user(conversation_id)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO token_usage "
                "(conversation_id, user_id, model, input_tokens, output_tokens, total_tokens, estimated_cost_usd, trigger) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    user_id,
                    model,
                    input_tokens,
                    output_tokens,
                    total,
                    cost,
                    trigger,
                ),
            )

    def get_token_summary(self, days: int | None = None) -> dict:
        """Return token usage summary. If days is set, only include the last N days."""
        where = ""
        params: list = []
        if days is not None:
            where = "WHERE ts >= datetime('now', ?)"
            params = [f"-{days} days"]

        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COALESCE(SUM(input_tokens), 0) as input, "
                f"COALESCE(SUM(output_tokens), 0) as output, "
                f"COALESCE(SUM(total_tokens), 0) as total, "
                f"COALESCE(SUM(estimated_cost_usd), 0) as cost, "
                f"COUNT(*) as runs "
                f"FROM token_usage {where}",
                params,
            ).fetchone()
            summary = {
                "input_tokens": row["input"],
                "output_tokens": row["output"],
                "total_tokens": row["total"],
                "estimated_cost_usd": round(row["cost"], 4),
                "runs": row["runs"],
            }

            daily = conn.execute(
                f"SELECT DATE(ts) as day, "
                f"SUM(input_tokens) as input, "
                f"SUM(output_tokens) as output, "
                f"SUM(total_tokens) as total, "
                f"SUM(estimated_cost_usd) as cost, "
                f"COUNT(*) as runs "
                f"FROM token_usage {where} "
                f"GROUP BY DATE(ts) ORDER BY day DESC LIMIT 30",
                params,
            ).fetchall()
            summary["daily"] = [
                {
                    "day": r["day"],
                    "input_tokens": r["input"],
                    "output_tokens": r["output"],
                    "total_tokens": r["total"],
                    "estimated_cost_usd": round(r["cost"], 4),
                    "runs": r["runs"],
                }
                for r in daily
            ]

            by_conv = conn.execute(
                f"SELECT conversation_id, "
                f"SUM(input_tokens) as input, "
                f"SUM(output_tokens) as output, "
                f"SUM(total_tokens) as total, "
                f"SUM(estimated_cost_usd) as cost, "
                f"COUNT(*) as runs "
                f"FROM token_usage {where} "
                f"GROUP BY conversation_id ORDER BY cost DESC",
                params,
            ).fetchall()
            summary["by_conversation"] = [
                {
                    "conversation_id": r["conversation_id"],
                    "input_tokens": r["input"],
                    "output_tokens": r["output"],
                    "total_tokens": r["total"],
                    "estimated_cost_usd": round(r["cost"], 4),
                    "runs": r["runs"],
                }
                for r in by_conv
            ]

            by_model = conn.execute(
                f"SELECT model, "
                f"SUM(input_tokens) as input, "
                f"SUM(output_tokens) as output, "
                f"SUM(total_tokens) as total, "
                f"SUM(estimated_cost_usd) as cost, "
                f"COUNT(*) as runs "
                f"FROM token_usage {where} "
                f"GROUP BY model ORDER BY cost DESC",
                params,
            ).fetchall()
            summary["by_model"] = [
                {
                    "model": r["model"],
                    "input_tokens": r["input"],
                    "output_tokens": r["output"],
                    "total_tokens": r["total"],
                    "estimated_cost_usd": round(r["cost"], 4),
                    "runs": r["runs"],
                }
                for r in by_model
            ]

            return summary

    def get_token_cost_brief(self) -> str:
        """One-line cost summary for inclusion in the system prompt."""
        today = self.get_token_summary(days=1)
        month = self.get_token_summary(days=30)
        return (
            f"Token usage — today: ${today['estimated_cost_usd']:.2f} "
            f"({today['total_tokens']:,} tokens, {today['runs']} runs) | "
            f"last 30d: ${month['estimated_cost_usd']:.2f} "
            f"({month['total_tokens']:,} tokens, {month['runs']} runs)"
        )
