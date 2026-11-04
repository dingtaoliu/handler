"""Uploads to Crontal Files say where the file came from."""

from __future__ import annotations

import json
from email import message_from_bytes
from email.policy import HTTP
from pathlib import Path

import httpx
import pytest

from handler.runtime.context import RunContext
from handler.runtime.gateway.crontal_api import CrontalApiTools

from .test_crontal_api import AGENT, FakeCrontal


def form_fields(request: httpx.Request) -> dict[str, str]:
    raw = (f"Content-Type: {request.headers['content-type']}\r\n\r\n").encode() + request.content
    message = message_from_bytes(raw, policy=HTTP)
    fields = {}
    for part in message.iter_parts():
        if part.get_filename() is None:
            fields[part.get_param("name", header="content-disposition")] = part.get_content()
    return fields


@pytest.fixture
def setup(tmp_path: Path):
    fake = FakeCrontal()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    sources = []
    tools = CrontalApiTools("https://crontal.test", AGENT,
                            workspace_for=lambda key: tmp_path / "ws",
                            client=client, upload_sources=sources)
    call = {s.name: s for s in tools.tools()}["crontal_api_call"]
    ctx = RunContext(run_id="r1", thread_key="mail:mbx1:conv9", user_id="7")
    workspace = tmp_path / "ws"
    (workspace / "mail/mbx1/m1/attachments").mkdir(parents=True)
    (workspace / "mail/mbx1/m1/attachments/po.pdf").write_bytes(b"%PDF")
    (workspace / "summary.md").write_text("notes")
    return fake, call, ctx, sources


def last_upload(fake):
    return next(r for r in reversed(fake.requests) if r.url.path == "/api/v1/files/upload")


async def test_agent_made_files_are_ai_generated_with_the_thread(setup):
    fake, call, ctx, _ = setup
    await call.handler(ctx, {"method": "POST", "path": "/api/v1/files/upload",
                             "upload_file": "summary.md",
                             "form": {"folder_path": "/Notes/"}})
    fields = form_fields(last_upload(fake))
    assert fields["source"] == "ai_generated"
    assert json.loads(fields["source_detail"]) == {"thread_key": "mail:mbx1:conv9"}
    assert fields["folder_path"] == "/Notes/"


async def test_email_attachments_are_email_with_the_message(setup):
    fake, call, ctx, sources = setup
    seen = []

    async def mail_source(run_ctx, path):
        seen.append(path)
        if path.startswith("mail/"):
            return {"mailbox": "sales@x.cn", "sender": "li@client.cn",
                    "subject": "RFQ", "message_id": "<m1@x>"}
        return None

    sources.append(mail_source)  # registered after the tools were built
    await call.handler(ctx, {"method": "POST", "path": "/api/v1/files/upload",
                             "upload_file": "./mail/mbx1/m1/attachments/po.pdf"})
    fields = form_fields(last_upload(fake))
    assert seen == ["mail/mbx1/m1/attachments/po.pdf"]
    assert fields["source"] == "email"
    assert json.loads(fields["source_detail"]) == {
        "mailbox": "sales@x.cn", "sender": "li@client.cn", "subject": "RFQ",
        "message_id": "<m1@x>", "thread_key": "mail:mbx1:conv9"}


async def test_agent_values_win_and_other_uploads_are_untouched(setup):
    fake, call, ctx, sources = setup

    async def broken(run_ctx, path):
        raise RuntimeError("store down")

    sources.append(broken)
    await call.handler(ctx, {"method": "POST", "path": "/api/v1/files/upload",
                             "upload_file": "summary.md",
                             "form": {"source": "email",
                                      "source_detail": {"sender": "a@b.cn"}}})
    fields = form_fields(last_upload(fake))
    assert fields["source"] == "email"
    assert json.loads(fields["source_detail"]) == {"sender": "a@b.cn",
                                                   "thread_key": "mail:mbx1:conv9"}

    await call.handler(ctx, {"method": "POST", "path": "/api/v1/bid/3/upload",
                             "upload_file": "summary.md"})
    bid = next(r for r in reversed(fake.requests) if r.url.path == "/api/v1/bid/3/upload")
    assert "source" not in form_fields(bid)
