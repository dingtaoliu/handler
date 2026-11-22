"""Idle tenants hold no time-driven task and cost no tenant-scoped query (step 2).

Postgres's own statement log is the witness, so no code path can hide a query.
"""
import asyncio

import httpx
import psycopg

from handler.runtime import server

from .conftest import SERVER_LOG
from .test_shared_connections import host_env, no_codex  # noqa: F401  (fixture)
from .test_tenant_registry import (  # noqa: F401  (fixtures)
    activate, asleep, base_env, create, fake_harness, serve, until,
)

# Per-tenant tasks step 2 removed (their names in v65); none may exist for any tenant.
# ("pool-N-scheduler" is psycopg_pool's own task, one per pool.)
def gone(name: str) -> bool:
    return name in ("scheduler", "housekeeping", "workspace-sync") or name.endswith(
        ":mailbox-poller")


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


async def test_env_defined_idle_tenants_have_no_timer_tasks_or_queries(tmp_path, pg_dsn, no_codex):
    names = ["main", *(f"t{n}" for n in range(7))]
    env = host_env(tmp_path, pg_dsn, names)
    env.update({"HANDLER_TIMER_POLL_SECONDS": "0.1", "HANDLER_INTEGRATIONS": "mailbox",
                "HANDLER_SECRET_KEY": "mail-key"})
    for name in names[1:]:
        env[f"HANDLER_I_{name.upper()}__HANDLER_SECRET_KEY"] = f"mail-key-{name}"
        env[f"HANDLER_I_{name.upper()}__INTEGRATIONS"] = "mailbox"
    log, since = log_statements(pg_dsn)
    host = server.create_host(server._parse_args(["serve"], env), env)
    async with host.router.lifespan_context(host):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host),
                                     base_url="http://h") as client:
            assert (await client.get("/i/t3/api/mailboxes",
                                     headers={"Authorization": "Bearer t3-admin"})).status_code == 200
        await asyncio.sleep(1.0)  # start-up work (claims, backfills) settles
        names_now = task_names()
        offset = log.stat().st_size
        await asyncio.sleep(1.5)  # about 15 timer polls
        seen = since(offset)
    assert names_now.count("host-timer") == 1 and names_now.count("workspace-backstop") == 1
    assert not [n for n in names_now if gone(n)], names_now
    assert "due_work" in seen  # the host's one timer really polled
    for marker in ("search_path TO", "FROM schedules", "FROM approvals", "FROM questions",
                   "FROM mail_accounts", "FROM settings", "held_inputs"):
        assert marker not in seen, marker


async def test_registry_idle_tenants_have_no_timer_tasks_or_queries(base_env, pg_dsn):
    base_env["HANDLER_TIMER_POLL_SECONDS"] = "0.1"
    log, since = log_statements(pg_dsn)
    async with serve(base_env) as (app, client):
        for merchant in (42, 43, 44):
            await create(client, merchant=merchant)
        await activate(client, 42)
        await asleep(app, "m42")
        await asyncio.sleep(0.3)
        names_now = task_names()
        offset = log.stat().st_size
        await asyncio.sleep(1.5)
        seen = since(offset)
    assert names_now.count("host-timer") == 1
    assert not [n for n in names_now if gone(n)], names_now
    assert "due_work" in seen and "tenant_state" in seen
    # The timer's claim names the served schemas as a parameter ({handler_m42,...});
    # a tenant-scoped statement would name a schema as an identifier.
    for marker in ('"handler_m4', "handler_m42.", "search_path TO", "FROM schedules",
                   "FROM approvals"):
        assert marker not in seen, marker


async def test_a_due_row_wakes_a_sleeping_registry_tenant_and_fires(base_env, pg_dsn):
    from datetime import timedelta

    from handler.runtime.types import Schedule, utcnow

    base_env["HANDLER_TIMER_POLL_SECONDS"] = "0.1"
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        store = app.state.tenants._slots["m42"].runtime.app.state.lifecycle.store
        schedule = Schedule("soon", "p", "every 1h", next_run_at=utcnow() + timedelta(seconds=1))
        await store.create_schedule(schedule)
        await asleep(app)
        # The tenant scan's next_wake_at is pushed far out: only the timer can wake it.
        with psycopg.connect(pg_dsn) as conn:
            conn.execute("UPDATE handler_control.tenant_state SET next_wake_at = NULL")

        def fired():
            with psycopg.connect(pg_dsn) as conn:
                return conn.execute("SELECT count(*) FROM handler_m42.events "
                                    "WHERE type = 'automation.fired'").fetchone()[0]

        await until(fired, timeout=10)
