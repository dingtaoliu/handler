import httpx
import pytest
from fastapi import FastAPI

from handler.runtime.auth import Authenticator
from handler.runtime.context import RunContext
from handler.runtime.gateway import approvals_router

CTX = RunContext(run_id="run1", thread_key="chat:u:1", user_id="u1", acting_as="u1")


@pytest.fixture
async def client(gateway, store):
    app = FastAPI()
    # The admin key never touches the users store, so the gateway store stands in.
    app.include_router(approvals_router(gateway, store, Authenticator(store, "test-admin-key")))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 headers={"Authorization": "Bearer test-admin-key"}) as c:
        yield c


async def test_list_get_approve_flow(client, gateway, tools):
    pending = (await gateway.call(CTX, "send_it", {"text": "m"})).content
    aid = pending["approval_id"]
    listed = (await client.get("/api/approvals", params={"status": "pending"})).json()
    assert [a["id"] for a in listed] == [aid]
    assert listed[0]["status"] == "pending" and "T" in listed[0]["created_at"]
    assert (await client.get(f"/api/approvals/{aid}")).json()["tool"] == "send_it"
    assert (await client.get("/api/approvals/nope")).status_code == 404
    assert (await client.get("/api/approvals", params={"status": "bad"})).status_code == 422
    by_thread = {"status": "pending", "thread_key": CTX.thread_key}
    assert [a["id"] for a in (await client.get("/api/approvals", params=by_thread)).json()] \
        == [aid]
    elsewhere = {"status": "pending", "thread_key": "chat:other"}
    assert (await client.get("/api/approvals", params=elsewhere)).json() == []

    done = await client.post(f"/api/approvals/{aid}/approve", json={"decided_by": "boss"})
    assert done.status_code == 200 and done.json()["status"] == "executed"
    again = await client.post(f"/api/approvals/{aid}/approve", json={"decided_by": "boss"})
    assert again.status_code == 409
    assert len(tools.calls) == 1
    assert (await client.get("/api/approvals", params={"status": "pending"})).json() == []


async def test_deny_and_409_and_404(client, gateway):
    aid = (await gateway.call(CTX, "send_it", {"text": "m"})).content["approval_id"]
    denied = await client.post(f"/api/approvals/{aid}/deny",
                               json={"decided_by": "boss", "reason": "no"})
    assert denied.status_code == 200 and denied.json()["status"] == "denied"
    assert (await client.post(f"/api/approvals/{aid}/deny",
                              json={"decided_by": "b"})).status_code == 409
    assert (await client.post("/api/approvals/zzz/approve",
                              json={"decided_by": "b"})).status_code == 404


async def test_audit_endpoint(client, gateway):
    await gateway.call(CTX, "echo", {"text": "a"})
    await gateway.call(RunContext(run_id="other", thread_key="t"), "echo", {"text": "b"})
    assert len((await client.get("/api/audit")).json()) == 2
    rows = (await client.get("/api/audit", params={"run_id": "run1"})).json()
    assert len(rows) == 1 and rows[0]["decision"] == "allow"
