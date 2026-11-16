"""Parked threads (list + Retry) and dead deliveries (list + redeliver) over the API."""

from __future__ import annotations

from handler.runtime.bus import RunEventBus
from handler.runtime.dispatcher import Dispatcher
from handler.runtime.harness.base import HarnessError, RunEvent, RunEventKind
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.run import RunConfig, RunExecutor
from handler.runtime.types import Event, Lane, RunStatus, ThreadSpec, ThreadType


class SwitchHarness:
    name = "fake"

    def __init__(self):
        self.fail = True

    async def run(self, req):
        if self.fail:
            raise HarnessError("provider down", retryable=False)
        yield RunEvent(RunEventKind.FINAL, {"text": "done"})

    async def interrupt(self, run_id):
        pass


async def _drain(dispatcher):
    import asyncio

    await dispatcher.claim_once()
    while dispatcher.active_count:
        await asyncio.sleep(0.01)


async def _park(world, key, thread_type, owner="alice"):
    harness = SwitchHarness()
    executor = RunExecutor(world.store, {"fake": harness}, RunEventBus(),
                           RunConfig(data_dir=world.tmp_path / "data"), PriceTable([]))
    dispatcher = Dispatcher(world.store, executor, lease_seconds=5)
    lane = Lane.INTERACTIVE if thread_type is ThreadType.CHAT else Lane.BACKGROUND
    await world.store.put(
        Event("automation.fired", "test", {"name": "job"}, f"{key}:1", thread_key=key,
              user_id=owner),
        ThreadSpec(key, thread_type, lane, owner_user_id=owner, harness="fake"))
    await _drain(dispatcher)
    assert (await world.store.get_thread(key)).dead
    return harness, dispatcher


async def test_parked_threads_are_listed_with_visibility(world, client):
    await _park(world, "automation:a1", ThreadType.AUTOMATION)
    await _park(world, "chat:bob:c1", ThreadType.CHAT, owner="bob")
    rows = (await client.get("/api/threads/parked", headers=world.h("root"))).json()
    assert {r["thread_key"] for r in rows} == {"automation:a1", "chat:bob:c1"}
    auto = next(r for r in rows if r["thread_key"] == "automation:a1")
    assert auto["pending_events"] == 1 and auto["attempts"] == 1
    assert "provider down" in auto["last_error"] and auto["parked_at"]
    # A member sees shared threads and their own chats only.
    rows = (await client.get("/api/threads/parked", headers=world.h("alice"))).json()
    assert [r["thread_key"] for r in rows] == ["automation:a1"]


async def test_retry_is_admin_only_and_runs_the_queued_work(world, client):
    harness, dispatcher = await _park(world, "automation:a1", ThreadType.AUTOMATION)
    url = "/api/threads/automation:a1/retry"
    assert (await client.post(url, headers=world.h("alice"))).status_code == 403
    assert (await client.post("/api/threads/automation:none/retry",
                              headers=world.h("root"))).status_code == 404
    harness.fail = False
    response = await client.post(url, headers=world.h("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["thread"]["dead"] is False and body["pending_events"] == 1
    await _drain(dispatcher)
    latest = (await world.store.list_runs("automation:a1"))[0]
    assert latest.status is RunStatus.COMPLETED
    thread = await world.store.get_thread("automation:a1")
    assert not thread.dead and thread.pending_since is None
    assert (await client.post(url, headers=world.h("root"))).status_code == 409  # not parked
    assert (await client.get("/api/threads/parked", headers=world.h("root"))).json() == []


async def test_dead_deliveries_can_be_listed_and_redelivered(world, client):
    store = world.store
    for key in ("k1", "k2"):
        await store.enqueue_delivery("crontal", key, {"n": key})
        await store.delivery_failed("crontal", key, "HTTP 500", None)
    await store.enqueue_delivery("crontal", "k3", {"n": "k3"})
    rows = (await client.get("/api/deliveries", headers=world.h("root"))).json()
    assert {r["key"] for r in rows} == {"k1", "k2"} and rows[0]["last_error"] == "HTTP 500"
    assert (await client.get("/api/deliveries", headers=world.h("alice"))).status_code == 403
    response = await client.post("/api/deliveries/redeliver", headers=world.h("root"),
                                 json={"sink": "crontal", "key": "k1"})
    assert response.json() == {"requeued": 1}
    row = await store.get_delivery("crontal", "k1")
    assert row.status == "pending" and row.attempts == 0
    response = await client.post("/api/deliveries/redeliver", headers=world.h("root"),
                                 json={"sink": "crontal", "key": "k1"})
    assert response.status_code == 404
    response = await client.post("/api/deliveries/redeliver", headers=world.h("root"),
                                 json={"sink": "crontal"})
    assert response.json() == {"requeued": 1}
    assert {d.status for d in await store.list_deliveries()} == {"pending"}
