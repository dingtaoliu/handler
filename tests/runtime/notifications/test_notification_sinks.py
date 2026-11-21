"""Notifications → domain events → outbox → sinks (Crontal push); failed runs and
dead deliveries notify nobody, only the operator's log alert."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from handler.runtime.bus import RunEventBus
from handler.runtime.dispatcher import Dispatcher
from handler.runtime.harness.base import HarnessError, RunEvent, RunEventKind
from handler.runtime.integrations.crontal.sink import (
    CALLBACK_PATH,
    CrontalNotificationSink,
    crontal_sink_from_config,
)
from handler.runtime.notifications import Notifier
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.run import RunConfig, RunExecutor
from handler.runtime.sinks import (
    NOTIFICATION_CREATED,
    DeliveryError,
    DomainEvent,
    Outbox,
    RetryPolicy,
)
from handler.runtime.store.deliveries import DEAD, DELIVERED, PENDING
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.store.sqlite_users import User
from handler.runtime.types import Event, Lane, RunStatus, ThreadSpec, ThreadType
from handler.runtime.web.ingest import verify


@pytest.fixture
async def store(tmp_path):
    instance = SQLiteStore(tmp_path / "h.db")
    yield instance
    await instance.close()


@dataclass
class FakeSink:
    name: str = "fake"
    retry: RetryPolicy = field(default_factory=lambda: RetryPolicy(3, (0.0,)))
    fail_with: Exception | None = None
    payloads: list = field(default_factory=list)

    def accepts(self, event: DomainEvent) -> bool:
        return event.type == NOTIFICATION_CREATED

    async def deliver(self, payload):
        if self.fail_with is not None:
            raise self.fail_with
        self.payloads.append(payload)


async def test_notifier_stores_publishes_and_emits_once_per_call(store):
    bus, sink = RunEventBus(), FakeSink()
    outbox = Outbox(store, [sink])
    notifier = Notifier(store, bus, outbox=outbox, users_provider=lambda: ["a", "b"],
                        instance="bss")
    async with bus.subscribe() as queue:
        nid, rows = await notifier.send("cron:x", "run_1", "done", "all")
        assert {queue.get_nowait().data["user_id"] for _ in rows} == {"a", "b"}
    _, listed = await notifier.send("cron:x", None, "again", ["a", "a"])
    assert len(listed) == 1
    assert await outbox.deliver_once() == 2
    first, second = sink.payloads
    assert first["type"] == NOTIFICATION_CREATED and first["key"] == nid
    data = first["data"]
    assert data["id"] == nid and data["instance"] == "bss" and data["recipients"] == "all"
    assert data["message"] == "done" and data["thread_key"] == "cron:x" and data["run_id"] == "run_1"
    assert data["notification_ids"] == [r.id for r in rows]
    assert second["data"]["recipients"] == ["a"]
    # Durable and idempotent: re-emitting the same event queues nothing.
    assert await outbox.emit(DomainEvent(NOTIFICATION_CREATED, nid, {})) == []
    assert (await store.get_delivery("fake", nid)).status == DELIVERED


async def test_notifier_without_sinks_queues_nothing(store):
    notifier = Notifier(store, RunEventBus(), outbox=Outbox(store), users_provider=lambda: [])
    await notifier.send("cron:x", None, "hi", ["a"])
    assert await store.list_deliveries() == []
    with pytest.raises(ValueError):
        await notifier.send("cron:x", None, "hi", "a")


async def test_outbox_retries_with_backoff_then_dead_letters(store, caplog):
    sink = FakeSink(fail_with=RuntimeError("down"))
    outbox = Outbox(store, [sink])
    await outbox.emit(DomainEvent(NOTIFICATION_CREATED, "n1", {"id": "n1"}))
    await outbox.emit(DomainEvent("task.status_changed", "t1", {}))  # not accepted
    for attempt in (1, 2):
        assert await outbox.deliver_once() == 0
        row = await store.get_delivery("fake", "n1")
        assert row.status == PENDING and row.attempts == attempt
    with caplog.at_level(logging.ERROR, logger="handler.runtime.sinks"):
        await outbox.deliver_once()
    row = await store.get_delivery("fake", "n1")
    assert row.status == DEAD and row.attempts == 3 and "down" in row.last_error
    assert any(r.message == "delivery dead" for r in caplog.records)
    assert len(await store.list_deliveries()) == 1


async def test_permanent_error_dead_letters_at_once(store):
    sink = FakeSink(fail_with=DeliveryError("HTTP 400", retryable=False))
    outbox = Outbox(store, [sink])
    await outbox.emit(DomainEvent(NOTIFICATION_CREATED, "n1", {}))
    await outbox.deliver_once()
    assert (await store.get_delivery("fake", "n1")).status == DEAD


async def test_run_forever_delivers_on_wake(store):
    sink = FakeSink()
    outbox = Outbox(store, [sink], poll_seconds=30)
    stop = asyncio.Event()
    task = asyncio.create_task(outbox.run_forever(stop))
    await asyncio.sleep(0.01)
    await outbox.emit(DomainEvent(NOTIFICATION_CREATED, "n1", {}))
    for _ in range(200):
        if sink.payloads:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert len(sink.payloads) == 1


def _payload(**data):
    base = {"id": "ntf_1", "instance": "ignored", "recipients": ["2"], "message": "Hi",
            "thread_key": "automation:daily", "run_id": "run_1", "thread_type": "automation",
            "thread_title": "Daily", "created_at": "2026-10-18T12:00:00+00:00",
            "notification_ids": ["ntf_local"]}
    return {"type": NOTIFICATION_CREATED, "key": "ntf_1", "data": {**base, **data}}


async def test_crontal_sink_signs_and_posts_wire_payload():
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"created": 1})

    sink = CrontalNotificationSink("https://crontal.test/", "whsec", "bss",
                                   transport=httpx.MockTransport(handle))
    await sink.deliver(_payload())
    (request,) = seen
    assert str(request.url) == f"https://crontal.test{CALLBACK_PATH}"
    assert request.headers["x-handler-instance"] == "bss"
    assert verify("whsec", request.headers["x-handler-signature"], request.content)
    assert not verify("other", request.headers["x-handler-signature"], request.content)
    body = json.loads(request.content)
    assert body == {"id": "ntf_1", "instance": "bss", "recipients": ["2"], "message": "Hi",
                    "thread_key": "automation:daily", "run_id": "run_1",
                    "thread_type": "automation", "thread_title": "Daily",
                    "created_at": "2026-10-18T12:00:00+00:00"}


@pytest.mark.parametrize("status,retryable", [(500, True), (401, True), (404, True),
                                              (400, False), (422, False)])
async def test_crontal_sink_errors(status, retryable):
    sink = CrontalNotificationSink(
        "https://crontal.test", "whsec", "",
        transport=httpx.MockTransport(lambda r: httpx.Response(status, text="no")))
    with pytest.raises(DeliveryError) as raised:
        await sink.deliver(_payload())
    assert raised.value.retryable is retryable
    assert sink.instance == "main"


def test_crontal_sink_needs_url_token_and_secret():
    url = "https://crontal.test/api/v1/agent-mcp"
    assert crontal_sink_from_config(None, "t", "s", "main") is None
    assert crontal_sink_from_config(url, None, "s", "main") is None
    assert crontal_sink_from_config(url, "t", None, "main") is None
    sink = crontal_sink_from_config(url, "t", "s", "bss")
    assert sink.url == f"https://crontal.test{CALLBACK_PATH}" and sink.instance == "bss"


# ── automatic notifications for dead background runs ────────────────────────


class FailingHarness:
    name = "fake"

    def __init__(self, retryable: bool = False) -> None:
        self.retryable = retryable
        self.fail = True
        self.prompts: list[str] = []

    async def run(self, req):
        self.prompts.append(req.prompt)
        yield RunEvent(RunEventKind.SESSION, {"session_id": "s"})
        if self.fail:
            raise HarnessError("model exploded", retryable=self.retryable)
        yield RunEvent(RunEventKind.FINAL, {"text": "done"})

    async def interrupt(self, run_id):
        pass


class FailingSink:
    """Fails every delivery for good (non-retryable) until `fail` is cleared."""

    name = "flaky"
    retry = RetryPolicy(max_attempts=1)

    def __init__(self):
        self.fail = True
        self.payloads = []

    def accepts(self, event):
        return True

    async def deliver(self, payload):
        if self.fail:
            raise DeliveryError("HTTP 500", retryable=False)
        self.payloads.append(payload)


async def _failure_env(store, tmp_path: Path, *, retryable=False):
    await store.create_user(User("owner", "Owner"))
    await store.create_user(User("boss", "Boss", role="admin"))
    bus, sink = RunEventBus(), FakeSink()
    notifier = Notifier(store, bus, outbox=Outbox(store, [sink]), users_provider=lambda: [])
    prices = tmp_path / "prices.yaml"
    prices.write_text("prices: []\n")
    harness = FailingHarness(retryable)
    executor = RunExecutor(
        store, {"fake": harness}, bus,
        RunConfig(data_dir=tmp_path / "data", heartbeat_seconds=0.05, lease_seconds=5,
                  retry_backoff_seconds=(0.0,), max_attempts=2),
        PriceTable.from_yaml(prices))
    return Dispatcher(store, executor, lease_seconds=5), notifier, harness


async def _put(store, key, thread_type, idem, event_type="automation.fired"):
    lane = Lane.INTERACTIVE if thread_type is ThreadType.CHAT else Lane.BACKGROUND
    payload = {"text": idem} if event_type == "chat.message" else {"name": "Daily digest"}
    await store.put(
        Event(type=event_type, source="test", payload=payload,
              idempotency_key=idem, thread_key=key, user_id="owner"),
        ThreadSpec(key=key, type=thread_type, lane=lane, owner_user_id="owner", harness="fake"))


async def _drain(dispatcher):
    await dispatcher.claim_once()
    while dispatcher.active_count:
        await asyncio.sleep(0.01)


async def _no_notices(store):
    for user in ("owner", "boss"):
        assert not await store.list_notifications(user)
    assert await store.list_deliveries() == []


@pytest.mark.parametrize("key,thread_type", [("automation:daily", ThreadType.AUTOMATION),
                                             ("chat:owner:c1", ThreadType.CHAT)])
async def test_terminal_failure_notifies_nobody_and_logs_one_alert(store, tmp_path, caplog,
                                                                   key, thread_type):
    dispatcher, _, _ = await _failure_env(store, tmp_path)
    await _put(store, key, thread_type, "e1")
    with caplog.at_level("WARNING", logger="handler.runtime.run"):
        await _drain(dispatcher)
    await _no_notices(store)
    (alert,) = [r for r in caplog.records if r.message == "run failed"]
    (run,) = await store.list_runs(key)
    (event,) = [e for e, _ in await store.list_events()]
    assert alert.levelname == "ERROR" and alert.tenant == "main"
    assert alert.thread_key == key and alert.thread_type == str(thread_type)
    assert alert.run_id == run.id and alert.attempts == 1
    assert "model exploded" in alert.error and alert.event_ids == [event.id]
    thread = await store.get_thread(key)
    assert not thread.dead and thread.pending_since is None and thread.attempts == 0
    assert "model exploded" in thread.last_error  # kept for debugging


async def test_retries_log_a_warning_then_one_alert(store, tmp_path, caplog):
    dispatcher, _, _ = await _failure_env(store, tmp_path, retryable=True)
    await _put(store, "crontal:rfq:1", ThreadType.CRONTAL, "e1")
    with caplog.at_level("WARNING", logger="handler.runtime.run"):
        await _drain(dispatcher)  # attempt 1 of 2: retry scheduled
        assert [r.message for r in caplog.records if r.levelname == "ERROR"] == []
        assert any(r.message == "run failed, will retry" for r in caplog.records)
        assert (await store.get_thread("crontal:rfq:1")).pending_since is not None
        await _drain(dispatcher)  # attempt 2: accepted
    (alert,) = [r for r in caplog.records if r.message == "run failed"]
    assert alert.attempts == 2
    await _no_notices(store)


async def test_a_failure_never_blocks_the_thread(store, tmp_path):
    dispatcher, _, harness = await _failure_env(store, tmp_path)
    await _put(store, "chat:owner:c1", ThreadType.CHAT, "hello", "chat.message")
    await _put(store, "automation:daily", ThreadType.AUTOMATION, "fire1")
    await _drain(dispatcher)
    await _drain(dispatcher)
    harness.fail = False
    # The person writes again, and the automation fires again: both run normally,
    # and only the new event runs (the failed one stays consumed).
    harness.prompts.clear()
    await _put(store, "chat:owner:c1", ThreadType.CHAT, "again", "chat.message")
    await _put(store, "automation:daily", ThreadType.AUTOMATION, "fire2")
    await _drain(dispatcher)
    await _drain(dispatcher)
    for key in ("chat:owner:c1", "automation:daily"):
        latest, failed = await store.list_runs(key)
        assert latest.status is RunStatus.COMPLETED and failed.status is RunStatus.FAILED
        (trigger,) = latest.trigger_event_ids
        assert trigger not in failed.trigger_event_ids
        thread = await store.get_thread(key)
        assert thread.last_error is None and thread.pending_since is None
    assert len(harness.prompts) == 2 and not any("hello" in p for p in harness.prompts)
    # The failed chat message is still in the conversation record.
    texts = [b.get("text", "") for m in await store.list_messages("chat:owner:c1")
             for b in m.blocks]
    assert any("hello" in t for t in texts) and any("again" in t for t in texts)
    await _no_notices(store)


async def test_dead_delivery_alerts_and_notifies_nobody(store, caplog):
    await store.create_user(User("boss", "Boss", role="admin"))
    sink = FailingSink()
    outbox = Outbox(store, [sink], tenant="bss")
    for key in ("k1", "k2"):
        await store.enqueue_delivery(sink.name, key, {"n": key})
    with caplog.at_level("ERROR", logger="handler.runtime.sinks"):
        await outbox.deliver_once()
    alerts = [r for r in caplog.records if r.message == "delivery dead"]
    assert len(alerts) == 2 and alerts[0].tenant == "bss" and alerts[0].sink == sink.name
    assert not await store.list_notifications("boss")
    assert {(d.key, d.status) for d in await store.list_deliveries()} == {
        ("k1", "dead"), ("k2", "dead")}


async def test_redeliver_requeues_dead_rows_and_they_deliver(store):
    sink = FailingSink()
    outbox = Outbox(store, [sink])
    for key in ("k1", "k2"):
        await store.enqueue_delivery(sink.name, key, {"n": key})
    await outbox.deliver_once()
    assert {d.status for d in await store.list_deliveries()} == {"dead"}
    sink.fail = False
    assert await store.redeliver(sink.name, "k1") == 1
    assert await store.redeliver(sink.name, "k1") == 0  # pending now, not dead
    assert await outbox.deliver_once() == 1
    assert (await store.get_delivery(sink.name, "k1")).status == "delivered"
    assert await store.redeliver(sink.name) == 1  # every dead row of the sink
    assert await outbox.deliver_once() == 1
    assert {d.status for d in await store.list_deliveries()} == {"delivered"}
