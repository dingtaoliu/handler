"""One bus for the host: each tenant sees only its own events, SSE included."""
import asyncio
import json
import socket

import httpx
import uvicorn
from fastapi import FastAPI

from handler.runtime.auth import Authenticator
from handler.runtime.bus import BusMessage, RunEventBus
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.web.app import create_app


async def test_tenant_views_never_cross():
    bus = RunEventBus()
    a, b = bus.for_tenant("a"), bus.for_tenant("b")
    assert bus.for_tenant("a") is a
    async with a.subscribe("*") as all_a, a.subscribe("chat:u:1") as one_a, \
            b.subscribe("chat:u:1") as one_b:
        b.publish(BusMessage("chat:u:1", None, "event_queued", {"from": "b"}))
        a.publish(BusMessage("chat:u:1", None, "event_queued", {"from": "a"}))
        # A message claiming another tenant is restamped by the publishing view.
        a.publish(BusMessage("chat:u:1", None, "event_queued", {"from": "a2"}, tenant="b"))
        assert [all_a.get_nowait().data["from"] for _ in range(all_a.qsize())] == ["a", "a2"]
        assert [one_a.get_nowait().data["from"] for _ in range(one_a.qsize())] == ["a", "a2"]
        assert [one_b.get_nowait().data["from"] for _ in range(one_b.qsize())] == ["b"]
        assert bus.subscriber_count("a") == 2 and bus.subscriber_count("b") == 1
    assert bus.subscriber_count() == 0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _frames(response, count, seen):
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            seen.append(json.loads(line[6:]))
            if len(seen) >= count:
                return


async def test_an_sse_stream_only_ever_carries_its_own_tenants_events(tmp_path):
    bus = RunEventBus()
    stores = {name: SQLiteStore(tmp_path / f"{name}.db") for name in ("a", "b")}
    host = FastAPI()
    for name, store in stores.items():
        host.mount(f"/i/{name}", create_app(store, bus.for_tenant(name),
                                            Authenticator(store, f"key-{name}"),
                                            instance_name=name))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(host, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient(timeout=5) as http:
            seen: list[dict] = []
            async with http.stream("GET", f"http://127.0.0.1:{port}/i/a/api/stream",
                                   params={"access_token": "key-a"}) as response:
                assert response.status_code == 200
                await asyncio.sleep(0.2)  # let the subscription register
                for tenant, key in (("b", "chat:u:1"), ("b", "automation:x"),
                                    ("a", "chat:u:1"), ("b", "chat:u:1"), ("a", "automation:y")):
                    bus.for_tenant(tenant).publish(
                        BusMessage(key, None, "event_queued", {"tenant": tenant}))
                await asyncio.wait_for(_frames(response, 2, seen), 5)
        assert [(f["thread_key"], f["data"]["tenant"]) for f in seen] == [
            ("chat:u:1", "a"), ("automation:y", "a")]
    finally:
        server.should_exit = True
        await task
        for store in stores.values():
            await store.close()
