"""Instructions are identical per thread type; the cache key hashes them."""

import hashlib
from datetime import datetime, timezone

import pytest

from handler.runtime.bus import RunEventBus
from handler.runtime.memory import MemoryTools
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.observability.prompt_tokens import (
    PromptComponent, prompt_cache_key, prompt_token_breakdown, stable_prefix_text,
)
from handler.runtime.run import RunConfig, RunExecutor
from handler.runtime.settings import SettingsService
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import Event, Lane, Thread, ThreadType

TOOLS = frozenset({"memory_save", "memory_read", "notify", "crontal_api_get"})


@pytest.fixture
async def store(tmp_path):
    instance = SQLiteStore(tmp_path / "store.db")
    try:
        yield instance
    finally:
        await instance.close()


class Memory(MemoryTools):
    """Memory whose index and linked topics differ per test run."""

    index = "people/alice: Alice"
    relevant = ""

    async def index_text(self):
        return self.index

    async def relevant_text(self, thread_key, events=None):
        return self.relevant


def _thread(key: str, type_: ThreadType, owner: str | None = "alice",
            harness: str = "openai_agents") -> Thread:
    now = datetime.now(timezone.utc)
    return Thread(key=key, type=type_, lane=Lane.INTERACTIVE, owner_user_id=owner,
                  harness=harness, model=None, priority=0, session_id=None,
                  pending_since=None, available_at=None, attempts=0, last_error=None,
                  dead=False, lease_owner=None, lease_until=None, lease_version=0,
                  summary=None, created_at=now)


def _executor(store, tmp_path, settings, memory, tools=TOOLS) -> RunExecutor:
    return RunExecutor(store, {}, RunEventBus(),
                       RunConfig(data_dir=tmp_path / "data", available_tools=tools),
                       PriceTable([]), settings=settings, memory_tools=memory)


async def test_dynamic_context_does_not_change_the_key(store, tmp_path):
    settings = SettingsService(store)
    memory = Memory(store, settings)
    executor = _executor(store, tmp_path, settings, memory)
    chat = await executor._plan(_thread("chat:alice:1", ThreadType.CHAT, "alice"))
    memory.index, memory.relevant = "rfq/42: changed", "linked topic body"
    other = await executor._plan(_thread("chat:bob:2", ThreadType.CHAT, "bob"),
                                 [Event("chat.message", "t", {"text": "hi"}, "e1")])
    automation = await executor._plan(_thread("automation:daily", ThreadType.AUTOMATION, None))
    # Per-run content left the instructions for the context message.
    assert chat.instructions == other.instructions
    assert "You are talking with" not in other.instructions
    assert "rfq/42" not in other.instructions and "linked topic body" not in other.instructions
    assert "You are talking with bob" in other.context and "rfq/42: changed" in other.context
    assert "linked topic body" in other.context
    assert "You are talking with alice" in chat.context and "people/alice" in chat.context
    assert chat.prompt_cache_key == other.prompt_cache_key
    # Another type has its own guidance, so its own instructions and key.
    assert automation.instructions != chat.instructions
    assert automation.prompt_cache_key != chat.prompt_cache_key
    assert chat.prompt_cache_key.startswith("handler:") and len(chat.prompt_cache_key) == 40
    assert not hasattr(chat, "prompt_cache_retention")


async def test_two_chats_and_two_mail_threads_share_instructions_and_key(store, tmp_path):
    settings = SettingsService(store)
    memory = Memory(store, settings)
    executor = _executor(store, tmp_path, settings, memory)
    alice = await executor._plan(_thread("chat:alice:1", ThreadType.CHAT, "alice"))
    bob = await executor._plan(_thread("chat:bob:9", ThreadType.CHAT, "bob"))
    assert alice.instructions == bob.instructions  # byte-identical
    assert alice.prompt_cache_key == bob.prompt_cache_key
    assert alice.context != bob.context
    mail_type = ThreadType("mail")
    memory.relevant = "### companies/acme\nPays net 30"
    first = await executor._plan(_thread("mail:mbx1:conv1", mail_type, None))
    memory.relevant = "### companies/bolt\nPays net 60"
    second = await executor._plan(_thread("mail:mbx1:conv2", mail_type, None))
    assert first.instructions == second.instructions
    assert first.prompt_cache_key == second.prompt_cache_key
    assert "Pays net 30" in first.context and "Pays net 60" in second.context
    assert "Pays net" not in first.instructions


async def test_memory_index_change_keeps_instructions_and_key(store, tmp_path):
    settings = SettingsService(store)
    memory = Memory(store, settings)
    executor = _executor(store, tmp_path, settings, memory)
    thread = _thread("chat:alice:1", ThreadType.CHAT)
    before = await executor._plan(thread)
    memory.index = "people/alice: Alice\nrfq/7: new topic"
    after = await executor._plan(thread)
    assert before.instructions == after.instructions
    assert before.prompt_cache_key == after.prompt_cache_key
    assert before.context != after.context and "rfq/7: new topic" in after.context


async def test_context_message_rules_are_stable_instructions(store, tmp_path):
    settings = SettingsService(store)
    memory = Memory(store, settings)
    plan = await _executor(store, tmp_path, settings, memory)._plan(
        _thread("chat:alice:1", ThreadType.CHAT))
    assert "<handler_context>" in plan.instructions  # the rule that explains the block
    assert "not from the person" in plan.instructions
    assert plan.context.startswith("<handler_context>")
    assert plan.context.endswith("</handler_context>")


async def test_codex_keeps_combined_instructions(store, tmp_path):
    settings = SettingsService(store)
    memory = Memory(store, settings)
    plan = await _executor(store, tmp_path, settings, memory)._plan(
        _thread("chat:alice:1", ThreadType.CHAT, harness="codex"))
    assert plan.context == ""
    assert "You are talking with alice" in plan.instructions
    assert "people/alice: Alice" in plan.instructions
    assert "<handler_context>" not in plan.instructions


async def test_tools_stable_text_and_model_change_the_key(store, tmp_path):
    settings = SettingsService(store)
    memory = Memory(store, settings)
    thread = _thread("chat:alice:1", ThreadType.CHAT)
    base = (await _executor(store, tmp_path, settings, memory)._plan(thread)).prompt_cache_key
    fewer = await _executor(store, tmp_path, settings, memory,
                            tools=TOOLS - {"notify"})._plan(thread)
    assert fewer.prompt_cache_key != base
    other_model = _thread("chat:alice:1", ThreadType.CHAT)
    other_model.model = "gpt-other"
    other = await _executor(store, tmp_path, settings, memory)._plan(other_model)
    assert other.model == "gpt-other" and other.prompt_cache_key != base
    await settings.put("persona", {"system": "Be terse.", "identity": "", "persona": ""},
                       None, None)
    changed = await _executor(store, tmp_path, settings, memory)._plan(thread)
    assert changed.prompt_cache_key != base


async def test_bounded_tool_is_part_of_the_inventory(store, tmp_path):
    settings = SettingsService(store)
    memory = Memory(store, settings)
    executor = _executor(store, tmp_path, settings, memory)
    with_helper = await executor._plan(_thread("chat:a:1", ThreadType.CHAT))
    await settings.put("efficiency", {"bounded_model_tasks": False}, None, None)
    without = await executor._plan(_thread("chat:a:1", ThreadType.CHAT))
    assert with_helper.bounded_task_models and not without.bounded_task_models
    assert with_helper.prompt_cache_key != without.prompt_cache_key


async def test_settings_switch_key_off(store, tmp_path):
    settings = SettingsService(store)
    await settings.put("efficiency", {"stable_prompt_cache_key": False}, None, None)
    plan = await _executor(store, tmp_path, settings, Memory(store, settings))._plan(
        _thread("chat:a:1", ThreadType.CHAT))
    assert plan.prompt_cache_key is None


async def test_retention_setting_is_gone_and_old_stored_settings_load(store, tmp_path):
    from handler.runtime.settings import EfficiencySettings

    assert "prompt_cache_retention" not in EfficiencySettings.model_fields
    # A value saved before the setting was removed (written straight to the store,
    # as an older release did) still loads; the retired key is dropped.
    await store.put_settings("efficiency", {"stable_prompt_cache_key": True,
                                            "prompt_cache_retention": "in_memory"},
                             updated_by=None, expected_version=None)
    settings = SettingsService(store)
    value, version = await settings.get("efficiency")
    assert "prompt_cache_retention" not in value and value["stable_prompt_cache_key"]
    assert version is not None
    # Saving it back (e.g. an old form) is accepted too.
    await settings.put("efficiency", {"prompt_cache_retention": "24h"}, None, version)
    plan = await _executor(store, tmp_path, settings, Memory(store, settings))._plan(
        _thread("chat:a:1", ThreadType.CHAT))
    assert plan.prompt_cache_key


async def test_without_memory_tools_the_configured_instructions_are_hashed(store, tmp_path):
    settings = SettingsService(store)
    config = RunConfig(data_dir=tmp_path / "data", instructions="Static rules.")
    executor = RunExecutor(store, {}, RunEventBus(), config, PriceTable([]), settings=settings)
    plan = await executor._plan(_thread("chat:a:1", ThreadType.CHAT))
    assert plan.prompt_cache_key == prompt_cache_key(
        "Static rules.", tools=None, model=plan.model, harness="openai_agents")
    bare = RunExecutor(store, {}, RunEventBus(), config, PriceTable([]))
    assert (await bare._plan(_thread("chat:a:1", ThreadType.CHAT))).prompt_cache_key


def test_key_and_preview_fingerprint_share_one_instructions_text():
    rules = PromptComponent("rules", "Keep all source facts.")
    components = [rules, PromptComponent("empty", ""), PromptComponent("rules2", "More."),
                  PromptComponent("speaker", "Alice", True)]
    stable = stable_prefix_text(components)
    assert stable == "Keep all source facts.\n\nMore."
    assert prompt_token_breakdown(components)["stable_prefix"]["sha256"] == \
        hashlib.sha256(stable.encode()).hexdigest()
    key = prompt_cache_key(stable, tools={"b", "a"}, model="m")
    assert key == prompt_cache_key(stable, tools=["a", "b"], model="m")
    assert key != prompt_cache_key(stable, tools={"a"}, model="m")
    assert key != prompt_cache_key(stable, tools={"a", "b"}, model="n")
    assert "Keep" not in key
