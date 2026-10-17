import json

import pytest

from handler.runtime.context import RunContext
from handler.runtime.gateway import Gateway, GrantPolicy
from handler.runtime.gateway.credentials import (
    ConnectionCredentials,
    StaticCredentials,
    UpstreamAccess,
)
from handler.runtime.gateway.upstream import UpstreamConnector
from handler.runtime.types import Connection, Risk

CURATED = {
    "add": {"risk": "read", "rename": "sum", "description": "Sum two ints"},
    "whoami": {"risk": "write_external"},
}


def ctx(user):
    return RunContext(run_id="r", thread_key="t", user_id=user, acting_as=user)


def static(upstream):
    return StaticCredentials({"calc": {
        "url": upstream.url,
        "headers_env": {"X-Api-Key": "CALC_KEY"},
        "owners": {"user:alice": {"headers": {"X-Api-Key": "key-alice"}},
                   "user:bob": {"headers": {"X-Api-Key": "key-bob"}},
                   "user:eve": {"headers": {"X-Api-Key": "bad"}}},
    }})


async def started(upstream, creds=None, **kw):
    c = UpstreamConnector("calc", creds or static(upstream), CURATED, **kw)
    await c.start(UpstreamAccess(upstream.url, {"X-Api-Key": "key-inst"}))
    return c


async def test_curation_schema_rename(upstream):
    specs = {s.name: s for s in (await started(upstream)).tools()}
    assert set(specs) == {"calc_sum", "calc_whoami"}  # "dangerous" not exposed
    s = specs["calc_sum"]
    assert s.action == "calc.sum" and s.risk is Risk.READ
    assert s.description == "Sum two ints"
    assert s.input_schema["required"] == ["a", "b"]
    assert specs["calc_whoami"].description == "Echo the caller key"
    assert specs["calc_whoami"].risk is Risk.WRITE_EXTERNAL


async def test_unknown_curated_name_fails_startup(upstream):
    c = UpstreamConnector("calc", static(upstream), {"nope": {"risk": "read"}})
    with pytest.raises(ValueError, match="nope"):
        await c.start(UpstreamAccess(upstream.url, {"X-Api-Key": "key-inst"}))


async def test_tools_before_start_errors(upstream):
    with pytest.raises(RuntimeError):
        UpstreamConnector("calc", static(upstream), CURATED).tools()


async def test_per_owner_credentials(upstream):
    by_name = {s.name: s for s in (await started(upstream)).tools()}
    who = by_name["calc_whoami"].handler
    a = await who(ctx("alice"), {})
    b = await who(ctx("bob"), {})
    assert (a.content, b.content) == ("key-alice", "key-bob")
    total = await by_name["calc_sum"].handler(ctx("alice"), {"a": 2, "b": 3})
    assert total.content == "5" and not total.is_error


async def test_default_owner_falls_back_to_instance(upstream):
    creds = StaticCredentials({"calc": {"url": upstream.url, "headers": {"X-Api-Key": "key-inst"}}})
    c = await started(upstream, creds)
    system = RunContext(run_id="r", thread_key="t")
    result = await c.tools()[1].handler(system, {})
    assert result.content == "key-inst"


async def test_static_env_secret(upstream, monkeypatch):
    monkeypatch.setenv("CALC_KEY", "key-inst")
    creds = StaticCredentials({"calc": {"url": upstream.url,
                                        "headers_env": {"X-Api-Key": "CALC_KEY"}}})
    c = await started(upstream, creds)
    assert (await c.tools()[1].handler(ctx("zed"), {})).content == "key-inst"


async def test_custom_owner_for(upstream):
    c = await started(upstream, owner_for=lambda _ctx: "user:bob")
    assert (await c.tools()[1].handler(ctx("alice"), {})).content == "key-bob"


async def test_missing_connection(upstream, connections):
    creds = ConnectionCredentials(connections, {"local": lambda c: None})
    c = await started(upstream, creds)
    result = await c.tools()[0].handler(ctx("alice"), {"a": 1, "b": 1})
    assert result.is_error and "connect" in result.content.lower()


def conn(upstream, owner, key, **kw):
    return Connection(connector="calc", owner=owner, provider="local", secret_ref=key, **kw)


def local_resolver(upstream):
    return lambda c: UpstreamAccess(upstream.url, {"X-Api-Key": c.secret_ref})


async def test_connection_credentials_dispatch(upstream, connections):
    await connections.put_connection(conn(upstream, "user:alice", "key-alice"))
    await connections.put_connection(conn(upstream, "user:bob", "key-bob"))
    creds = ConnectionCredentials(connections, {"local": local_resolver(upstream)})
    c = await started(upstream, creds)
    who = c.tools()[1].handler
    assert (await who(ctx("alice"), {})).content == "key-alice"
    assert (await who(ctx("bob"), {})).content == "key-bob"
    used = [x for x in connections.items.values() if x.last_used_at]
    assert len(used) == 2


async def test_401_marks_needs_reauth(upstream, connections):
    stored = conn(upstream, "user:eve", "bad")
    await connections.put_connection(stored)
    creds = ConnectionCredentials(connections, {"local": local_resolver(upstream)})
    c = await started(upstream, creds)
    result = await c.tools()[0].handler(ctx("eve"), {"a": 1, "b": 2})
    assert result.is_error and "reconnect" in result.content
    assert connections.items[stored.id].status == "needs_reauth"
    again = await c.tools()[0].handler(ctx("eve"), {"a": 1, "b": 2})  # no upstream call
    assert again.is_error and "reconnect" in again.content
    assert not any(k == "bad" for _, _, k in upstream.calls)


async def test_existing_needs_reauth_connection(upstream, connections):
    await connections.put_connection(conn(upstream, "user:alice", "key-alice", status="needs_reauth"))
    creds = ConnectionCredentials(connections, {"local": local_resolver(upstream)})
    c = await started(upstream, creds)
    result = await c.tools()[0].handler(ctx("alice"), {"a": 1, "b": 2})
    assert result.is_error and "reconnect" in result.content
    assert upstream.calls == []


async def test_static_401_error_result(upstream):
    c = await started(upstream)
    result = await c.tools()[0].handler(ctx("eve"), {"a": 1, "b": 2})
    assert result.is_error and "reconnect" in result.content


async def test_connection_error_is_error_result():
    creds = StaticCredentials({"calc": {"url": "http://127.0.0.1:1/", "headers": {}}})
    c = UpstreamConnector("calc", creds, CURATED)
    c._specs = []  # skip discovery
    result = await c._call(ctx("alice"), "add", {"a": 1, "b": 1})
    assert result.is_error


async def test_through_gateway(upstream, store_gateway):
    gateway = store_gateway
    gateway.register(await started(upstream))
    assert {s.name for s in gateway.specs()} == {"calc_sum", "calc_whoami"}
    ok = await gateway.call(ctx("alice"), "calc_sum", {"a": 4, "b": 5})
    assert ok.content == "9" and not ok.is_error
    bad = await gateway.call(ctx("alice"), "calc_sum", {"a": 4})
    assert bad.is_error
    pending = await gateway.call(ctx("alice"), "calc_whoami", {})  # write_external -> ask
    assert json.loads(pending.content if isinstance(pending.content, str)
                      else json.dumps(pending.content))["status"] == "pending_approval"
