"""Conversation metadata behaves identically on both database backends."""
from datetime import timedelta

from handler.runtime.types import Message, Role


async def test_metadata_title_watermark_and_activity(domain_store, spec, event, clock):
    store = domain_store
    await store.put(event("first"), spec)
    lease, = await store.claim("worker", spec.lane, 1, 60)
    first = Message(spec.key, Role.ASSISTANT, [{"type": "text", "text": "First response"}],
                    created_at=clock())
    assert await store.add_messages(lease, [first])
    assert await store.set_thread_title(spec.key, "Generated name", manual=False)
    assert not await store.set_thread_title(spec.key, "Later name", manual=False)
    await store.set_thread_title(spec.key, "Manual name")
    meta = (await store.thread_metadata([spec.key], "u"))[spec.key]
    assert meta["title"] == "Manual name"
    assert meta["unread"]
    await store.mark_thread_read(spec.key, "u", first.created_at)
    assert not (await store.thread_metadata([spec.key], "u"))[spec.key]["unread"]
    assert (await store.thread_metadata([spec.key], "other"))[spec.key]["unread"]
    clock.advance(1)
    second = Message(spec.key, Role.TOOL, [{"type": "text", "text": "Later response"}],
                     created_at=clock())
    assert await store.add_messages(lease, [second])
    await store.mark_thread_read(spec.key, "u", first.created_at)
    assert (await store.thread_metadata([spec.key], "u"))[spec.key]["unread"]
    assert await store.mark_thread_read(spec.key, "u", clock() + timedelta(days=1)) == second.created_at
    assert await store.mark_thread_read(spec.key, "u", first.created_at) == second.created_at
    assert await store.thread_metadata([], "u") == {}
    assert not await store.set_thread_title("missing", "Missing")
