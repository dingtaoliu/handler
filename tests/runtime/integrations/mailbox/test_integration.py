"""The `mailbox` integration on a multi-instance host: wiring, API, isolation."""
import asyncio
from datetime import datetime, timezone

import pytest

from handler.runtime.integrations import default_registry
from handler.runtime.integrations.mailbox import MailboxIntegration
from handler.runtime.integrations.mailbox import transport as transport_module

from ...test_multi_instance import config
from ..test_crontal_integration import CRONTAL_ENV, instance_app, serving, snapshot
from .fake_mail import FakeMailServer, FakeTransport, make_mail

PASSWORD = "auth-code-Zq81"
MAILBOX = {"label": "Sales", "email": "sales@example.cn", "password": PASSWORD,
           "imap_host": "imap.example.cn", "imap_port": 993, "imap_security": "tls",
           "smtp_host": "smtp.example.cn", "smtp_port": 465, "smtp_security": "tls"}


@pytest.fixture
def fake(monkeypatch):
    server = FakeMailServer(username="sales@example.cn", password=PASSWORD)
    monkeypatch.setattr(transport_module, "StdlibTransport", lambda: FakeTransport(server))
    return server


def headers(name):
    return {"Authorization": f"Bearer {name}-admin"}


def integration(app) -> MailboxIntegration:
    return next(i for i in app.state.integrations.integrations if i.name == "mailbox")


def test_mailbox_is_registered():
    assert "mailbox" in default_registry.names()


async def test_enabling_on_bss_adds_mailboxes_and_keeps_everything_else(tmp_path, monkeypatch,
                                                                         fake):
    base = {**config(tmp_path), **CRONTAL_ENV}
    async with serving(base, monkeypatch) as (_, host, calls):
        legacy = {n: snapshot(instance_app(host, n), calls) for n in ("main", "bss")}
    env = {**base, "HANDLER_I_BSS__INTEGRATIONS": "crontal,mailbox"}
    async with serving(env, monkeypatch) as (_, host, calls):
        snaps = {n: snapshot(instance_app(host, n), calls) for n in ("main", "bss")}
    assert snaps["main"] == legacy["main"]  # HANDLER_INTEGRATIONS unset there
    bss, old = snaps["bss"], legacy["bss"]
    assert old["routes"] <= bss["routes"] and old["sinks"] == bss["sinks"]
    assert old["connector"] == bss["connector"] and old["tools"] <= bss["tools"]
    assert bss["tools"] - old["tools"] == {
        "mail_list_mailboxes", "mail_search", "mail_read", "mail_read_thread",
        "mail_get_attachment", "mail_draft_reply", "mail_send", "mail_remember",
        "mail_list_drafts", "mail_read_draft", "mail_update_draft",
        "mail_list_server_drafts", "mail_read_server_draft"}
    # The host app's mailbox routes are served to bss only (route group).
    assert bss["routes"] - old["routes"] == {"integration:mailbox"}


async def test_conversations_scope_messages_and_attachments_to_mailbox(tmp_path, monkeypatch, fake):
    env = {**config(tmp_path), "HANDLER_INTEGRATIONS": "mailbox"}
    async with serving(env, monkeypatch) as (client, host, _):
        admin = headers("main")
        for user_id in ("owner", "other"):
            user = await client.post("/api/users", headers=admin, json={
                "id": user_id, "display_name": user_id, "role": "member",
            })
            assert user.status_code == 201, user.text
        owner = {**admin, "X-Handler-User": "owner"}
        other = {**admin, "X-Handler-User": "other"}
        created = await client.post("/api/mailboxes", headers=admin, json={
            **MAILBOX, "owner_user_id": "owner", "wake_mode": "off",
        })
        mailbox_id = created.json()["id"]
        integration_ = integration(instance_app(host, "main"))
        mailbox = await integration_.mail_store.get_mailbox(mailbox_id)
        await integration_.poller.wait_idle()
        root = "<root@example.com>"
        raw = make_mail(message_id=root, date=datetime(2026, 11, 19, tzinfo=timezone.utc),
                        attachments=[("quote.txt", b"attachment content", "text/plain")])
        fake.deliver(raw)
        reply = make_mail(subject="Re: Quote request", in_reply_to=root, references=root,
                          sender=MAILBOX["email"], to="buyer@client.cn", body="Quote attached.",
                          date=datetime(2026, 11, 20, tzinfo=timezone.utc))
        fake.deliver(reply, folder="Sent")
        await integration_.service.poll(mailbox)

        listing = await client.get("/api/mailboxes/conversations", headers=owner)
        assert listing.status_code == 200, listing.text
        assert listing.headers["cache-control"] == "no-store"
        threads = listing.json()["threads"]
        assert len(threads) == 1 and threads[0]["message_count"] == 2
        assert threads[0]["snippet"].strip() == "Quote attached."
        assert [m["id"] for m in listing.json()["mailboxes"]] == [mailbox_id]
        assert (await client.get("/api/mailboxes/conversations", headers=other)).json()["threads"] == []
        for query in ("QUOTE REQUEST", "buyer@client.cn", "Buyer"):
            match = await client.get("/api/mailboxes/conversations", headers=owner, params={"q": query})
            assert match.json()["threads"] == threads
        for query in ("%", "_", "nonexistent"):
            empty = await client.get("/api/mailboxes/conversations", headers=owner, params={"q": query})
            assert empty.json()["threads"] == []

        path = f"/api/mailboxes/{mailbox_id}/conversations/{threads[0]['id']}"
        assert (await client.get(path, headers=other)).status_code == 404
        detail = await client.get(path, headers=owner)
        messages = detail.json()["messages"]
        assert [message["direction"] for message in messages] == ["in", "out"]
        assert messages[0]["attachments"] == [
            {"index": 0, "filename": "quote.txt", "size": len(b"attachment content")}
        ]
        attachment = f"/api/mailboxes/{mailbox_id}/messages/{messages[0]['id']}/attachments/0"
        assert (await client.get(attachment, headers=other)).status_code == 404
        download = await client.get(attachment, headers=owner)
        assert download.status_code == 200, download.text
        assert download.content == b"attachment content"
        assert download.headers["content-type"] == "application/octet-stream"


async def test_api_never_returns_secrets_and_instances_are_isolated(tmp_path, monkeypatch,
                                                                    fake):
    env = {**config(tmp_path), "HANDLER_INTEGRATIONS": "mailbox",
           "HANDLER_I_BSS__INTEGRATIONS": "mailbox"}
    async with serving(env, monkeypatch) as (client, host, _):
        bad = await client.post("/i/bss/api/mailboxes", headers=headers("bss"),
                                json={**MAILBOX, "imap_port": 0})
        assert bad.status_code == 422 and PASSWORD not in bad.text
        created = await client.post("/i/bss/api/mailboxes", headers=headers("bss"),
                                    json={**MAILBOX, "wake_mode": "off"})
        assert created.status_code == 201, created.text
        body = created.json()
        assert PASSWORD not in created.text and "secret_ref" not in body
        assert body["has_password"] is True and body["wake_mode"] == "off"
        mailbox_id = body["id"]

        tested = await client.post(f"/i/bss/api/mailboxes/{mailbox_id}/test",
                                   headers=headers("bss"))
        assert tested.json()["ok"] is True, tested.text
        assert tested.json()["imap"]["drafts_folder"] == "草稿箱"
        wrong = await client.post("/i/bss/api/mailboxes/test", headers=headers("bss"),
                                  json={**MAILBOX, "password": "not-it-777"})
        assert wrong.json()["ok"] is False and "not-it-777" not in wrong.text
        assert "authorization code" in wrong.json()["imap"]["error"] or \
            "login failed" in wrong.json()["imap"]["error"]

        bss_app, main_app = instance_app(host, "bss"), instance_app(host, "main")
        stored = await integration(bss_app).mail_store.list_mailboxes()
        secret = await integration(bss_app).service.store.get_secret(stored[0].secret_ref)
        assert secret and PASSWORD not in secret  # ciphertext at rest

        # Main has its own (empty) mailbox list and cannot reach bss's mailbox.
        listing = await client.get("/api/mailboxes", headers=headers("main"))
        assert listing.json()["mailboxes"] == []
        assert (await client.get(f"/api/mailboxes/{mailbox_id}",
                                 headers=headers("main"))).status_code == 404
        # And bss's admin key is not valid on main.
        assert (await client.get("/api/mailboxes", headers=headers("bss"))).status_code == 401

        # Ingest on bss: switch to triage, deliver, poll.
        poller = integration(bss_app).poller
        patched = await client.patch(f"/i/bss/api/mailboxes/{mailbox_id}",
                                     headers=headers("bss"), json={"wake_mode": "triage"})
        assert patched.json()["wake_mode"] == "triage" and PASSWORD not in patched.text
        await asyncio.sleep(0.1)
        await poller.wait_idle()  # a poll that read the mailbox as "off" has finished
        fake.deliver(make_mail(subject="New order"))
        # Saving the mailbox makes its poll due now (the host timer's row).
        await client.patch(f"/i/bss/api/mailboxes/{mailbox_id}", headers=headers("bss"),
                           json={"wake_mode": "triage"})
        bss_store = integration(bss_app).service.store
        for _ in range(250):  # the host timer's poll
            await asyncio.sleep(0.02)
            await poller.wait_idle()
            thread_rows = await bss_store._call(lambda db: [dict(r) for r in db.execute(
                "SELECT key, type FROM threads")])
            if thread_rows:
                break
        main_store = integration(main_app).service.store
        main_rows = await main_store._call(lambda db: [dict(r) for r in db.execute(
            "SELECT key FROM threads")])
        assert [r["type"] for r in thread_rows] == ["mail"]
        assert thread_rows[0]["key"].startswith(f"mail:{mailbox_id}:")
        assert main_rows == []

        detail = await client.get(f"/i/bss/api/mailboxes/{mailbox_id}", headers=headers("bss"))
        assert detail.json()["sync"][0]["folder"] == "INBOX"
        assert detail.json()["status"] == "active" and detail.json()["last_sync_at"]

        deleted = await client.delete(f"/i/bss/api/mailboxes/{mailbox_id}",
                                      headers=headers("bss"))
        assert deleted.json() == {"deleted": True}
        assert await bss_store.get_secret(stored[0].secret_ref) is None
        manifest = await client.get("/i/bss/api/ui/manifest", headers=headers("bss"))
        assert any(e["id"] == "mailboxes" for e in manifest.json()["entries"])
