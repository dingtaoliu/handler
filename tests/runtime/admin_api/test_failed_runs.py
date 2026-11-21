"""Failed runs (list + Replay) and dead deliveries (list + redeliver) over the API."""

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


async def _fail(world, key, thread_type, owner="alice"):
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
    thread = await world.store.get_thread(key)
    assert not thread.dead and thread.pending_since is None  # never blocked
    (run,) = await world.store.list_runs(key)
    assert run.status is RunStatus.FAILED
    return harness, dispatcher, run


async def test_failed_runs_are_listed_for_admins(world, client):
    _, _, auto = await _fail(world, "automation:a1", ThreadType.AUTOMATION)
    _, _, chat = await _fail(world, "chat:bob:c1", ThreadType.CHAT, owner="bob")
    response = await client.get("/api/runs/failed", headers=world.h("root"))
    assert response.status_code == 200, response.text
    page = response.json()
    assert [r["run_id"] for r in page["items"]] == [chat.id, auto.id]  # newest first
    row = page["items"][1]
    assert row["thread_key"] == "automation:a1" and row["thread_type"] == "automation"
    assert row["attempts"] == 1 and "provider down" in row["error"] and row["failed_at"]
    assert row["event_ids"] == auto.trigger_event_ids and row["replayed_at"] is None
    assert page["next_cursor"] is None
    first = (await client.get("/api/runs/failed?limit=1", headers=world.h("root"))).json()
    assert [r["run_id"] for r in first["items"]] == [chat.id] and first["next_cursor"]
    rest = (await client.get(f"/api/runs/failed?limit=1&before={first['next_cursor']}",
                             headers=world.h("root"))).json()
    assert [r["run_id"] for r in rest["items"]] == [auto.id]
    assert (await client.get("/api/runs/failed?before=junk",
                             headers=world.h("root"))).status_code == 422
    assert (await client.get("/api/runs/failed", headers=world.h("alice"))).status_code == 403
    # The run itself still reads as before.
    detail = (await client.get(f"/api/runs/{auto.id}", headers=world.h("root"))).json()
    assert detail["run"]["status"] == "failed"


async def test_replay_is_admin_only_and_runs_the_events_once(world, client):
    harness, dispatcher, run = await _fail(world, "automation:a1", ThreadType.AUTOMATION)
    url = f"/api/runs/{run.id}/replay"
    assert (await client.post(url, headers=world.h("alice"))).status_code == 403
    assert (await client.post("/api/runs/run_none/replay",
                              headers=world.h("root"))).status_code == 404
    harness.fail = False
    response = await client.post(url, headers=world.h("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requeued"] == 1 and body["thread_key"] == "automation:a1"
    await _drain(dispatcher)
    latest = (await world.store.list_runs("automation:a1"))[0]
    assert latest.status is RunStatus.COMPLETED
    assert latest.trigger_event_ids == run.trigger_event_ids
    thread = await world.store.get_thread("automation:a1")
    assert thread.pending_since is None and thread.last_error is None
    again = await client.post(url, headers=world.h("root"))
    assert again.status_code == 409 and "already replayed" in again.text
    # A run that did not fail for good cannot be replayed.
    other = await client.post(f"/api/runs/{latest.id}/replay", headers=world.h("root"))
    assert other.status_code == 409 and "not a terminal failure" in other.text
    (row,) = (await client.get("/api/runs/failed", headers=world.h("root"))).json()["items"]
    assert row["replayed_at"] and row["replayed_by"]


async def test_parked_routes_are_gone(world, client):
    assert (await client.get("/api/threads/parked", headers=world.h("root"))).status_code \
        in (404, 405)
    assert (await client.post("/api/threads/automation:a1/retry",
                              headers=world.h("root"))).status_code in (404, 405)


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
