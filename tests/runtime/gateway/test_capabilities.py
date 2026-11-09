from types import SimpleNamespace
from unittest.mock import AsyncMock

from handler.runtime.context import RunContext
from handler.runtime.gateway.base import ToolResult, ToolSpec
from handler.runtime.gateway.gmail import GmailTools
from handler.runtime.gateway.google_oauth import GMAIL_READONLY, GMAIL_COMPOSE, GMAIL_SEND
from handler.runtime.types import Risk


async def test_capabilities_are_resolved_once_per_provider_and_per_user(gateway):
    class Provider:
        def __init__(self):
            self.checked = []

        async def available_tool_names(self, ctx):
            self.checked.append(ctx.user_id)
            return {"private_a", "private_b"} if ctx.user_id == "alice" else set()

        def tools(self):
            async def handle(ctx, args):
                return ToolResult("ok")
            return [ToolSpec(name, name, Risk.READ, name, {"type": "object"}, handle)
                    for name in ("private_a", "private_b")]

    provider = Provider()
    gateway.register(provider)
    alice = RunContext("a", "chat:alice:c", "alice")
    bob = RunContext("b", "chat:bob:c", "bob")
    assert {"private_a", "private_b"} <= {s.name for s in await gateway.specs_for(alice)}
    assert "private_a" not in {s.name for s in await gateway.specs_for(bob)}
    assert provider.checked == ["alice", "bob"]
    assert "private_a" in {s.name for s in gateway.specs()}  # registration != availability


async def test_gmail_inventory_matches_effective_owner_scopes_without_refresh(tmp_path):
    credentials = SimpleNamespace(granted_scopes=AsyncMock(return_value=None), resolve=AsyncMock())
    gmail = GmailTools(credentials, workspace_for=lambda _: tmp_path,
                       owner_for=lambda ctx: f"user:{ctx.user_id}")
    gmail.tools()
    ctx = RunContext("r", "chat:alice:c", "alice")
    assert await gmail.available_tool_names(ctx) == set()
    credentials.granted_scopes.assert_awaited_with("user:alice")
    credentials.granted_scopes.return_value = [GMAIL_READONLY]
    read_names = await gmail.available_tool_names(ctx)
    assert "gmail_search" in read_names and "gmail_read_message" in read_names
    assert "gmail_create_draft" not in read_names and "gmail_send" not in read_names
    credentials.granted_scopes.return_value = [GMAIL_COMPOSE, GMAIL_SEND]
    names = await gmail.available_tool_names(ctx)
    assert "gmail_create_draft" in names and "gmail_send" in names
    credentials.resolve.assert_not_awaited()
