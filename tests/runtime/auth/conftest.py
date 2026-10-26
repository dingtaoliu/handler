from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from handler.runtime.store.postgres import PostgresStore
from handler.runtime.store.sqlite import SQLiteStore
from tests.runtime.store_pg.conftest import pg_dsn, pg_server  # noqa: F401  (cluster fixtures)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(params=["sqlite", "postgres"])
async def users_store(request, tmp_path, clock):
    if request.param == "sqlite":
        instance = SQLiteStore(tmp_path / "users.db", clock=clock)
    else:
        instance = PostgresStore(request.getfixturevalue("pg_dsn"), clock=clock)
    try:
        yield instance
    finally:
        await instance.close()


# --- the full authenticated app ----------------------------------------------
import httpx  # noqa: E402

from handler.runtime.auth import Authenticator  # noqa: E402
from handler.runtime.bus import RunEventBus  # noqa: E402
from handler.runtime.context import RunContext  # noqa: E402
from handler.runtime.gateway import Gateway, GrantPolicy, ToolResult, ToolSpec  # noqa: E402
from handler.runtime.gateway import approvals_router  # noqa: E402
from handler.runtime.settings import SettingsService  # noqa: E402
from handler.runtime.store.sqlite_users import User  # noqa: E402
from handler.runtime.types import (  # noqa: E402
    Event, Lane, Message, Risk, Role, Run, RunStatus, ThreadSpec, ThreadType,
)
from handler.runtime.web.app import create_app  # noqa: E402
from handler.runtime.web.settings_api import settings_router  # noqa: E402

ADMIN_KEY = "test-admin-key-0123456789"


class World:
    """An app with users root (admin), alice and bob, and a few threads."""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}
        self.token_ids: dict[str, str] = {}

    def as_user(self, name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[name]}"}

    admin_key = {"Authorization": f"Bearer {ADMIN_KEY}"}


async def _make_thread(store, key, type_, lane, owner):
    event = Event("chat.message", "test", {"text": key}, f"idem-{key}", thread_key=key,
                  user_id=owner)
    await store.put(event, ThreadSpec(key, type_, lane, owner_user_id=owner, harness="codex"))
    lease, = await store.claim(f"w-{key}", lane, 1, 60)
    assert lease.thread_key == key
    run = Run(f"run-{key}", key, lease.version, [event.id], RunStatus.COMPLETED, "codex", "m")
    assert await store.create_run(lease, run)
    await store.add_messages(lease, [Message(key, Role.USER, [{"type": "text", "text": key}])])
    return run


@pytest.fixture
async def store(tmp_path):
    instance = SQLiteStore(tmp_path / "app.db")
    yield instance
    await instance.close()


@pytest.fixture
def bus():
    return RunEventBus()


@pytest.fixture
async def world(store, bus):
    w = World()
    for name, role in (("root", "admin"), ("alice", "member"), ("bob", "member")):
        await store.create_user(User(name, name.title(), role))
        w.token_ids[name], w.tokens[name] = await store.create_token(name, "test")
    await _make_thread(store, "chat:alice:c1", ThreadType.CHAT, Lane.INTERACTIVE, "alice")
    await _make_thread(store, "chat:bob:c1", ThreadType.CHAT, Lane.INTERACTIVE, "bob")
    await _make_thread(store, "gmail:inbox", ThreadType.GMAIL, Lane.BACKGROUND, "alice")
    return w


class Tools:
    def tools(self):
        async def send(ctx, args):
            return ToolResult({"sent": True})

        return [ToolSpec(
            name="send_it", action="test.send", risk=Risk.WRITE_EXTERNAL, description="send",
            input_schema={"type": "object", "properties": {}}, handler=send,
        )]


@pytest.fixture
def gateway(store):
    async def emit(event):
        pass

    gw = Gateway(store, GrantPolicy({}), emit_event=emit)
    gw.register(Tools())
    return gw


@pytest.fixture
def app(store, bus, gateway):
    auth = Authenticator(store, ADMIN_KEY)
    app = create_app(store, bus, auth)
    app.include_router(approvals_router(gateway, store, auth))
    app.include_router(settings_router(SettingsService(store), auth))
    return app


@pytest.fixture
async def client(app, world):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        yield c


def ctx(thread_key: str) -> RunContext:
    return RunContext(run_id=f"run-{thread_key}", thread_key=thread_key, user_id="alice",
                      acting_as="alice")
