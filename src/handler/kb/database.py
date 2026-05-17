"""Database module for Gmail email indexing."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import (
    Message,
    Category,
    EmailCategory,
    IndexingProgress,
)

class EmailDatabase:
    """Manages SQLite database for email indexing."""

    def __init__(self, db_path: str):
        """Initialize database connection.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._initialize_schema()

    def _initialize_schema(self):
        """Create database tables if they don't exist."""
        cursor = self.conn.cursor()

        # Messages table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_id TEXT UNIQUE NOT NULL,
                thread_id TEXT,
                subject TEXT,
                from_email TEXT NOT NULL,
                from_name TEXT,
                to_emails TEXT,
                cc_emails TEXT,
                date TEXT,
                date_timestamp INTEGER,
                body_plain TEXT,
                body_html TEXT,
                labels TEXT,
                file_path TEXT,
                has_attachments BOOLEAN DEFAULT 0,
                attachment_count INTEGER DEFAULT 0,
                attachment_info TEXT,
                size_bytes INTEGER,
                indexed_at INTEGER
            )
        """)

        # Indexes for messages
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_gmail_id ON messages(gmail_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date_timestamp DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_email)"
        )

        # Indexing progress table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS indexing_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                month INTEGER,
                last_message_id TEXT,
                total_messages INTEGER,
                indexed_messages INTEGER,
                started_at INTEGER,
                updated_at INTEGER,
                completed BOOLEAN DEFAULT 0,
                UNIQUE(year, month)
            )
        """)

        # Full-text search table
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                subject, from_email, from_name, body_plain,
                content=messages,
                content_rowid=id
            )
        """)

        # FTS triggers to keep in sync
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, subject, from_email, from_name, body_plain)
                VALUES (new.id, new.subject, new.from_email, new.from_name, new.body_plain);
            END
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                DELETE FROM messages_fts WHERE rowid = old.id;
            END
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
                UPDATE messages_fts SET 
                    subject = new.subject,
                    from_email = new.from_email,
                    from_name = new.from_name,
                    body_plain = new.body_plain
                WHERE rowid = new.id;
            END
        """)

        # Categories table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                parent_id INTEGER,
                description TEXT,
                color TEXT,
                icon TEXT,
                automation_enabled BOOLEAN DEFAULT 0,
                created_at INTEGER,
                FOREIGN KEY (parent_id) REFERENCES categories(id)
            )
        """)

        # Email categorization results
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS email_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                confidence REAL NOT NULL,
                method TEXT,
                model_name TEXT,
                sub_category TEXT,
                tags TEXT,
                suggested_actions TEXT,
                categorized_at INTEGER,
                reviewed BOOLEAN DEFAULT 0,
                corrected_category_id INTEGER,
                
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
                FOREIGN KEY (category_id) REFERENCES categories(id),
                UNIQUE(message_id)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_email_categories_message 
            ON email_categories(message_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_email_categories_category 
            ON email_categories(category_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_email_categories_confidence 
            ON email_categories(confidence)
        """)

        # Categorization statistics
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS categorization_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                category_id INTEGER,
                method TEXT,
                total_count INTEGER,
                avg_confidence REAL,
                api_cost REAL,
                processing_time_ms INTEGER,
                
                FOREIGN KEY (category_id) REFERENCES categories(id)
            )
        """)

        # Action queue
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS action_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                action_data TEXT,
                priority INTEGER DEFAULT 5,
                status TEXT DEFAULT 'pending',
                scheduled_at INTEGER,
                executed_at INTEGER,
                error_message TEXT,
                
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
            )
        """)

        # Learned rules
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS learned_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                rule_type TEXT NOT NULL,
                rule_value TEXT NOT NULL,
                occurrence_count INTEGER DEFAULT 1,
                correct_count INTEGER DEFAULT 1,
                accuracy REAL,
                avg_confidence REAL,
                first_seen INTEGER,
                last_seen INTEGER,
                promoted_to_production BOOLEAN DEFAULT 0,
                promoted_at INTEGER,
                human_reviewed BOOLEAN DEFAULT 0,
                human_approved BOOLEAN DEFAULT 0,
                reviewed_by TEXT,
                notes TEXT,
                
                FOREIGN KEY (category_id) REFERENCES categories(id),
                UNIQUE(category_id, rule_type, rule_value)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_learned_rules_category 
            ON learned_rules(category_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_learned_rules_accuracy 
            ON learned_rules(accuracy DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_learned_rules_promoted 
            ON learned_rules(promoted_to_production)
        """)

        # Rule learning evidence
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rule_learning_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                learned_rule_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                confidence REAL NOT NULL,
                method TEXT NOT NULL,
                created_at INTEGER,
                
                FOREIGN KEY (learned_rule_id) REFERENCES learned_rules(id) ON DELETE CASCADE,
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
                UNIQUE(learned_rule_id, message_id)
            )
        """)

        # Rule performance
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rule_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                learned_rule_id INTEGER,
                date TEXT,
                matches INTEGER DEFAULT 0,
                correct_predictions INTEGER DEFAULT 0,
                false_positives INTEGER DEFAULT 0,
                accuracy REAL,
                
                FOREIGN KEY (learned_rule_id) REFERENCES learned_rules(id)
            )
        """)

        # Classification audit log
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS classification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                attempt_number INTEGER DEFAULT 1,
                started_at INTEGER NOT NULL,
                completed_at INTEGER,
                total_duration_ms INTEGER,
                final_category_id INTEGER,
                final_confidence REAL,
                final_method TEXT,
                call_chain TEXT,
                total_tokens_used INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                estimated_cost REAL DEFAULT 0.0,
                error_occurred BOOLEAN DEFAULT 0,
                error_message TEXT,
                error_step TEXT,
                
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
                FOREIGN KEY (final_category_id) REFERENCES categories(id)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_classification_log_message 
            ON classification_log(message_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_classification_log_date 
            ON classification_log(started_at DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_classification_log_cost 
            ON classification_log(estimated_cost DESC)
        """)

        # Per-step classification details
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS classification_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_id INTEGER NOT NULL,
                step_number INTEGER NOT NULL,
                step_type TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                completed_at INTEGER,
                duration_ms INTEGER,
                input_data_preview TEXT,
                input_tokens INTEGER,
                predicted_category TEXT,
                confidence REAL,
                output_tokens INTEGER,
                raw_response TEXT,
                step_cost REAL,
                model_name TEXT,
                was_accepted BOOLEAN,
                acceptance_reason TEXT,
                matched_rules TEXT,
                
                FOREIGN KEY (log_id) REFERENCES classification_log(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_classification_steps_log 
            ON classification_steps(log_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_classification_steps_type 
            ON classification_steps(step_type)
        """)

        self.conn.commit()

    def message_exists(self, gmail_id: str) -> bool:
        """Check if a message already exists in the database.

        Args:
            gmail_id: Gmail message ID

        Returns:
            True if message exists, False otherwise
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT 1 FROM messages WHERE gmail_id = ? LIMIT 1", (gmail_id,))
        return cursor.fetchone() is not None

    def insert_message(self, message_data: Dict[str, Any]) -> int:
        """Insert a new message into the database.

        Args:
            message_data: Dictionary containing message fields

        Returns:
            ID of inserted message
        """
        cursor = self.conn.cursor()

        # Convert lists/dicts to JSON strings
        for field in ["to_emails", "cc_emails", "labels", "attachment_info"]:
            if field in message_data and message_data[field] is not None:
                if not isinstance(message_data[field], str):
                    message_data[field] = json.dumps(message_data[field])

        # Set indexed timestamp
        message_data["indexed_at"] = int(datetime.now().timestamp())

        fields = list(message_data.keys())
        placeholders = ",".join(["?" for _ in fields])
        field_names = ",".join(fields)

        query = f"INSERT INTO messages ({field_names}) VALUES ({placeholders})"
        cursor.execute(query, [message_data[f] for f in fields])
        self.conn.commit()

        return cursor.lastrowid

    def insert_message_model(self, message: Message) -> int:
        """Insert a Message object into the database.

        Args:
            message: Message object to insert

        Returns:
            ID of inserted message
        """
        message.indexed_at = int(datetime.now().timestamp())
        message_data = message.to_dict()
        return self.insert_message(message_data)

    def update_message(self, gmail_id: str, message_data: Dict[str, Any]) -> None:
        """Update an existing message in the database.

        Args:
            gmail_id: Gmail message ID
            message_data: Dictionary containing message fields to update
        """
        cursor = self.conn.cursor()

        # Convert lists/dicts to JSON strings
        for field in ["to_emails", "cc_emails", "labels", "attachment_info"]:
            if field in message_data and message_data[field] is not None:
                if not isinstance(message_data[field], str):
                    message_data[field] = json.dumps(message_data[field])

        # Update indexed timestamp
        message_data["indexed_at"] = int(datetime.now().timestamp())

        fields = list(message_data.keys())
        set_clause = ",".join([f"{f} = ?" for f in fields])

        query = f"UPDATE messages SET {set_clause} WHERE gmail_id = ?"
        cursor.execute(query, [message_data[f] for f in fields] + [gmail_id])
        self.conn.commit()

    def get_progress(
        self, year: int, month: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """Get indexing progress for a year/month.

        Args:
            year: Year to check
            month: Optional month (1-12)

        Returns:
            Progress data or None if not found
        """
        cursor = self.conn.cursor()
        if month is None:
            cursor.execute(
                "SELECT * FROM indexing_progress WHERE year = ? AND month IS NULL",
                (year,),
            )
        else:
            cursor.execute(
                "SELECT * FROM indexing_progress WHERE year = ? AND month = ?",
                (year, month),
            )

        row = cursor.fetchone()
        return dict(row) if row else None

    def get_progress_as_model(
        self, year: int, month: Optional[int] = None
    ) -> Optional[IndexingProgress]:
        """Get indexing progress as IndexingProgress object.

        Args:
            year: Year to check
            month: Optional month (1-12)

        Returns:
            IndexingProgress object or None if not found
        """
        cursor = self.conn.cursor()
        if month is None:
            cursor.execute(
                "SELECT * FROM indexing_progress WHERE year = ? AND month IS NULL",
                (year,),
            )
        else:
            cursor.execute(
                "SELECT * FROM indexing_progress WHERE year = ? AND month = ?",
                (year, month),
            )

        row = cursor.fetchone()
        return IndexingProgress.from_dict(dict(row)) if row else None

    def update_progress(
        self,
        year: int,
        month: Optional[int],
        total_messages: int,
        indexed_messages: int,
        last_message_id: Optional[str] = None,
        completed: bool = False,
    ) -> None:
        """Update or create indexing progress record.

        Args:
            year: Year being indexed
            month: Month being indexed (optional)
            total_messages: Total messages to index
            indexed_messages: Number of messages indexed so far
            last_message_id: ID of last successfully indexed message
            completed: Whether indexing is complete
        """
        cursor = self.conn.cursor()
        now = int(datetime.now().timestamp())

        existing = self.get_progress(year, month)

        if existing:
            cursor.execute(
                """
                UPDATE indexing_progress
                SET total_messages = ?,
                    indexed_messages = ?,
                    last_message_id = ?,
                    updated_at = ?,
                    completed = ?
                WHERE year = ? AND month IS ?
            """,
                (
                    total_messages,
                    indexed_messages,
                    last_message_id,
                    now,
                    completed,
                    year,
                    month,
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO indexing_progress 
                (year, month, total_messages, indexed_messages, last_message_id, 
                 started_at, updated_at, completed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    year,
                    month,
                    total_messages,
                    indexed_messages,
                    last_message_id,
                    now,
                    now,
                    completed,
                ),
            )

        self.conn.commit()

    def get_message_count(self) -> int:
        """Get total number of indexed messages.

        Returns:
            Total message count
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM messages")
        return cursor.fetchone()[0]

    def search_messages(self, query: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Search messages using full-text search.

        Args:
            query: Search query
            limit: Maximum results to return

        Returns:
            List of matching messages
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT m.* FROM messages m
            JOIN messages_fts fts ON m.id = fts.rowid
            WHERE messages_fts MATCH ?
            ORDER BY m.date_timestamp DESC
            LIMIT ?
        """,
            (query, limit),
        )

        return [dict(row) for row in cursor.fetchall()]

    # Categorization methods

    def init_default_categories(self):
        """Initialize default category taxonomy."""
        categories = [
            ("finance", "Financial transactions, bills, statements, banking"),
            ("career", "Job applications, networking, work communications"),
            ("health", "Medical appointments, health insurance, fitness"),
            ("social", "Event invitations, social gatherings, meetups"),
            ("transaction", "E-commerce purchases, order confirmations, shipping"),
            ("travel", "Bookings, itineraries, travel updates"),
            ("education", "Courses, workshops, academic communications"),
            ("personal", "Family, friends, hobbies, personal projects"),
            ("marketing", "Promotional content, newsletters, advertising"),
            ("spam", "Junk mail, phishing attempts, unsolicited emails"),
            ("other", "Uncategorizable emails"),
        ]

        cursor = self.conn.cursor()
        for name, description in categories:
            cursor.execute(
                "INSERT OR IGNORE INTO categories (name, description, created_at) VALUES (?, ?, ?)",
                (name, description, int(datetime.now().timestamp())),
            )
        self.conn.commit()

    def get_category_id(self, category_name: str) -> Optional[int]:
        """Get category ID by name."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
        result = cursor.fetchone()
        return result[0] if result else None

    def get_category_by_name(self, category_name: str) -> Optional[Category]:
        """Get Category object by name.

        Args:
            category_name: Name of the category

        Returns:
            Category object or None if not found
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM categories WHERE name = ?", (category_name,))
        row = cursor.fetchone()
        return Category.from_dict(dict(row)) if row else None

    def get_category_by_id(self, category_id: int) -> Optional[Category]:
        """Get Category object by ID.

        Args:
            category_id: Database ID of the category

        Returns:
            Category object or None if not found
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM categories WHERE id = ?", (category_id,))
        row = cursor.fetchone()
        return Category.from_dict(dict(row)) if row else None

    def get_all_categories(self) -> List[Category]:
        """Get all categories as Category objects.

        Returns:
            List of Category objects
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM categories ORDER BY name")
        return [Category.from_dict(dict(row)) for row in cursor.fetchall()]

    def insert_category(self, category: Category) -> int:
        """Insert a Category object into the database.

        Args:
            category: Category object to insert

        Returns:
            ID of inserted category
        """
        if category.created_at is None:
            category.created_at = int(datetime.now().timestamp())

        cursor = self.conn.cursor()
        data = category.to_dict()

        fields = list(data.keys())
        placeholders = ",".join(["?" for _ in fields])
        field_names = ",".join(fields)

        query = f"INSERT INTO categories ({field_names}) VALUES ({placeholders})"
        cursor.execute(query, [data[f] for f in fields])
        self.conn.commit()

        return cursor.lastrowid

    def save_email_category(
        self,
        message_id: int,
        category_name: str,
        confidence: float,
        method: str,
        model_name: str = None,
        sub_category: str = None,
        tags: List[str] = None,
    ) -> int:
        """Save email categorization result."""
        category_id = self.get_category_id(category_name)
        if not category_id:
            # Create category if it doesn't exist
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO categories (name, created_at) VALUES (?, ?)",
                (category_name, int(datetime.now().timestamp())),
            )
            category_id = cursor.lastrowid
            self.conn.commit()

        cursor = self.conn.cursor()
        tags_json = json.dumps(tags) if tags else None

        cursor.execute(
            """
            INSERT OR REPLACE INTO email_categories 
            (message_id, category_id, confidence, method, model_name, 
             sub_category, tags, categorized_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                message_id,
                category_id,
                confidence,
                method,
                model_name,
                sub_category,
                tags_json,
                int(datetime.now().timestamp()),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def save_email_category_model(self, email_category: EmailCategory) -> int:
        """Save an EmailCategory object.

        Args:
            email_category: EmailCategory object to save

        Returns:
            ID of saved email category
        """
        if email_category.categorized_at is None:
            email_category.categorized_at = int(datetime.now().timestamp())

        cursor = self.conn.cursor()
        data = email_category.to_dict()

        fields = list(data.keys())
        placeholders = ",".join(["?" for _ in fields])
        field_names = ",".join(fields)

        query = f"INSERT OR REPLACE INTO email_categories ({field_names}) VALUES ({placeholders})"
        cursor.execute(query, [data[f] for f in fields])
        self.conn.commit()

        return cursor.lastrowid

    def get_email_category_by_message_id(
        self, message_id: int
    ) -> Optional[EmailCategory]:
        """Get EmailCategory object for a message.

        Args:
            message_id: Database ID of the message

        Returns:
            EmailCategory object or None if not found
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM email_categories WHERE message_id = ?", (message_id,)
        )
        row = cursor.fetchone()
        return EmailCategory.from_dict(dict(row)) if row else None

    def save_classification_log(
        self,
        message_id: int,
        attempt_number: int,
        started_at: int,
        completed_at: int,
        total_duration_ms: int,
        final_category: str,
        final_confidence: float,
        final_method: str,
        call_chain: str,
        total_tokens_used: int,
        input_tokens: int,
        output_tokens: int,
        estimated_cost: float,
        error_occurred: bool = False,
    ) -> int:
        """Save classification audit log."""
        category_id = self.get_category_id(final_category)

        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO classification_log
            (message_id, attempt_number, started_at, completed_at, total_duration_ms,
             final_category_id, final_confidence, final_method, call_chain,
             total_tokens_used, input_tokens, output_tokens, estimated_cost, error_occurred)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                message_id,
                attempt_number,
                started_at,
                completed_at,
                total_duration_ms,
                category_id,
                final_confidence,
                final_method,
                call_chain,
                total_tokens_used,
                input_tokens,
                output_tokens,
                estimated_cost,
                error_occurred,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def save_classification_step(
        self,
        log_id: int,
        step_number: int,
        step_type: str,
        started_at: int,
        completed_at: int,
        duration_ms: int,
        predicted_category: str,
        confidence: float,
        input_tokens: int,
        output_tokens: int,
        step_cost: float,
        model_name: str = None,
        was_accepted: bool = False,
        acceptance_reason: str = None,
        matched_rules: str = None,
        raw_response: str = None,
    ) -> int:
        """Save individual classification step details."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO classification_steps
            (log_id, step_number, step_type, started_at, completed_at, duration_ms,
             predicted_category, confidence, input_tokens, output_tokens, step_cost,
             model_name, was_accepted, acceptance_reason, matched_rules, raw_response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                log_id,
                step_number,
                step_type,
                started_at,
                completed_at,
                duration_ms,
                predicted_category,
                confidence,
                input_tokens,
                output_tokens,
                step_cost,
                model_name,
                was_accepted,
                acceptance_reason,
                matched_rules,
                raw_response,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_uncategorized_messages(
        self, limit: int = None, year: int = None, month: int = None
    ) -> List[Dict[str, Any]]:
        """Get messages that haven't been categorized yet.

        Args:
            limit: Maximum number of messages to return
            year: Filter by specific year
            month: Filter by specific month (1-12)

        Returns:
            List of uncategorized message dictionaries
        """
        cursor = self.conn.cursor()
        query = """
            SELECT m.* FROM messages m
            LEFT JOIN email_categories ec ON m.id = ec.message_id
            WHERE ec.id IS NULL
        """

        params = []
        if year:
            if month:
                # Filter by specific year and month
                start_ts = int(datetime(year, month, 1).timestamp())
                if month == 12:
                    end_ts = int(datetime(year + 1, 1, 1).timestamp())
                else:
                    end_ts = int(datetime(year, month + 1, 1).timestamp())
                query += " AND m.date_timestamp >= ? AND m.date_timestamp < ?"
                params.extend([start_ts, end_ts])
            else:
                # Filter by entire year
                start_ts = int(datetime(year, 1, 1).timestamp())
                end_ts = int(datetime(year + 1, 1, 1).timestamp())
                query += " AND m.date_timestamp >= ? AND m.date_timestamp < ?"
                params.extend([start_ts, end_ts])

        query += " ORDER BY m.date_timestamp DESC"

        if limit:
            query += f" LIMIT {limit}"

        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_messages(
        self, limit: int = None, year: int = None, month: int = None
    ) -> List[Dict[str, Any]]:
        """Get all messages (including already categorized ones).

        Args:
            limit: Maximum number of messages to return
            year: Filter by specific year
            month: Filter by specific month (1-12)

        Returns:
            List of message dictionaries
        """
        cursor = self.conn.cursor()
        query = "SELECT * FROM messages WHERE 1=1"

        params = []
        if year:
            if month:
                # Filter by specific year and month
                start_ts = int(datetime(year, month, 1).timestamp())
                if month == 12:
                    end_ts = int(datetime(year + 1, 1, 1).timestamp())
                else:
                    end_ts = int(datetime(year, month + 1, 1).timestamp())
                query += " AND date_timestamp >= ? AND date_timestamp < ?"
                params.extend([start_ts, end_ts])
            else:
                # Filter by entire year
                start_ts = int(datetime(year, 1, 1).timestamp())
                end_ts = int(datetime(year + 1, 1, 1).timestamp())
                query += " AND date_timestamp >= ? AND date_timestamp < ?"
                params.extend([start_ts, end_ts])

        query += " ORDER BY date_timestamp DESC"

        if limit:
            query += f" LIMIT {limit}"

        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_email_by_id(self, message_id: int) -> Optional[Dict[str, Any]]:
        """Get email details by database ID."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_message_by_id(self, message_id: int) -> Optional[Message]:
        """Get Message object by database ID.

        Args:
            message_id: Database ID of the message

        Returns:
            Message object or None if not found
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
        row = cursor.fetchone()
        return Message.from_dict(dict(row)) if row else None

    def get_message_by_gmail_id(self, gmail_id: str) -> Optional[Message]:
        """Get Message object by Gmail ID.

        Args:
            gmail_id: Gmail message ID

        Returns:
            Message object or None if not found
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM messages WHERE gmail_id = ?", (gmail_id,))
        row = cursor.fetchone()
        return Message.from_dict(dict(row)) if row else None

    def get_uncategorized_messages_as_models(self, limit: int = None) -> List[Message]:
        """Get uncategorized messages as Message objects.

        Args:
            limit: Maximum number of messages to return

        Returns:
            List of Message objects
        """
        cursor = self.conn.cursor()
        query = """
            SELECT m.* FROM messages m
            LEFT JOIN email_categories ec ON m.id = ec.message_id
            WHERE ec.id IS NULL
            ORDER BY m.date_timestamp DESC
        """
        if limit:
            query += f" LIMIT {limit}"

        cursor.execute(query)
        return [Message.from_dict(dict(row)) for row in cursor.fetchall()]

    def search_messages_as_models(self, query: str, limit: int = 100) -> List[Message]:
        """Search messages using full-text search, returning Message objects.

        Args:
            query: Search query
            limit: Maximum results to return

        Returns:
            List of Message objects
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT m.* FROM messages m
            JOIN messages_fts fts ON m.id = fts.rowid
            WHERE messages_fts MATCH ?
            ORDER BY m.date_timestamp DESC
            LIMIT ?
        """,
            (query, limit),
        )
        return [Message.from_dict(dict(row)) for row in cursor.fetchall()]

    def close(self):
        """Close database connection."""
        self.conn.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
