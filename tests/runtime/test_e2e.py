"""End-to-end: event queue → dispatcher → run lifecycle, with a fake harness."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from handler.runtime.bus import RunEventBus
from handler.runtime.dispatcher import Dispatcher
from handler.runtime.harness.base import HarnessError, RunEvent, RunEventKind, RunRequest
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.run import RunConfig, RunExecutor, render_turn
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import (
    Event,
    Lane,
    Role,
    RunStatus,
    StepKind,
    ThreadSpec,
    ThreadType,
    TokenUsage,
)


class FakeHarness:
    name = "fake"

    def __init__(self) -> None:
        self.requests: list[RunRequest] = []
        self.fail_with: HarnessError | None = None
        self.gate: asyncio.Event | None = None  # when set, the run waits on it
        self.started = asyncio.Event()

    async def run(self, req: RunRequest):
        self.requests.append(req)
        self.started.set()
        yield RunEvent(RunEventKind.SESSION, {"session_id": req.session_id or "sess-1"})
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_with is not None:
            yield RunEvent(RunEventKind.ERROR, {"message": str(self.fail_with),
                                               "retryable": self.fail_with.retryable})
            raise self.fail_with
        yield RunEvent(RunEventKind.TOOL_STARTED, {"id": "t1", "tool": "shell", "kind": "command_execution",
                                                   "input": "ls"}, raw={"n": 1})
        yield RunEvent(RunEventKind.TOOL_FINISHED, {"id": "t1", "tool": "shell", "kind": "command_execution",
                                                    "input": "ls", "output": "a.txt", "status": "completed",
                                                    "exit_code": 0}, raw={"n": 2})
        yield RunEvent(RunEventKind.ASSISTANT_TEXT, {"text": f"echo: {req.prompt}"}, raw={"n": 3})
        yield RunEvent(RunEventKind.USAGE, {"model": "fake-model"},
                       usage=TokenUsage(input_tokens=100, cached_input_tokens=60, output_tokens=10))
        yield RunEvent(RunEventKind.FINAL, {"text": f"echo: {req.prompt}", "session_id": "sess-1"})

    async def interrupt(self, run_id: str) -> None:
        pass


@pytest.fixture
async def env(tmp_path: Path):
    store = SQLiteStore(tmp_path / "h.db")
    bus = RunEventBus()
    harness = FakeHarness()
    prices_file = tmp_path / "prices.yaml"
    prices_file.write_text(
        "prices:\n  - model: fake-model\n    effective_from: 2020-01-01\n"
        "    input_per_mtok: 1.0\n    cached_input_per_mtok: 0.1\n"
        "    cache_write_per_mtok: 0\n    output_per_mtok: 10.0\n"
    )
    config = RunConfig(data_dir=tmp_path / "data", heartbeat_seconds=0.05, lease_seconds=5,
                       retry_backoff_seconds=(0.0,), max_attempts=2)
    executor = RunExecutor(store, {"fake": harness}, bus, config, PriceTable.from_yaml(prices_file))
    dispatcher = Dispatcher(store, executor, lease_seconds=5)
    yield store, bus, harness, dispatcher
    await store.close()


def chat(text: str, key: str = "chat:u1:c1", idem: str | None = None) -> tuple[Event, ThreadSpec]:
    ev = Event(type="chat.message", source="test", payload={"text": text},
               idempotency_key=idem or f"k-{text}", thread_key=key, user_id="u1")
    return ev, ThreadSpec(key=key, type=ThreadType.CHAT, lane=Lane.INTERACTIVE,
                          owner_user_id="u1", harness="fake")


async def drain(dispatcher: Dispatcher) -> None:
    await dispatcher.claim_once()
    while dispatcher.active_count:
        await asyncio.sleep(0.01)


async def test_chat_run_commits_transcript_run_and_steps(env):
    store, bus, harness, dispatcher = env
    async with bus.subscribe("chat:u1:c1") as q:
        await store.put(*chat("hello"))
        await drain(dispatcher)
        types = []
        while not q.empty():
            types.append((await q.get()).type)
    assert types[0] == "run_started" and types[-1] == "run_finished"

    thread = await store.get_thread("chat:u1:c1")
    assert thread.pending_since is None and thread.session_id == "sess-1"

    msgs = await store.list_messages("chat:u1:c1")
    assert [m.role for m in msgs] == [Role.USER, Role.TOOL, Role.ASSISTANT]
    assert msgs[2].blocks[0]["text"] == "echo: hello"

    (run,) = await store.list_runs("chat:u1:c1")
    assert run.status is RunStatus.COMPLETED
    assert run.usage.input_tokens == 100 and run.usage.output_tokens == 10
    # 40 uncached * $1 + 60 cached * $0.1 + 10 out * $10, per million tokens
    assert run.cost_usd == pytest.approx((40 * 1.0 + 60 * 0.1 + 10 * 10.0) / 1e6)

    steps = await store.list_steps(run.id)
    kinds = [s.kind for s in steps]
    assert StepKind.TOOL_CALL in kinds and StepKind.LLM_CALL in kinds
    tool = next(s for s in steps if s.kind is StepKind.TOOL_CALL)
    assert tool.status == "ok" and tool.ended_at is not None


async def test_second_turn_resumes_session_and_sends_only_new_events(env):
    store, _, harness, dispatcher = env
    await store.put(*chat("one"))
    await drain(dispatcher)
    await store.put(*chat("two"))
    await drain(dispatcher)
    assert harness.requests[0].session_id is None
    assert harness.requests[1].session_id == "sess-1"
    assert harness.requests[1].prompt == "two"


async def test_event_arriving_mid_run_is_picked_up_next(env):
    store, _, harness, dispatcher = env
    harness.gate = asyncio.Event()
    await store.put(*chat("first"))
    await dispatcher.claim_once()
    await harness.started.wait()
    await store.put(*chat("second"))
    harness.gate.set()
    while dispatcher.active_count:
        await asyncio.sleep(0.01)
    assert (await store.get_thread("chat:u1:c1")).pending_since is not None
    harness.gate = None
    await drain(dispatcher)
    assert [r.prompt for r in harness.requests] == ["first", "second"]


async def test_retryable_failure_backs_off_then_dead_letters(env):
    store, _, harness, dispatcher = env
    harness.fail_with = HarnessError("network blip", retryable=True)
    await store.put(*chat("hi"))
    await drain(dispatcher)
    thread = await store.get_thread("chat:u1:c1")
    assert thread.attempts == 1 and not thread.dead
    assert len(await store.pending_events("chat:u1:c1")) == 1
    (run,) = await store.list_runs("chat:u1:c1")
    assert run.status is RunStatus.FAILED and "network blip" in run.error

    await drain(dispatcher)  # max_attempts=2 → second failure dead-letters
    thread = await store.get_thread("chat:u1:c1")
    assert thread.dead and thread.attempts == 2


async def test_unknown_harness_fails_without_retry(env):
    store, _, _, dispatcher = env
    ev, spec = chat("hi")
    spec = ThreadSpec(key=spec.key, type=spec.type, lane=spec.lane, harness="nope")
    await store.put(ev, spec)
    await drain(dispatcher)
    assert (await store.get_thread(spec.key)).dead


async def test_run_forever_processes_and_stops(env):
    store, _, harness, dispatcher = env
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run_forever(stop))
    await store.put(*chat("bg"))
    for _ in range(200):
        if harness.requests and not dispatcher.active_count:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert harness.requests[0].prompt == "bg"


def test_render_turn_wraps_non_chat_events():
    ev = Event(type="automation.fired", source="scheduler", payload={"job": "digest"},
               idempotency_key="x")
    out = render_turn([ev])
    assert out.startswith('<event type="automation.fired"') and '"job": "digest"' in out
