"""The tenant context cache (tenancy/contexts.py): nothing is closed under a user."""
import asyncio

from handler.runtime.tenancy.contexts import ContextCache, TenantContext


def builder(closed: list[str], gates: dict[str, asyncio.Event] | None = None):
    async def build(name):
        if gates and name in gates:
            await gates[name].wait()

        async def close():
            closed.append(name)

        return TenantContext(name=name, key=name, store=None, close=close)

    return build


async def test_a_fresh_build_is_not_trimmed_before_its_waiters_take_it():
    """With more tenants than slots, a build that finishes has no user until the
    requests waiting on it resume; another tenant's release in that gap trims the
    cache, and must not pick the fresh entry (it was closed under them before)."""
    closed: list[str] = []
    gate = {"b": asyncio.Event()}
    cache = ContextCache(builder(closed, gate), max_size=1)
    release = {"a": asyncio.Event(), "c": asyncio.Event()}

    async def hold(name):
        async with cache.use(name):
            await release[name].wait()

    held = [asyncio.create_task(hold("a")), asyncio.create_task(hold("c"))]
    await asyncio.sleep(0.01)
    uses = [cache.use("b") for _ in range(3)]  # kept: a dropped one releases its entry
    waiters = [asyncio.create_task(use.__aenter__()) for use in uses]
    await asyncio.sleep(0)
    gate["b"].set()  # b's build finishes ...
    release["c"].set()  # ... and c's release trims while b's waiters have not resumed
    await held[1]
    contexts = await asyncio.gather(*waiters)
    await cache.settle()
    assert "b" not in closed
    assert {c.name for c in contexts} == {"b"} and cache.builds == 3  # a, c, b once
    assert closed == ["c"]
    release["a"].set()
    await held[0]
    await cache.close()


async def test_an_older_entry_still_closes_while_a_rebuild_has_waiters():
    closed: list[str] = []
    gate = {}
    cache = ContextCache(builder(closed, gate), max_size=4)
    async with cache.use("a"):
        pass
    gate["a"] = asyncio.Event()
    cache.invalidate("a")  # the old one is idle: closed now
    use = cache.use("a")
    waiter = asyncio.create_task(use.__aenter__())
    await asyncio.sleep(0)
    await cache.settle()
    assert closed == ["a"]
    gate["a"].set()
    assert (await waiter).name == "a"
    await cache.close()
