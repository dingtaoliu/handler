"""Load: 50 registry tenants chat at once (role-split-design.md, "Proof before release").

Every tenant sends one chat at the same moment; ten of them read a document through
the reader pool (child processes) during the run, and one paid tenant is also busy
with a flood of background Crontal events. The fake harness takes about 0.2 s a run.

Done when: every run completes; Handler never holds more than 12 connections; the
gate's rules hold (the host cap, each plan's cap, chats before background work, so
the busy tenant's flood delays no other tenant's chat); and afterwards the host is
idle again: nothing due, no context cached, no task per tenant, no reader process.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict

import psycopg
import pytest

from handler.runtime import documents, server
from handler.runtime.context import current_tenant
from handler.runtime.harness.base import RunEvent, RunEventKind
from handler.runtime.types import TokenUsage
from handler.runtime.web.ingest import sign
from tests.runtime import reader_fakes

from .test_idle_tenants import FAST, HOST_TASKS, per_tenant, task_names
from .test_shared_connections import ConnectionWatch
from .test_tenant_registry import PLATFORM, base_env, serve, until  # noqa: F401 (fixture)

TENANTS = 50
READERS = 10  # tenants whose chat reads a document
FLOOD = 30  # background events of the busy tenant
RUN_SECONDS = 0.2
MAX_RUNS = 6
log = logging.getLogger("load")


class Recorder:
    def __init__(self):
        self.runs: list[dict] = []
        self.running: dict[str, int] = defaultdict(int)
        self.peak_tenant: dict[str, int] = defaultdict(int)
        self.total = 0
        self.peak_total = 0


RECORD = Recorder()


class LoadHarness:
    name = "openai_agents"

    def __init__(self, *args, **kwargs):
        pass

    async def run(self, req):
        tenant = current_tenant()
        entry = {"tenant": tenant, "start": time.monotonic(), "prompt": req.prompt,
                 "kind": "chat" if "chat from" in req.prompt else "background"}
        RECORD.running[tenant] += 1
        RECORD.total += 1
        RECORD.peak_tenant[tenant] = max(RECORD.peak_tenant[tenant], RECORD.running[tenant])
        RECORD.peak_total = max(RECORD.peak_total, RECORD.total)
        try:
            yield RunEvent(RunEventKind.SESSION, {"session_id": "s"})
            if "READ" in req.prompt:
                entry["read"] = await documents.capped_read(
                    "read_file", reader_fakes.text, str(LoadHarness.document))
            await asyncio.sleep(RUN_SECONDS)
            yield RunEvent(RunEventKind.USAGE, {"model": "fake"},
                           usage=TokenUsage(input_tokens=1))
            yield RunEvent(RunEventKind.ASSISTANT_TEXT, {"text": "ok"})
            yield RunEvent(RunEventKind.FINAL, {"text": "ok", "session_id": "s"})
        finally:
            RECORD.running[tenant] -= 1
            RECORD.total -= 1
            entry["end"] = time.monotonic()
            RECORD.runs.append(entry)

    async def interrupt(self, run_id):
        pass


@pytest.fixture
def load_env(base_env, tmp_path, monkeypatch):
    global RECORD
    RECORD = Recorder()
    monkeypatch.setattr(server, "OpenAIAgentsHarness", LoadHarness)

    def no_codex():
        raise FileNotFoundError("test has no codex")

    monkeypatch.setattr(server, "codex_binary", no_codex)
    LoadHarness.document = tmp_path / "quote.txt"
    LoadHarness.document.write_text("304 2B sheet, 2 mm, 5 t")
    base_env.update(FAST)
    base_env.update({"HANDLER_GLOBAL_MAX_RUNS": str(MAX_RUNS),
                     "HANDLER_PLAN_RUN_CAPS": "free:1,paid:3",
                     "HANDLER_TENANT_CACHE_SIZE": "32",  # production's default, < 50
                     "HANDLER_DOC_READ_MAX_CONCURRENT": "1",  # production's default
                     "HANDLER_READER_IDLE_SECONDS": "0.5"})
    yield base_env
    documents.configure_readers({})


async def test_fifty_tenants_chat_at_once(load_env, pg_dsn):
    merchants = list(range(1, TENANTS + 1))
    busy = merchants[0]
    plans = {m: "paid" if m <= 10 else "free" for m in merchants}
    async with ConnectionWatch(pg_dsn) as watch:
        async with serve(load_env) as (app, client):
            services = app.state.services
            keys = {}
            for m in merchants:
                created = await client.put(f"/platform/tenants/{m}", headers=PLATFORM, json={
                    "plan": plans[m], "integrations": ["crontal"],
                    "secrets": {"HANDLER_WEBHOOK_SECRET": f"hook-{m}"}})
                assert created.status_code == 201, created.text
                keys[m] = created.json()["admin_key"]
                user = await client.post(f"/i/m{m}/api/users",
                                         headers={"Authorization": f"Bearer {keys[m]}"},
                                         json={"id": "u", "display_name": "U"})
                assert user.status_code == 201, user.text
            # The busy tenant's flood is queued first: background work, one thread each.
            for n in range(FLOOD):
                note = json.dumps({"id": 1000 + n, "recipient_user_id": "u",
                                   "entity_type": "rfq", "entity_id": n}).encode()
                hook = await client.post(f"/i/m{busy}/ingest/crontal", content=note, headers={
                    "X-Crontal-Signature": sign(f"hook-{busy}", note)})
                assert hook.status_code == 202, hook.text
            posted: dict[str, float] = {}

            async def chat(m):
                text = f"chat from m{m}" + (" READ" if 1 < m <= READERS + 1 else "")
                posted[f"m{m}"] = time.monotonic()
                response = await client.post(f"/i/m{m}/api/chat", headers={
                    "Authorization": f"Bearer {keys[m]}", "X-Handler-User": "u"},
                    json={"text": text, "conversation_id": "load"})
                assert response.status_code == 200, response.text

            started = time.monotonic()
            await asyncio.gather(*(chat(m) for m in merchants))
            await until(lambda: len(RECORD.runs) == TENANTS + FLOOD, timeout=180)
            elapsed = time.monotonic() - started

            def stored():  # each run's end is written just after the harness returns
                with psycopg.connect(pg_dsn) as conn:
                    return sum(conn.execute(
                        f"SELECT count(*) FROM handler_m{m}.runs WHERE status = 'completed'"
                    ).fetchone()[0] for m in merchants) == TENANTS + FLOOD

            await until(stored, timeout=30)
            # Every run completed, as recorded by each tenant's store too.
            with psycopg.connect(pg_dsn) as conn:
                for m in merchants:
                    statuses = conn.execute(
                        f"SELECT status, count(*) FROM handler_m{m}.runs GROUP BY 1").fetchall()
                    expected = FLOOD + 1 if m == busy else 1
                    assert dict(statuses) == {"completed": expected}, (m, statuses)

            # Afterwards: idle again.
            def idle():
                with psycopg.connect(pg_dsn) as conn:
                    due = conn.execute(
                        "SELECT count(*) FROM handler_control.tenant_state "
                        "WHERE next_wake_at IS NOT NULL OR next_delivery_at IS NOT NULL"
                    ).fetchone()[0]
                pool = documents.limiter.reader_pool()
                return (due == 0 and len(services.cache) == 0 and not services.cache._closing
                        and not pool._idle and not pool._running)

            await until(idle, timeout=60)
            names_now = task_names()

    chats = [r for r in RECORD.runs if r["kind"] == "chat"]
    background = [r for r in RECORD.runs if r["kind"] == "background"]
    assert len(chats) == TENANTS and len(background) == FLOOD
    # Documents went through the reader pool, read in its child process.
    assert sum(r.get("read") == "text" for r in chats) == READERS
    # The gate: the host cap and each plan's cap.
    assert RECORD.peak_total <= MAX_RUNS
    for tenant, peak in RECORD.peak_tenant.items():
        assert peak <= (3 if plans[int(tenant[1:])] == "paid" else 1), (tenant, peak)
    # Chats before background work: once chats were waiting, the busy tenant's flood
    # took no slot ahead of them (a run already going may finish).
    waits = {r["tenant"]: r["start"] - posted[r["tenant"]] for r in chats}
    last_chat_start = max(r["start"] for r in chats)
    first_chat_post = min(posted.values())
    ahead = [r for r in background if first_chat_post + 0.5 < r["start"] < last_chat_start]
    assert ahead == [], f"{len(ahead)} background runs started while chats waited"
    others = [w for t, w in waits.items() if t != f"m{busy}"]
    # About ceil(50 / 6) rounds of a 0.2 s run, plus claim and context-build time.
    assert max(others) < 10, f"slowest chat waited {max(others):.2f} s"
    # Connections: the shared pool (11) plus one LISTEN, however many tenants.
    assert watch.samples > 10
    assert 2 <= watch.peak <= 12, f"peak {watch.peak}"
    # Idle cost back to nothing.
    assert not [n for n in names_now if per_tenant(n)], names_now
    assert len(names_now) < 25, names_now
    log.warning("load: %d tenants, %d runs in %.1f s; chat wait median %.2f s, max %.2f s; "
                "peak runs %d, peak connections %d; busy tenant's flood finished %.1f s "
                "after the last chat started", TENANTS, len(RECORD.runs), elapsed,
                sorted(others)[len(others) // 2], max(others), RECORD.peak_total, watch.peak,
                max(r["end"] for r in background) - last_chat_start)
    assert HOST_TASKS - {"tenant-scan"} <= set(names_now) | {"tenant-scan"}
