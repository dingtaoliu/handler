"""Merchant-wide Crontal events: one input for all of a merchant's users."""
import asyncio
import json

from handler.runtime.web.ingest import sign

from .test_plan_limits import MAIL_SECRETS
from .test_tenant_registry import (  # noqa: F401  (fixtures)
    FakeHarness,
    admin,
    base_env,
    create,
    fake_harness,
    rows,
    serve,
    until,
)


def merchant_event(event_id=7, entity_id=3):
    return {"id": event_id, "scope": "merchant", "recipient_user_id": None, "type": "rfq.received",
            "entity_type": "rfq", "entity_id": entity_id, "data": {"title": "316L tube"},
            "created_at": "2026-11-12T10:00:00Z"}


def note(note_id, recipient, entity_id=3):
    return {"id": note_id, "recipient_user_id": recipient, "type": "rfq.received",
            "entity_type": "rfq", "entity_id": entity_id, "data": {}, "created_at": "2026-11-12T10:00:00Z"}


async def post(client, body):
    raw = json.dumps(body).encode()
    return await client.post("/i/m42/ingest/crontal", content=raw,
                             headers={"X-Crontal-Signature": sign("hook-42", raw)})


def counted(dsn):
    found = rows(dsn, "SELECT coalesce(sum(used), 0) FROM handler_m42.input_usage")
    return found[0][0]


async def test_a_merchant_event_is_one_input_one_thread_and_a_retry_is_a_duplicate(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, plan="free", limits={"monthly_inputs": 10}, secrets=MAIL_SECRETS)
        first = await post(client, merchant_event())
        assert first.status_code == 202 and first.json() == {"thread_key": "crontal:rfq:3",
                                                              "duplicate": False}
        again = await post(client, merchant_event())
        assert again.status_code == 200 and again.json()["duplicate"] is True
        assert rows(pg_dsn, "SELECT idempotency_key, user_id FROM handler_m42.events") == [
            ("crontal:merchant:7", None)]
        assert rows(pg_dsn, "SELECT key, owner_user_id FROM handler_m42.threads") == [
            ("crontal:rfq:3", None)]
        assert counted(pg_dsn) == 1
        await until(lambda: FakeHarness.runs)  # the shared thread runs like any other


async def test_per_user_notes_are_unchanged(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, plan="free", limits={"monthly_inputs": 10}, secrets=MAIL_SECRETS)
        for user in ("5", "6"):
            assert (await post(client, note(int(user) * 10, user))).status_code == 202
        assert sorted(rows(pg_dsn, "SELECT idempotency_key, user_id FROM handler_m42.events")) == [
            ("crontal:50", "5"), ("crontal:60", "6")]
        assert rows(pg_dsn, "SELECT owner_user_id FROM handler_m42.threads") == [("5",)]
        assert counted(pg_dsn) == 2


async def test_merchant_and_note_ids_never_collide(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        assert (await post(client, note(7, "5"))).status_code == 202
        assert (await post(client, merchant_event(7))).json()["duplicate"] is False
        without_entity = {**merchant_event(7), "entity_type": None, "entity_id": None}
        response = await post(client, without_entity)
        assert response.json()["thread_key"] == "crontal:notification:merchant-7"


async def test_an_unknown_scope_is_refused(base_env):
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        assert (await post(client, {**merchant_event(), "scope": "everyone"})).status_code == 422
        assert (await post(client, {k: v for k, v in note(1, "5").items()
                                    if k != "recipient_user_id"})).status_code == 422  # as before


async def test_a_merchant_thread_is_visible_to_every_user_through_the_proxy(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, secrets=MAIL_SECRETS)
        for user in ("5", "6"):
            await client.post("/i/m42/api/users", headers=admin("admin-42"),
                              json={"id": user, "display_name": user, "role": "member"})
        await post(client, merchant_event())
        await until(lambda: rows(pg_dsn, "SELECT count(*) FROM handler_m42.runs "
                                         "WHERE status = 'completed'")[0][0] == 1)
        for user in ("5", "6"):
            as_user = {**admin("admin-42"), "X-Handler-User": user}  # how Crontal proxies
            threads = (await client.get("/i/m42/api/threads", headers=as_user)).json()
            assert "crontal:rfq:3" in [t["key"] for t in threads]
            messages = await client.get("/i/m42/api/threads/crontal:rfq:3/messages", headers=as_user)
            assert messages.status_code == 200 and messages.json()
            tasks = (await client.get("/i/m42/api/tasks", headers=as_user)).json()
            assert [t["thread_key"] for t in tasks["items"]] == ["crontal:rfq:3"]


async def test_a_merchant_event_over_the_limit_is_held_once(base_env, pg_dsn):
    async with serve(base_env) as (app, client):
        await create(client, plan="free", limits={"monthly_inputs": 0}, secrets=MAIL_SECRETS)
        response = await post(client, merchant_event())
        assert response.status_code == 202 and response.json()["held"] is True
        assert (await post(client, merchant_event())).json()["duplicate"] is True
        assert rows(pg_dsn, "SELECT count(*) FROM handler_m42.held_inputs")[0][0] == 1
        await asyncio.sleep(0.1)
        assert FakeHarness.runs == []
