"""Idle tenants hold no task and cost no tenant-scoped query (steps 2 and 3).

Postgres's own statement log is the witness, so no code path can hide a query. With
50 idle tenants, env-defined or in the registry, the host has only its own loops
(timer, claim loop, outbox loop, MCP, workspace backstop): no dispatcher, outbox,
scheduler, poller or runtime task per tenant, no cached context once the TTL passes,
and its only statements read the control schema.
"""
import asyncio
import re

import httpx
import psycopg

from handler.runtime import server

from .conftest import SERVER_LOG
from .test_shared_connections import host_env, no_codex  # noqa: F401  (fixture)
from .test_tenant_registry import (  # noqa: F401  (fixtures)
    activate, base_env, create, fake_harness, serve, until,
)

TENANTS = 50
HOST_TASKS = {"host-mcp", "host-timer", "host-runner", "host-outbox", "workspace-backstop",
              "tenant-scan"}
FAST = {"HANDLER_TIMER_POLL_SECONDS": "0.1", "HANDLER_CLAIM_POLL_SECONDS": "0.1",
        "HANDLER_OUTBOX_POLL_SECONDS": "0.1", "HANDLER_TENANT_CACHE_TTL_SECONDS": "0.5",
        "HANDLER_TENANT_CACHE_SIZE": "64"}


def per_tenant(name: str) -> bool:
    """Task names v65 and step 2 gave per-tenant work; none may exist for any tenant."""
    if name in HOST_TASKS:
        return False
    return (name in ("dispatcher", "outbox", "scheduler", "housekeeping", "workspace-sync",
                     "crontal-connector")
            or name.startswith(("tenant-", "instance-", "activate-", "deactivate-", "run:",
                                "due:", "replay-", "context-"))
            or name.endswith(":mailbox-poller"))


def task_names() -> list[str]:
    return [task.get_name() for task in asyncio.all_tasks()]


def log_statements(pg_dsn):
    dbname = pg_dsn.rsplit("dbname=", 1)[1]
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f'ALTER DATABASE "{dbname}" SET log_statement = \'all\'')
    log = SERVER_LOG["path"]

    def since(offset):
        with open(log) as handle:
            handle.seek(offset)
            return handle.read()

    return log, since


_STATEMENT = re.compile(r"LOG:  (?:statement|execute [^:]*): (.*)")


def statements(text: str) -> list[str]:
    return [m.group(1).strip() for m in _STATEMENT.finditer(text)]


def only_control_reads(text: str) -> list[str]:
    """Every statement in the window; each must be a host loop's control-schema read
    (the timer's claim is an UPDATE ... RETURNING that finds no due row)."""
    found = statements(text)
    for statement in found:
        if statement in ("BEGIN", "COMMIT", "ROLLBACK"):
            continue
        assert "handler_control." in statement, statement
        assert "search_path" not in statement, statement
        assert not re.search(r'\bhandler_(?!control)\w+\.', statement), statement
    return found


async def settle(pg_dsn, services, names, timeout=120.0):
    """Every tenant looked at once (boot) and written back; every context expired."""
    def idle():
        with psycopg.connect(pg_dsn) as conn:
            due = conn.execute(
                "SELECT count(*) FROM handler_control.tenant_state WHERE name = ANY(%s) "
                "AND (next_wake_at IS NOT NULL OR next_delivery_at IS NOT NULL)",
                (names,)).fetchone()[0]
        return due == 0 and len(services.cache) == 0 and not services.cache._closing

    await until(idle, timeout)


async def test_fifty_idle_env_tenants_have_no_tasks_or_tenant_queries(tmp_path, pg_dsn, no_codex):
    names = ["main", *(f"t{n}" for n in range(TENANTS - 1))]
    env = {**host_env(tmp_path, pg_dsn, names), **FAST,
           "HANDLER_INTEGRATIONS": "mailbox", "HANDLER_SECRET_KEY": "mail-key"}
    for name in names[1:]:
        env[f"HANDLER_I_{name.upper()}__HANDLER_SECRET_KEY"] = f"mail-key-{name}"
        env[f"HANDLER_I_{name.upper()}__INTEGRATIONS"] = "mailbox"
    log, since = log_statements(pg_dsn)
    host = server.create_host(server._parse_args(["serve"], env), env)
    async with host.router.lifespan_context(host):
        services = host.state.services
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host),
                                     base_url="http://h") as client:
            assert (await client.get("/i/t3/api/mailboxes",
                                     headers={"Authorization": "Bearer t3-admin"})).status_code == 200
        await settle(pg_dsn, services, names)
        names_now = task_names()
        offset = log.stat().st_size
        await asyncio.sleep(1.5)  # about 15 passes of each host loop
        seen = since(offset)
        cached = len(services.cache)
    assert cached == 0  # an idle tenant costs at most its cache entry, here none
    assert not [n for n in names_now if per_tenant(n)], names_now
    assert {n for n in names_now if n in HOST_TASKS} == HOST_TASKS - {"tenant-scan"}
    assert len(names_now) < 25, names_now  # nothing grows with the tenant count
    found = only_control_reads(seen)
    assert any("due_work" in s for s in found)  # the timer really polled
    assert any("next_wake_at" in s for s in found)  # so did the claim loop
    assert any("next_delivery_at" in s for s in found)  # and the outbox loop


async def test_fifty_idle_registry_tenants_have_no_tasks_or_tenant_queries(base_env, pg_dsn):
    base_env.update(FAST)
    log, since = log_statements(pg_dsn)
    async with serve(base_env) as (app, client):
        services = app.state.services
        merchants = list(range(100, 100 + TENANTS))
        for merchant in merchants:
            await create(client, merchant=merchant)
        await activate(client, merchants[0])
        await settle(pg_dsn, services, [f"m{m}" for m in merchants])
        names_now = task_names()
        offset = log.stat().st_size
        await asyncio.sleep(1.5)
        seen = since(offset)
    assert not [n for n in names_now if per_tenant(n)], names_now
    assert HOST_TASKS <= set(names_now)
    assert len(names_now) < 25, names_now
    found = only_control_reads(seen)
    assert any("tenant_state" in s for s in found) and any("due_work" in s for s in found)
    # The loops name the served tenants as a parameter ({m100,...}); a tenant-scoped
    # statement would name its schema as an identifier.
    for marker in ('"handler_m1', "handler_m100.", "FROM schedules", "FROM approvals",
                   "FROM threads", "FROM deliveries"):
        assert marker not in seen, marker


async def test_a_due_row_fires_for_a_registry_tenant_with_no_context(base_env, pg_dsn):
    from datetime import timedelta

    from handler.runtime.types import Schedule, utcnow

    base_env.update(FAST)
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        store = app.state.services.cache.cached("m42").store
        schedule = Schedule("soon", "p", "every 1h", next_run_at=utcnow() + timedelta(seconds=1))
        await store.create_schedule(schedule)
        app.state.services.cache.invalidate("m42")
        await app.state.services.cache.settle()
        with psycopg.connect(pg_dsn) as conn:  # only the timer's row can start it
            conn.execute("UPDATE handler_control.tenant_state SET next_wake_at = NULL")

        def fired():
            with psycopg.connect(pg_dsn) as conn:
                return conn.execute("SELECT count(*) FROM handler_m42.events "
                                    "WHERE type = 'automation.fired'").fetchone()[0]

        await until(fired, timeout=10)
