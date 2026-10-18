from __future__ import annotations

import httpx
import pytest

from handler.runtime.auth import Authenticator
from handler.runtime.bus import RunEventBus
from handler.runtime.gateway import Gateway, GrantPolicy, ToolResult, ToolSpec
from handler.runtime.scheduler import Scheduler
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.store.sqlite_users import User
from handler.runtime.uploads import UploadStore
from handler.runtime.types import Risk
from handler.runtime.web.admin_api import admin_router
from handler.runtime.web.app import create_app

ADMIN_KEY = "test-admin-key-0123456789"


async def _noop(ctx, args):
    return ToolResult({})


class Tools:
    def tools(self):
        schema = {"type": "object", "properties": {}}
        return [
            ToolSpec("read_it", "test.read", Risk.READ, "read", schema, _noop),
            ToolSpec("send_it", "test.send", Risk.WRITE_EXTERNAL, "send", schema, _noop),
        ]


class World:
    def __init__(self, store, app, tmp_path, scheduler, uploads):
        self.store, self.app, self.tmp_path = store, app, tmp_path
        self.scheduler, self.uploads = scheduler, uploads
        self.tokens: dict[str, str] = {}
        self.log_file = tmp_path / "logs" / "handler-rt.jsonl"

    def h(self, name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[name]}"}


@pytest.fixture
async def world(tmp_path):
    store = SQLiteStore(tmp_path / "app.db")
    for name, role in (("root", "admin"), ("alice", "member"), ("bob", "member")):
        await store.create_user(User(name, name.title(), role))
    auth = Authenticator(store, ADMIN_KEY)
    async def emit(event):
        pass

    gateway = Gateway(store, GrantPolicy({"actions": {"test.read": "deny"}}), emit_event=emit)
    gateway.register(Tools())
    scheduler = Scheduler(store)
    uploads = UploadStore(tmp_path / "uploads")
    log_file = tmp_path / "logs" / "handler-rt.jsonl"
    app = create_app(store, RunEventBus(), auth, uploads=uploads)
    app.include_router(admin_router(store, auth, scheduler=scheduler, gateway=gateway,
                                    uploads=uploads, log_file=log_file))
    w = World(store, app, tmp_path, scheduler, uploads)
    for name in ("root", "alice", "bob"):
        _, w.tokens[name] = await store.create_token(name, "t")
    yield w
    await store.close()


@pytest.fixture
async def client(world):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app),
                                 base_url="http://t") as c:
        yield c
