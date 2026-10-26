"""UID sync, backfill, UIDVALIDITY resets, threading and wake rules."""
import datetime as dt
import json

from handler.runtime.integrations.mailbox.service import thread_key
from handler.runtime.types import ThreadType

from .fake_mail import make_mail

UTC = dt.timezone.utc


def now():
    return dt.datetime.now(UTC)


async def test_first_sync_backfills_recent_mail_without_waking(env):
    env.server.deliver(make_mail(subject="ancient"), when=now() - dt.timedelta(days=40))
    env.server.deliver(make_mail(subject="last week"), when=now() - dt.timedelta(days=7))
    env.server.deliver(make_mail(subject="today"))
    mailbox = await env.add_mailbox(backfill_days=14)
    result = await env.service.poll(mailbox)
    assert result.new == 2 and result.woke == 0
    rows = await env.mail_store.search([mailbox.id])
    assert sorted(r["subject"] for r in rows) == ["last week", "today"]
    assert await env.events() == []
    state = await env.mail_store.get_folder(mailbox.id, "INBOX")
    assert state["last_uid"] == 3 and state["uidvalidity"] == 1000


async def test_new_mail_after_backfill_wakes_the_agent_once(env):
    env.server.deliver(make_mail(subject="old"))
    mailbox = await env.add_mailbox()
    await env.service.poll(mailbox)
    raw = make_mail(subject="New RFQ", attachments=[("规格书.pdf", b"%PDF-1.4 x", "application/pdf")])
    env.server.deliver(raw)
    result = await env.service.poll(mailbox)
    assert (result.new, result.woke) == (1, 1)
    (event,) = await env.events()
    payload = json.loads(event["payload"])
    assert event["type"] == "mail.received" and event["status"] == "routed"
    assert event["thread_key"].startswith(f"mail:{mailbox.id}:c")
    assert payload["wake_mode"] == "triage" and payload["notify_user_ids"] == ["7"]
    (attachment,) = payload["attachments"]
    assert attachment["filename"] == "规格书.pdf"
    thread = await env.store.get_thread(event["thread_key"])
    assert thread.type == ThreadType.MAIL and thread.owner_user_id == "7"
    saved = next(env.workspaces.iterdir()) / attachment["path"]
    assert saved.read_bytes() == b"%PDF-1.4 x"
    assert f"/{mailbox.id}/" in attachment["path"]
    # Polling again changes nothing: UIDs are remembered.
    again = await env.service.poll(mailbox)
    assert again.fetched == 0 and len(await env.events()) == 1


async def test_uidvalidity_reset_rebuilds_mapping_without_duplicates(env):
    mailbox = await env.add_mailbox()
    await env.service.poll(mailbox)
    env.server.deliver(make_mail(subject="first", message_id="<m1@x>"))
    env.server.deliver(make_mail(subject="second", message_id="<m2@x>"))
    await env.service.poll(mailbox)
    assert len(await env.events()) == 2
    env.server.renumber()  # UIDVALIDITY 1000 -> 1001, UIDs shift by 100
    env.server.deliver(make_mail(subject="during reset", message_id="<m3@x>"))
    result = await env.service.poll(mailbox)
    assert result.duplicates == 2 and result.new == 1
    first = await env.mail_store.find_message(mailbox.id, "m1@x")
    assert first["uidvalidity"] == 1001 and first["uid"] == 101
    events = await env.events()
    # Mail that arrived during the renumbering (after the last sync) still wakes.
    assert [json.loads(e["payload"])["subject"] for e in events] == [
        "first", "second", "during reset"]
    state = await env.mail_store.get_folder(mailbox.id, "INBOX")
    assert state["uidvalidity"] == 1001


async def test_threading_by_headers_then_subject(env):
    mailbox = await env.add_mailbox()
    await env.service.poll(mailbox)
    env.server.deliver(make_mail(subject="304 pipe quote", message_id="<root@client.cn>",
                                 sender="Buyer <buyer@client.cn>"))
    env.server.deliver(make_mail(subject="Re: 304 pipe quote", message_id="<r1@client.cn>",
                                 in_reply_to="<root@client.cn>",
                                 references="<root@client.cn>"))
    # A reply whose client dropped the headers: matched by subject + participant.
    env.server.deliver(make_mail(subject="回复: 304 pipe quote", message_id="<r2@client.cn>",
                                 sender="buyer@client.cn"))
    # Same subject from an unrelated sender: a separate conversation.
    env.server.deliver(make_mail(subject="Re: 304 pipe quote", message_id="<z@other.cn>",
                                 sender="someone@other.cn"))
    # A child arriving before its (never seen) parent still groups by the root id.
    env.server.deliver(make_mail(subject="Re: steel", message_id="<k2@y>",
                                 references="<k0@y> <k1@y>", sender="k@y.cn"))
    env.server.deliver(make_mail(subject="Re: steel", message_id="<k3@y>",
                                 references="<k0@y> <k2@y>", sender="k@y.cn"))
    await env.service.poll(mailbox)
    rows = {r["message_id"]: r["conversation_id"]
            for r in await env.mail_store.search([mailbox.id], limit=50)}
    assert rows["root@client.cn"] == rows["r1@client.cn"] == rows["r2@client.cn"]
    assert rows["z@other.cn"] != rows["root@client.cn"]
    assert rows["k2@y"] == rows["k3@y"]
    keys = {e["thread_key"] for e in await env.events()}
    assert thread_key(mailbox.id, rows["root@client.cn"]) in keys and len(keys) == 3


async def test_outbound_and_off_mode_never_wake(env):
    mailbox = await env.add_mailbox()
    await env.service.poll(mailbox)
    env.server.deliver(make_mail(subject="our own", sender="Sales <SALES@example.cn>"))
    env.server.deliver(make_mail(subject="sent copy", sender="x@y.cn"), folder="Sent")
    mailbox.folders = ["INBOX", "Sent"]
    await env.mail_store.put_mailbox(mailbox)
    await env.service.poll(mailbox)
    assert await env.events() == []
    mailbox.wake_mode = "off"
    await env.mail_store.put_mailbox(mailbox)
    env.server.deliver(make_mail(subject="ingest only"))
    result = await env.service.poll(mailbox)
    assert result.new == 1 and result.woke == 0 and await env.events() == []
    found = await env.mail_store.search([mailbox.id], text="ingest only")
    assert found and found[0]["direction"] == "in"
    directions = {r["subject"]: r["direction"] for r in await env.mail_store.search([mailbox.id])}
    assert directions["our own"] == "out" and directions["sent copy"] == "out"


async def test_wake_rate_limit_and_large_messages(env):
    env.service.max_wakes_per_hour = 2
    env.service.max_message_bytes = 2000
    mailbox = await env.add_mailbox()
    await env.service.poll(mailbox)
    for i in range(3):
        env.server.deliver(make_mail(subject=f"burst {i}"))
    env.server.deliver(make_mail(subject="huge", attachments=[("big.bin", b"x" * 5000,
                                                                "application/octet-stream")]))
    result = await env.service.poll(mailbox)
    assert result.new == 4 and result.woke == 2
    (huge,) = await env.mail_store.search([mailbox.id], subject="huge")
    assert huge["attachments"] == []  # headers only; fetched on demand


async def test_poll_is_bounded_per_cycle(env, monkeypatch):
    from handler.runtime.integrations.mailbox import service as service_module

    monkeypatch.setattr(service_module, "MAX_MESSAGES_PER_POLL", 3)
    for i in range(7):
        env.server.deliver(make_mail(subject=f"m{i}"))
    mailbox = await env.add_mailbox()
    first = await env.service.poll(mailbox)
    assert first.new == 3 and first.folders["INBOX"]["pending"] == 4
    await env.service.poll(mailbox)
    third = await env.service.poll(mailbox)
    assert third.new == 1 and len(await env.mail_store.search([mailbox.id], limit=50)) == 7
    assert await env.events() == []  # all backfill


async def test_poll_marks_nothing_read_and_never_writes_flags(env):
    env.server.deliver(make_mail(subject="a"))
    mailbox = await env.add_mailbox()
    await env.service.poll(mailbox)
    assert env.server.folders["INBOX"].messages[1][2] == ""  # flags untouched
