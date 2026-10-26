"""mail_* tools through the gateway: reads, drafts, and approval-gated sends."""
from email import policy
from email.parser import BytesParser

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
