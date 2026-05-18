"""Knowledge base pipeline — filter and extract life facts from indexed emails."""

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .preprocessing import preprocess_email_for_classification

# Six categories intentionally minimal — expand by adding here and re-running extract
KB_CATEGORIES = {
    "finances": "Bank accounts, credit cards, investments, balances, bills, payments",
    "property": "Owned real estate, mortgages, HOA, property records",
    "insurance": "Health, life, auto, home policies, premiums, claims",
    "taxes": "Tax filings, W2s, 1099s, refunds, estimated payments",
    "subscriptions": "Recurring services, memberships, software subscriptions",
    "personal": "Employment, address changes, identity documents, important personal records",
}

DEFAULT_FILTER_MODEL = "gpt-5.4-nano-2026-03-17"
DEFAULT_EXTRACT_MODEL = "gpt-5.4-mini-2026-03-17"

_FILTER_PROMPT = """Does this email likely contain important personal life information worth keeping long-term?

Important: bank/investment statements, mortgage/property docs, insurance policies, tax documents, subscription receipts, employment/HR records, address changes, identity documents.

Skip: marketing, newsletters, promotions, shipping updates, job postings, social invites, news.

From: {from_email}
Subject: {subject}

Reply ONLY with "yes" or "no"."""

_EXTRACT_PROMPT = """Extract the key life facts from this email for a personal knowledge base.

Categories:
- finances: bank/credit/investment accounts, balances, payments, bills
- property: owned real estate, mortgage, HOA
- insurance: health/life/auto/home policies, premiums, claims
- taxes: filings, refunds, W2/1099s, payments
- subscriptions: recurring services/memberships
- personal: employment, address, identity documents, important records

Email:
From: {from_email}
Date: {date}
Subject: {subject}

{body}

If this email contains facts worth keeping long-term, respond with JSON:
{{"category": "<one of the 6 categories>", "note": "<2-5 sentence concise summary of key facts>"}}

If no important long-term facts (marketing, one-off notification, etc.):
{{"skip": true}}"""


class KnowledgeBase:
    """SQLite-backed store for filter results and extracted notes."""

    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS kb_filter (
                gmail_id TEXT PRIMARY KEY,
                is_important INTEGER NOT NULL,
                filtered_at INTEGER NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS kb_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_id TEXT UNIQUE NOT NULL,
                email_date TEXT,
                email_from TEXT,
                email_subject TEXT,
                category TEXT NOT NULL,
                note TEXT NOT NULL,
                extracted_at INTEGER NOT NULL
            )
        """)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kb_notes_category ON kb_notes(category)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kb_notes_date ON kb_notes(email_date)"
        )
        self.conn.commit()

    def filter_status(self, gmail_id: str) -> Optional[bool]:
        """None = not yet processed, True = important, False = skip."""
        row = self.conn.execute(
            "SELECT is_important FROM kb_filter WHERE gmail_id = ?", (gmail_id,)
        ).fetchone()
        return bool(row[0]) if row is not None else None

    def has_note(self, gmail_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM kb_notes WHERE gmail_id = ?", (gmail_id,)
            ).fetchone()
            is not None
        )

    def save_filter(self, gmail_id: str, is_important: bool):
        self.conn.execute(
            "INSERT OR REPLACE INTO kb_filter (gmail_id, is_important, filtered_at) VALUES (?, ?, ?)",
            (gmail_id, int(is_important), int(time.time())),
        )
        self.conn.commit()

    def save_note(
        self,
        gmail_id: str,
        email_date: str,
        email_from: str,
        email_subject: str,
        category: str,
        note: str,
    ):
        self.conn.execute(
            """INSERT OR REPLACE INTO kb_notes
               (gmail_id, email_date, email_from, email_subject, category, note, extracted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (gmail_id, email_date, email_from, email_subject, category, note, int(time.time())),
        )
        self.conn.commit()

    def get_notes_by_category(self, category: str) -> list:
        return self.conn.execute(
            "SELECT email_date, email_from, email_subject, note FROM kb_notes "
            "WHERE category = ? ORDER BY email_date ASC",
            (category,),
        ).fetchall()

    def get_stats(self) -> dict:
        total_filtered = self.conn.execute("SELECT COUNT(*) FROM kb_filter").fetchone()[0]
        total_important = self.conn.execute(
            "SELECT COUNT(*) FROM kb_filter WHERE is_important = 1"
        ).fetchone()[0]
        total_notes = self.conn.execute("SELECT COUNT(*) FROM kb_notes").fetchone()[0]
        by_category = dict(
            self.conn.execute(
                "SELECT category, COUNT(*) FROM kb_notes GROUP BY category"
            ).fetchall()
        )
        return {
            "total_filtered": total_filtered,
            "total_important": total_important,
            "total_notes": total_notes,
            "by_category": by_category,
        }

    def export_markdown(self, output_dir: Path) -> Path:
        """Write per-category markdown files to output_dir."""
        output_dir.mkdir(parents=True, exist_ok=True)

        for category, description in KB_CATEGORIES.items():
            notes = self.get_notes_by_category(category)
            if not notes:
                continue

            lines = [f"# {category.title()}", f"_{description}_", ""]
            for date, from_email, subject, note in notes:
                lines.append(f"## {date or 'Unknown date'} | {from_email or ''}")
                lines.append(f"**{subject or ''}**")
                lines.append("")
                lines.append(note)
                lines.append("")

            (output_dir / f"{category}.md").write_text("\n".join(lines))

        stats = self.get_stats()
        index_lines = [
            "# Personal Knowledge Base",
            "",
            f"_Built from Gmail. Last updated: {datetime.now().strftime('%Y-%m-%d')}_",
            "",
            "## Categories",
            "",
        ]
        for cat, desc in KB_CATEGORIES.items():
            count = stats["by_category"].get(cat, 0)
            if count > 0:
                index_lines.append(f"- [{cat}.md]({cat}.md) — {desc} ({count} entries)")

        (output_dir / "index.md").write_text("\n".join(index_lines))
        return output_dir

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _filter_email(client, subject: str, from_email: str, model: str) -> bool:
    prompt = _FILTER_PROMPT.format(from_email=from_email or "", subject=subject or "")
    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=5,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip().lower().startswith("yes")


def _extract_facts(
    client, email_date: str, from_email: str, subject: str, body: str, model: str
) -> Optional[dict]:
    body_text = preprocess_email_for_classification(body or "", level=2)[:2000]
    prompt = _EXTRACT_PROMPT.format(
        from_email=from_email or "",
        date=email_date or "",
        subject=subject or "",
        body=body_text or "(no body)",
    )
    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.choices[0].message.content.strip()

    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if result.get("skip"):
        return None

    category = result.get("category", "").strip()
    note = result.get("note", "").strip()
    if category not in KB_CATEGORIES or not note:
        return None

    return {"category": category, "note": note}


def run_pipeline(
    user_id: str,
    limit: Optional[int] = None,
    refilter: bool = False,
    reextract: bool = False,
    progress_callback=None,
    year: Optional[int] = None,
    filter_model: str = DEFAULT_FILTER_MODEL,
    extract_model: str = DEFAULT_EXTRACT_MODEL,
) -> dict:
    """
    Run the KB pipeline for a specific user over their indexed emails.

    For each email:
    1. Filter (Haiku, sender+subject only) — is this worth keeping?
    2. Extract (Haiku, full body) — what are the key facts?

    Results stored in kb_filter and kb_notes tables in the user's emails.db.
    Markdown files exported to the user's knowledge/ directory.

    progress_callback(event: dict) receives per-email status updates.
    """
    from openai import OpenAI
    from .database import EmailDatabase
    from ..users import get_user

    user = get_user(user_id)
    db_path = str(user.emails_db_path)
    output_dir = user.knowledge_dir

    client = OpenAI()
    stats = {
        "total": 0,
        "filtered_important": 0,
        "filtered_skip": 0,
        "extracted": 0,
        "errors": 0,
        "filter_api_calls": 0,
        "extract_api_calls": 0,
    }

    with EmailDatabase(db_path) as email_db, KnowledgeBase(db_path) as kb:
        emails = email_db.get_messages(limit=limit, year=year)
        stats["total"] = len(emails)

        for i, email in enumerate(emails):
            gmail_id = email["gmail_id"]
            subject = email.get("subject") or ""
            from_email = email.get("from_email") or ""
            date = email.get("date") or ""
            body_plain = email.get("body_plain") or ""
            body_html = email.get("body_html") or ""

            try:
                # Phase 1: filter
                cached = kb.filter_status(gmail_id)
                if cached is None or refilter:
                    is_important = _filter_email(client, subject, from_email, filter_model)
                    kb.save_filter(gmail_id, is_important)
                    stats["filter_api_calls"] += 1
                else:
                    is_important = cached

                if not is_important:
                    stats["filtered_skip"] += 1
                    if progress_callback:
                        progress_callback({"phase": "skip", "subject": subject, "index": i, "total": stats["total"]})
                    continue

                stats["filtered_important"] += 1

                # Phase 2: extract
                if kb.has_note(gmail_id) and not reextract:
                    if progress_callback:
                        progress_callback({"phase": "cached", "subject": subject, "index": i, "total": stats["total"]})
                    continue

                result = _extract_facts(client, date, from_email, subject, body_plain or body_html, extract_model)
                stats["extract_api_calls"] += 1

                if result:
                    kb.save_note(
                        gmail_id=gmail_id,
                        email_date=date,
                        email_from=from_email,
                        email_subject=subject,
                        category=result["category"],
                        note=result["note"],
                    )
                    stats["extracted"] += 1
                    if progress_callback:
                        progress_callback({
                            "phase": "extracted",
                            "subject": subject,
                            "category": result["category"],
                            "index": i,
                            "total": stats["total"],
                        })
                else:
                    if progress_callback:
                        progress_callback({"phase": "extract_skip", "subject": subject, "index": i, "total": stats["total"]})

            except Exception as e:
                stats["errors"] += 1
                if progress_callback:
                    progress_callback({"phase": "error", "subject": subject, "error": str(e), "index": i, "total": stats["total"]})

        kb.export_markdown(output_dir)
        stats["output_dir"] = str(output_dir)
        stats["kb_stats"] = kb.get_stats()

    return stats
