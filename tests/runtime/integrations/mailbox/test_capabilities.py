from types import SimpleNamespace

from handler.runtime.context import RunContext
from handler.runtime.integrations.mailbox.tools import MailTools


async def test_intake_has_read_but_no_draft_or_send_capabilities(env):
    tools = MailTools(env.service, env.mail_store, workspace_for=lambda _: env.tmp_path)
    ctx = RunContext("r", "chat:7:test", "7")
    assert await tools.available_tool_names(ctx) == {"mail_list_mailboxes"}
    tools.intake = SimpleNamespace(address="intake@example.test")
    names = await tools.available_tool_names(ctx)
    assert "mail_read" in names and "mail_search" in names and "mail_remember" in names
    assert "mail_send" not in names and "mail_draft_reply" not in names
    await env.add_mailbox()
    names = await tools.available_tool_names(ctx)
    assert "mail_send" in names and "mail_draft_reply" in names
