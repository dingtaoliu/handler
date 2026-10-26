from __future__ import annotations

import json
from datetime import timedelta

import pytest

from handler.runtime.types import (
    Event, Lane, Run, RunStatus, ThreadSpec, ThreadType, TokenUsage, utcnow,
)
from handler.runtime.uploads import MAX_FILE_BYTES

ROUTES = [
    ("GET", "/api/memory"), ("GET", "/api/memory/a"), ("PUT", "/api/memory/a"),
    ("DELETE", "/api/memory/a"), ("GET", "/api/automations"), ("POST", "/api/automations"),
    ("PATCH", "/api/automations/x"), ("DELETE", "/api/automations/x"),
    ("POST", "/api/automations/x/run"), ("GET", "/api/usage"), ("GET", "/api/tools"),
    ("POST", "/api/uploads"), ("GET", "/api/uploads"), ("GET", "/api/uploads/x"),
    ("DELETE", "/api/uploads/x"), ("GET", "/api/logs"), ("GET", "/api/connections"),
]


@pytest.mark.parametrize("method,path", ROUTES)
async def test_requires_auth(client, method, path):
    assert (await client.request(method, path)).status_code == 401


# ---- memory --------------------------------------------------------------

async def test_memory_crud_and_conflict(client, world):
    alice = world.h("alice")
    r = await client.put("/api/memory/people/danny", headers=alice,
                         json={"content": "likes tea", "description": "about danny"})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1 and body["updated_by"] == "alice" and body["size"] == 9
    assert (await client.get("/api/memory", headers=world.h("bob"))).json() == []
    listing = (await client.get("/api/memory", headers=alice)).json()
    assert [t["name"] for t in listing] == ["people/danny"]
    assert "content" not in listing[0]
    got = (await client.get("/api/memory/people/danny", headers=alice)).json()
    assert got["content"] == "likes tea"

    ok = await client.put("/api/memory/people/danny", headers={**alice, "If-Match": "1"},
                          json={"content": "v2", "description": "d"})
    assert ok.json()["version"] == 2
    stale = await client.put("/api/memory/people/danny", headers={**alice, "If-Match": '"1"'},
                             json={"content": "v3", "description": "d"})
    assert stale.status_code == 409
    missing = await client.put("/api/memory/nope", headers={**alice, "If-Match": "1"},
                               json={"content": "x", "description": "d"})
    assert missing.status_code == 409
    assert (await client.put("/api/memory/people/danny", headers=alice,
                             json={"content": "v3", "description": "d"})).json()["version"] == 3
    assert (await client.put("/api/memory/a", headers={**alice, "If-Match": "x"},
                             json={"content": "x", "description": "d"})).status_code == 422

    assert (await client.delete("/api/memory/people/danny", headers=alice)).status_code == 200
    assert (await client.delete("/api/memory/people/danny", headers=alice)).status_code == 404
    assert (await client.get("/api/memory/people/danny", headers=alice)).status_code == 404


async def test_memory_is_private_unless_shared_is_explicit(client, world):
    alice, bob = world.h("alice"), world.h("bob")
    assert (await client.put("/api/memory/preferences", headers=alice,
                             json={"content": "private", "description": "mine"})).status_code == 200
    assert (await client.get("/api/memory/preferences", headers=bob)).status_code == 404
    assert (await client.put("/api/memory/shared/household", headers=alice,
                             json={"content": "shared", "description": "ours"})).status_code == 200
    assert (await client.get("/api/memory/shared/household", headers=bob)
            ).json()["content"] == "shared"
    assert {item["name"] for item in (await client.get("/api/memory", headers=bob)).json()} == {
        "shared/household"}
    admin_key = {"Authorization": "Bearer test-admin-key-0123456789"}
    assert (await client.get("/api/memory", headers=admin_key)).status_code == 400


async def test_memory_name_validation(client, world):
    for bad in ("Bad", "a..b", "-x"):
        r = await client.put(f"/api/memory/{bad}", headers=world.h("alice"),
                             json={"content": "x", "description": "d"})
        assert r.status_code == 422, bad


# ---- automations -----------------------------------------------------------

async def make_automation(client, headers, **over):
    body = {"name": "digest", "prompt": "summarize", "schedule": "every 1h", **over}
    return await client.post("/api/automations", headers=headers, json=body)


async def test_automation_lifecycle(client, world):
    alice, bob, root = world.h("alice"), world.h("bob"), world.h("root")
    r = await make_automation(client, alice, notify=["alice"])
    assert r.status_code == 200
    a = r.json()
    assert a["created_by"] == "alice" and a["enabled"] and not a["one_shot"]
    assert a["next_run_at"] and a["thread_key"] == f"automation:{a['id']}"
    assert a["last_run"] is None and a["notify"] == ["alice"] and a["spec"] == "every 1h"

    assert (await client.get("/api/automations", headers=bob)).json() == []
    # Roles do not grant access to someone else's automation.
    assert (await client.patch(f"/api/automations/{a['id']}", headers=bob,
                               json={"name": "x"})).status_code == 404
    assert (await client.delete(f"/api/automations/{a['id']}", headers=bob)).status_code == 404
    assert (await client.post(f"/api/automations/{a['id']}/run", headers=bob)).status_code == 404

    p = await client.patch(f"/api/automations/{a['id']}", headers=alice,
                           json={"schedule": "at: 2999-01-01T00:00", "enabled": True})
    assert p.json()["one_shot"] is True and p.json()["next_run_at"].startswith("2999-01-01")
    assert (await client.patch(f"/api/automations/{a['id']}", headers=root,
                               json={"enabled": False})).status_code == 404
    assert (await client.patch(f"/api/automations/{a['id']}", headers=alice,
                               json={"schedule": "bogus"})).status_code == 422
    assert (await client.patch(f"/api/automations/{a['id']}", headers=alice,
                               json={"name": None})).status_code == 422
    assert (await client.patch("/api/automations/nope", headers=alice, json={})).status_code == 404

    assert (await client.delete(f"/api/automations/{a['id']}", headers=root)).status_code == 404
    assert (await client.delete(f"/api/automations/{a['id']}", headers=alice)).status_code == 200
    assert (await client.get("/api/automations", headers=alice)).json() == []


@pytest.mark.parametrize("body", [
    {"schedule": "whenever"}, {"schedule": "cron: * *"}, {"timezone": "Mars/Base"},
])
async def test_automation_validation(client, world, body):
    assert (await make_automation(client, world.h("alice"), **body)).status_code == 422


async def test_automation_run_now(client, world):
    a = (await make_automation(client, world.h("alice"))).json()
    before = a["next_run_at"]
    r1 = await client.post(f"/api/automations/{a['id']}/run", headers=world.h("alice"))
    r2 = await client.post(f"/api/automations/{a['id']}/run", headers=world.h("root"))
    assert r1.status_code == 200 and r2.status_code == 404
    events = await world.store.pending_events(a["thread_key"])
    assert len(events) == 1
    assert all(e.type == "automation.fired" and f"automation:{a['id']}:manual:" in e.idempotency_key
               for e in events)
    thread = await world.store.get_thread(a["thread_key"])
    assert thread.type is ThreadType.AUTOMATION and thread.owner_user_id == "alice"
    after = (await client.get("/api/automations", headers=world.h("alice"))).json()[0]
    assert after["next_run_at"] == before and after["last_run_at"] is None


async def test_automation_last_run(client, world):
    a = (await make_automation(client, world.h("alice"))).json()
    await world.store.put(
        Event("automation.fired", "t", {}, "k1", thread_key=a["thread_key"], user_id="alice"),
        ThreadSpec(a["thread_key"], ThreadType.AUTOMATION, Lane.BACKGROUND,
                   owner_user_id="alice", harness="codex"))
    (lease,) = await world.store.claim("w", Lane.BACKGROUND, 1, 60)
    run = Run("r1", a["thread_key"], lease.version, [], RunStatus.FAILED, "codex", "m",
              finished_at=utcnow(), error="boom")
    await world.store.create_run(lease, run)
    await world.store.finish_run(lease, run)
    assert (await client.get("/api/automations", headers=world.h("bob"))).json() == []
    last = (await client.get("/api/automations", headers=world.h("alice"))).json()[0]["last_run"]
    assert last["status"] == "failed" and last["error"] == "boom" and last["finished_at"]


# ---- usage -------------------------------------------------------------------

async def add_run(store, key, owner, run_id, usage, cost, model="m1", age_days=0):
    event = Event("chat.message", "t", {}, f"e-{run_id}", thread_key=key, user_id=owner)
    await store.put(
        event,
        ThreadSpec(key, ThreadType.CHAT if key.startswith("chat:") else ThreadType.GMAIL,
                   Lane.INTERACTIVE, owner_user_id=owner, harness="codex"))
    (lease,) = await store.claim(f"w-{run_id}", Lane.INTERACTIVE, 1, 60)
    run = Run(run_id, key, lease.version, [], RunStatus.COMPLETED, "codex", model,
              started_at=utcnow() - timedelta(days=age_days), usage=usage, cost_usd=cost)
    await store.create_run(lease, run)
    await store.finish_run(lease, run)
    await store.complete(lease, run_id, [event.id])


async def test_usage_aggregation_and_visibility(client, world):
    s = world.store
    await add_run(s, "chat:alice:c", "alice", "r1", TokenUsage(100, 40, 0, 10, 4), 0.5)
    await add_run(s, "chat:bob:c", "bob", "r2", TokenUsage(1000, 0, 0, 100, 0), None, "m2")
    await add_run(s, "gmail:inbox", None, "r3", TokenUsage(10, 0, 0, 1, 0), 0.25)
    await add_run(s, "chat:alice:old", "alice", "r4", TokenUsage(7, 0, 0, 7, 0), 1.0, age_days=60)

    alice = (await client.get("/api/usage", headers=world.h("alice"))).json()
    assert alice["totals"] == {
        "input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10,
        "reasoning_tokens": 4, "cost_usd": 0.5, "runs": 1}
    assert {t["thread_key"] for t in alice["by_thread"]} == {"chat:alice:c"}
    assert [m["model"] for m in alice["by_model"]] == ["m1"]
    assert len(alice["by_day"]) == 1 and alice["by_day"][0]["runs"] == 1

    root = (await client.get("/api/usage?days=90", headers=world.h("root"))).json()
    assert root["totals"]["runs"] == 0 and root["by_thread"] == []
    assert (await client.get("/api/usage?days=0", headers=world.h("root"))).status_code == 422


# ---- tools / connections -----------------------------------------------------

async def test_tools_decisions(client, world):
    tools = {t["name"]: t for t in (await client.get("/api/tools", headers=world.h("alice"))).json()}
    assert tools["read_it"] == {"name": "read_it", "action": "test.read", "risk": "read",
                                "description": "read", "decision": "deny"}
    assert tools["send_it"]["decision"] == "ask" and tools["send_it"]["risk"] == "write_external"


async def test_connections_scoping(client, world):
    from handler.runtime.types import Connection

    for owner in ("user:alice", "user:bob", "instance"):
        await world.store.put_connection(Connection(
            "google", owner, "local", secret_ref="SECRET", scopes=["mail"],
            last_used_at=utcnow()))
    mine = (await client.get("/api/connections", headers=world.h("alice"))).json()
    assert [c["owner"] for c in mine] == ["user:alice"]
    assert set(mine[0]) == {"id", "connector", "owner", "provider", "status", "scopes",
                            "last_used_at"}
    everyone = (await client.get("/api/connections", headers=world.h("root"))).json()
    assert everyone == []
    assert "SECRET" not in json.dumps(everyone)


# ---- uploads -----------------------------------------------------------------

async def test_upload_roundtrip_and_access(client, world):
    r = await client.post("/api/uploads", headers=world.h("alice"), files=[
        ("files", ("../../etc/pass wd.txt", b"hello", "text/plain")),
        ("files", ("pic.png", b"\x89PNG", "image/png"))])
    assert r.status_code == 200
    a, b = r.json()
    assert set(a) == {"id", "filename", "size", "content_type"}
    assert a["filename"] == "pass wd.txt" and a["size"] == 5 and a["content_type"] == "text/plain"
    stored = list((world.tmp_path / "uploads" / "alice" / a["id"]).iterdir())
    assert {p.name for p in stored} == {"pass wd.txt", "meta.json"}

    dl = await client.get(f"/api/uploads/{a['id']}", headers=world.h("alice"))
    assert dl.content == b"hello" and "attachment" in dl.headers["content-disposition"]
    assert (await client.get(f"/api/uploads/{a['id']}", headers=world.h("root"))).status_code == 404
    assert (await client.get(f"/api/uploads/{a['id']}", headers=world.h("bob"))).status_code == 404
    assert (await client.delete(f"/api/uploads/{a['id']}", headers=world.h("bob"))).status_code == 404

    assert len((await client.get("/api/uploads", headers=world.h("alice"))).json()) == 2
    assert (await client.get("/api/uploads", headers=world.h("bob"))).json() == []
    assert (await client.get("/api/uploads", headers=world.h("root"))).json() == []

    assert (await client.delete(f"/api/uploads/{a['id']}", headers=world.h("alice"))).status_code == 200
    assert (await client.get(f"/api/uploads/{a['id']}", headers=world.h("alice"))).status_code == 404


async def test_upload_limits_and_safety(client, world):
    big = await client.post("/api/uploads", headers=world.h("alice"), files=[
        ("files", ("ok.txt", b"x", "text/plain")),
        ("files", ("big.bin", b"x" * (MAX_FILE_BYTES + 1), "application/octet-stream"))])
    assert big.status_code == 413
    assert world.uploads.list() == []  # nothing from the failed request is kept
    many = await client.post("/api/uploads", headers=world.h("alice"),
                             files=[("files", (f"f{i}", b"x", "text/plain")) for i in range(11)])
    assert many.status_code == 422
    # ids are random hex; traversal-looking ids never resolve
    for bad in ("..", "%2e%2e%2falice", "x" * 32):
        assert (await client.get(f"/api/uploads/{bad}", headers=world.h("root"))).status_code == 404
    # admin key without X-Handler-User has no owner
    nokey = await client.post("/api/uploads", headers={"Authorization": "Bearer test-admin-key-0123456789"},
                              files=[("files", ("a.txt", b"x", "text/plain"))])
    assert nokey.status_code == 400


def test_sanitize_filename():
    from handler.runtime.uploads import sanitize_filename

    assert sanitize_filename("../../x.txt") == "x.txt"
    assert sanitize_filename("..\\..\\win.txt") == "win.txt"
    assert sanitize_filename("..") == "file" and sanitize_filename(None) == "file"
    assert sanitize_filename(".hidden") == "hidden"


# ---- logs ------------------------------------------------------------------

def write_log(world, entries):
    world.log_file.parent.mkdir(parents=True, exist_ok=True)
    world.log_file.write_text("".join(
        (e if isinstance(e, str) else json.dumps(e)) + "\n" for e in entries))


async def test_logs_tail_and_filters(client, world):
    write_log(world, [{"level": "INFO", "msg": f"line {i}"} for i in range(10)]
              + ["not json", {"level": "ERROR", "msg": "kaboom"}, {"level": "WARNING", "msg": "hmm"}])
    root = world.h("root")
    tail = (await client.get("/api/logs?lines=3", headers=root)).json()
    assert [e["msg"] for e in tail][-2:] == ["kaboom", "hmm"]
    errs = (await client.get("/api/logs?level=warning", headers=root)).json()
    assert [e["msg"] for e in errs] == ["kaboom", "hmm"]
    found = (await client.get("/api/logs?q=LINE 7", headers=root)).json()
    assert [e["msg"] for e in found] == ["line 7"]
    assert (await client.get("/api/logs?level=nope", headers=root)).status_code == 422
    assert (await client.get("/api/logs", headers=world.h("alice"))).status_code == 403


async def test_logs_missing_file(client, world):
    assert (await client.get("/api/logs", headers=world.h("root"))).json() == []


def test_configure_json_logging_file(tmp_path):
    import logging

    from handler.runtime.observability.logging import JsonFormatter, configure_json_logging

    path = tmp_path / "l" / "x.jsonl"
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        import io
        configure_json_logging("INFO", stream=io.StringIO(), file=path)
        logging.getLogger("t").info("hello", extra={"k": 1})
        for h in root.handlers:
            h.flush()
        entry = json.loads(path.read_text().splitlines()[-1])
        assert entry["msg"] == "hello" and entry["k"] == 1
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
