import asyncio

from handler.runtime.dispatcher import Dispatcher
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import Event, Lane, ThreadSpec, ThreadType


async def test_global_limit_leaves_other_lane_unclaimed(tmp_path):
    store = SQLiteStore(tmp_path / "handler.db")
    gate = asyncio.Event()

    class Executor:
        async def execute(self, lease):
            await gate.wait()
            assert await store.fail(lease, "test complete", None)

    dispatcher = Dispatcher(store, Executor(), max_concurrent=1)
    try:
        for lane in (Lane.INTERACTIVE, Lane.BACKGROUND):
            key = str(lane)
            await store.put(Event(type="test", source="test", payload={},
                                  idempotency_key=key, thread_key=key),
                            ThreadSpec(key=key, type=ThreadType.CHAT, lane=lane))
        assert await dispatcher.claim_once() == 1
        assert await dispatcher.claim_once() == 0
        background = await store.get_thread(str(Lane.BACKGROUND))
        assert background.lease_owner is None
        gate.set()
        while dispatcher.active_count:
            await asyncio.sleep(0.01)
        assert await dispatcher.claim_once() == 1
        background = await store.get_thread(str(Lane.BACKGROUND))
        assert background.lease_owner == dispatcher.owner
    finally:
        gate.set()
        await dispatcher._drain(1)
        await store.close()
