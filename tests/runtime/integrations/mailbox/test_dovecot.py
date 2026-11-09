"""Opt-in real IMAP validation, isolated Docker mailbox, never SMTP/customer mail.

Run HANDLER_TEST_DOVECOT=1 pytest .../test_dovecot.py. Uses a pinned image and
loopback-only random port; creates/removes its own container and anonymous data.
"""
from email import policy
from email.parser import BytesParser
import os
import subprocess
import time
import uuid

import pytest

from handler.runtime.context import RunContext
from handler.runtime.gateway import Gateway, GrantPolicy
from handler.runtime.integrations.mailbox.tools import MailTools
from handler.runtime.integrations.mailbox.transport import ServerParams, StdlibTransport, quote_folder
from handler.runtime.settings import GrantsSettings

from .fake_mail import make_mail, self_signed_cert

IMAGE = "dovecot/dovecot:2.4.4@sha256:723e3392fe16c6fad8ddc605ea767cc01b4bad9cd9f13eb1dbac15e79c89b2d4"
PASSWORD = "handler-local-test"


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


@pytest.fixture(scope="module")
def dovecot(tmp_path_factory):
    if os.environ.get("HANDLER_TEST_DOVECOT") != "1":
        pytest.skip("opt-in real Dovecot integration test")
    directory = tmp_path_factory.mktemp("dovecot")
    cert, key = self_signed_cert(directory)
    key.chmod(0o644)  # Disposable test certificate, readable by container UID 1000.
    name = "handler-imap-test-" + uuid.uuid4().hex[:12]
    try:
        docker("run", "--detach", "--name", name, "-p", "127.0.0.1::31993",
               "-e", "USER_PASSWORD=" + PASSWORD,
               "-v", f"{cert}:/etc/dovecot/ssl/tls.crt:ro",
               "-v", f"{key}:/etc/dovecot/ssl/tls.key:ro", IMAGE)
        port = int(docker("port", name, "31993/tcp").rsplit(":", 1)[1])
        for attempt in range(100):
            try:
                session = StdlibTransport().open_imap(ServerParams(
                    "127.0.0.1", port, "tls", "probe@handler.invalid", PASSWORD,
                    allow_self_signed=True, timeout=1))
                session.logout()
                break
            except Exception:
                if attempt == 99:
                    raise
                time.sleep(0.1)
        yield port
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True, check=False)


@pytest.fixture
async def real_mail(env, dovecot):
    class NoSmtp(StdlibTransport):
        def open_smtp(self, params):
            raise AssertionError("Draft validation must never open SMTP")
    env.service.transport = NoSmtp()
    env.mailbox = await env.add_mailbox(
        username=uuid.uuid4().hex + "@handler.invalid", imap_host="127.0.0.1",
        imap_port=dovecot, allow_self_signed=True, drafts_folder="草稿箱")
    await env.service.set_password(env.mailbox, PASSWORD)
    params = env.service.params(env.mailbox, PASSWORD, "imap")
    session = env.service.transport.open_imap(params)
    try:
        assert session._imap.create(quote_folder(env.mailbox.drafts_folder))[0] == "OK"
    finally:
        session.logout()
    env.params = params
    env.workspace = env.tmp_path / "workspace"
    env.workspace.mkdir()
    (env.workspace / "清单.csv").write_bytes("sku,quantity\n001,102\n".encode())
    env.ctx = RunContext("run-real-imap", "chat:7:test", user_id="7", acting_as="7")
    async def emit(event):
        pass
    env.gateway = Gateway(env.store, GrantPolicy(GrantsSettings().model_dump(mode="json")), emit_event=emit)
    env.gateway.register(MailTools(env.service, env.mail_store, workspace_for=lambda _: env.workspace))
    return env


async def call(env, tool, args):
    return await env.gateway.call(env.ctx, tool, args)


async def create(env):
    args = {"mailbox_id": env.mailbox.id, "to": ["buyer@handler.invalid"],
            "subject": "RFQ 测试", "body": "Original quote\n\n完整签名", "inquiry_key": "synthetic-102",
            "attachments": ["清单.csv"]}
    result = await call(env, "mail_draft_reply", args)
    assert not result.is_error, result.content
    again = await call(env, "mail_draft_reply", args)
    assert not again.is_error and again.content["reused"]
    return result.content["draft_id"]


async def test_replace_preserves_unrelated_deleted_draft_and_unicode_attachment(real_mail):
    env = real_mail
    draft_id = await create(env)
    session = env.service.transport.open_imap(env.params)
    try:
        session.append(env.mailbox.drafts_folder, make_mail(message_id="<unrelated@handler.invalid>"), flags="(\\Deleted)")
        session.select(env.mailbox.drafts_folder, readonly=False)
        old_uid, = session.uid_search("HEADER Message-ID " + (await env.mail_store.get_draft(draft_id))["fields"]["_server_message_id"])
        unrelated, = session.uid_search("HEADER Message-ID <unrelated@handler.invalid>")
    finally:
        session.logout()
    read = await call(env, "mail_read_draft", {"draft_id": draft_id})
    updated = await call(env, "mail_update_draft", {"draft_id": draft_id,
        "body_sha256": read.content["body_sha256"], "body": "Revised quote\n\n完整签名"})
    assert not updated.is_error, updated.content
    session = env.service.transport.open_imap(env.params)
    try:
        session.select(env.mailbox.drafts_folder)
        uids = session.uid_search("ALL")
        assert unrelated in uids and old_uid not in uids and len(uids) == 2
        assert b"\\Deleted" in session._imap.uid("FETCH", str(unrelated), "(FLAGS)")[1][0]
        uid, = [u for u in uids if u != unrelated]
        message = BytesParser(policy=policy.default).parsebytes(session.fetch_message(uid))
        attachment, = list(message.iter_attachments())
        assert attachment.get_filename() == "清单.csv" and attachment.get_payload(decode=True) == (env.workspace / "清单.csv").read_bytes()
    finally:
        session.logout()
    listed = await call(env, "mail_list_server_drafts", {"mailbox_id": env.mailbox.id})
    assert not listed.is_error and len(listed.content["drafts"]) == 2
    refs = [x["draft_ref"] for x in listed.content["drafts"]]
    found = [await call(env, "mail_read_server_draft", {"mailbox_id": env.mailbox.id, "draft_ref": ref}) for ref in refs]
    assert any("Revised quote" in r.content.get("body", "") and "完整签名" in r.content["body"] for r in found)
    assert not env.server.sent


async def test_external_imap_edit_is_not_overwritten(real_mail):
    env = real_mail
    draft_id = await create(env)
    session = env.service.transport.open_imap(env.params)
    try:
        session.select(env.mailbox.drafts_folder, readonly=False)
        uid, = session.uid_search("ALL")
        message = BytesParser(policy=policy.default).parsebytes(session.fetch_message(uid))
        plain = message.get_body(preferencelist=("plain",))
        plain.set_content("User changed this in another mail app")
        session.append(env.mailbox.drafts_folder, message.as_bytes(), flags="(\\Draft)")
        session.delete_uid(uid)
    finally:
        session.logout()
    read = await call(env, "mail_read_draft", {"draft_id": draft_id})
    result = await call(env, "mail_update_draft", {"draft_id": draft_id,
        "body_sha256": read.content["body_sha256"], "body": "Must not overwrite user edit"})
    assert result.is_error and "edited outside Handler" in str(result.content)
    assert (await env.mail_store.get_draft(draft_id))["status"] == "draft"
    session = env.service.transport.open_imap(env.params)
    try:
        session.select(env.mailbox.drafts_folder)
        uid, = session.uid_search("ALL")
        assert b"User changed this" in session.fetch_message(uid)
    finally:
        session.logout()
