"""Import data from a legacy handler instance into the runtime's SQLite store.

Copies, never moves: the legacy directory is only read. Imports are
idempotent (threads, users and memory topics that already exist are kept).

- Persona: config/identity.md and config/persona.md → the `persona` settings
  section (config/system.md is skipped: it describes the legacy tools).
- Memory: memory/*.md and users/<id>/memory/*.md → shared memory topics. For
  a name present in several places, the newest file wins; other users' topics
  with a colliding name are appended under a "From <user>" heading.
- Users: every legacy user who owns a conversation or memory → a member.
- Conversations: each legacy conversation → a chat thread
  `chat:<user>:legacy-<id>` with its messages (history only; the first new
  turn gets a recap because the thread has no harness session).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .settings import SettingsService
from .store.sqlite import SQLiteStore, _insert, _timestamp, _transaction
from .store.sqlite_users import User
from .types import MemoryTopic, new_id

_TOPIC_RE = re.compile(r"[^a-z0-9/_-]+")
_INDEX_LINE = re.compile(r"^-\s*([^:]+?)\.md\s*:\s*(.*)$")
_ROLES = {"user": "user", "assistant": "assistant", "system": "event", "tool": "tool"}


@dataclass
class ImportReport:
    persona: bool = False
    topics_created: list[str] = field(default_factory=list)
    topics_merged: list[str] = field(default_factory=list)
    topics_skipped: list[str] = field(default_factory=list)
    users_created: list[str] = field(default_factory=list)
    threads_created: int = 0
    threads_skipped: int = 0
    messages_imported: int = 0

    def lines(self) -> list[str]:
        return [
            f"persona imported: {self.persona}",
            f"memory topics created: {len(self.topics_created)} {self.topics_created}",
            f"memory topics merged: {self.topics_merged}",
            f"memory topics already present: {self.topics_skipped}",
            f"users created: {self.users_created}",
            f"threads created: {self.threads_created} (skipped existing: {self.threads_skipped})",
            f"messages imported: {self.messages_imported}",
        ]


def _topic_name(stem: str) -> str:
    name = _TOPIC_RE.sub("-", stem.strip().lower()).strip("-/") or "untitled"
    return name[:100]


def _index(directory: Path) -> dict[str, str]:
    index = directory / "index.md"
    out: dict[str, str] = {}
    if index.exists():
        for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _INDEX_LINE.match(line.strip())
            if m:
                out[m.group(1).strip()] = m.group(2).strip()
    return out


def _memory_files(source: Path, owner: str | None, directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    index = _index(directory)
    files = []
    for path in sorted(directory.glob("*.md")):
        if path.name == "index.md":
            continue
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            continue
        files.append({"name": _topic_name(path.stem), "owner": owner, "content": content,
                      "description": index.get(path.stem) or content.splitlines()[0][:150],
                      "mtime": path.stat().st_mtime})
    return files


def _blocks(content: str) -> list[dict[str, Any]]:
    """Legacy content is plain text or a JSON list of {type: text|image} blocks."""
    if content.startswith("["):
        try:
            parsed = json.loads(content)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            out = []
            for block in parsed:
                if isinstance(block, dict) and block.get("type") == "text":
                    out.append({"type": "text", "text": str(block.get("text", ""))})
                elif isinstance(block, dict) and block.get("type") == "image":
                    out.append({"type": "text",
                                "text": f"[image attached in the old handler: {block.get('path')}]"})
            if out:
                return out
    return [{"type": "text", "text": content}]


def _legacy_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return datetime.now(timezone.utc)
    # SQLite datetime('now') is UTC without an offset.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def import_legacy(
    source: Path, store: SQLiteStore, settings: SettingsService, *, default_user: str = "danny",
) -> ImportReport:
    source = Path(source).expanduser()
    report = ImportReport()

    # Persona -----------------------------------------------------------
    config = source / "config"
    identity = (config / "identity.md").read_text() if (config / "identity.md").exists() else ""
    persona = (config / "persona.md").read_text() if (config / "persona.md").exists() else ""
    if identity or persona:
        current, version = await settings.get("persona")
        if not (current.get("identity") or current.get("persona")):
            await settings.put("persona", {**current, "identity": identity.strip(),
                                           "persona": persona.strip()},
                               updated_by="import-legacy", expected_version=version)
            report.persona = True

    # Legacy database: owners and conversations --------------------------
    db_path = source / "handler.db"
    legacy = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) if db_path.exists() else None
    conversations: list[tuple[str, str, str]] = []
    if legacy is not None:
        conversations = [(cid, (uid or default_user), created) for cid, uid, created in
                         legacy.execute("SELECT id, user_id, created_at FROM conversations")]

    # Users --------------------------------------------------------------
    owners = {uid for _, uid, _ in conversations}
    users_dir = source / "users"
    if users_dir.is_dir():
        owners |= {d.name for d in users_dir.iterdir() if (d / "memory").is_dir()}
    for uid in sorted(owners):
        if await store.get_user(uid) is None:
            await store.create_user(User(id=uid, display_name=uid.capitalize(), role="member"))
            report.users_created.append(uid)

    # Memory -------------------------------------------------------------
    candidates = _memory_files(source, default_user, source / "memory")
    for uid in sorted(owners):
        candidates += _memory_files(source, uid, users_dir / uid / "memory")
    chosen: dict[str, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []
    for item in candidates:
        best = chosen.get(item["name"])
        if best is None:
            chosen[item["name"]] = item
        elif best["owner"] == item["owner"]:
            if item["mtime"] > best["mtime"]:
                chosen[item["name"]] = item  # same person, newer copy wins
        else:
            extras.append(item)  # another person's topic with the same name
    for name, item in sorted(chosen.items()):
        if await store.get_topic(name) is not None:
            report.topics_skipped.append(name)
            continue
        await store.put_topic(MemoryTopic(name, item["description"], item["content"],
                                          updated_by="import-legacy"), None)
        report.topics_created.append(name)
    for item in extras:
        if item["name"] in report.topics_skipped:
            continue
        topic = await store.get_topic(item["name"])
        marker = f"## From {item['owner']}"
        if topic is None or marker in topic.content:
            continue
        merged = MemoryTopic(topic.name, topic.description,
                             f"{topic.content}\n\n{marker}\n\n{item['content']}",
                             updated_by="import-legacy")
        if await store.put_topic(merged, topic.version):
            report.topics_merged.append(f"{item['name']} (+{item['owner']})")

    # Conversations ------------------------------------------------------
    for cid, uid, created in conversations:
        key = f"chat:{uid}:legacy-{_TOPIC_RE.sub('-', cid.lower())}"
        if await store.get_thread(key) is not None:
            report.threads_skipped += 1
            continue
        rows = legacy.execute(
            "SELECT role, content, ts FROM messages WHERE conversation_id = ? ORDER BY id",
            (cid,)).fetchall()
        summary_row = legacy.execute(
            "SELECT content FROM summaries WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
            (cid,)).fetchone()
        created_at = _timestamp(_legacy_time(created))

        def write(db: sqlite3.Connection, key=key, rows=rows, uid=uid, created_at=created_at,
                  summary=summary_row[0] if summary_row else None) -> int:
            with _transaction(db):
                _insert(db, "threads", {
                    "key": key, "type": "chat", "lane": "interactive", "owner_user_id": uid,
                    "harness": "codex", "model": None, "priority": 0, "session_id": None,
                    "pending_since": None, "available_at": None, "attempts": 0,
                    "last_error": None, "dead": 0, "lease_owner": None, "lease_until": None,
                    "lease_version": 0, "summary": summary, "created_at": created_at,
                })
                for role, content, ts in rows:
                    _insert(db, "messages", {
                        "id": new_id("msg"), "thread_key": key,
                        "role": _ROLES.get(role, "event"),
                        "blocks": json.dumps(_blocks(content or ""), ensure_ascii=False),
                        "run_id": None, "raw": json.dumps({"legacy_role": role}),
                        "created_at": _timestamp(_legacy_time(ts)),
                    })
            return len(rows)

        report.messages_imported += await store._call(write)
        report.threads_created += 1
    if legacy is not None:
        legacy.close()
    return report
