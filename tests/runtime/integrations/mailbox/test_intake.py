"""Email intake copies filed under the read-only `intake` mail account."""
import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from handler.runtime.context import RunContext
from handler.runtime.gateway import Gateway, GrantPolicy
from handler.runtime.integrations.base import IntegrationConfig, IntegrationContext
from handler.runtime.integrations.mailbox import MailboxIntegration
from handler.runtime.integrations.mailbox.intake import INTAKE_ID, IntakeArchive
from handler.runtime.integrations.mailbox.tools import MailTools
from handler.runtime.settings import GrantsSettings, SettingsService
from handler.runtime.store.sqlite_users import User
from handler.runtime.web.email_ingest import email_ingest_router, sign_email
from handler.runtime.workspaces import WorkspaceSync

from ...email.test_email_intake import ATTACHED, INLINE, SECRET, resolver
from .fake_mail import make_mail


def post(client, raw):
    t, sig = sign_email(SECRET, raw)
    return client.post("/ingest/email", content=raw, headers={
        "x-handler-timestamp": t, "x-handler-signature": sig,
        "x-handler-envelope-from": "mei.lin%40client.example",
        "x-handler-envelope-to": "contact%40crontal.ai",
        "x-handler-auth-results": base64.b64encode(json.dumps({}).encode()).decode()})


@pytest.fixture
async def intake(env, tmp_path):
    await env.store.create_user(User("17", "Admin", role="admin"))
    settings = SettingsService(env.store)
    email, _ = await settings.get("email")
    await settings.put("email", {**email, "act_as": "17"}, "t", None)
    archive = IntakeArchive(env.service)
    observers: list = []
    app = FastAPI()
    app.include_router(email_ingest_router(env.store, SECRET, settings,
                                           WorkspaceSync(None, tmp_path / "data"),
                                           resolver=resolver, observers=observers))
    observers.append(archive)  # filled after the router exists, as the server does

    async def emit(event):
        pass

    workspace = tmp_path / "ws"
    workspace.mkdir()
    gateway = Gateway(env.store, GrantPolicy(GrantsSettings().model_dump(mode="json")),
                      emit_event=emit)
    gateway.register(MailTools(env.service, env.mail_store, workspace_for=lambda key: workspace,
                               intake=archive))
    env.gateway, env.client, env.archive, env.observers = gateway, TestClient(app), archive, \
        observers
    env.ctx = RunContext("run_1", "chat:17:abc", user_id="17", acting_as="17")
    return env


async def call(env, name, args):
    return await env.gateway.call(env.ctx, name, args)


async def test_accepted_intake_mail_is_searchable_and_read_only(intake):
    assert post(intake.client, ATTACHED).status_code == 202
    assert post(intake.client, ATTACHED).status_code == 200  # duplicate: stored once
    assert intake.archive.address == "contact@crontal.ai"
    found = (await call(intake, "mail_search", {"text": "Q-7781"})).content["messages"]
    assert len(found) == 1 and found[0]["mailbox_id"] == INTAKE_ID
    message = found[0]
    assert (await call(intake, "mail_search", {"mailbox_id": INTAKE_ID})).content["messages"]
    read = await call(intake, "mail_read", {"message_id": message["id"]})
    assert not read.is_error and "thread_key" not in read.content
    assert "email intake" in read.content["source"]
    thread = await call(intake, "mail_read_thread", {"message_id": message["id"]})
    assert thread.content["mailbox_id"] == INTAKE_ID and thread.content["thread_key"] is None
    boxes = (await call(intake, "mail_list_mailboxes", {})).content["mailboxes"]
    assert boxes == [{"mailbox_id": INTAKE_ID, "label": "Email intake (read-only)",
                      "email": "contact@crontal.ai", "read_only": True,
                      "note": boxes[0]["note"], "messages": {"in": 1}}]
    # Never a source of drafts, sends or IMAP fetches.
    for name, args in (("mail_draft_reply", {"reply_to_message_id": message["id"],
                                             "body": "Thanks"}),
                       ("mail_draft_reply", {"mailbox_id": INTAKE_ID, "to": "a@b.c",
                                             "subject": "x", "body": "y"}),
                       ("mail_send", {"mailbox_id": INTAKE_ID, "to": "a@b.c",
                                      "subject": "x", "body": "y"})):
        result = await call(intake, name, args)
        assert result.is_error and "read-only" in str(result.content), name
    assert message["attachments"]
    attachment = await call(intake, "mail_get_attachment",
                            {"message_id": message["id"], "attachment": 0})
    assert attachment.is_error and "workspace" in attachment.content
    # Not a configured mailbox: the poller and /api/mailboxes never see it.
    assert await intake.mail_store.list_mailboxes() == []
    assert await intake.store.list_approvals() == []


async def test_intake_sits_next_to_connected_mailboxes(intake):
    mailbox = await intake.add_mailbox()
    intake.server.deliver(make_mail(subject="Pipe order", message_id="<p1@client.cn>",
                                    sender="Li <li@client.cn>", body="pipe please"))
    mailbox.wake_mode = "off"
    await intake.mail_store.put_mailbox(mailbox)
    await intake.service.poll(mailbox)
    post(intake.client, INLINE)
    everywhere = (await call(intake, "mail_search", {})).content["messages"]
    assert {m["mailbox_id"] for m in everywhere} == {mailbox.id, INTAKE_ID}
    boxes = (await call(intake, "mail_list_mailboxes", {})).content["mailboxes"]
    assert [b["mailbox_id"] for b in boxes] == [mailbox.id, INTAKE_ID]
    # With one real mailbox the default for drafts is still that mailbox.
    draft = await call(intake, "mail_draft_reply", {"to": "li@client.cn", "subject": "Re",
                                                    "body": "ok"})
    assert not draft.is_error and draft.content["mailbox_id"] == mailbox.id


async def test_observer_failure_does_not_fail_the_intake(intake):
    async def broken(raw, **kwargs):
        raise RuntimeError("boom")

    intake.observers.insert(0, broken)
    assert post(intake.client, INLINE).status_code == 202
    assert (await call(intake, "mail_search", {})).content["messages"]  # next one still ran


async def test_ignored_mail_is_not_archived(intake):
    response = post(intake.client, INLINE.replace(b"2 tons", b"9 tons"))  # DKIM breaks
    assert response.json()["status"] == "ignored"
    assert (await call(intake, "mail_search", {})).content["messages"] == []


def _ctx(store, env, tmp_path):
    return IntegrationContext(instance_name="", base_path="", env=env, store=store, bus=None,
                              settings=SettingsService(store), gateway=None, outbox=None,
                              notifier=None, auth=None, workspaces_dir=tmp_path / "ws",
                              workspace_sync=None)


@pytest.mark.parametrize("email_secret, archived", [("s", True), ("", False)])
def test_integration_archives_only_with_email_intake(env, tmp_path, email_secret, archived):
    integration = MailboxIntegration()
    variables = {"HANDLER_ADMIN_KEY": "k" * 32, "HANDLER_EMAIL_SECRET": email_secret,
                 "HANDLER_EMAIL_INTAKE_ADDRESS": "contact@crontal.ai"}
    integration.configure(IntegrationConfig("", variables))
    ctx = _ctx(env.store, variables, tmp_path)
    observers = integration.email_intake_observers(ctx)
    (tools,) = integration.tool_sets(ctx)
    assert bool(observers) is archived and (tools.intake is not None) is archived
    if archived:
        assert observers[0] is tools.intake and observers[0].address == "contact@crontal.ai"
