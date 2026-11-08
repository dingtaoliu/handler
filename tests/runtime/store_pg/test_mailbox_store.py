"""MailStore on both backends: accounts, folder state, threading refs, search."""
from handler.runtime.integrations.mailbox.config import Mailbox
from handler.runtime.integrations.mailbox.store import MailStore


def mailbox(**kw):
    values = dict(label="S", email="s@x.cn", username="s@x.cn", imap_host="i.x.cn",
                  imap_port=993, imap_security="tls", smtp_host="m.x.cn", smtp_port=465,
                  smtp_security="tls")
    values.update(kw)
    return Mailbox(**values)


def row(mid, conv, **kw):
    values = dict(id=f"mm_{mid}", account_id="mbx_1", conversation_id=conv, folder="INBOX",
                  uidvalidity=7, uid=4_000_000_000, message_id=mid, subject="报价 304",
                  from_addr="li@client.cn", from_name="李", to_addrs=[{"address": "s@x.cn"}],
                  cc_addrs=[], reply_to=[], in_reply_to=[], refs=[],
                  date="2026-10-20T00:00:00+00:00", direction="in", body_text="请报价 100% 钢管",
                  attachments=[{"index": 0, "filename": "清单.xlsx"}])
    values.update(kw)
    return values


async def test_mail_store_round_trip(domain_store):
    store = MailStore(domain_store)
    box = mailbox(id="mbx_1", secret_ref="mailbox:mbx_1:password")
    await store.put_mailbox(box)
    loaded = await store.get_mailbox("mbx_1")
    assert loaded.email == "s@x.cn" and loaded.folders == ["INBOX"]
    await store.record_sync("mbx_1", status="error", error="boom", synced=False)
    await store.record_sync("mbx_1", status="active", error=None, synced=True)
    loaded = await store.get_mailbox("mbx_1")
    assert loaded.last_error is None and loaded.last_sync_at

    await store.put_folder("mbx_1", "INBOX", uidvalidity=4_294_967_295, last_uid=0,
                           backfill_until_uid=10, backfill_since=None, wake_after=None)
    await store.advance_folder("mbx_1", "INBOX", 5)
    await store.advance_folder("mbx_1", "INBOX", 3)  # never goes back
    assert (await store.get_folder("mbx_1", "INBOX"))["last_uid"] == 5

    conv = {"subject": "报价 304", "norm_subject": "报价 304", "participants": ["li@client.cn"],
            "last_message_at": "2026-10-20T00:00:00+00:00"}
    await store.add_message(row("a@x", "c1"), conversation=conv, refs=["a@x"])
    await store.add_message(row("b@x", "c1", date="2026-10-21T00:00:00+00:00"),
                            conversation={**conv, "last_message_at": "2026-10-21T00:00:00+00:00"},
                            refs=["b@x", "a@x"])
    assert await store.conversation_for_refs("mbx_1", ["zz", "b@x"]) == "c1"
    found = await store.conversations_by_subject("mbx_1", "报价 304", "2026-10-01")
    assert found[0]["message_count"] == 2 and found[0]["participants"] == ["li@client.cn"]
    assert [r["message_id"] for r in await store.search(["mbx_1"], text="100%")] == [
        "b@x", "a@x"]
    assert await store.search(["mbx_1"], text="200%") == []
    assert len(await store.search(["mbx_1"], text="清单")) == 2
    assert len(await store.search(["mbx_1"], sender="李", since="2026-10-21")) == 1
    assert len(await store.search(["other"])) == 0

    await store.put_draft({"id": "mdr_1", "account_id": "mbx_1", "fields": {"body": "x"}})
    assert (await store.get_draft("mdr_1"))["fields"] == {"body": "x"}
    await store.delete_mailbox("mbx_1")
    assert await store.get_mailbox("mbx_1") is None
    assert await store.search(["mbx_1"]) == []


async def test_full_read_coverage_receipt_and_summary_on_each_backend(domain_store):
    store = MailStore(domain_store)
    args = ("chat:user:one", "run1", "message1", "digest1")
    await store.record_body_read(*args, 0, 4)
    assert not await store.body_read_in_full(*args, 10)
    await store.record_body_read(*args, 5, 10)
    assert not await store.body_read_in_full(*args, 10)  # gap still unread
    await store.record_body_read(*args, 4, 5)
    assert await store.body_read_in_full(*args, 10)
    receipt = await store.full_read_receipt(*args, create=True)
    assert receipt and receipt == await store.full_read_receipt(*args)
    await store.save_body_summary(args[0], args[2], args[3], "修订：316L，10吨，12月交付", args[1])
    saved = await MailStore(domain_store).get_body_summary(args[0], args[2], args[3])
    assert saved["summary"] == "修订：316L，10吨，12月交付"
    assert await store.get_body_summary("another-thread", args[2], args[3]) is None
    assert await store.get_body_summary(args[0], args[2], "changed-digest") is None
