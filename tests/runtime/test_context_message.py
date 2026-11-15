"""The per-run Handler context block is sent append-only, once per change and session."""

import pytest

from handler.runtime.bus import RunEventBus
from handler.runtime.harness.base import HarnessError, RunEvent, RunEventKind, RunRequest
from handler.runtime.memory import MemoryTools
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.run import RunConfig, RunExecutor
from handler.runtime.settings import SettingsService
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import (
    Approval, ApprovalStatus, Event, Lane, RunStatus, ThreadSpec, ThreadType, TokenUsage,
)

KEY = "chat:alice:c1"


class Memory(MemoryTools):
    index = "people/alice: Alice"

    async def index_text(self):
        return self.index

    async def relevant_text(self, thread_key, events=None):
        return ""


class SessionHarness:
    """Native-harness stand-in: keeps the session it is given, else starts one."""

    name = "openai_agents"

    def __init__(self):
        self.requests: list[RunRequest] = []
        self.sessions = 0
        self.fail_next: Exception | None = None

    async def run(self, req):
        self.requests.append(req)
        sid = req.session_id
        if not sid:
            self.sessions += 1
            sid = f"conv_{self.sessions}"
        yield RunEvent(RunEventKind.SESSION, {"session_id": sid})
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            yield RunEvent(RunEventKind.ERROR, {"message": str(error)})
            raise error
        yield RunEvent(RunEventKind.ASSISTANT_TEXT, {"text": "ok"})
        yield RunEvent(RunEventKind.USAGE, {"model": "m"}, usage=TokenUsage(input_tokens=1))
        yield RunEvent(RunEventKind.FINAL, {"text": "ok", "session_id": sid})

    async def interrupt(self, run_id):
        pass


@pytest.fixture
async def store(tmp_path):
    instance = SQLiteStore(tmp_path / "store.db")
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
def setup(store, tmp_path):
    settings = SettingsService(store)
    memory = Memory(store, settings)
    harness = SessionHarness()
    executor = RunExecutor(store, {harness.name: harness}, RunEventBus(),
                           RunConfig(data_dir=tmp_path / "data", heartbeat_seconds=0.01,
                                     max_attempts=1),
                           PriceTable([]), settings=settings, memory_tools=memory)
    return executor, harness, memory


async def turn(store, executor, text, *, key=KEY, type_=ThreadType.CHAT,
               event_type="chat.message", lane=Lane.INTERACTIVE):
    payload = {"text": text} if event_type == "chat.message" else {"prompt": text}
    await store.put(Event(event_type, "test", payload, f"{key}:{text}", thread_key=key,
                          user_id="alice"),
                    ThreadSpec(key, type_, lane, owner_user_id="alice",
                               harness="openai_agents"))
    lease, = await store.claim("worker", lane, 1, 60)
    await executor.execute(lease)


async def test_first_turn_injects_unchanged_is_not_repeated_change_is(store, setup):
    executor, harness, memory = setup
    await turn(store, executor, "one")
    await turn(store, executor, "two")
    memory.index = "people/alice: Alice\nrfq/7: Acme RFQ"
    await turn(store, executor, "three")
    await turn(store, executor, "four")
    first, second, third, fourth = harness.requests
    assert first.session_id is None and first.context.startswith("<handler_context>")
    assert "people/alice: Alice" in first.context
    assert "You are talking with alice" in first.context
    assert second.session_id == "conv_1" and second.context is None
    # The index changed: the full new block goes in, on the next turn only.
    assert third.session_id == "conv_1" and "rfq/7: Acme RFQ" in third.context
    assert fourth.context is None
    # The cached part never moved.
    assert first.instructions == second.instructions == third.instructions
    assert first.prompt_cache_key == third.prompt_cache_key
    assert "rfq/7" not in third.instructions and "You are talking with" not in third.instructions
    thread = await store.get_thread(KEY)
    assert thread.context_hash.startswith("conv_1:")


async def test_session_change_resets_and_reinjects(store, setup):
    executor, harness, _ = setup
    await turn(store, executor, "one")
    thread = await store.get_thread(KEY)
    assert thread.context_hash
    assert await store.revive_thread(KEY, reset_session=True)  # e.g. an operator reset
    await turn(store, executor, "two")
    first, second = harness.requests
    assert second.session_id is None and second.context == first.context
    thread = await store.get_thread(KEY)
    assert thread.session_id == "conv_2" and thread.context_hash.startswith("conv_2:")


async def test_set_session_clears_the_hash(store, setup):
    executor, _, _ = setup
    await turn(store, executor, "one")
    await store.put(Event("chat.message", "test", {"text": "x"}, "x", thread_key=KEY),
                    ThreadSpec(KEY, ThreadType.CHAT, Lane.INTERACTIVE))
    lease, = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
    assert (await store.get_thread(KEY)).context_hash
    assert await store.set_session(lease, "other")
    assert (await store.get_thread(KEY)).context_hash is None


async def test_failed_run_does_not_record_and_next_turn_reinjects(store, setup):
    executor, harness, _ = setup
    await turn(store, executor, "one")
    harness.fail_next = HarnessError("boom", retryable=False)
    await turn(store, executor, "two")
    # The failed run's events are still pending; a new message runs with them.
    await turn(store, executor, "three")
    first, failed, retried = harness.requests
    assert failed.context is None  # unchanged block, already in conv_1
    assert retried.context is None
    assert (await store.get_thread(KEY)).context_hash.startswith("conv_1:")


async def test_missing_session_retry_sends_the_full_block(store, setup):
    executor, harness, _ = setup
    await turn(store, executor, "one")

    original = harness.run

    async def lost_once(req):
        if req.session_id == "conv_1":
            harness.requests.append(req)
            raise HarnessError("No session found with id conv_1")
        async for ev in original(req):
            yield ev

    harness.run = lost_once
    await turn(store, executor, "two")
    _, lost, recovered = harness.requests
    assert lost.context is None
    assert recovered.session_id is None and recovered.context.startswith("<handler_context>")
    assert (await store.get_thread(KEY)).context_hash.startswith("conv_2:")


async def test_fresh_automation_session_always_injects(store, setup):
    executor, harness, _ = setup
    key = "automation:daily"
    for text in ("first", "second"):
        await turn(store, executor, text, key=key, type_=ThreadType.AUTOMATION,
                   event_type="automation.fired", lane=Lane.BACKGROUND)
    first, second = harness.requests
    assert first.fresh_session and second.fresh_session
    assert second.session_id is None and second.context == first.context
    assert "watermark" in second.instructions  # per-type guidance stays in instructions


async def test_receipt_only_turn_leaves_the_hash_and_next_turn_skips(store, setup):
    executor, harness, _ = setup
    await turn(store, executor, "one")
    marker = (await store.get_thread(KEY)).context_hash
    approval = Approval(run_id="earlier", thread_key=KEY, tool="send", action="send", args={},
                        requested_by=None, status=ApprovalStatus.EXECUTED,
                        result={"handler_receipt_only": True, "content": "sent"})
    await store.create_approval(approval)
    await store.put(Event("approval.resolved", "gateway", {
        "approval_id": approval.id, "tool": "send", "status": "executed",
        "resume_model": False, "result_preview": "sent"}, "receipt", thread_key=KEY),
        ThreadSpec(KEY, ThreadType.CHAT, Lane.INTERACTIVE))
    lease, = await store.claim("worker", Lane.INTERACTIVE, 1, 60)
    await executor.execute(lease)
    assert len(harness.requests) == 1  # no model run
    assert (await store.get_thread(KEY)).context_hash == marker
    await turn(store, executor, "what happened?")
    _, after = harness.requests
    assert after.context is None and "Approved send completed. sent" in after.prompt
    runs = await store.list_runs(KEY)
    assert all(r.status == RunStatus.COMPLETED for r in runs)


async def test_compacted_turn_makes_the_next_turn_resend(store, setup):
    executor, harness, _ = setup
    await turn(store, executor, "one")
    original = harness.run

    async def compacting(req):
        async for ev in original(req):
            if ev.kind is RunEventKind.FINAL:
                ev = RunEvent(RunEventKind.FINAL, {**ev.data, "compacted": True})
            yield ev

    harness.run = compacting
    await turn(store, executor, "two")
    assert (await store.get_thread(KEY)).context_hash is None
    harness.run = original
    await turn(store, executor, "three")
    await turn(store, executor, "four")
    _, two, three, four = harness.requests
    assert two.context is None  # unchanged before the compaction
    assert three.session_id == "conv_1" and three.context.startswith("<handler_context>")
    assert four.context is None
