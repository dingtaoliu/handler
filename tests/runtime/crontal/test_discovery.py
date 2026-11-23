"""Crontal tool discovery: once per host with a TTL, retried on use, never a task."""
import asyncio
import logging

from handler.runtime.gateway.crontal import CRONTAL_TOOLS
from handler.runtime.integrations.crontal.connector import CrontalDiscovery, CrontalTools

URL = "https://crontal.example/api/v1/agent-mcp"


class Tool:
    def __init__(self, name):
        self.name, self.description, self.inputSchema = name, name, {"type": "object"}


class FakeConnector:
    """Stands in for crontal_connector(url, token): records the token it was built
    with and every listing it was asked for."""

    listings: list[str] = []
    fail: list[Exception] = []

    def __init__(self, url, token):
        self.url, self.token, self.adopted = url, token, None

    async def discover(self):
        FakeConnector.listings.append(self.token)
        if FakeConnector.fail:
            raise FakeConnector.fail.pop(0)
        return {name: Tool(name) for name in CRONTAL_TOOLS}

    def adopt(self, tools):
        self.adopted = tools

    def tools(self):
        return [type("Spec", (), {"name": f"crontal_{n}"}) for n in self.adopted]


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class Gateway:
    def __init__(self):
        self.registered = []

    def register(self, provider):
        self.registered.append(provider)


def discovery(clock=None, **kwargs):
    FakeConnector.listings, FakeConnector.fail = [], []
    return CrontalDiscovery(clock=clock or Clock(), connect=FakeConnector, **kwargs)


async def test_one_listing_for_every_tenant_each_with_its_own_token():
    cache = discovery()
    connectors = await asyncio.gather(*(cache.connector(URL, f"token-{n}") for n in range(20)))
    assert FakeConnector.listings == ["token-0"]  # discovered once, concurrently shared
    assert [c.token for c in connectors] == [f"token-{n}" for n in range(20)]
    assert all(c.adopted is connectors[0].adopted for c in connectors)


async def test_listing_expires_after_the_ttl():
    clock = Clock()
    cache = discovery(clock, ttl_seconds=60)
    await cache.connector(URL, "a")
    clock.now += 59
    await cache.connector(URL, "b")
    assert FakeConnector.listings == ["a"]
    clock.now += 2
    await cache.connector(URL, "c")
    assert FakeConnector.listings == ["a", "c"]


async def test_a_failure_backs_off_host_wide_then_recovers(caplog):
    clock = Clock()
    cache = discovery(clock)
    FakeConnector.fail = [ConnectionError("crontal down"), ConnectionError("still down")]
    with caplog.at_level(logging.WARNING):
        assert await cache.connector(URL, "a") is None
    assert "crontal tool discovery failed" in caplog.text
    # Within the backoff no tenant tries again (no network, no task).
    assert await cache.connector(URL, "b") is None
    assert FakeConnector.listings == ["a"]
    clock.now += 5
    assert await cache.connector(URL, "b") is None  # second failure: backoff doubles
    clock.now += 9
    assert await cache.connector(URL, "c") is None
    assert FakeConnector.listings == ["a", "b"]
    clock.now += 1
    assert (await cache.connector(URL, "c")).token == "c"
    assert FakeConnector.listings == ["a", "b", "c"]


async def test_an_expired_listing_is_kept_when_rediscovery_fails():
    clock = Clock()
    cache = discovery(clock, ttl_seconds=60)
    await cache.connector(URL, "a")
    clock.now += 61
    FakeConnector.fail = [TimeoutError()]
    connector = await cache.connector(URL, "b")
    assert connector is not None and connector.token == "b"


async def test_a_listing_missing_a_curated_tool_counts_as_a_failure():
    cache = discovery()

    class Partial(FakeConnector):
        async def discover(self):
            return {"app_get_rfqs": Tool("app_get_rfqs")}

        def adopt(self, tools):
            raise ValueError("connector 'crontal': upstream has no tool(s) [...]")

    cache._connect = Partial
    assert await cache.connector(URL, "a") is None


async def test_a_slow_crontal_does_not_hold_a_context_load():
    cache = discovery(timeout_seconds=0.05)

    class Slow(FakeConnector):
        async def discover(self):
            await asyncio.sleep(10)

    cache._connect = Slow
    assert await asyncio.wait_for(cache.connector(URL, "a"), 1) is None


async def test_tenant_tools_register_once_available(caplog):
    clock = Clock()
    cache = discovery(clock)
    gateway, changed = Gateway(), []
    tools = CrontalTools(cache, URL, "token-m7", gateway, "m7",
                         on_registered=lambda: changed.append(True))
    FakeConnector.fail = [ConnectionError("down")]
    with caplog.at_level(logging.INFO):
        assert await tools.ensure() is False  # the context loads without them
        assert await tools.ensure() is False  # before a run, within the backoff
    assert gateway.registered == [] and FakeConnector.listings == ["token-m7"]
    assert caplog.text.count("crontal tools not available yet") == 1
    clock.now += 5
    with caplog.at_level(logging.INFO):
        assert await tools.ensure() is True
    assert [c.token for c in gateway.registered] == ["token-m7"] and changed == [True]
    record = next(r for r in caplog.records if r.message == "crontal tools registered")
    assert record.tenant == "m7"
    assert await tools.ensure() is True and len(gateway.registered) == 1


async def test_no_token_means_no_crontal_tools(caplog):
    tools = CrontalTools(discovery(), URL, None, Gateway(), "m8")
    with caplog.at_level(logging.ERROR):
        assert await tools.ensure() is True  # nothing to retry
    assert "CRONTAL_AGENT_TOKEN missing" in caplog.text
    assert FakeConnector.listings == []
