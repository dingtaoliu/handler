"""The agent's Crontal AI identity: discovery, caching, on-behalf-of, prompt."""

from __future__ import annotations

import httpx
import pytest

from handler.runtime.context import RunContext
from handler.runtime.gateway.crontal_identity import (
    CrontalIdentity,
    CrontalIdentityClient,
    IdentityUnavailable,
    crontal_identity,
    on_behalf_of,
)
from handler.runtime.instructions import build_instructions
from handler.runtime.memory import MemoryTools
from handler.runtime.settings import SettingsService
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import ThreadType


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_identity_is_discovered_and_cached():
    calls = []

    def crontal(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/api/v1/agent-access/identity"
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json={"user_id": 501, "display_name": "Brava AI",
                                         "merchant_id": 2, "merchant_name": "Brava"})

    client = CrontalIdentityClient("https://crontal.test/", "tok", client=_client(crontal))
    assert client.cached is None
    identity = await client.get()
    assert identity == CrontalIdentity(user_id="501", display_name="Brava AI",
                                       merchant_id="2", merchant_name="Brava")
    assert await client.get() is identity and len(calls) == 1
    await client.get(refresh=True)
    assert len(calls) == 2 and client.cached == identity


@pytest.mark.parametrize("status, definitive", [(401, True), (403, True), (404, True),
                                                (500, False), (502, False)])
async def test_refusals_say_whether_they_are_definitive(status, definitive):
    client = CrontalIdentityClient(
        "https://crontal.test", "tok",
        client=_client(lambda request: httpx.Response(status, json={"error": "x"})))
    with pytest.raises(IdentityUnavailable) as raised:
        await client.get()
    assert raised.value.definitive is definitive


async def test_unreachable_crontal_is_not_definitive():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = CrontalIdentityClient("https://crontal.test", "tok", client=_client(down))
    with pytest.raises(IdentityUnavailable) as raised:
        await client.get()
    assert raised.value.definitive is False


def test_shared_client_per_crontal_and_token():
    a = crontal_identity("https://c.test", "token-a")
    assert crontal_identity("https://c.test/", "token-a") is a
    assert crontal_identity("https://c.test", "token-b") is not a


def test_on_behalf_of_is_the_chatting_person_only():
    assert on_behalf_of(RunContext(run_id="r", thread_key="chat:7:x", user_id="7")) == "7"
    assert on_behalf_of(RunContext(run_id="r", thread_key="chat:7:x", user_id="7",
                                   acting_as="user:9")) == "9"
    for ctx in (RunContext(run_id="r", thread_key="email:a:b", user_id="1", acting_as="1"),
                RunContext(run_id="r", thread_key="crontal:rfq:1", user_id="7"),
                RunContext(run_id="r", thread_key="automation:x"),
                RunContext(run_id="r", thread_key="chat:danny:x", user_id="danny"),
                None):
        assert on_behalf_of(ctx) is None


class _Identity:
    def __init__(self, identity=None):
        self.identity, self.cached = identity, None

    async def get(self, *, refresh=False):
        if self.identity is None:
            raise IdentityUnavailable("down", definitive=False)
        return self.identity


async def test_prompt_names_the_ai_identity(tmp_path):
    store = SQLiteStore(tmp_path / "h.db")
    try:
        settings, memory = SettingsService(store), MemoryTools(store)
        text = await build_instructions(
            settings, memory, speaker="7", thread_type=ThreadType.CHAT,
            crontal_identity=_Identity(CrontalIdentity(
                user_id="501", display_name="Brava AI", merchant_name="Brava")))
        assert 'act as "Brava AI" (Crontal user 501), the AI member of Brava' in text
        fallback = await build_instructions(settings, memory, speaker=None,
                                            thread_type=ThreadType.EMAIL,
                                            crontal_identity=_Identity())
        assert "your merchant's AI user" in fallback
        plain = await build_instructions(settings, memory, speaker=None,
                                         thread_type=ThreadType.EMAIL)
        assert "AI member" not in plain and "AI user (" not in plain
    finally:
        await store.close()
