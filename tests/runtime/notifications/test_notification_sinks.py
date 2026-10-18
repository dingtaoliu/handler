"""Notifications → domain events → outbox → sinks (Crontal push), and
automatic notifications for background runs that die."""
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
from handler.runtime.notifications import FailureNotifier, Notifier
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
from handler.runtime.types import Event, Lane, ThreadSpec, ThreadType
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
    assert any(r.message == "delivery dead-lettered" for r in caplog.records)
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

    async def run(self, req):
        yield RunEvent(RunEventKind.SESSION, {"session_id": "s"})
        raise HarnessError("model exploded", retryable=self.retryable)

    async def interrupt(self, run_id):
        pass


async def _failure_env(store, tmp_path: Path, *, retryable=False, cooldown=3600.0):
    await store.create_user(User("owner", "Owner"))
    await store.create_user(User("boss", "Boss", role="admin"))
    await store.create_user(User("gone", "Gone", role="admin", disabled=True))
    await store.create_user(User("member", "Member"))
    bus, sink = RunEventBus(), FakeSink()
    notifier = Notifier(store, bus, outbox=Outbox(store, [sink]), users_provider=lambda: [])
    prices = tmp_path / "prices.yaml"
    prices.write_text("prices: []\n")
    executor = RunExecutor(
        store, {"fake": FailingHarness(retryable)}, bus,
        RunConfig(data_dir=tmp_path / "data", heartbeat_seconds=0.05, lease_seconds=5,
                  retry_backoff_seconds=(0.0,), max_attempts=2),
        PriceTable.from_yaml(prices),
        failure_notifier=FailureNotifier(notifier, cooldown_seconds=cooldown))
    return Dispatcher(store, executor, lease_seconds=5), notifier, sink


async def _put(store, key, thread_type, idem):
    lane = Lane.INTERACTIVE if thread_type is ThreadType.CHAT else Lane.BACKGROUND
    await store.put(
        Event(type="automation.fired", source="test", payload={"name": "Daily digest"},
              idempotency_key=idem, thread_key=key, user_id="owner"),
        ThreadSpec(key=key, type=thread_type, lane=lane, owner_user_id="owner", harness="fake"))


async def _drain(dispatcher):
    await dispatcher.claim_once()
    while dispatcher.active_count:
        await asyncio.sleep(0.01)


async def test_dead_background_run_notifies_owner_and_admins_once(store, tmp_path):
    dispatcher, notifier, sink = await _failure_env(store, tmp_path)
    await _put(store, "automation:daily", ThreadType.AUTOMATION, "e1")
    await _drain(dispatcher)
    assert [n.user_id for n in await store.list_notifications("owner")] == ["owner"]
    (boss,) = await store.list_notifications("boss")
    assert not await store.list_notifications("gone")
    assert not await store.list_notifications("member")
    assert "automation:daily" in boss.message and "model exploded" in boss.message
    assert "Daily digest" in boss.message and boss.message.startswith("Automation run failed")
    await notifier.outbox.deliver_once()
    (payload,) = sink.payloads
    assert payload["data"]["recipients"] == ["owner", "boss"]
    assert payload["data"]["thread_type"] == "automation"
    # Another event on the same thread dies too: deduped within the cooldown.
    await _put(store, "automation:daily", ThreadType.AUTOMATION, "e2")
    await _drain(dispatcher)
    assert len(await store.list_notifications("boss")) == 1


async def test_retries_do_not_notify_until_dead(store, tmp_path):
    dispatcher, _, _ = await _failure_env(store, tmp_path, retryable=True, cooldown=0)
    await _put(store, "crontal:rfq:1", ThreadType.CRONTAL, "e1")
    await _drain(dispatcher)  # attempt 1 of 2: retry scheduled
    assert not await store.list_notifications("boss")
    await _drain(dispatcher)  # attempt 2: dead
    assert len(await store.list_notifications("boss")) == 1


async def test_chat_failures_do_not_notify(store, tmp_path):
    dispatcher, _, _ = await _failure_env(store, tmp_path)
    await _put(store, "chat:owner:c1", ThreadType.CHAT, "e1")
    await _drain(dispatcher)
    assert (await store.get_thread("chat:owner:c1")).dead
    assert not await store.list_notifications("boss")
