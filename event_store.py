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

logger = logging.getLogger("handler.event_store")


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
                    source TEXT DEFAULT '',
                    data TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_events_type_ts
                    ON events(event_type, ts);
                CREATE INDEX IF NOT EXISTS idx_events_conversation
                    ON events(conversation_id, ts);

                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
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
                    enabled INTEGER DEFAULT 1,
                    one_shot INTEGER DEFAULT 0,
                    last_run TEXT DEFAULT NULL,
                    next_run TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY,
                    conversation_id TEXT,
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
            conn.execute(
                "ALTER TABLE cron_jobs ADD COLUMN one_shot INTEGER DEFAULT 0"
            )
        except Exception:
            pass

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
                event_type = "agent_message" if r["role"] == "assistant" else "user_message"
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
    ):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (event_type, source, conversation_id, data) "
                "VALUES (?, ?, ?, ?)",
                (event_type, source, conversation_id, json.dumps(data, default=str)),
            )

    # ------------------------------------------------------------------
    # Conversations & Messages
    # ------------------------------------------------------------------

    def ensure_conversation(self, conversation_id: str, channel: str = ""):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversations (id, channel) VALUES (?, ?)",
                (conversation_id, channel),
            )

    def add_message(self, conversation_id: str, role: str, content: str):
        event_type = "agent_message" if role == "assistant" else "user_message"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
                (conversation_id, role, content),
            )
            # Also record as an event
            conn.execute(
                "INSERT INTO events (event_type, conversation_id, data) VALUES (?, ?, ?)",
                (
                    event_type,
                    conversation_id,
                    json.dumps({"role": role, "content": content[:500]}),
                ),
            )

    def get_messages(
        self, conversation_id: str, limit: int | None = None
    ) -> list[dict]:
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
            return [{"role": r["role"], "content": r["content"]} for r in rows]

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
                    c.created_at,
                    COUNT(m.id) AS message_count,
                    MAX(m.ts) AS last_ts,
                    (SELECT content FROM messages
                     WHERE conversation_id = c.id AND compacted_at IS NULL
                     ORDER BY ts DESC LIMIT 1) AS last_content,
                    (SELECT role FROM messages
                     WHERE conversation_id = c.id AND compacted_at IS NULL
                     ORDER BY ts DESC LIMIT 1) AS last_role
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id AND m.compacted_at IS NULL
                WHERE c.channel = 'web'
                GROUP BY c.id
                ORDER BY last_ts DESC NULLS LAST
                """
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "message_count": r["message_count"] or 0,
                    "last_ts": r["last_ts"],
                    "last_content": (r["last_content"] or "")[:120],
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
        one_shot: bool = False,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO cron_jobs (name, type, schedule, next_run, payload, conversation_id, one_shot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    type,
                    schedule,
                    next_run,
                    payload,
                    conversation_id,
                    1 if one_shot else 0,
                ),
            )
            return cursor.lastrowid

    def get_due_jobs(self) -> list[dict]:
        """Return enabled jobs whose next_run <= now."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, type, schedule, payload, conversation_id, one_shot "
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
                "SELECT id, name, type, schedule, payload, conversation_id, enabled, last_run, next_run "
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
        "gpt-4o": (2.50, 10.00),
        "gpt-4o-mini": (0.15, 0.60),
        "gpt-4.1": (2.00, 8.00),
        "gpt-4.1-mini": (0.40, 1.60),
        "gpt-4.1-nano": (0.10, 0.40),
        "gpt-5": (2.00, 8.00),
        "gpt-5-mini": (0.40, 1.60),
        "gpt-5.4-2026-03-05": (2.00, 8.00),
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
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO token_usage "
                "(conversation_id, model, input_tokens, output_tokens, total_tokens, estimated_cost_usd, trigger) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, model, input_tokens, output_tokens, total, cost, trigger),
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
