"""The chat owner's Retry (POST /api/runs/{run_id}/retry) on both stores.

Retry re-runs a failed chat run's own events, like the admin replay, so it is
not a new input: nothing is charged against the plan (Postgres runs with an
input limit on) and the user message is not saved twice.
"""
from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest

from handler.runtime.auth import Authenticator
from handler.runtime.bus import RunEventBus
from handler.runtime.dispatcher import Dispatcher
from handler.runtime.harness.base import HarnessError, RunEvent, RunEventKind
from handler.runtime.observability.pricing import PriceTable
from handler.runtime.run import RunConfig, RunExecutor
from handler.runtime.store.postgres import PostgresStore
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.store.sqlite_users import User
from handler.runtime.tenancy.limits import LiveLimits, PlanLimits
from handler.runtime.types import Event, Lane, Role, RunStatus, ThreadSpec, ThreadType
from handler.runtime.web.app import create_app

ADMIN_KEY = "test-admin-key-0123456789"


class SwitchHarness:
    name = "fake"

    def __init__(self):
        self.fail = True
        self.prompts: list[str] = []

    async def run(self, req):
        self.prompts.append(req.prompt)
        if self.fail:
            raise HarnessError("provider down", retryable=False)
        yield RunEvent(RunEventKind.FINAL, {"text": "done"})

    async def interrupt(self, run_id):
        pass


class World:
    def __init__(self, store, bus, dispatcher, harness, client, tokens, metered):
        self.store, self.bus, self.dispatcher = store, bus, dispatcher
        self.harness, self.client, self.tokens = harness, client, tokens
        self.metered = metered
        self.seen: list = []

    def h(self, name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[name]}"}

    def as_user(self, name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {ADMIN_KEY}", "X-Handler-User": name}

    async def drain(self) -> None:
        await self.dispatcher.claim_once()
        while self.dispatcher.active_count:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)  # let the bus recorder catch up

    async def say(self, text: str, user: str = "alice", conversation: str = "c1") -> dict:
        response = await self.client.post("/api/chat", headers=self.h(user), json={
            "text": text, "conversation_id": conversation})
        assert response.status_code == 200, response.text
        return response.json()

    async def used(self) -> int | None:
        usage = await self.client.get("/api/limits/inputs", headers=self.h("alice"))
        body = usage.json()
        return body["used"] if body["limited"] else None

    async def events(self, type_: str) -> list:
        for _ in range(3):
            await asyncio.sleep(0)  # let the recorder take what was published
        return [m for m in self.seen if m.type == type_]


@pytest.fixture(params=["sqlite", "postgres"])
async def world(request, tmp_path):
    if request.param == "sqlite":
        store = SQLiteStore(tmp_path / "app.db")
    else:
        store = PostgresStore(request.getfixturevalue("pg_dsn"))
        store.limits = LiveLimits(PlanLimits("free", monthly_inputs=10))
    for name, role in (("root", "admin"), ("alice", "member"), ("bob", "member")):
        await store.create_user(User(name, name.title(), role))
    tokens = {}
    for name in ("root", "alice", "bob"):
        _, tokens[name] = await store.create_token(name, "t")
    bus = RunEventBus()
    harness = SwitchHarness()
    executor = RunExecutor(store, {"fake": harness}, bus,
                           RunConfig(data_dir=tmp_path / "data"), PriceTable([]))
    dispatcher = Dispatcher(store, executor, lease_seconds=5)
    app = create_app(store, bus, Authenticator(store, ADMIN_KEY), default_harness="fake")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        w = World(store, bus, dispatcher, harness, client, tokens,
                  metered=request.param == "postgres")

        async def record():
            async with bus.subscribe("*") as queue:
                while True:
                    w.seen.append(await queue.get())

        recorder = asyncio.create_task(record())
        await asyncio.sleep(0)
        try:
            yield w
        finally:
            recorder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recorder
    await store.close()


async def failed_chat(w: World, text: str = "hello", **kwargs):
    sent = await w.say(text, **kwargs)
    await w.drain()
    run = (await w.store.list_runs(sent["thread_key"]))[0]
    assert run.status is RunStatus.FAILED
    return sent, run


async def test_owner_retries_their_latest_failed_run_once_without_charging(world):
    w = world
    sent, run = await failed_chat(w)
    key = sent["thread_key"]
    before = await w.used()
    assert before == (1 if w.metered else None)

    # What a client reads to show Retry: the stream event...
    (finished,) = await w.events("run_finished")
    assert finished.run_id == run.id and finished.data["status"] == "failed"
    assert finished.data["retryable"] is True
    assert finished.data["event_ids"] == [sent["event_id"]]
    # ...the runs listing...
    runs = (await w.client.get(f"/api/threads/{key}/runs", headers=w.h("alice"))).json()
    assert runs[0]["id"] == run.id and runs[0]["status"] == "failed"
    assert runs[0]["retryable"] is True and runs[0]["trigger_event_ids"] == [sent["event_id"]]
    # ...and the user message it was for.
    messages = (await w.client.get(f"/api/threads/{key}/messages", headers=w.h("alice"))).json()
    (user_message,) = [m for m in messages if m["role"] == Role.USER.value]
    assert user_message["raw"]["event_id"] in runs[0]["trigger_event_ids"]

    w.harness.fail = False
    w.seen.clear()
    response = await w.client.post(f"/api/runs/{run.id}/retry", headers=w.h("alice"))
    assert response.status_code == 202, response.text
    assert response.json() == {"run_id": run.id, "thread_key": key, "status": "queued"}
    (queued,) = await w.events("event_queued")
    assert queued.thread_key == key and queued.data["event_ids"] == [sent["event_id"]]
    assert queued.data["event_id"] == sent["event_id"] and queued.data["retry_of"] == run.id
    runs = (await w.client.get(f"/api/threads/{key}/runs", headers=w.h("alice"))).json()
    assert runs[0]["retryable"] is False  # used, even before it runs

    await w.drain()
    latest = (await w.store.list_runs(key))[0]
    assert latest.id != run.id and latest.status is RunStatus.COMPLETED
    assert latest.trigger_event_ids == run.trigger_event_ids
    assert len(w.harness.prompts) == 2 and "hello" in w.harness.prompts[1]
    assert await w.used() == before  # the retry is not a new input
    messages = (await w.client.get(f"/api/threads/{key}/messages", headers=w.h("alice"))).json()
    assert [m["role"] for m in messages].count("user") == 1  # not saved twice
    assert all(r["retryable"] is False for r in (await w.client.get(
        f"/api/threads/{key}/runs", headers=w.h("alice"))).json())

    again = await w.client.post(f"/api/runs/{run.id}/retry", headers=w.h("alice"))
    assert again.status_code == 409 and "already" in again.json()["detail"]
    done = await w.client.post(f"/api/runs/{latest.id}/retry", headers=w.h("alice"))
    assert done.status_code == 409 and "not a terminal failure" in done.json()["detail"]
    (row,) = await w.store.failed_runs()
    assert row["replayed_at"] and row["replayed_by"] == "retry:alice"


async def test_retry_after_admin_replay_is_refused(world):
    w = world
    _, run = await failed_chat(w)
    replay = await w.client.post(f"/api/runs/{run.id}/replay", headers=w.h("root"))
    assert replay.status_code == 200, replay.text
    assert [m.data.get("retry_of") for m in await w.events("event_queued")][-1] == run.id
    retry = await w.client.post(f"/api/runs/{run.id}/retry", headers=w.h("alice"))
    assert retry.status_code == 409 and "already" in retry.json()["detail"]
    # And replay after a retry is refused the same way: one shared flag.
    await w.drain()
    _, second = await failed_chat(w, "again")
    assert (await w.client.post(f"/api/runs/{second.id}/retry",
                                headers=w.h("alice"))).status_code == 202
    replay = await w.client.post(f"/api/runs/{second.id}/replay", headers=w.h("root"))
    assert replay.status_code == 409 and "already replayed" in replay.json()["detail"]


async def test_only_the_latest_run_can_be_retried(world):
    w = world
    sent, first = await failed_chat(w, "one")
    _, second = await failed_chat(w, "two")
    stale = await w.client.post(f"/api/runs/{first.id}/retry", headers=w.h("alice"))
    assert stale.status_code == 409 and "not the latest" in stale.json()["detail"]
    runs = (await w.client.get(f"/api/threads/{sent['thread_key']}/runs",
                               headers=w.h("alice"))).json()
    assert [(r["id"], r["retryable"]) for r in runs] == [(second.id, True), (first.id, False)]
    # A newer message waiting to run makes the failed run not the latest either.
    await w.say("three")
    pending = await w.client.post(f"/api/runs/{second.id}/retry", headers=w.h("alice"))
    assert pending.status_code == 409 and "not the latest" in pending.json()["detail"]
    runs = (await w.client.get(f"/api/threads/{sent['thread_key']}/runs",
                               headers=w.h("alice"))).json()
    assert runs[0]["retryable"] is False
    # Refused ones are not used up: the flag is still unset.
    assert all(row["replayed_at"] is None for row in await w.store.failed_runs())


async def test_other_users_runs_are_not_found(world):
    w = world
    _, run = await failed_chat(w)
    url = f"/api/runs/{run.id}/retry"
    assert (await w.client.post(url, headers=w.h("bob"))).status_code == 404
    assert (await w.client.post(url, headers=w.as_user("bob"))).status_code == 404
    # An admin user is still not the owner of alice's chat.
    assert (await w.client.post(url, headers=w.h("root"))).status_code == 404
    assert (await w.client.post("/api/runs/run_none/retry",
                                headers=w.h("alice"))).status_code == 404
    no_user = await w.client.post(url, headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    assert no_user.status_code == 400
    # The admin key acting as the owner follows the owner's rules.
    response = await w.client.post(url, headers=w.as_user("alice"))
    assert response.status_code == 202, response.text
    assert (await w.store.failed_runs())[0]["replayed_by"] == "retry:alice"


async def test_shared_thread_runs_cannot_be_retried(world):
    w = world
    key = "automation:a1"
    await w.store.put(
        Event("automation.fired", "test", {"name": "job"}, "a1:1", thread_key=key,
              user_id="alice"),
        ThreadSpec(key, ThreadType.AUTOMATION, Lane.BACKGROUND, owner_user_id="alice",
                   harness="fake"))
    await w.drain()
    (run,) = await w.store.list_runs(key)
    assert run.status is RunStatus.FAILED
    (finished,) = await w.events("run_finished")
    assert finished.data["retryable"] is False
    runs = (await w.client.get(f"/api/threads/{key}/runs", headers=w.h("alice"))).json()
    assert runs[0]["retryable"] is False
    response = await w.client.post(f"/api/runs/{run.id}/retry", headers=w.h("alice"))
    assert response.status_code == 409 and "only chat" in response.json()["detail"]
    assert (await w.store.failed_runs())[0]["replayed_at"] is None


def test_free_plans_reach_retry():
    from handler.runtime.tenancy.plan_guard import allowed_without_console

    assert allowed_without_console("/api/runs/run_1/retry")
