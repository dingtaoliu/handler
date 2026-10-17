"""Legacy handler → runtime import, plus the recap for imported threads."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from handler.runtime.migrate_legacy import import_legacy
from handler.runtime.settings import SettingsService
from handler.runtime.store.sqlite import SQLiteStore


def _legacy(root: Path) -> Path:
    (root / "config").mkdir(parents=True)
    (root / "config" / "identity.md").write_text("You are Handler, Danny's assistant.")
    (root / "config" / "persona.md").write_text("Be concise.")
    (root / "config" / "system.md").write_text("Use shell() and read_file().")
    old, new, zj = root / "memory", root / "users/danny/memory", root / "users/zhijian/memory"
    for d in (old, new, zj):
        d.mkdir(parents=True)
    (old / "user.md").write_text("old user facts")
    (old / "index.md").write_text("- user.md: Danny profile (old)\n")
    (new / "user.md").write_text("new user facts")
    (new / "index.md").write_text("- user.md: Danny profile\n")
    os.utime(old / "user.md", (1_000_000, 1_000_000))
    (old / "daily_growth.md").write_text("pipeline notes")
    (zj / "user.md").write_text("zhijian facts")
    (zj / "recipes.md").write_text("dumplings")
    db = sqlite3.connect(root / "handler.db")
    db.executescript("""
        CREATE TABLE conversations (id TEXT PRIMARY KEY, channel TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')), user_id TEXT DEFAULT '');
        CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id TEXT, role TEXT,
            content TEXT, ts TEXT DEFAULT (datetime('now')), compacted_at TEXT DEFAULT NULL);
        CREATE TABLE summaries (id INTEGER PRIMARY KEY, conversation_id TEXT,
            ts TEXT DEFAULT (datetime('now')), content TEXT, message_count INTEGER);
        INSERT INTO conversations VALUES ('web-abc', 'web', '2026-03-12 02:16:43', 'danny');
        INSERT INTO conversations VALUES ('telegram:42', 'telegram', '2026-03-16 02:26:27', 'zhijian');
        INSERT INTO messages (conversation_id, role, content, ts) VALUES
          ('web-abc', 'user', 'what is my tax situation?', '2026-03-12 02:16:44'),
          ('web-abc', 'assistant', 'You file jointly.', '2026-03-12 02:16:50'),
          ('web-abc', 'system', '[SCHEDULED TASK] digest', '2026-03-13 07:00:00'),
          ('telegram:42', 'user', '[{"type":"text","text":"look"},{"type":"image","path":"/u/p.jpg"}]', '2026-03-16 02:26:28');
        INSERT INTO summaries (conversation_id, content) VALUES ('web-abc', 'Tax chat.');
    """)
    db.commit()
    db.close()
    return root


async def test_import_legacy(tmp_path):
    source = _legacy(tmp_path / "legacy")
    (tmp_path / "rt").mkdir()
    store = SQLiteStore(tmp_path / "rt" / "handler.db")
    settings = SettingsService(store)
    report = await import_legacy(source, store, settings)

    persona, _ = await settings.get("persona")
    assert persona["identity"].startswith("You are Handler") and persona["persona"] == "Be concise."
    assert "shell()" not in json.dumps(persona)  # system.md skipped

    user = await store.get_topic("user")
    assert user.content.startswith("new user facts")  # newest of Danny's copies wins
    assert user.description == "Danny profile"
    assert "## From zhijian" in user.content and "zhijian facts" in user.content
    assert (await store.get_topic("recipes")).content == "dumplings"
    assert (await store.get_topic("daily_growth")).content == "pipeline notes"
    assert {"danny", "zhijian"} <= set(report.users_created)

    danny = await store.get_thread("chat:danny:legacy-web-abc")
    assert danny.owner_user_id == "danny" and danny.session_id is None
    assert danny.summary == "Tax chat." and danny.pending_since is None
    msgs = await store.list_messages(danny.key)
    assert [str(m.role) for m in msgs] == ["user", "assistant", "event"]
    assert msgs[0].created_at.isoformat().startswith("2026-03-12T02:16:44")
    zj = await store.get_thread("chat:zhijian:legacy-telegram-42")
    (photo,) = await store.list_messages(zj.key)
    assert photo.blocks[1]["text"].startswith("[image attached in the old handler")

    again = await import_legacy(source, store, settings)  # idempotent
    assert again.threads_created == 0 and again.threads_skipped == 2
    assert again.topics_created == [] and not again.persona
    assert (await store.get_topic("user")).content.count("## From zhijian") == 1
    await store.close()


async def test_imported_thread_gets_a_recap_on_its_first_new_turn(tmp_path):
    from handler.runtime.bus import RunEventBus
    from handler.runtime.dispatcher import Dispatcher
    from handler.runtime.observability.pricing import PriceTable
    from handler.runtime.run import RunConfig, RunExecutor
    from handler.runtime.types import Event, Lane, ThreadSpec, ThreadType
    from tests.runtime.test_e2e import FakeHarness, drain

    source = _legacy(tmp_path / "legacy")
    (tmp_path / "rt").mkdir()
    store = SQLiteStore(tmp_path / "rt" / "handler.db")
    await import_legacy(source, store, SettingsService(store))
    harness = FakeHarness()
    executor = RunExecutor(store, {"codex": harness}, RunEventBus(),
                           RunConfig(data_dir=tmp_path / "d"), PriceTable([]))
    key = "chat:danny:legacy-web-abc"
    await store.put(Event(type="chat.message", source="t", payload={"text": "and now?"},
                          idempotency_key="k", thread_key=key, user_id="danny"),
                    ThreadSpec(key, ThreadType.CHAT, Lane.INTERACTIVE, owner_user_id="danny"))
    await drain(Dispatcher(store, executor))
    (req,) = harness.requests
    assert "Earlier conversation in this thread" in req.prompt
    assert "You file jointly." in req.prompt and req.prompt.endswith("and now?")
    await store.close()
