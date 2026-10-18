from __future__ import annotations

import json
from dataclasses import replace

import pytest

from handler.runtime.gateway import Gateway, GrantPolicy
from handler.runtime.gateway.gmail import (
    GmailTools,
    MAX_BODY_CHARS,
    body_text,
    build_message,
    reply_subject,
)
from handler.runtime.settings import GrantsSettings
from handler.runtime.types import Decision, Risk

from .conftest import ACCOUNT, message, parse_raw, part

NAMES = {"gmail_search", "gmail_read_thread", "gmail_read_message", "gmail_get_attachment",
         "gmail_create_draft", "gmail_update_draft", "gmail_list_drafts"}
SEND_NAMES = {"gmail_send", "gmail_send_draft"}


@pytest.fixture
async def connected(world):
    await world.connect()
    g = world.google
    g.messages["m1"] = message("m1", "t1", subject="RFQ 304 pipe", parts=[
        part("text/plain", "Please quote.\n\n> On Monday you wrote:\n> earlier text",
             part_id="0"),
        part("text/csv", filename="po.csv", attachment_id="att-1", part_id="1", size=16)])
    g.messages["m2"] = message("m2", "t1", subject="Re: RFQ 304 pipe", parts=[
        part("text/html", "<p>Hello <b>team</b></p><blockquote>quoted</blockquote>")],
        extra_headers={"Reply-To": "sales@acme.test", "References": "<m1@mail.test>"})
    g.attachments["att-1"] = b"name,qty\npipe,4\n"
    return world


# ---- surface and grants --------------------------------------------------------

def test_tool_set_has_no_delete_or_modify(world):
    assert set(world.tools) == NAMES | SEND_NAMES
    assert {s.action for s in world.tools.values()} == {
        n.replace("gmail_", "gmail.") for n in NAMES | SEND_NAMES}
    for name in world.tools:
        assert not any(word in name for word in ("delete", "trash", "label", "modify"))
    for name in ("gmail_create_draft", "gmail_update_draft", "gmail_list_drafts"):
        assert "review and send" in world.tools[name].description
    for name in SEND_NAMES:
        spec = world.tools[name]
        assert spec.risk == Risk.WRITE_EXTERNAL and spec.explicit_allow_only
        assert spec.approval_preview is not None and "approval" in spec.description


def test_grant_defaults_allow_read_and_draft_but_ask_for_send(world):
    async def emit(event):
        pass

    policy = GrantPolicy(GrantsSettings().model_dump(mode="json"))
    gateway = Gateway(object(), policy, emit_event=emit)
    for spec in world.tools.values():
        gateway.register(spec)
        expected = Decision.ASK if spec.name in SEND_NAMES else Decision.ALLOW
        assert gateway.decide(world.ctx, spec)[0] == expected, spec.name


@pytest.mark.parametrize("grants", [
    {"actions": {"gmail.*": "allow"}},
    {"actions": {"*": "allow"}},
    {"defaults": {"write_external": "allow"}},
])
def test_send_is_never_allowed_by_globs_or_defaults(world, grants):
    policy = GrantPolicy(GrantsSettings.model_validate(grants).model_dump(mode="json"))
    for name in SEND_NAMES:
        decision, reason = policy.decide(world.ctx, world.tools[name], {})
        assert decision == Decision.ASK and "exact grant rule" in reason
    assert policy.decide(world.ctx, world.tools["gmail_search"], {})[0] == Decision.ALLOW


def test_send_exact_rules_decide(world):
    allow = GrantPolicy({"actions": {"gmail.send": "allow", "gmail.*": "deny"}})
    assert allow.decide(world.ctx, world.tools["gmail_send"], {})[0] == Decision.ALLOW
    assert allow.decide(world.ctx, world.tools["gmail_send_draft"], {})[0] == Decision.DENY


# ---- connection errors -------------------------------------------------------------

async def test_not_connected(world):
    result = await world.call("gmail_search", query="x")
    assert result.is_error and "Connect Google" in result.content


async def test_needs_reauth_is_a_clear_error(connected):
    connected.oauth.invalidate()
    connected.google.refresh_mode = "invalid_grant"
    result = await connected.call("gmail_search", query="x")
    assert result.is_error
    assert "reconnected" in result.content and "Reconnect Google" in result.content
    conn = await connected.oauth.connection()
    assert conn.status == "needs_reauth"
    again = await connected.call("gmail_read_message", message_id="m1")
    assert again.is_error and "Reconnect Google" in again.content


async def test_401_refreshes_once_and_retries(connected):
    connected.google.valid.clear()  # Google revoked the access token early
    result = await connected.call("gmail_read_message", message_id="m1")
    assert not result.is_error, result.content
    assert connected.google.issued == 2


async def test_missing_scope_asks_for_reconnect(connected):
    conn = await connected.oauth.connection()
    await connected.store.put_connection(replace(
        conn, scopes=["openid", "https://www.googleapis.com/auth/gmail.readonly"]))
    assert not (await connected.call("gmail_search", query="x")).is_error
    result = await connected.call("gmail_create_draft", to="a@b.test", subject="s", body="b")
    assert result.is_error and "reconnect Google with the scope" in result.content
    assert "gmail.modify" in result.content


async def test_api_errors_are_reported(connected):
    result = await connected.call("gmail_read_message", message_id="nope")
    assert result.is_error and "not found" in result.content


# ---- read tools ------------------------------------------------------------------

async def test_search(connected):
    result = await connected.call("gmail_search", query="paged", max_results=500)
    body = result.content
    assert body["next_page_token"] == "page-2"
    listed = connected.google.requests[-3]
    assert listed.url.params["q"] == "paged" and listed.url.params["maxResults"] == "50"
    first = body["messages"][0]
    assert {k: first[k] for k in ("id", "thread_id", "from", "to", "subject")} == {
        "id": "m1", "thread_id": "t1", "from": "buyer@acme.test", "to": ACCOUNT,
        "subject": "RFQ 304 pipe"}
    assert first["date"].startswith("Mon, 12 Oct 2026")
    assert first["attachments"] == [{"filename": "po.csv", "size": 16,
                                     "mime_type": "text/csv"}]
    paged = await connected.call("gmail_search", query="x", page_token="page-2")
    assert not paged.is_error
    assert any(r.url.params.get("pageToken") == "page-2" for r in connected.google.requests)


async def test_read_message_plain_html_and_attachments(connected):
    first = (await connected.call("gmail_read_message", message_id="m1")).content
    assert "Please quote." in first["body"] and "> earlier text" in first["body"]
    assert first["message_id"] == "<m1@mail.test>"
    assert first["attachments"] == [{"filename": "po.csv", "mime_type": "text/csv",
                                     "size": 16, "attachment_id": "att-1", "part_id": "1"}]
    second = (await connected.call("gmail_read_message", message_id="m2")).content
    assert "Hello team" in second["body"] and "quoted" in second["body"]
    assert "<p>" not in second["body"] and second["reply_to"] == "sales@acme.test"


def test_body_prefers_plain_and_decodes_charsets():
    payload = {"mimeType": "multipart/alternative", "parts": [
        part("text/plain", "Grüße", charset="iso-8859-1"), part("text/html", "<p>x</p>")]}
    assert body_text(payload) == "Grüße"


async def test_long_bodies_are_cut_and_saved(connected):
    long_text = "\n".join(f"line {i} " + "x" * 80 for i in range(400))
    connected.google.messages["m3"] = message("m3", "t3", parts=[part("text/plain", long_text)])
    result = (await connected.call("gmail_read_message", message_id="m3")).content
    assert len(result["body"]) <= MAX_BODY_CHARS
    assert "downloads/gmail/m3/body.txt" in result["body_truncated"]
    assert (connected.workspace / "downloads/gmail/m3/body.txt").read_text() == long_text


async def test_read_thread(connected):
    result = (await connected.call("gmail_read_thread", thread_id="t1")).content
    assert result["thread_id"] == "t1" and result["message_count"] == 2
    assert [m["id"] for m in result["messages"]] == ["m1", "m2"]


async def test_get_attachment_saves_and_extracts_text(connected):
    result = (await connected.call("gmail_get_attachment", message_id="m1",
                                   attachment_id="att-1")).content
    assert result["saved_to"] == "downloads/gmail/m1/po.csv"
    assert (connected.workspace / result["saved_to"]).read_bytes() == b"name,qty\npipe,4\n"
    assert result["size"] == 16 and result["mime_type"] == "text/csv"
    assert "pipe" in result["text"]
    # Gmail can hand out a fresh attachmentId each fetch: a part id works too.
    by_part = (await connected.call("gmail_get_attachment", message_id="m1",
                                    attachment_id="1")).content
    assert by_part["saved_to"] == "downloads/gmail/m1/po.csv"


# ---- drafts ----------------------------------------------------------------------

def _sent_raw(world):
    request = [r for r in world.google.requests if r.url.path.endswith("/drafts")
               or "/drafts/" in r.url.path and r.method == "PUT"][-1]
    return json.loads(request.content)


async def test_create_new_draft_with_attachment(connected):
    (connected.workspace / "out").mkdir(parents=True)
    (connected.workspace / "out/quote.pdf").write_bytes(b"%PDF-1.4 quote")
    result = await connected.call(
        "gmail_create_draft", to=["a@buyer.test", "b@buyer.test"], cc="c@buyer.test",
        bcc=["boss@crontal.test"], subject="Quote", body="Hi,\nattached.",
        attachments=["out/quote.pdf"])
    out = result.content
    assert out["draft_id"] == "d1" and "Not sent" in out["note"]
    assert out["gmail_link"] == (
        f"https://mail.google.com/mail/?authuser={ACCOUNT.replace('@', '%40')}"
        f"#drafts?compose={out['message_id']}")
    sent = _sent_raw(connected)
    assert "threadId" not in sent["message"]
    mime = parse_raw(sent["message"]["raw"])
    assert mime["To"] == "a@buyer.test, b@buyer.test" and mime["Cc"] == "c@buyer.test"
    assert mime["Bcc"] == "boss@crontal.test" and mime["Subject"] == "Quote"
    assert mime.get_body(("plain",)).get_content().strip() == "Hi,\r\nattached."
    attached, = mime.iter_attachments()
    assert attached.get_filename() == "quote.pdf"
    assert attached.get_content_type() == "application/pdf"
    assert attached.get_content() == b"%PDF-1.4 quote"


async def test_reply_draft_threads_and_sets_headers(connected):
    result = await connected.call("gmail_create_draft", reply_to_message_id="m2",
                                  body="Thanks, quote follows.")
    assert not result.is_error, result.content
    sent = _sent_raw(connected)
    assert sent["message"]["threadId"] == "t1"
    mime = parse_raw(sent["message"]["raw"])
    assert mime["To"] == "sales@acme.test"  # Reply-To wins over From
    assert mime["Subject"] == "Re: RFQ 304 pipe"  # no double Re:
    assert mime["In-Reply-To"] == "<m2@mail.test>"
    assert mime["References"] == "<m1@mail.test> <m2@mail.test>"
    await connected.call("gmail_create_draft", reply_to_message_id="m1", body="x",
                         subject="Pricing")
    assert parse_raw(_sent_raw(connected)["message"]["raw"])["Subject"] == "Re: Pricing"


def test_reply_subject_and_mime_builder():
    assert reply_subject("RE: x") == "RE: x" and reply_subject("x") == "Re: x"
    message = build_message(to="a@b.test", subject="s", body="b",
                            files=[("a.txt", b"hi", "text/plain")])
    assert message.is_multipart() and [a.get_filename() for a in message.iter_attachments()] == ["a.txt"]


async def test_draft_validation(connected):
    assert "`to` is required" in (await connected.call(
        "gmail_create_draft", subject="s", body="b")).content
    assert "`subject` is required" in (await connected.call(
        "gmail_create_draft", to="a@b.test", body="b")).content
    outside = await connected.call("gmail_create_draft", to="a@b.test", subject="s",
                                   body="b", attachments=["../../etc/passwd"])
    assert outside.is_error and "not found in the workspace" in outside.content


async def test_large_drafts_use_the_upload_endpoint(connected, monkeypatch):
    monkeypatch.setattr("handler.runtime.gateway.gmail.SIMPLE_UPLOAD_LIMIT", 1000)
    connected.workspace.mkdir(parents=True, exist_ok=True)
    (connected.workspace / "big.bin").write_bytes(b"\0" * 5000)
    result = await connected.call("gmail_create_draft", reply_to_message_id="m1",
                                  body="big", attachments=["big.bin"])
    assert not result.is_error, result.content
    upload, = connected.google.uploads
    assert upload.url.path == "/upload/gmail/v1/users/me/drafts"
    mime = parse_raw(connected.google.drafts["d1"]["message"]["raw"])
    assert next(mime.iter_attachments()).get_content() == b"\0" * 5000
    assert connected.google.drafts["d1"]["message"]["threadId"] == "t1"


async def test_update_draft_keeps_unchanged_fields(connected):
    connected.workspace.mkdir(parents=True, exist_ok=True)
    (connected.workspace / "a.csv").write_text("x,y\n")
    created = (await connected.call(
        "gmail_create_draft", reply_to_message_id="m1", body="v1", cc="c@x.test",
        attachments=["a.csv"])).content
    updated = await connected.call("gmail_update_draft", draft_id=created["draft_id"],
                                   body="v2")
    assert not updated.is_error, updated.content
    put = [r for r in connected.google.requests if r.method == "PUT"][-1]
    sent = json.loads(put.content)
    assert sent["id"] == "d1" and sent["message"]["threadId"] == "t1"
    mime = parse_raw(sent["message"]["raw"])
    assert mime.get_body(("plain",)).get_content().strip() == "v2"
    assert mime["Cc"] == "c@x.test" and mime["Subject"] == "Re: RFQ 304 pipe"
    assert mime["In-Reply-To"] == "<m1@mail.test>"
    assert [a.get_filename() for a in mime.iter_attachments()] == ["a.csv"]
    cleared = await connected.call("gmail_update_draft", draft_id="d1", attachments=[],
                                   to=["new@x.test"])
    mime = parse_raw(json.loads([r for r in connected.google.requests
                                 if r.method == "PUT"][-1].content)["message"]["raw"])
    assert list(mime.iter_attachments()) == [] and mime["To"] == "new@x.test"
    assert mime.get_body(("plain",)).get_content().strip() == "v2"
    assert not cleared.is_error


async def test_list_drafts(connected):
    await connected.call("gmail_create_draft", to="a@b.test", subject="One", body="1")
    await connected.call("gmail_create_draft", to="c@d.test", subject="Two", body="2")
    drafts = (await connected.call("gmail_list_drafts", max_results=5)).content["drafts"]
    assert [(d["draft_id"], d["subject"], d["to"]) for d in drafts] == [
        ("d1", "One", "a@b.test"), ("d2", "Two", "c@d.test")]
    assert drafts[0]["gmail_link"].startswith("https://mail.google.com/mail/")


async def test_gateway_call_end_to_end(connected):
    async def emit(event):
        pass

    from handler.runtime.store.sqlite import SQLiteStore

    store = SQLiteStore(connected.workspace.parent / "gw.sqlite")
    try:
        gateway = Gateway(store, GrantPolicy(), emit_event=emit)
        gateway.register(GmailTools(connected.oauth,
                                    workspace_for=lambda key: connected.workspace,
                                    client=connected.oauth._client))
        result = await gateway.call(connected.ctx, "gmail_search", {"query": "x"})
        assert not result.is_error
        audit, = await store.list_audit(connected.ctx.run_id)
        assert (audit.action, audit.status) == ("gmail.search", "ok")
    finally:
        await store.close()

