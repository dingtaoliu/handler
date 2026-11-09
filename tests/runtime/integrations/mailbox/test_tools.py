"""mail_* tools through the gateway: reads, drafts, and approval-gated sends."""
from email import policy
from email.parser import BytesParser
import asyncio

import pytest

from handler.runtime.context import RunContext
from handler.runtime.gateway import Gateway, GrantPolicy
from handler.runtime.integrations.mailbox.tools import MailTools
from handler.runtime.settings import GrantsSettings
from handler.runtime.types import ApprovalStatus

from .fake_mail import make_mail


@pytest.fixture
async def gw(env):
    mailbox = await env.add_mailbox()
    await env.service.poll(mailbox)
    env.server.deliver(make_mail(
        subject="RFQ 316L 圆钢", message_id="<rfq1@client.cn>", sender="Li <li@client.cn>",
        body="请报价 316L 圆钢 50mm。", attachments=[("清单.csv", b"grade,qty\n316L,5\n",
                                                   "text/csv")]))
    mailbox.wake_mode = "off"  # stored, attachments not saved: tools fetch on demand
    await env.mail_store.put_mailbox(mailbox)
    await env.service.poll(mailbox)
    mailbox = await env.mail_store.get_mailbox(mailbox.id)
    workspace = env.tmp_path / "ws"
    workspace.mkdir()
    (workspace / "quote.pdf").write_bytes(b"%PDF-1.4 quote")
    async def emit(event):
        pass

    gateway = Gateway(env.store, GrantPolicy(GrantsSettings().model_dump(mode="json")),
                      emit_event=emit)
    gateway.register(MailTools(env.service, env.mail_store, workspace_for=lambda key: workspace))
    env.gateway, env.mailbox, env.workspace = gateway, mailbox, workspace
    env.ctx = RunContext("run_1", "chat:7:abc", user_id="7", acting_as="7")
    return env


async def call(gw, name, args):
    return await gw.gateway.call(gw.ctx, name, args)


async def pending(gw):
    return await gw.store.list_approvals(ApprovalStatus.PENDING)


async def test_search_read_and_attachment(gw):
    found = await call(gw, "mail_search", {"text": "圆钢"})
    (hit,) = found.content["messages"]
    assert hit["subject"] == "RFQ 316L 圆钢" and hit["attachments"][0]["filename"] == "清单.csv"
    read = await call(gw, "mail_read", {"message_id": hit["id"]})
    assert "请报价" in read.content["body"] and read.content["thread_key"].startswith("mail:")
    by_header = await call(gw, "mail_read", {"message_id": "<rfq1@client.cn>"})
    assert by_header.content["id"] == hit["id"]
    attachment = await call(gw, "mail_get_attachment", {"message_id": hit["id"],
                                                        "attachment": 0})
    assert not attachment.is_error, attachment.content
    assert (gw.workspace / attachment.content["saved_to"]).read_bytes().startswith(b"grade")
    assert "316L" in attachment.content["text"]
    thread = await call(gw, "mail_read_thread", {"message_id": hit["id"]})
    assert thread.content["message_count"] == 1
    listed = await call(gw, "mail_list_mailboxes", {})
    (box,) = listed.content["mailboxes"]
    assert box["email"] == "sales@example.cn" and "password" not in str(listed.content)


async def test_draft_reply_is_stored_and_appended_to_drafts(gw):
    result = await call(gw, "mail_draft_reply", {"reply_to_message_id": "<rfq1@client.cn>",
                                                 "body": "您好，报价见附件。"})
    assert not result.is_error, result.content
    assert result.content["to"] == "li@client.cn"
    assert result.content["subject"] == "Re: RFQ 316L 圆钢"
    assert result.content["server_copy"] == "saved to 草稿箱"
    assert gw.server.sent == [] and await pending(gw) == []
    (draft_raw, _, _), = gw.server.folders["草稿箱"].messages.values()
    draft = BytesParser(policy=policy.default).parsebytes(draft_raw)
    assert draft["In-Reply-To"] == "<rfq1@client.cn>"


async def test_list_read_and_reuse_draft_preserve_signature(gw):
    args = {"reply_to_message_id": "<rfq1@client.cn>", "body": "Quote\n\nLisa Li\nFull signature"}
    first = await call(gw, "mail_draft_reply", args)
    second = await call(gw, "mail_draft_reply", args)
    assert second.content["reused"] is True
    assert first.content["draft_id"] == second.content["draft_id"]
    assert len(gw.server.folders["草稿箱"].messages) == 1
    listing = await call(gw, "mail_list_drafts", {})
    assert [d["draft_id"] for d in listing.content["drafts"]] == [first.content["draft_id"]]
    assert "Full signature" not in str(listing.content)
    read = await call(gw, "mail_read_draft", {"draft_id": first.content["draft_id"]})
    assert read.content["body"] == args["body"]
    assert read.content["next_offset"] is None
    assert "_server_message_id" not in read.content
    changed = await call(gw, "mail_draft_reply", {**args, "body": "New text"})
    assert changed.is_error and changed.content["draft_id"] == first.content["draft_id"]
    assert len(gw.server.folders["草稿箱"].messages) == 1


async def test_concurrent_duplicate_inquiry_reserves_only_one_server_copy(gw):
    args = {"to": "buyer@client.cn", "subject": "RFQ duplicate", "body": "body",
            "inquiry_key": "client.cn/AM40041"}
    results = await asyncio.gather(*(call(gw, "mail_draft_reply", args) for _ in range(8)))
    assert len({r.content["draft_id"] for r in results}) == 1
    assert len(gw.server.folders["草稿箱"].messages) == 1
    assert len(await gw.mail_store.list_drafts()) == 1


async def test_same_path_changed_attachment_is_not_silently_reused(gw):
    args = {"reply_to_message_id": "<rfq1@client.cn>", "body": "quote",
            "attachments": ["quote.pdf"]}
    await call(gw, "mail_draft_reply", args)
    (gw.workspace / "quote.pdf").write_bytes(b"different quotation")
    result = await call(gw, "mail_draft_reply", args)
    assert result.is_error
    assert len(gw.server.folders["草稿箱"].messages) == 1


async def test_update_draft_replaces_one_copy_and_preserves_identity(gw):
    created = await call(gw, "mail_draft_reply", {
        "reply_to_message_id": "<rfq1@client.cn>", "body": "old body"})
    draft_id = created.content["draft_id"]
    unrelated = gw.server.deliver(make_mail(subject="unrelated", message_id="<other@test>"), "草稿箱")
    read = await call(gw, "mail_read_draft", {"draft_id": draft_id})
    result = await call(gw, "mail_update_draft", {"draft_id": draft_id,
        "body_sha256": read.content["body_sha256"], "body": "new body\nFull signature"})
    assert not result.is_error, result.content
    assert result.content["draft_id"] == draft_id
    messages = gw.server.folders["草稿箱"].messages
    assert len(messages) == 2 and unrelated in messages
    stored = await gw.mail_store.get_draft(draft_id)
    assert stored["fields"]["body"] == "new body\nFull signature"
    assert stored["status"] == "draft" and gw.server.sent == []
    raw = next(value[0] for uid, value in messages.items() if uid != unrelated)
    assert "Full signature" in BytesParser(policy=policy.default).parsebytes(raw).get_content()
    stale = await call(gw, "mail_update_draft", {"draft_id": draft_id,
        "body_sha256": read.content["body_sha256"], "body": "stale edit"})
    assert stale.is_error and len(messages) == 2


async def test_server_side_edit_is_never_overwritten(gw):
    created = await call(gw, "mail_draft_reply", {
        "reply_to_message_id": "<rfq1@client.cn>", "body": "body"})
    draft_id = created.content["draft_id"]
    read = await call(gw, "mail_read_draft", {"draft_id": draft_id})
    folder = gw.server.folders["草稿箱"]
    uid = next(iter(folder.messages))
    raw, when, flags = folder.messages[uid]
    folder.messages[uid] = (raw.replace(b"body", b"user-edited body"), when, flags)
    result = await call(gw, "mail_update_draft", {"draft_id": draft_id,
        "body_sha256": read.content["body_sha256"], "body": "agent change"})
    assert result.is_error and "outside Handler" in str(result.content)
    assert len(folder.messages) == 1
    assert (await gw.mail_store.get_draft(draft_id))["status"] == "draft"


async def test_cleanup_failure_blocks_retry_and_send(gw, monkeypatch):
    from .fake_mail import FakeImapSession
    from handler.runtime.integrations.mailbox.transport import MailError
    def fail(self, uid):
        raise MailError("cleanup failed")
    created = await call(gw, "mail_draft_reply", {
        "reply_to_message_id": "<rfq1@client.cn>", "body": "body"})
    draft_id = created.content["draft_id"]
    read = await call(gw, "mail_read_draft", {"draft_id": draft_id})
    monkeypatch.setattr(FakeImapSession, "delete_uid", fail)
    result = await call(gw, "mail_update_draft", {"draft_id": draft_id,
        "body_sha256": read.content["body_sha256"], "body": "new"})
    assert result.is_error and "Do not retry" in str(result.content)
    assert (await gw.mail_store.get_draft(draft_id))["status"] == "reconcile"
    assert len(gw.server.folders["草稿箱"].messages) == 2
    assert (await call(gw, "mail_send", {"draft_id": draft_id})).is_error


async def test_server_draft_listing_reads_untracked_copies_and_rejects_old_uids(gw):
    gw.server.deliver(make_mail(subject="Manual unsigned draft", message_id="<manual@test>",
                                body="No signature"), "草稿箱")
    listing = await call(gw, "mail_list_server_drafts", {})
    assert not listing.is_error, listing.content
    item = listing.content["drafts"][0]
    assert item["subject"] == "Manual unsigned draft"
    result = await call(gw, "mail_read_server_draft", {"draft_ref": item["draft_ref"]})
    assert "No signature" in result.content["body"]
    assert gw.server.sent == []
    gw.server.renumber("草稿箱")
    stale = await call(gw, "mail_read_server_draft", {"draft_ref": item["draft_ref"]})
    assert stale.is_error and "UIDVALIDITY" in str(stale.content)


async def test_send_requires_approval_and_pins_content(gw):
    result = await call(gw, "mail_send", {
        "reply_to_message_id": "<rfq1@client.cn>", "cc": "boss@example.cn",
        "body": "Quote attached.", "attachments": ["quote.pdf"]})
    assert result.content["status"] == "pending_approval"
    assert gw.server.sent == []
    (approval,) = await pending(gw)
    assert approval.action == "mail.send"
    for text in ("from sales@example.cn (reply)", "To: li@client.cn", "Cc: boss@example.cn",
                 "Subject: Re: RFQ 316L 圆钢", "Quote attached.", "quote.pdf (14 bytes)"):
        assert text in approval.reason, text
    assert approval.args["to"] == "li@client.cn"
    done = await gw.gateway.approve(approval.id, "root")
    assert done.status == ApprovalStatus.EXECUTED, done.result
    (sender, recipients, data), = gw.server.sent
    assert sender == "sales@example.cn"
    assert sorted(recipients) == ["boss@example.cn", "li@client.cn"]
    sent = BytesParser(policy=policy.default).parsebytes(data)
    assert sent["In-Reply-To"] == "<rfq1@client.cn>" and sent["Bcc"] is None
    content = done.result["content"]
    assert content["status"] == "sent" and content["sent_copy"] == "saved to Sent"
    # Our own send is recorded outbound in the same conversation and never wakes.
    row = await gw.mail_store.find_message(gw.mailbox.id, content["message_id"])
    original = await gw.mail_store.find_message(gw.mailbox.id, "rfq1@client.cn")
    assert row["direction"] == "out" and row["conversation_id"] == original["conversation_id"]
    await gw.service.poll(await gw.mail_store.get_mailbox(gw.mailbox.id))
    assert all(e["type"] != "mail.received" for e in await gw.events())


async def test_glob_allow_cannot_skip_send_approval(gw):
    gw.gateway.set_policy(GrantPolicy({"actions": {"mail.*": "allow", "*": "allow"}}))
    result = await call(gw, "mail_send", {"to": "x@y.cn", "subject": "s", "body": "b"})
    assert result.content["status"] == "pending_approval" and gw.server.sent == []


async def test_changed_attachment_or_draft_after_approval_is_refused(gw):
    await call(gw, "mail_send", {"to": "x@y.cn", "subject": "s", "body": "b",
                                 "attachments": ["quote.pdf"]})
    (approval,) = await pending(gw)
    (gw.workspace / "quote.pdf").write_bytes(b"tampered")
    done = await gw.gateway.approve(approval.id, "root")
    assert done.status == ApprovalStatus.FAILED
    assert "changed after the send was approved" in done.result["content"]

    draft = await call(gw, "mail_draft_reply", {"to": "x@y.cn", "subject": "s", "body": "v1"})
    await call(gw, "mail_send", {"draft_id": draft.content["draft_id"]})
    (approval,) = await pending(gw)
    assert "v1" in approval.reason
    stored = await gw.mail_store.get_draft(draft.content["draft_id"])
    await gw.mail_store.put_draft({**stored, "fields": {**stored["fields"], "body": "v2"}})
    done = await gw.gateway.approve(approval.id, "root")
    assert done.status == ApprovalStatus.FAILED and gw.server.sent == []


async def test_send_errors_fail_before_parking(gw):
    result = await call(gw, "mail_send", {"to": "x@y.cn", "body": "no subject"})
    assert result.is_error and "subject" in str(result.content)
    assert await pending(gw) == []


async def _body(gw, text, *, capped=False):
    row = await gw.mail_store.find_message(gw.mailbox.id, "rfq1@client.cn")
    await gw.mail_store._execute(
        "UPDATE mail_messages SET body_text=?,body_truncated=? WHERE id=?",
        (text, int(capped), row["id"]))
    return row["id"]


async def test_remember_requires_full_original_and_is_explicit(gw):
    ref = await _body(gw, "Prior commitment: 5 tons due Nov 20. Buyer changed this to 7 tons.")
    read = (await call(gw, "mail_read", {"message_id": ref})).content
    # Merely seeing the original never suppresses it on a repeated read.
    again = (await call(gw, "mail_read", {"message_id": ref})).content
    assert again["body"] == read["body"]
    saved = await call(gw, "mail_remember", {
        "message_id": ref, "body_sha256": read["body_sha256"],
        "full_read_receipt": read["full_read_receipt"], "summary": "Changed 5 to 7 tons; due Nov 20."})
    assert not saved.is_error
    # Reset/new run: the factual summary travels with the result, not just a
    # 'seen before' flag that relies on inaccessible conversation history.
    gw.ctx = RunContext("run_2", gw.ctx.thread_key, user_id="7", acting_as="7")
    remembered = (await call(gw, "mail_read", {"message_id": ref})).content
    assert remembered["body_summary"] == "Changed 5 to 7 tons; due Nov 20."
    assert remembered["source_untrusted"] is True and "body" not in remembered
    assert remembered["summary_source_run_id"] == "run_1"
    original = (await call(gw, "mail_read", {"message_id": ref, "force_full": True})).content
    assert original["body"] == read["body"]


async def test_partial_pages_never_count_as_full_and_gaps_must_be_read(gw):
    ref = await _body(gw, "A" * 45_000)
    first = (await call(gw, "mail_read", {"message_id": ref})).content
    args = {"message_id": ref, "body_sha256": first["body_sha256"], "summary": "A body"}
    assert (await call(gw, "mail_remember", args)).is_error
    last = (await call(gw, "mail_read", {"message_id": ref, "body_offset": 40_000})).content
    assert "next_body_offset" not in last
    assert (await call(gw, "mail_remember", args)).is_error
    middle = (await call(gw, "mail_read", {"message_id": ref,
                                          "body_offset": first["next_body_offset"]})).content
    assert first["body"] + middle["body"] + last["body"] == "A" * 45_000
    args["full_read_receipt"] = middle["full_read_receipt"]
    assert not (await call(gw, "mail_remember", args)).is_error


async def test_remember_receipts_are_run_and_thread_scoped_and_digest_checked(gw):
    ref = await _body(gw, "Important original")
    read = (await call(gw, "mail_read", {"message_id": ref})).content
    args = {"message_id": ref, "body_sha256": read["body_sha256"],
            "full_read_receipt": read.get("full_read_receipt", "unavailable"), "summary": "Original"}
    original_ctx = gw.ctx
    gw.ctx = RunContext("run_2", original_ctx.thread_key, user_id="7")
    assert (await call(gw, "mail_remember", args)).is_error
    gw.ctx = RunContext(original_ctx.run_id, "chat:8:other", user_id="8")
    assert (await call(gw, "mail_remember", args)).is_error
    gw.ctx = original_ctx
    assert not (await call(gw, "mail_remember", args)).is_error
    gw.ctx = RunContext("run_2", "chat:8:other", user_id="8")
    assert "body" in (await call(gw, "mail_read", {"message_id": ref})).content
    gw.ctx = original_ctx
    await _body(gw, "Corrected original")
    assert "body" in (await call(gw, "mail_read", {"message_id": ref})).content
    assert (await call(gw, "mail_remember", args)).is_error


async def test_storage_capped_body_cannot_be_remembered(gw):
    ref = await _body(gw, "Only the ingested prefix", capped=True)
    read = (await call(gw, "mail_read", {"message_id": ref})).content
    result = await call(gw, "mail_remember", {
        "message_id": ref, "body_sha256": read["body_sha256"],
            "full_read_receipt": read.get("full_read_receipt", "unavailable"), "summary": "Prefix"})
    assert result.is_error
    assert "not a full read" in read["note"]


async def test_thread_and_batch_budgets_paginate_without_losing_messages(gw):
    ref = await _body(gw, "A" * 25_000)
    original = await gw.mail_store.get_message(ref)
    # More than 15 long emails exceeded the previous 60k budget due to a
    # per-message minimum. The new limit applies across the complete page.
    for index in range(1, 18):
        await gw.mail_store._execute(
            "INSERT INTO mail_messages (id,account_id,conversation_id,message_id,"
            "body_text,direction,created_at,date) VALUES (?,?,?,?,?,?,?,?)",
            (f"mm_budget{index}", gw.mailbox.id, original["conversation_id"],
             f"budget{index}@example.cn", "B" * 25_000, "in", original["created_at"],
             original["date"]))
    refs = []
    offset = 0
    while True:
        page = (await call(gw, "mail_read_thread", {"message_id": ref, "offset": offset})).content
        assert page["message_count"] == 18
        assert sum(len(m["body"]) for m in page["messages"]) <= 60_000
        refs.extend(m["id"] for m in page["messages"])
        assert all(m["next_body_offset"] == 20_000 for m in page["messages"])
        if "next_offset" not in page:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert len(refs) == len(set(refs)) == 18
    batch = (await call(gw, "mail_read", {"message_ids": refs[:5]})).content
    assert len(batch["messages"]) == 3
    assert batch["remaining_message_ids"] == refs[3:5]
    assert sum(len(m["body"]) for m in batch["messages"]) == 60_000


async def test_summary_survives_store_reopen_but_never_crosses_instances(gw):
    from handler.runtime.integrations.mailbox.store import MailStore
    from handler.runtime.store.sqlite import SQLiteStore

    ref = await _body(gw, "The order changed to seven tons; November 20 remains the due date.")
    read = (await call(gw, "mail_read", {"message_id": ref})).content
    summary = "7 tons, due November 20."
    args = {"message_id": ref, "body_sha256": read["body_sha256"],
            "full_read_receipt": read.get("full_read_receipt", "unavailable"), "summary": summary}
    assert not (await call(gw, "mail_remember", args)).is_error
    reopened = MailStore(gw.store)
    remembered = await reopened.get_body_summary(gw.ctx.thread_key, ref, read["body_sha256"])
    assert remembered["summary"] == summary
    other_store = SQLiteStore(gw.tmp_path / "other-instance.db")
    try:
        other = MailStore(other_store)
        assert await other.get_body_summary(gw.ctx.thread_key, ref, read["body_sha256"]) is None
        assert not await other.body_read_in_full(gw.ctx.thread_key, gw.ctx.run_id, ref,
                                                  read["body_sha256"], len(read["body"]))
    finally:
        await other_store.close()


async def test_multilingual_thread_preserves_historical_amendment_and_unread_replies(gw):
    prefix = "Original RFQ: 316L, 5 吨, USD 2,500/ton, delivery November 20.\n"
    amendment = "\n历史修订：数量改为 7 吨，交期改为 11 月 25 日；价格待确认。Do not use the original deadline."
    ref = await _body(gw, prefix + "旧邮件报价讨论。" * 3_000 + amendment)
    original = await gw.mail_store.get_message(ref)
    await gw.mail_store._execute("UPDATE mail_messages SET date=? WHERE id=?",
                                  ("2026-11-01T10:00:00+00:00", ref))
    replies = [
        ("mm_amendment_reply", "Buyer: 请确认 revised 7 tons and November 25. Price still open.",
         "2026-11-02T10:00:00+00:00"),
        ("mm_amendment_unread", "Latest: 价格批准为 USD 2,450/ton. 包装标准仍待确认。",
         "2026-11-03T10:00:00+00:00"),
    ]
    for message_id, body, date in replies:
        await gw.mail_store._execute(
            "INSERT INTO mail_messages (id,account_id,conversation_id,message_id,"
            "body_text,direction,created_at,date) VALUES (?,?,?,?,?,?,?,?)",
            (message_id, gw.mailbox.id, original["conversation_id"], message_id + "@client.cn",
             body, "in", original["created_at"], date))
    page = (await call(gw, "mail_read_thread", {"message_id": ref, "max_results": 2})).content
    historical = page["messages"][0]
    assert amendment not in historical["body"]
    assert "full_read_receipt" not in historical
    assert page["messages"][1]["body"] == replies[0][1]
    assert page["next_offset"] == 2
    summary = ("316L RFQ originally 5 tons, USD 2,500/ton, November 20. "
               "历史修订改为 7 吨、11 月 25 日；价格待确认。")
    remember = {"message_id": ref, "body_sha256": historical["body_sha256"],
                "full_read_receipt": "cannot-guess-completed-read", "summary": summary}
    assert (await call(gw, "mail_remember", remember)).is_error
    tail = (await call(gw, "mail_read", {"message_id": ref,
                                        "body_offset": historical["next_body_offset"]})).content
    assert amendment in tail["body"]
    remember["full_read_receipt"] = tail["full_read_receipt"]
    assert not (await call(gw, "mail_remember", remember)).is_error
    gw.ctx = RunContext("run_2", gw.ctx.thread_key, user_id="7")
    reread = (await call(gw, "mail_read_thread", {"message_id": ref})).content
    assert reread["messages"][0]["body_summary"] == summary
    assert reread["messages"][1]["body"] == replies[0][1]
    assert reread["messages"][2]["body"] == replies[1][1]
    full = (await call(gw, "mail_read", {"message_id": ref, "force_full": True})).content
    assert full["body"] == historical["body"]


async def test_remember_rejects_guessed_receipt_even_with_full_coverage(gw):
    ref = await _body(gw, "Previously visible full original body")
    read = (await call(gw, "mail_read", {"message_id": ref})).content
    for receipt in ("", read["body_sha256"], "mailread_forged"):
        result = await call(gw, "mail_remember", {
            "message_id": ref, "body_sha256": read["body_sha256"],
            "full_read_receipt": receipt, "summary": "Original body."})
        assert result.is_error
    assert "body" in (await call(gw, "mail_read", {"message_id": ref})).content
