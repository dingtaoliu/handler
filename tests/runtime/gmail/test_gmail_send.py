"""gmail_send / gmail_send_draft: approval-gated, and what runs is what was shown."""

from __future__ import annotations

import pytest

from handler.runtime.gateway import Gateway, GrantPolicy
from handler.runtime.gateway.gmail import GmailTools
from handler.runtime.settings import GrantsSettings
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import ApprovalStatus

from .conftest import ACCOUNT, message, parse_raw, part


@pytest.fixture
async def gw(world):
    await world.connect()
    world.google.messages["m1"] = message(
        "m1", "t1", subject="RFQ 304 pipe", sender="buyer@acme.test",
        extra_headers={"Reply-To": "sales@acme.test"},
        parts=[part("text/plain", "Please quote.")])
    world.workspace.mkdir(parents=True, exist_ok=True)
    (world.workspace / "quote.pdf").write_bytes(b"%PDF-1.4 quote")
    events = []

    async def emit(event):
        events.append(event)

    store = SQLiteStore(world.workspace.parent / "gw.sqlite")
    gateway = Gateway(store, GrantPolicy(GrantsSettings().model_dump(mode="json")),
                      emit_event=emit)
    gateway.register(GmailTools(world.oauth, workspace_for=lambda key: world.workspace,
                                client=world.oauth._client))
    world.gateway, world.gw_store, world.events = gateway, store, events
    yield world
    await store.close()


async def pending(world):
    return await world.gw_store.list_approvals(ApprovalStatus.PENDING)


async def test_send_waits_for_approval_and_shows_the_email(gw):
    result = await gw.gateway.call(gw.ctx, "gmail_send", {
        "to": ["a@b.test", "c@d.test"], "cc": "boss@b.test", "subject": "Quote",
        "body": "Hello,\n\nquote attached.", "attachments": ["quote.pdf"]})
    assert result.content["status"] == "pending_approval"
    assert gw.google.sent == []
    approval, = await pending(gw)
    assert approval.action == "gmail.send"
    for text in ("To: a@b.test, c@d.test", "Cc: boss@b.test", "Subject: Quote",
                 "quote attached.", "quote.pdf (14 bytes)"):
        assert text in approval.reason, text

    done = await gw.gateway.approve(approval.id, "root")
    assert done.status == ApprovalStatus.EXECUTED, done.result
    sent, = gw.google.sent
    mail = parse_raw(sent["raw"])
    assert (mail["To"], mail["Cc"], mail["Subject"]) == ("a@b.test, c@d.test",
                                                        "boss@b.test", "Quote")
    assert [a.get_filename() for a in mail.iter_attachments()] == ["quote.pdf"]
    assert done.result["content"]["status"] == "sent"
    assert done.result["content"]["message_id"] == "s1"


async def test_reply_is_threaded_and_preview_shows_resolved_recipient(gw):
    await gw.gateway.call(gw.ctx, "gmail_send", {
        "reply_to_message_id": "m1", "body": "Thanks, quote below."})
    approval, = await pending(gw)
    assert "Send email (reply)" in approval.reason
    assert "To: sales@acme.test" in approval.reason
    assert "Subject: Re: RFQ 304 pipe" in approval.reason
    assert approval.args["to"] == "sales@acme.test"
    await gw.gateway.approve(approval.id, "root")
    sent, = gw.google.sent
    mail = parse_raw(sent["raw"])
    assert sent["threadId"] == "t1"
    assert mail["In-Reply-To"] == "<m1@mail.test>"
    assert mail["Subject"] == "Re: RFQ 304 pipe" and mail["To"] == "sales@acme.test"


async def test_attachment_changed_after_approval_is_refused(gw):
    await gw.gateway.call(gw.ctx, "gmail_send", {
        "to": "a@b.test", "subject": "Quote", "body": "x", "attachments": ["quote.pdf"]})
    approval, = await pending(gw)
    (gw.workspace / "quote.pdf").write_bytes(b"something else")
    done = await gw.gateway.approve(approval.id, "root")
    assert done.status == ApprovalStatus.FAILED
    assert "changed after the send was approved" in done.result["content"]
    assert gw.google.sent == []


async def test_denied_send_sends_nothing(gw):
    await gw.gateway.call(gw.ctx, "gmail_send", {
        "to": "a@b.test", "subject": "Quote", "body": "x"})
    approval, = await pending(gw)
    await gw.gateway.deny(approval.id, "root", "not now")
    assert gw.google.sent == []


async def test_invalid_send_is_rejected_before_an_approval_exists(gw):
    result = await gw.gateway.call(gw.ctx, "gmail_send", {"subject": "x", "body": "y"})
    assert result.is_error and "`to` is required" in result.content
    result = await gw.gateway.call(gw.ctx, "gmail_send", {
        "to": "a@b.test", "subject": "x", "body": "y", "attachments": ["../etc/passwd"]})
    assert result.is_error and "not found in the workspace" in result.content
    assert await pending(gw) == []


async def test_send_needs_a_send_capable_scope(gw):
    connection = await gw.store.find_connection("google", "instance")
    from dataclasses import replace

    await gw.store.put_connection(replace(connection, scopes=[
        "https://www.googleapis.com/auth/gmail.readonly"]))
    result = await gw.gateway.call(gw.ctx, "gmail_send", {
        "to": "a@b.test", "subject": "x", "body": "y"})
    assert result.is_error and "reconnect" in result.content
    assert await pending(gw) == []


async def test_long_body_preview_is_cut(gw):
    await gw.gateway.call(gw.ctx, "gmail_send", {
        "to": "a@b.test", "subject": "x", "body": "word " * 1000})
    approval, = await pending(gw)
    assert "(4,999 characters in all)" in approval.reason
    assert len(approval.reason) < 1500


async def test_large_send_uses_the_upload_endpoint(gw, monkeypatch):
    from handler.runtime.gateway import gmail

    monkeypatch.setattr(gmail, "SIMPLE_UPLOAD_LIMIT", 100)
    await gw.gateway.call(gw.ctx, "gmail_send", {
        "to": "a@b.test", "subject": "Big", "body": "x" * 500})
    approval, = await pending(gw)
    await gw.gateway.approve(approval.id, "root")
    assert len(gw.google.uploads) == 1
    assert "/upload/gmail/v1/users/me/messages/send" in str(gw.google.uploads[0].url)
    assert parse_raw(gw.google.sent[0]["raw"])["Subject"] == "Big"


async def test_send_draft_shows_the_draft_and_sends_it(gw):
    created = await gw.gateway.call(gw.ctx, "gmail_create_draft", {
        "to": "a@b.test", "subject": "Draft one", "body": "Draft body",
        "attachments": ["quote.pdf"]})
    draft_id = created.content["draft_id"]
    result = await gw.gateway.call(gw.ctx, "gmail_send_draft", {"draft_id": draft_id})
    assert result.content["status"] == "pending_approval"
    approval, = await pending(gw)
    assert approval.action == "gmail.send_draft"
    for text in ("To: a@b.test", "Subject: Draft one", "Draft body", "quote.pdf"):
        assert text in approval.reason, text
    done = await gw.gateway.approve(approval.id, "root")
    assert done.status == ApprovalStatus.EXECUTED, done.result
    assert done.result["content"]["status"] == "sent"
    assert parse_raw(gw.google.sent[0]["raw"])["Subject"] == "Draft one"
    assert draft_id not in gw.google.drafts


async def test_draft_changed_after_approval_request_is_refused(gw):
    created = await gw.gateway.call(gw.ctx, "gmail_create_draft", {
        "to": "a@b.test", "subject": "v1", "body": "first"})
    draft_id = created.content["draft_id"]
    await gw.gateway.call(gw.ctx, "gmail_send_draft", {"draft_id": draft_id})
    approval, = await pending(gw)
    await gw.gateway.call(gw.ctx, "gmail_update_draft", {
        "draft_id": draft_id, "to": "someone-else@evil.test"})
    done = await gw.gateway.approve(approval.id, "root")
    assert done.status == ApprovalStatus.FAILED
    assert "draft changed" in done.result["content"]
    assert gw.google.sent == []


async def test_send_draft_without_recipients_is_rejected(gw):
    gw.google.messages.clear()
    created = await gw.gateway.call(gw.ctx, "gmail_create_draft", {
        "to": "a@b.test", "subject": "x", "body": "y"})
    draft_id = created.content["draft_id"]
    raw = gw.google.drafts[draft_id]["message"]["raw"]
    mail = parse_raw(raw)
    del mail["To"]
    import base64

    gw.google.drafts[draft_id]["message"]["raw"] = base64.urlsafe_b64encode(
        mail.as_bytes()).decode()
    result = await gw.gateway.call(gw.ctx, "gmail_send_draft", {"draft_id": draft_id})
    assert result.is_error and "no recipients" in result.content
    assert await pending(gw) == []


async def test_exact_grant_sends_without_approval(gw):
    gw.gateway.set_policy(GrantPolicy({"actions": {"gmail.send": "allow"}}))
    result = await gw.gateway.call(gw.ctx, "gmail_send", {
        "to": "a@b.test", "subject": "Now", "body": "x"})
    assert result.content["status"] == "sent", result.content
    assert result.content["to"] == "a@b.test"
    assert "authuser=" + ACCOUNT.replace("@", "%40") in result.content["gmail_link"]
    assert await pending(gw) == []
