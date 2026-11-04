"""Memory kinds, links, untrusted marks, search, index and run-start memory."""

import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from handler.runtime.auth import Authenticator
from handler.runtime.context import RunContext
from handler.runtime.instructions import build_instructions
from handler.runtime.integrations.crontal.memory_kinds import CRONTAL_MEMORY_DEFAULTS
from handler.runtime.integrations.mailbox import mail_thread_links
from handler.runtime.integrations.registry import IntegrationRegistry
from handler.runtime.memory import (
    UNTRUSTED_MARK,
    MemoryTools,
    normalize_link,
    thread_links,
)
from handler.runtime.settings import SettingsService
from handler.runtime.store.sqlite import SQLiteCore
from handler.runtime.store.sqlite_memory import MemoryMixin, NotificationMixin, SettingsMixin
from handler.runtime.types import Event, MemoryTopic, ThreadType


class _Store(MemoryMixin, SettingsMixin, NotificationMixin, SQLiteCore):
    pass


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
async def store(tmp_path):
    instance = _Store(tmp_path / "memory.db", clock=Clock())
    yield instance
    await instance.close()


@pytest.fixture
async def crontal_settings(store):
    return SettingsService(store, section_defaults={"memory": CRONTAL_MEMORY_DEFAULTS})


CHAT = RunContext("run", "chat:alice:1", "alice", "alice")
MAIL = RunContext("run", "mail:mbx1:conv9", None, None)
CRONTAL = RunContext("run", "crontal:rfq:12", None, None)


async def invoke(provider, tool_name, ctx, **args):
    spec = next(spec for spec in provider.tools() if spec.name == tool_name)
    return await spec.handler(ctx, args)


async def test_metadata_round_trip_and_search(store):
    await store.put_topic(MemoryTopic("inquiries/a", "A", "x", kind="inquiry",
                                      links=["crontal:rfq:1", "domain:acme.com"],
                                      source_untrusted=True, updated_thread_key="mail:m:c"),
                          None)
    await store.put_topic(MemoryTopic("companies/acme", "Acme", "Likes 316L",
                                      kind="company", links=["domain:acme.com"]), None)
    await store.put_topic(MemoryTopic("policies", "General", "no weekends"), None)
    topic = await store.get_topic("inquiries/a")
    assert topic.links == ["crontal:rfq:1", "domain:acme.com"]
    assert topic.source_untrusted is True and topic.kind == "inquiry"
    assert topic.updated_thread_key == "mail:m:c"
    plain = await store.get_topic("policies")
    assert plain.links == [] and plain.source_untrusted is False and plain.kind is None

    names = lambda topics: [t.name for t in topics]  # noqa: E731
    assert names(await store.search_topics(prefix="companies/")) == ["companies/acme"]
    assert names(await store.search_topics(kind="inquiry")) == ["inquiries/a"]
    assert names(await store.search_topics(kindless=True)) == ["policies"]
    assert names(await store.search_topics(links=["domain:acme.com"])) == [
        "companies/acme", "inquiries/a"]
    # A link matches only whole: crontal:rfq:1 is not crontal:rfq:12.
    assert names(await store.search_topics(links=["crontal:rfq:12"])) == []
    assert names(await store.search_topics(links=["crontal:rfq:12", "crontal:rfq:1"])) == [
        "inquiries/a"]
    assert names(await store.search_topics(text="316l")) == ["companies/acme"]
    assert names(await store.search_topics(order="recent")) == [
        "policies", "companies/acme", "inquiries/a"]
    assert names(await store.search_topics(limit=1, offset=1)) == ["inquiries/a"]
    assert await store.count_topics() == 3
    # A version bump keeps metadata.
    updated = await store.put_topic(MemoryTopic("inquiries/a", "A2", "y", kind="inquiry",
                                                links=["crontal:rfq:1"]), 1)
    assert updated.version == 2
    assert (await store.get_topic("inquiries/a")).links == ["crontal:rfq:1"]


async def test_old_sqlite_table_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE memory_topics (name TEXT PRIMARY KEY, description TEXT NOT NULL, "
               "content TEXT NOT NULL, version INTEGER NOT NULL, updated_by TEXT, "
               "updated_at TEXT NOT NULL)")
    db.execute("INSERT INTO memory_topics VALUES ('old', 'Old', 'kept', 3, 'bob', "
               "'2026-10-01T00:00:00.000000+00:00')")
    db.commit()
    db.close()
    store = _Store(path)
    try:
        topic = await store.get_topic("old")
        assert (topic.content, topic.version, topic.links, topic.kind,
                topic.source_untrusted) == ("kept", 3, [], None, False)
        assert await store.put_topic(MemoryTopic("old", "Old", "new", links=["thread:x"]), 3)
        assert (await store.get_topic("old")).links == ["thread:x"]
    finally:
        await store.close()
    again = _Store(path)  # reopening doesn't add the columns twice
    try:
        assert (await again.get_topic("old")).version == 4
    finally:
        await again.close()


def test_links_normalized_and_validated():
    assert normalize_link(" domain:@ACME.com. ") == "domain:acme.com"
    assert normalize_link("Crontal:rfq:1") == "crontal:rfq:1"
    for bad in ("", "nolink", "a b:c", "x:", "x:has space", 3):
        with pytest.raises(ValueError):
            normalize_link(bad)


async def test_save_on_untrusted_thread_marks_and_merges(store, crontal_settings):
    memory = MemoryTools(store, crontal_settings)
    result = await invoke(memory, "memory_save", MAIL, name="inquiries/acme-304-pipe",
                          description="Acme 304 pipe", content="Status: new",
                          links=["crontal:rfq:12", "domain:Acme.com"])
    assert not result.is_error and result.content["kind"] == "inquiry"  # from the prefix
    topic = await store.get_topic("inquiries/acme-304-pipe")
    assert topic.source_untrusted and topic.updated_thread_key == "mail:mbx1:conv9"
    assert topic.links == ["crontal:rfq:12", "domain:acme.com"]
    await invoke(memory, "memory_save", CHAT, name="inquiries/acme-304-pipe",
                 description="Acme 304 pipe", content="Quoted", kind="inquiry",
                 links=["crontal:bid:45", "crontal:rfq:12"])
    topic = await store.get_topic("inquiries/acme-304-pipe")
    assert topic.content == "Status: new\n\nQuoted"
    assert topic.links == ["crontal:rfq:12", "domain:acme.com", "crontal:bid:45"]
    assert topic.source_untrusted  # sticky: earlier content came from outside input
    assert topic.updated_thread_key == CHAT.thread_key and topic.updated_by == "alice"
    # A chat save of a fresh topic is trusted.
    await invoke(memory, "memory_save", CHAT, name="policies", description="P", content="x")
    assert not (await store.get_topic("policies")).source_untrusted
    read = await invoke(memory, "memory_read", CHAT, name="inquiries/acme-304-pipe")
    assert "kind: inquiry" in read.content and "crontal:bid:45" in read.content
    assert "outside input" in read.content


async def test_kind_errors(store, crontal_settings):
    memory = MemoryTools(store, crontal_settings)
    with pytest.raises(ValueError, match="Unknown kind"):
        await invoke(memory, "memory_save", CHAT, name="x", description="", content="",
                     kind="journal")
    with pytest.raises(ValueError, match="companies/"):
        await invoke(memory, "memory_save", CHAT, name="acme", description="", content="",
                     kind="company")
    with pytest.raises(ValueError):
        await invoke(memory, "memory_save", CHAT, name="x", description="", content="",
                     links=["not a link"])
    # No kinds configured: every kind is unknown, names stay general.
    plain = MemoryTools(store)
    with pytest.raises(ValueError, match="none configured"):
        await invoke(plain, "memory_save", CHAT, name="inquiries/x", description="",
                     content="", kind="inquiry")
    await invoke(plain, "memory_save", CHAT, name="inquiries/x", description="", content="")
    assert (await store.get_topic("inquiries/x")).kind is None


async def test_rewrite_keeps_mark_and_replaces_links(store, crontal_settings):
    memory = MemoryTools(store, crontal_settings)
    await invoke(memory, "memory_save", MAIL, name="companies/acme", description="Acme",
                 content="a", links=["domain:acme.com"])
    result = await invoke(memory, "memory_rewrite", CHAT, name="companies/acme",
                          description="Acme Ltd", content="summary")
    assert not result.is_error
    topic = await store.get_topic("companies/acme")
    assert topic.links == ["domain:acme.com"] and topic.kind == "company"
    assert topic.source_untrusted  # only a human clears it
    await invoke(memory, "memory_rewrite", CHAT, name="companies/acme", description="Acme",
                 content="s", links=["domain:acme.co.uk"], new_name="companies/acme-ltd")
    moved = await store.get_topic("companies/acme-ltd")
    assert moved.links == ["domain:acme.co.uk"] and moved.kind == "company"
    assert await store.get_topic("companies/acme") is None


async def test_size_warning(store):
    settings = SettingsService(store)
    await settings.put("memory", {"max_topic_chars": 500}, None, None)
    memory = MemoryTools(store, settings)
    small = await invoke(memory, "memory_save", CHAT, name="t", description="", content="x")
    assert "size_warning" not in small.content
    big = await invoke(memory, "memory_save", CHAT, name="t", description="", content="y" * 600)
    assert not big.is_error and "memory_rewrite" in big.content["size_warning"]
    assert (await store.get_topic("t")).content.endswith("y" * 600)  # still saved
    assert "approval" not in big.content["size_warning"]
    on_mail = await invoke(memory, "memory_save", MAIL, name="t", description="", content="z")
    assert "approval" in on_mail.content["size_warning"]


async def test_search_tool_paging(store):
    memory = MemoryTools(store)
    for i in range(5):
        await invoke(memory, "memory_save", CHAT, name=f"companies/c{i}", description=f"C{i}",
                     content="steel" if i % 2 else "other", links=[f"domain:c{i}.com"])
    page = (await invoke(memory, "memory_search", CHAT, prefix="companies/", limit=2)).content
    assert [t["name"] for t in page["topics"]] == ["companies/c0", "companies/c1"]
    assert page["next_offset"] == 2
    last = (await invoke(memory, "memory_search", CHAT, prefix="companies/", limit=2,
                         offset=4)).content
    assert [t["name"] for t in last["topics"]] == ["companies/c4"] and last["next_offset"] is None
    found = (await invoke(memory, "memory_search", CHAT, text="STEEL")).content["topics"]
    assert [t["name"] for t in found] == ["companies/c1", "companies/c3"]
    linked = (await invoke(memory, "memory_search", CHAT, link="domain:C2.com")).content
    assert [t["name"] for t in linked["topics"]] == ["companies/c2"]
    recent = (await invoke(memory, "memory_search", CHAT, order="recent", limit=1)).content
    assert recent["topics"][0]["name"] == "companies/c4"


async def test_index_lists_general_and_recent(store):
    settings = SettingsService(store)
    await settings.put("memory", {"index_general_limit": 2, "index_recent_limit": 2}, None, None)
    memory = MemoryTools(store, settings)
    for name in ("policies/a", "policies/b", "policies/c"):
        await store.put_topic(MemoryTopic(name, name.upper(), ""), None)
    for i in range(4):
        await store.put_topic(MemoryTopic(f"inquiries/i{i}", f"I{i}", "", kind="inquiry",
                                          source_untrusted=i == 3), None)
    index = await memory.index_text()
    lines = index.splitlines()
    assert lines[:2] == ["policies/a: POLICIES/A", "policies/b: POLICIES/B"]
    assert lines[2] == "Recently updated:"
    assert lines[3] == f"inquiries/i3: I3 {UNTRUSTED_MARK}"
    assert lines[4] == "inquiries/i2: I2"
    assert "4 of 7 topics listed" in lines[5]
    assert await memory.index_text(10) == index[:10]


async def test_thread_links():
    mail_event = Event(type="mail.received", source="mailbox", idempotency_key="k",
                       payload={"from": {"address": "Buyer@Acme.com"}})
    assert thread_links("mail:mbx1:conv9", [mail_event]) == [
        "thread:mail:mbx1:conv9", "mail:mbx1:conv9", "domain:acme.com"]
    free = Event(type="mail.received", source="mailbox", idempotency_key="k",
                 payload={"from": {"address": "someone@gmail.com"}})
    assert thread_links("mail:mbx1:c", [free]) == ["thread:mail:mbx1:c", "mail:mbx1:c"]
    assert thread_links("crontal:rfq:12") == ["thread:crontal:rfq:12", "crontal:rfq:12"]
    assert thread_links("crontal:notification:5") == ["thread:crontal:notification:5"]
    intake = Event(type="email.received", source="email", idempotency_key="k",
                   payload={"accepted_sender": "a@supplier.cn",
                            "original": {"address": "b@buyer.de"}})
    assert thread_links("email:a@supplier.cn:<m@x>", [intake]) == [
        "thread:email:a@supplier.cn:<m@x>", "domain:supplier.cn", "domain:buyer.de"]
    assert thread_links("chat:alice:1") == ["thread:chat:alice:1"]
    assert mail_thread_links("mail:x", []) == []


async def test_relevant_memory_and_prompt(store, crontal_settings):
    memory = MemoryTools(store, crontal_settings)
    await store.put_topic(MemoryTopic(
        "inquiries/acme-304", "Acme 304 pipe", "Status: quoting", kind="inquiry",
        links=["crontal:rfq:12", "domain:acme.com", "topic:companies/acme"]), None)
    await store.put_topic(MemoryTopic("companies/acme", "Acme", "Pays net 30", kind="company",
                                      links=["domain:acme.com"]), None)
    await store.put_topic(MemoryTopic("conversations/acme-1", "Thread", "c", kind="conversation",
                                      links=["domain:acme.com"], source_untrusted=True), None)
    await store.put_topic(MemoryTopic("unrelated", "U", "secret stuff"), None)
    relevant = await memory.relevant_text("crontal:rfq:12")
    assert "### inquiries/acme-304 (kind: inquiry)" in relevant and "Status: quoting" in relevant
    assert "Pays net 30" not in relevant  # one hop: names only
    assert "Related topics (memory_read if needed): companies/acme, conversations/acme-1" \
        in relevant
    assert "unrelated" not in relevant
    assert await memory.relevant_text("chat:nobody") == ""

    prompt = await build_instructions(crontal_settings, memory, speaker=None,
                                      thread_type=ThreadType.CRONTAL,
                                      thread_key="crontal:rfq:12")
    for fragment in ("Kinds of topics to keep", "inquiry (`inquiries/…`)", "**Status:**",
                     "Never save raw email text", "## Index", "## Linked to this thread",
                     "Status: quoting", "unrelated: U"):
        assert fragment in prompt, fragment
    assert "secret stuff" not in prompt
    # Without kinds (core default) the prompt still has the rules but no kinds.
    bare = await build_instructions(SettingsService(store), MemoryTools(store), speaker=None,
                                    thread_type=ThreadType.CHAT)
    assert "Never save raw email text" in bare and "Kinds of topics" not in bare
    assert "## Linked to this thread" not in bare


async def test_relevant_memory_is_bounded(store):
    settings = SettingsService(store)
    await settings.put("memory", {"relevant_max_chars": 1000}, None, None)
    memory = MemoryTools(store, settings)
    for i in range(3):
        await store.put_topic(MemoryTopic(f"t{i}", f"T{i}", "z" * 5000,
                                          links=["crontal:rfq:1"]), None)
    text = await memory.relevant_text("crontal:rfq:1")
    assert len(text) < 1500 and "memory_read" in text
    await settings.put("memory", {"relevant_max_chars": 0}, None, 1)
    assert await memory.relevant_text("crontal:rfq:1") == ""


async def test_memory_settings_validation(store):
    service = SettingsService(store)
    value, version = await service.get("memory")
    assert version is None and value["kinds"] == [] and value["max_topic_chars"] == 8000
    for bad in ({"kinds": [{"name": "a", "prefix": "a/"}, {"name": "a", "prefix": "b/"}]},
                {"kinds": [{"name": "a", "prefix": "x/"}, {"name": "b", "prefix": "x/"}]},
                {"kinds": [{"name": "a", "prefix": "no-slash"}]},
                {"kinds": [{"name": "Bad", "prefix": "b/"}]},
                {"max_topic_chars": 10}, {"unknown": 1}):
        with pytest.raises(ValidationError):
            await service.put("memory", bad, None, None)
    crontal = SettingsService(store, section_defaults={"memory": CRONTAL_MEMORY_DEFAULTS})
    value, version = await crontal.get("memory")
    assert version is None
    assert [k["name"] for k in value["kinds"]] == ["inquiry", "company", "conversation"]
    assert [k["prefix"] for k in value["kinds"]] == ["inquiries/", "companies/",
                                                     "conversations/"]
    # Once saved, the saved value wins over the defaults.
    await crontal.put("memory", {"kinds": []}, None, None)
    assert (await crontal.get("memory"))[0]["kinds"] == []


def test_crontal_integration_provides_memory_defaults():
    from handler.runtime.integrations.crontal import CrontalIntegration
    from handler.runtime.integrations.mailbox import MailboxIntegration

    registry = IntegrationRegistry()
    registry.register(CrontalIntegration)
    registry.register(MailboxIntegration)
    loaded = registry.load(["crontal", "mailbox"], env={"HANDLER_ADMIN_KEY": "k" * 32})
    owner, value = loaded.settings_defaults["memory"]
    assert owner == "crontal" and value is CRONTAL_MEMORY_DEFAULTS
    assert registry.load(["mailbox"], env={"HANDLER_ADMIN_KEY": "k" * 32}).settings_defaults == {}


async def test_settings_api_serves_memory_section(store, crontal_settings):
    from handler.runtime.web.settings_api import settings_router

    app = FastAPI()
    app.include_router(settings_router(crontal_settings, Authenticator(store, "test-admin-key")))
    headers = {"Authorization": "Bearer test-admin-key"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=headers) as client:
        body = (await client.get("/api/settings/memory")).json()
        assert body["version"] is None and len(body["value"]["kinds"]) == 3
        assert body["schema"]["properties"]["kinds"]["type"] == "array"
        value = body["value"]
        value["kinds"] = value["kinds"][:1]
        saved = await client.put("/api/settings/memory", json=value)
        assert saved.status_code == 200 and saved.json()["version"] == 1
        bad = await client.put("/api/settings/memory", json={"kinds": [{"name": "x"}]},
                               headers={"If-Match": "1"})
        assert bad.status_code == 422


async def test_admin_memory_api_preserves_metadata(store, tmp_path):
    from handler.runtime.web.admin_api import admin_router

    await store.put_topic(MemoryTopic("inquiries/a", "A", "x", kind="inquiry",
                                      links=["crontal:rfq:1"], source_untrusted=True), None)
    app = FastAPI()
    app.include_router(admin_router(store, Authenticator(store, "test-admin-key"),
                                    scheduler=None, gateway=None, uploads=None))
    headers = {"Authorization": "Bearer test-admin-key"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=headers) as client:
        listed = (await client.get("/api/memory")).json()
        assert listed[0]["kind"] == "inquiry" and listed[0]["source_untrusted"] is True
        put = await client.put("/api/memory/inquiries/a", json={"content": "y",
                                                                "description": "A"})
        assert put.status_code == 200 and put.json()["links"] == ["crontal:rfq:1"]
        assert put.json()["source_untrusted"] is True and put.json()["kind"] == "inquiry"
        cleared = await client.put("/api/memory/inquiries/a",
                                   json={"content": "checked", "source_untrusted": False,
                                         "links": ["crontal:rfq:1", "domain:a.com"]})
        assert cleared.json()["source_untrusted"] is False
        assert cleared.json()["links"] == ["crontal:rfq:1", "domain:a.com"]
        assert (await client.put("/api/memory/inquiries/a",
                                 json={"content": "", "links": ["bad link"]})).status_code == 422
        found = (await client.get("/api/memory", params={"link": "domain:A.com"})).json()
        assert [t["name"] for t in found] == ["inquiries/a"]
        assert (await client.get("/api/memory", params={"kind": "company"})).json() == []
