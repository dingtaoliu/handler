"""Plans and input limits (real Postgres, multi-tenant Phase 3)."""
import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from functools import partial

import psycopg
import pytest

from handler.runtime import server
from handler.runtime.scheduler import Scheduler
from handler.runtime.settings import ModelName
from handler.runtime.store.postgres import PostgresStore
from handler.runtime.store.postgres_hub import PostgresHub
from handler.runtime.tenancy.limits import (
    OverLimit,
    LiveLimits,
    PlanLimits,
    parse_plan_table,
    period_of,
    resolve_limits,
)
from handler.runtime.types import Event, Lane, Schedule, ThreadSpec, ThreadType
from handler.runtime.web.email_ingest import sign_email

from .test_tenant_registry import (  # noqa: F401  (fixtures)
    PLATFORM,
    FakeHarness,
    RuntimeSpy,
    activate,
    admin,
    asleep,
    base_env,
    create,
    fake_harness,
    live_store,
    raw_email,
    rows,
    serve,
    slot,
    spy,
    state,
    until,
)

# The free plan's input limit, on a plan that also has the channels being tested.
FREE = {"plan": "free", "limits": {"monthly_inputs": 2, "at_limit": "hold", "email": True,
                                   "automations": True}}
MAIL_SECRETS = {"HANDLER_ADMIN_KEY": "admin-42", "HANDLER_EMAIL_SECRET": "mail-42",
                "HANDLER_WEBHOOK_SECRET": "hook-42"}


@pytest.fixture
async def tenant(pg_dsn, clock):
    hub = PostgresHub(pg_dsn)
    store = PostgresStore(pg_dsn, schema="handler_q", clock=clock, hub=hub)
    store.limits = LiveLimits(PlanLimits("free", monthly_inputs=3))
    try:
        await store.get_thread("x")
        yield store
    finally:
        await store.close()
        await hub.close()


def spec_for(kind, key):
    return ThreadSpec(key, kind, Lane.BACKGROUND, owner_user_id="u", harness="openai_agents")


def input_event(idem, event_type="chat.message", clock=None, thread_key="chat:u:1"):
    values = {"received_at": clock()} if clock else {}
    return Event(type=event_type, source="test", payload={"text": idem}, idempotency_key=idem,
                 thread_key=thread_key, user_id="u", **values)


CHAT = spec_for(ThreadType.CHAT, "chat:u:1")
EMAIL = spec_for(ThreadType.EMAIL, "email:a")


async def used(store, period):
    async with store._conn() as conn:
        row = await (await conn.execute(
            "SELECT used FROM input_usage WHERE period = %s", (period,))).fetchone()
    return row["used"] if row else 0


async def held(store):
    async with store._conn() as conn:
        cursor = await conn.execute(
            "SELECT e.idempotency_key, e.status FROM held_inputs h JOIN events e ON e.id = h.event_id "
            "ORDER BY h.seq")
        return [(r["idempotency_key"], r["status"]) for r in await cursor.fetchall()]


# --- counting --------------------------------------------------------------


async def test_parallel_puts_at_the_limit_count_exactly(tenant, clock):
    results = await asyncio.gather(*(
        tenant.put(input_event(f"k{n}"), CHAT) for n in range(40)))
    assert sum(r is True for r in results) == 3
    refused = [r for r in results if isinstance(r, OverLimit)]
    assert len(refused) == 37 and all(r.action == "refused" for r in refused)
    assert await used(tenant, period_of(clock())) == 3


async def test_duplicates_do_not_count(tenant, clock):
    event = input_event("same")
    assert await tenant.put(event, CHAT) is True
    results = await asyncio.gather(*(
        tenant.put(input_event("same"), CHAT) for _ in range(10)))
    assert results == [False] * 10
    assert await used(tenant, period_of(clock())) == 1


async def test_a_duplicate_of_a_held_input_stays_uncounted(tenant, clock):
    for n in range(3):
        assert await tenant.put(input_event(f"m{n}", "email.received"), EMAIL) is True
    outcome = await tenant.put(input_event("late", "email.received"), EMAIL)
    assert outcome and outcome.action == "held"
    assert await tenant.put(input_event("late", "email.received"), EMAIL) is False
    assert await held(tenant) == [("late", "held")]
    assert await used(tenant, period_of(clock())) == 3


@pytest.mark.parametrize("event_type", ["approval.resolved", "question.answered"])
async def test_internal_events_are_never_counted(tenant, clock, event_type):
    for n in range(10):
        assert await tenant.put(input_event(f"i{n}", event_type), CHAT) is True
    assert await used(tenant, period_of(clock())) == 0


async def test_events_with_no_thread_are_not_inputs(tenant, clock):
    assert await tenant.put(input_event("rejected", "email.received"), None) is True
    assert await used(tenant, period_of(clock())) == 0


async def test_no_limits_means_plain_booleans_and_no_counting(pg_dsn, clock):
    hub = PostgresHub(pg_dsn)
    store = PostgresStore(pg_dsn, schema="handler_n", clock=clock, hub=hub)
    try:
        for n in range(60):
            assert await store.put(input_event(f"k{n}"), CHAT) is True
        assert await store.put(input_event("k0"), CHAT) is False
        assert await used(store, period_of(clock())) == 0
    finally:
        await store.close()
        await hub.close()


async def test_sqlite_is_untouched(tmp_path, clock):
    from handler.runtime.store.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "s.db", clock=clock)
    try:
        for n in range(60):
            assert await store.put(input_event(f"k{n}"), CHAT) is True
    finally:
        await store.close()


async def test_a_paid_plan_is_not_limited(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        store = live_store(app)
        assert store.limits.current.monthly_inputs is None
        for n in range(55):
            assert await store.put(input_event(f"k{n}"), CHAT) is True


def test_limits_are_configurable_per_plan_and_per_tenant():
    table = parse_plan_table('{"free": {"monthly_inputs": 75}, "trial": {"monthly_inputs": 10}}')
    assert resolve_limits("free", {}, table).monthly_inputs == 75
    assert resolve_limits("trial", {}, table).monthly_inputs == 10
    assert resolve_limits("free", {"monthly_inputs": 9}, table).monthly_inputs == 9
    assert resolve_limits("paid", {}, table).monthly_inputs is None
    assert resolve_limits("unheard_of", {}, table).monthly_inputs == 75  # never unlimited
    assert resolve_limits("free", {}, parse_plan_table(None)).monthly_inputs == 50
    with pytest.raises(ValueError):
        parse_plan_table('{"free": {"monthly_input": 1}}')


# --- each input type -------------------------------------------------------


async def test_chat_over_limit_is_refused_with_usage_and_reset(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, **FREE)
        send = lambda n: client.post(
            "/i/m42/api/chat", headers={**admin("admin-42"), "X-Handler-User": "u"},
            json={"text": "hi", "client_message_id": f"c{n}"})
        await client.post("/i/m42/api/users", headers=admin("admin-42"),
                          json={"id": "u", "display_name": "U"})
        assert (await send(1)).status_code < 300
        assert (await send(2)).status_code < 300
        response = await send(3)
        assert response.status_code == 429
        body = response.json()
        assert body["error"] == "input_limit_reached" and body["plan"] == "free"
        assert body["usage"] == {"used": 2, "limit": 2}
        reset = datetime.fromisoformat(body["resets_at"])
        assert reset.day == 1 and reset.tzinfo is not None and reset > datetime.now(timezone.utc)
        assert body["message"]
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.events")[0][0] == 2  # refused: not stored
        limits = await client.get("/i/m42/api/limits/inputs", headers=admin("admin-42"))
        assert limits.json()["limited"] is True and limits.json()["used"] == 2


async def test_limits_endpoint_reports_no_limit_on_paid(base_env):
    async with serve(base_env) as (app, client):
        await create(client)
        response = await client.get("/i/m42/api/limits/inputs", headers=admin("admin-42"))
        assert response.json() == {"limited": False}


async def exhaust(store):
    limit = store.limits.current.monthly_inputs
    for n in range(limit):
        assert await store.put(input_event(f"fill{n}", "automation.fired",
                                           thread_key=f"automation:x{n}"),
                               spec_for(ThreadType.AUTOMATION, f"automation:x{n}")) is True


def consumed(dsn, idempotency_key):
    """The run that consumed this input, once one has."""
    found = rows(dsn, "SELECT consumed_by_run FROM handler_m42.events WHERE idempotency_key = %s",
                 idempotency_key)
    return found[0][0] if found else None


async def drained(app, pg_dsn, name="m42"):
    """Wait until the fill runs are done, so the tenant can stop."""
    schema = app.state.tenants._slots[name].tenant.schema_name
    await until(lambda: rows(pg_dsn, f"SELECT count(*) FROM {schema}.threads "
                                     "WHERE pending_since IS NOT NULL")[0][0] == 0)
    await until(lambda: not slot(app, name).runtime.app.state.lifecycle.dispatcher.active_count)


async def _available(*args):
    return True


def signed_email(body):
    timestamp, signature = sign_email("mail-42", body)
    return {"x-handler-timestamp": timestamp, "x-handler-signature": signature}


async def test_email_over_limit_is_stored_held_and_acknowledged(base_env, pg_dsn, monkeypatch):
    from handler.runtime.web import email_ingest
    from handler.runtime.mail.intake import GateResult

    async def accept(*args, **kwargs):
        return GateResult(accepted=True, reason=None, sender="buyer@example.com", auth=None,
                          trusted=False, scope="buyer@example.com")

    monkeypatch.setattr(email_ingest, "check_sender", accept)
    monkeypatch.setattr(email_ingest, "ai_identity_available", _available)
    async with serve(base_env) as (app, client):
        await create(client, **FREE, secrets=MAIL_SECRETS)
        await activate(client)
        store = live_store(app)
        await exhaust(store)
        body = raw_email("Over the limit")
        response = await client.post("/i/m42/ingest/email", content=body, headers=signed_email(body))
        assert response.status_code == 202 and response.json()["status"] == "held"
        assert rows(pg_dsn, "SELECT status FROM handler_m42.events WHERE type = 'email.received'") == [("held",)]
        # The fill inputs may run meanwhile (parsing the email in a reader takes
        # a moment); the held email must not.
        assert not any("Over the limit" in str(run) for run in FakeHarness.runs)


async def test_email_held_over_the_limit_is_later_processed(base_env, pg_dsn, monkeypatch):
    from handler.runtime.web import email_ingest
    from handler.runtime.mail.intake import GateResult

    async def accept(*args, **kwargs):
        return GateResult(accepted=True, reason=None, sender="buyer@example.com", auth=None,
                          trusted=False, scope="buyer@example.com")

    monkeypatch.setattr(email_ingest, "check_sender", accept)
    monkeypatch.setattr(email_ingest, "ai_identity_available", _available)
    async with serve(base_env) as (app, client):
        await create(client, **FREE, secrets=MAIL_SECRETS)
        await activate(client)
        store = live_store(app)
        await exhaust(store)
        body = raw_email("Later")
        assert (await client.post("/i/m42/ingest/email", content=body,
                                  headers=signed_email(body))).status_code == 202
        runs_before = rows(pg_dsn, "SELECT count(*) FROM handler_m42.runs")[0][0]
        store._clock = lambda: datetime.now(timezone.utc).replace(day=1) + timedelta(days=40)
        assert await store.release_held_inputs() == 1
        await until(lambda: rows(pg_dsn, "SELECT consumed_by_run FROM handler_m42.events "
                                         "WHERE type = 'email.received'")[0][0] is not None)
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.runs")[0][0] > runs_before


async def test_crontal_notification_over_limit_is_held_with_a_2xx(base_env, pg_dsn):
    from handler.runtime.web.ingest import sign

    async with serve(base_env) as (app, client):
        await create(client, **FREE, secrets=MAIL_SECRETS)
        await activate(client)
        await exhaust(live_store(app))
        note = b'{"id": 9, "recipient_user_id": 1, "entity_type": "rfq", "entity_id": 3}'
        response = await client.post("/i/m42/ingest/crontal", content=note,
                                     headers={"X-Crontal-Signature": sign("hook-42", note)})
        assert response.status_code == 202 and response.json()["held"] is True
        assert rows(pg_dsn, "SELECT status FROM handler_m42.events "
                            "WHERE type = 'crontal.notification'") == [("held",)]


async def test_mailbox_mail_over_limit_is_held(tenant):
    await exhaust(tenant)
    outcome = await tenant.put(input_event("mail1", "mail.received"), EMAIL)
    assert outcome and outcome.action == "held"


async def test_automation_over_limit_skips_with_one_notice_per_period(tenant, clock):
    await exhaust(tenant)
    schedule = Schedule(name="daily", prompt="p", spec="every 1h", next_run_at=clock(),
                        created_by="u", harness="openai_agents")
    await tenant.create_schedule(schedule)
    scheduler = Scheduler(tenant, clock=clock, default_harness="openai_agents")
    notices = []

    async def tell(sched, over):
        notices.append((sched.id, over.action))

    scheduler.on_skipped = tell
    for _ in range(3):
        assert await scheduler.tick() == 0
        clock.advance(3700)
    assert notices == [(schedule.id, "skipped")]
    assert (await tenant.get_schedule(schedule.id)).next_run_at > schedule.next_run_at
    assert rows_of_type(await tenant.list_events(), "automation.fired") == 3  # only the fill


def rows_of_type(events, event_type):
    return sum(1 for event, _ in events if event.type == event_type)


async def test_automation_notice_returns_next_period(tenant, clock):
    await exhaust(tenant)
    schedule = Schedule(name="daily", prompt="p", spec="every 1h", next_run_at=clock(),
                        created_by="u", harness="openai_agents")
    await tenant.create_schedule(schedule)
    scheduler = Scheduler(tenant, clock=clock, default_harness="openai_agents")
    notices = []

    async def tell(sched, over):
        notices.append(sched.id)

    scheduler.on_skipped = tell
    await scheduler.tick()
    clock.now = datetime(2026, 11, 1, tzinfo=timezone.utc)  # the next month, allowance back
    await tenant.update_schedule(Schedule(**{**(await tenant.get_schedule(schedule.id)).__dict__,
                                             "next_run_at": clock()}))
    await exhaust_again(tenant)
    await scheduler.tick()
    assert notices == [schedule.id, schedule.id]


async def exhaust_again(store):
    for n in range(store.limits.current.monthly_inputs):
        assert await store.put(input_event(f"again{n}", "automation.fired",
                                           thread_key=f"automation:y{n}"),
                               spec_for(ThreadType.AUTOMATION, f"automation:y{n}")) is True


async def test_host_sends_the_skip_notice_to_the_automation_owner(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, **FREE)
        await activate(client)
        store = live_store(app)
        await exhaust(store)
        await store.create_schedule(Schedule(
            name="daily", prompt="p", spec="every 1h", created_by="owner", harness="openai_agents",
            next_run_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        await drained(app, pg_dsn)
        await asleep(app)
        with psycopg.connect(pg_dsn) as conn:  # came due while it slept
            conn.execute("UPDATE handler_m42.schedules SET next_run_at = now()")
            conn.execute("UPDATE handler_control.tenant_state SET next_wake_at = now()")
        await until(lambda: rows(pg_dsn, "SELECT count(*) FROM handler_m42.notifications")[0][0] == 1,
                    timeout=20)
        text = rows(pg_dsn, "SELECT message FROM handler_m42.notifications")[0][0]
        assert "skipped" in text and "daily" in text


async def test_manual_automation_over_limit_gets_a_429(base_env):
    async with serve(base_env) as (app, client):
        # A limited plan that has both automations and their API (free has neither).
        await create(client, plan="free", limits={**FREE["limits"], "console": True})
        await activate(client)
        store = live_store(app)
        await exhaust(store)
        schedule = Schedule(name="m", prompt="p", spec="every 1h", created_by="owner",
                            harness="openai_agents", next_run_at=datetime.now(timezone.utc) + timedelta(hours=1))
        await store.create_schedule(schedule)
        response = await client.post(f"/i/m42/api/automations/{schedule.id}/run",
                                     headers=admin("admin-42"))
        assert response.status_code == 429 and response.json()["error"] == "input_limit_reached"


# --- release ---------------------------------------------------------------


async def fill_and_hold(store, count):
    await exhaust(store)
    for n in range(count):
        outcome = await store.put(input_event(f"held{n}", "email.received",
                                              thread_key=f"email:h{n}"),
                                  spec_for(ThreadType.EMAIL, f"email:h{n}"))
        assert outcome and outcome.action == "held"


async def test_release_oldest_first_counts_new_period_and_keeps_the_rest(tenant, clock):
    await fill_and_hold(tenant, 5)
    period = period_of(clock())
    assert await tenant.release_held_inputs() == 0  # the month is still used up
    clock.now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    assert await tenant.release_held_inputs() == 3
    assert await used(tenant, period_of(clock())) == 3
    assert await used(tenant, period) == 3
    assert await held(tenant) == [("held3", "held"), ("held4", "held")]
    for n in range(3):
        assert (await tenant.get_thread(f"email:h{n}")).pending_since is not None
    assert await tenant.get_thread("email:h3") is None
    assert await tenant.release_held_inputs() == 0  # allowance gone again
    clock.now = datetime(2026, 12, 2, tzinfo=timezone.utc)
    assert await tenant.release_held_inputs() == 2
    assert await held(tenant) == []


async def test_released_inputs_are_not_counted_twice(tenant, clock):
    await fill_and_hold(tenant, 1)
    clock.now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    await asyncio.gather(*(tenant.release_held_inputs() for _ in range(10)))
    assert await used(tenant, period_of(clock())) == 1
    assert await held(tenant) == []


async def test_several_held_inputs_on_one_thread_share_one_run(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, **FREE)
        await activate(client)
        store = live_store(app)
        await exhaust(store)
        spec = spec_for(ThreadType.EMAIL, "email:same")
        for n in range(2):
            await store.put(input_event(f"h{n}", "email.received", thread_key="email:same"), spec)
        response = await client.put("/platform/tenants/42", headers=PLATFORM,
                                    json={"plan": "paid", "limits": {}})
        assert response.status_code == 200
        await until(lambda: consumed(pg_dsn, "h0") and consumed(pg_dsn, "h1"))
        assert consumed(pg_dsn, "h0") == consumed(pg_dsn, "h1")  # one turn read both


async def test_held_inputs_do_not_block_deactivation_and_wake_at_reset(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, **FREE)
        await activate(client)
        await fill_and_hold(live_store(app), 1)
        await drained(app, pg_dsn)
        await asleep(app)  # asserts the stop succeeded
        now = datetime.now(timezone.utc)
        first = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=timezone.utc)
        assert state(pg_dsn) == (None, first)


async def test_a_tenant_without_the_new_tables_can_still_be_stopped_and_swept(tenant, pg_dsn):
    async with tenant._conn() as conn:
        await conn.execute("DROP TABLE held_inputs")
    async with tenant._conn() as conn:  # a sleeping tenant from before this release
        from handler.runtime.store.postgres import earliest_work_at

        assert await earliest_work_at(conn, None) is None


async def test_sleeping_tenant_is_woken_for_its_reset_and_runs_held_work(base_env, pg_dsn, monkeypatch):
    next_month = datetime.now(timezone.utc).replace(day=1) + timedelta(days=40)
    async with serve(base_env) as (app, client):
        await create(client, **FREE)
        await activate(client)
        await fill_and_hold(live_store(app), 1)
        await drained(app, pg_dsn)
        await asleep(app)
        monkeypatch.setattr(server, "PostgresStore",
                            partial(server.PostgresStore, clock=lambda: next_month))
        with psycopg.connect(pg_dsn) as conn:  # the reset has arrived
            conn.execute("UPDATE handler_control.tenant_state SET next_wake_at = now() "
                         "WHERE name = 'm42'")
        await until(lambda: slot(app).runtime is not None and consumed(pg_dsn, "held0"))
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.held_inputs")[0][0] == 0


async def test_upgrade_releases_held_work_promptly_on_a_running_tenant(base_env, pg_dsn, spy):
    FakeHarness.gate = asyncio.Event()  # the tenant stays busy, so no restart can happen
    async with serve(base_env) as (app, client):
        await create(client, **FREE)
        await activate(client)
        store = live_store(app)
        await store.put(input_event("busy", "chat.message"), CHAT)
        await until(lambda: FakeHarness.runs)
        await fill_and_hold_after_busy(store)
        response = await client.put("/platform/tenants/42", headers=PLATFORM, json={"plan": "paid", "limits": {}})
        assert response.status_code == 200
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.held_inputs")[0][0] == 0
        assert spy.starts["m42"] == 1  # no restart was needed
        assert store.limits.current.plan == "paid"
        FakeHarness.gate.set()


async def fill_and_hold_after_busy(store):
    for n in range(1):
        await store.put(input_event("fill", "chat.message", thread_key="chat:u:2"),
                        spec_for(ThreadType.CHAT, "chat:u:2"))
    outcome = await store.put(input_event("over", "email.received", thread_key="email:o"),
                              spec_for(ThreadType.EMAIL, "email:o"))
    assert outcome and outcome.action == "held"


async def test_upgrade_wakes_a_sleeping_tenant_and_releases_its_work(base_env, pg_dsn, spy):
    async with serve(base_env) as (app, client):
        await create(client, **FREE)
        await activate(client)
        await fill_and_hold(live_store(app), 1)
        await drained(app, pg_dsn)
        await asleep(app)
        await client.put("/platform/tenants/42", headers=PLATFORM, json={"plan": "paid", "limits": {}})
        await until(lambda: consumed(pg_dsn, "held0"))
        assert spy.starts["m42"] == 2


# --- free plan cost limits and channels -------------------------------------


async def test_free_runs_use_the_fixed_model_and_limits(base_env, monkeypatch):
    base_env["HANDLER_RT_MODEL"] = "an-expensive-default"
    seen = []

    class Recording(FakeHarness):
        async def run(self, req):
            seen.append(req)
            async for event in FakeHarness.run(self, req):
                yield event

    monkeypatch.setattr(server, "OpenAIAgentsHarness", Recording)
    async with serve(base_env) as (app, client):
        await create(client, plan="free", limits={"max_run_tokens": 1234, "max_run_turns": 5})
        await activate(client)
        store = live_store(app)
        chosen = Event(type="chat.message", source="t",
                       payload={"text": "hi", "model": "an-expensive-model", "reasoning_effort": "high"},
                       idempotency_key="k", thread_key="chat:u:1", user_id="u")
        await store.put(chosen, ThreadSpec("chat:u:1", ThreadType.CHAT, Lane.INTERACTIVE, owner_user_id="u",
                                           harness="openai_agents", model="another-expensive-model"))
        await until(lambda: seen)
    assert seen[0].model == ModelName.GPT_6_1_TERRA.value
    assert seen[0].limits.max_tokens == 1234 and seen[0].limits.max_turns == 5
    assert seen[0].bounded_task_models == {}


async def test_a_paid_run_keeps_its_model_and_has_no_cost_limits(base_env, monkeypatch):
    base_env["HANDLER_RT_MODEL"] = "the-default-model"
    seen = []

    class Recording(FakeHarness):
        async def run(self, req):
            seen.append(req)
            async for event in FakeHarness.run(self, req):
                yield event

    monkeypatch.setattr(server, "OpenAIAgentsHarness", Recording)
    async with serve(base_env) as (app, client):
        await create(client)
        await activate(client)
        chosen = Event(type="chat.message", source="t", payload={"text": "hi", "model": "chosen"},
                       idempotency_key="k", thread_key="chat:u:1", user_id="u")
        await live_store(app).put(chosen, CHAT)
        await until(lambda: seen)
    assert seen[0].limits.max_tokens is None and seen[0].limits.max_turns is None
    assert seen[0].model == "chosen"


async def test_a_harness_reporting_usage_at_the_end_keeps_its_answer(base_env, monkeypatch, caplog):
    """Codex reports usage only when the turn is over: nothing can be stopped by then,
    so the real answer is kept and the overspend is logged, not claimed as a stop."""
    from handler.runtime.harness.base import RUN_LIMIT_NOTICE, RunEvent, RunEventKind
    from handler.runtime.types import TokenUsage

    interrupted = []

    class EndOfTurnUsage(FakeHarness):
        name = "codex"

        async def run(self, req):
            yield RunEvent(RunEventKind.SESSION, {"session_id": "s"})
            yield RunEvent(RunEventKind.USAGE, {"model": "m"},
                           usage=TokenUsage(input_tokens=900, output_tokens=200))
            yield RunEvent(RunEventKind.ASSISTANT_TEXT, {"text": "The full answer."})
            yield RunEvent(RunEventKind.FINAL, {"text": "The full answer.", "session_id": "s"})

        async def interrupt(self, run_id):
            interrupted.append(run_id)

    monkeypatch.setattr(server, "CodexHarness", lambda *args, **kwargs: EndOfTurnUsage())
    monkeypatch.setattr(server, "codex_binary", lambda: "codex")
    async with serve(base_env) as (app, client):
        await create(client, limits={"max_run_tokens": 1000})  # paid: free has no Codex
        await activate(client)
        store = live_store(app)
        with caplog.at_level("WARNING", logger="handler.runtime.run"):
            await store.put(Event(type="chat.message", source="t", payload={"text": "hi"},
                                  idempotency_key="k", thread_key="chat:u:2", user_id="u"),
                            ThreadSpec("chat:u:2", ThreadType.CHAT, Lane.INTERACTIVE,
                                       owner_user_id="u", harness="codex"))
            await until(lambda: rows(base_env["DATABASE_URL"], "SELECT count(*) FROM handler_m42.runs "
                                                               "WHERE status = 'completed'")[0][0] == 1)
        texts = [b.get("text") for m in await store.list_messages("chat:u:2") for b in m.blocks]
    assert "The full answer." in texts and RUN_LIMIT_NOTICE not in texts
    assert interrupted == []
    overspent = [r for r in caplog.records if r.getMessage() == "run went past its plan's token limit"]
    assert len(overspent) == 1 and overspent[0].tokens == 1100 and overspent[0].limit == 1000


async def test_connecting_a_mailbox_on_a_free_tenant_is_refused(base_env):
    async with serve(base_env) as (app, client):
        await create(client, plan="free")
        response = await client.post("/i/m42/api/mailboxes", headers=admin("admin-42"), json={})
        assert response.status_code == 403
        assert response.json()["error"] == "plan_requires_upgrade"
        assert response.json()["feature"] == "mailbox"


async def test_connecting_gmail_on_a_free_tenant_is_refused(base_env):
    async with serve(base_env) as (app, client):
        await create(client, plan="free")
        response = await client.post("/i/m42/api/connections/google/start", headers=admin("admin-42"))
        assert response.status_code == 403
        assert response.json()["feature"] == "gmail"


@pytest.mark.parametrize("integrations", [["mailbox"], ["google"], ["gmail"]])
async def test_platform_refuses_polling_integrations_on_free(base_env, integrations):
    async with serve(base_env) as (app, client):
        response = await client.put("/platform/tenants/7", headers=PLATFORM, json={
            "plan": "free", "integrations": integrations})
        assert response.status_code == 409, response.text


async def test_unknown_limit_keys_are_refused(base_env):
    async with serve(base_env) as (app, client):
        response = await client.put("/platform/tenants/7", headers=PLATFORM, json={
            "limits": {"monthly_input": 3}})
        assert response.status_code == 409


# --- spooling email a tenant cannot take -----------------------------------


async def post_mail(client, path, body=None, secret="mail-42"):
    body = body if body is not None else raw_email("Spool me")
    timestamp, signature = sign_email(secret, body)
    return await client.post(path, content=body, headers={
        "x-handler-timestamp": timestamp, "x-handler-signature": signature,
        "x-handler-envelope-from": "buyer%40example.com",
        "x-handler-envelope-to": "contact%40crontal.ai"})


def spooled(dsn):
    return rows(dsn, "SELECT tenant, length(body), headers ? 'x-handler-envelope-from' "
                     "FROM handler_control.spooled_email")


async def test_mail_to_a_disabled_tenant_is_spooled_and_replayed_on_enable(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        await activate(client)
        await client.post("/platform/tenants/42/disable", headers=PLATFORM)
        response = await post_mail(client, "/i/m42/ingest/email")
        assert response.status_code == 202 and response.json() == {"status": "spooled"}
        assert len(spooled(pg_dsn)) == 1 and spooled(pg_dsn)[0][2] is True
        assert (await post_mail(client, "/i/m42/ingest/email")).status_code == 202  # repeat
        assert len(spooled(pg_dsn)) == 1
        await client.post("/platform/tenants/42/enable", headers=PLATFORM)
        await until(lambda: len(spooled(pg_dsn)) == 0)
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.events "
                            "WHERE type = 'email.received'")[0][0] == 1


async def test_mail_during_a_failed_activation_is_spooled_and_replayed(base_env, pg_dsn, spy, monkeypatch):
    monkeypatch.setattr("handler.runtime.tenancy.host.ACTIVATION_RETRY_SECONDS", 0.2)
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        await activate(client)
        await asleep(app)
        spy.fail_next.add("m42")
        response = await post_mail(client, "/i/m42/ingest/email")
        assert response.status_code == 202 and response.json()["status"] == "spooled"
        await until(lambda: len(spooled(pg_dsn)) == 0)
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.events "
                            "WHERE type = 'email.received'")[0][0] == 1


async def test_mail_during_shutdown_is_spooled_and_replayed_after_restart(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        await activate(client)
        app.state.tenants._closing = True
        response = await post_mail(client, "/i/m42/ingest/email")
        assert response.status_code == 202 and response.json()["status"] == "spooled"
        other = await client.get("/i/m42/api/me", headers=admin("admin-42"))
        assert other.status_code == 503  # only mail is kept; other requests retry
        app.state.tenants._closing = False
    assert len(spooled(pg_dsn)) == 1
    async with serve(base_env) as (app, client):
        await until(lambda: len(spooled(pg_dsn)) == 0)
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.events "
                            "WHERE type = 'email.received'")[0][0] == 1


async def test_spooled_mail_keeps_the_root_alias(base_env, pg_dsn):
    env = {**base_env, "HANDLER_INSTANCES": "main", "HANDLER_ADMIN_KEY": "main-admin",
           "HANDLER_EMAIL_SECRET": "mail-42", "HANDLER_DB_SCHEMA": "handler_crontal"}
    async with serve(env) as (app, client):
        await until(lambda: "main" in app.state.tenants._slots)
        app.state.tenants._closing = True
        response = await post_mail(client, "/ingest/email")
        assert response.status_code == 202
        app.state.tenants._closing = False
    assert spooled(pg_dsn)[0][0] == "main"


async def test_an_invalid_signature_is_never_stored(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        await client.post("/platform/tenants/42/disable", headers=PLATFORM)
        response = await post_mail(client, "/i/m42/ingest/email", secret="wrong")
        assert response.status_code == 401
        assert spooled(pg_dsn) == []


async def test_a_stale_timestamp_is_never_stored(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        await client.post("/platform/tenants/42/disable", headers=PLATFORM)
        body = raw_email()
        timestamp, signature = sign_email("mail-42", body, timestamp=1_000)
        response = await client.post("/i/m42/ingest/email", content=body, headers={
            "x-handler-timestamp": timestamp, "x-handler-signature": signature})
        assert response.status_code == 401 and spooled(pg_dsn) == []


async def test_oversized_mail_is_refused_not_stored(base_env, pg_dsn, monkeypatch):
    monkeypatch.setattr("handler.runtime.tenancy.host.MAX_EMAIL_BYTES", 100)
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        await client.post("/platform/tenants/42/disable", headers=PLATFORM)
        response = await post_mail(client, "/i/m42/ingest/email", body=b"x" * 500)
        assert response.status_code == 413 and spooled(pg_dsn) == []


async def test_only_the_email_route_is_spooled(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        await client.post("/platform/tenants/42/disable", headers=PLATFORM)
        response = await client.post("/i/m42/ingest/crontal", content=b"{}")
        assert response.status_code == 404 and spooled(pg_dsn) == []


async def test_a_tenant_with_no_email_secret_gets_the_normal_refusal(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client)
        await client.post("/platform/tenants/42/disable", headers=PLATFORM)
        assert (await post_mail(client, "/i/m42/ingest/email")).status_code == 404
        assert spooled(pg_dsn) == []


async def test_an_unreachable_database_leaves_only_a_503(base_env, pg_dsn, monkeypatch):
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        await client.post("/platform/tenants/42/disable", headers=PLATFORM)

        async def down(*args):
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(app.state.registry, "spool_email", down)
        response = await post_mail(client, "/i/m42/ingest/email")
        assert response.status_code == 503


async def test_replay_goes_through_the_gate_and_does_not_double_count(base_env, pg_dsn, monkeypatch):
    from handler.runtime.web import email_ingest
    from handler.runtime.mail.intake import GateResult

    async def accept(*args, **kwargs):
        return GateResult(accepted=True, reason=None, sender="buyer@example.com", auth=None,
                          trusted=False, scope="buyer@example.com")

    monkeypatch.setattr(email_ingest, "check_sender", accept)
    monkeypatch.setattr(email_ingest, "ai_identity_available", _available)
    async with serve(base_env) as (app, client):
        await create(client, **FREE, secrets=MAIL_SECRETS)
        await activate(client)
        await exhaust(live_store(app))
        await client.post("/platform/tenants/42/disable", headers=PLATFORM)
        body = raw_email("Replay me")
        assert (await post_mail(client, "/i/m42/ingest/email", body=body)).status_code == 202
        await client.post("/platform/tenants/42/enable", headers=PLATFORM)
        await until(lambda: len(spooled(pg_dsn)) == 0)
        assert rows(pg_dsn, "SELECT status FROM handler_m42.events WHERE type='email.received'") == [("held",)]
        period = period_of(datetime.now(timezone.utc))
        assert rows(pg_dsn, "SELECT used FROM handler_m42.input_usage WHERE period=%s", period) == [(2,)]
        # The same mail delivered again in the normal way is a duplicate, uncounted.
        await post_mail(client, "/i/m42/ingest/email", body=body)
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.events WHERE type='email.received'")[0][0] == 1
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.held_inputs")[0][0] == 1


async def test_a_refused_replay_stays_spooled_for_a_later_try(base_env, pg_dsn, monkeypatch):
    from handler.runtime.web import email_ingest

    def unparseable(raw):
        raise ValueError("cannot parse")

    monkeypatch.setattr(email_ingest, "parse_email", unparseable)
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        await client.post("/platform/tenants/42/disable", headers=PLATFORM)
        await post_mail(client, "/i/m42/ingest/email")
        await client.post("/platform/tenants/42/enable", headers=PLATFORM)
        await until(lambda: rows(pg_dsn, "SELECT attempts FROM handler_control.spooled_email")[0][0] >= 1)
        assert len(spooled(pg_dsn)) == 1


# --- task outcomes on a hub (merged from main) -------------------------------


async def test_task_outcomes_are_scoped_per_tenant_on_a_hub(pg_dsn, clock):
    hub = PostgresHub(pg_dsn)
    a = PostgresStore(pg_dsn, schema="handler_oa", clock=clock, hub=hub)
    b = PostgresStore(pg_dsn, schema="handler_ob", clock=clock, hub=hub)
    try:
        await asyncio.gather(a.get_thread("x"), b.get_thread("x"))
        await a.bind_task_run("run", "chat:u:1", "task-a")
        assert (await a.task_outcome_for_run("run"))["id"] == "task-a"
        assert await b.task_outcome_for_run("run") is None
        assert rows(pg_dsn, "SELECT count(*) FROM handler_oa.task_outcomes")[0][0] == 1
        assert rows(pg_dsn, "SELECT count(*) FROM handler_ob.task_outcomes")[0][0] == 0
    finally:
        await asyncio.gather(a.close(), b.close())
        await hub.close()
