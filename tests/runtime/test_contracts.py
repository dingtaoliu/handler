import asyncio

from handler.runtime.bus import BusMessage, RunEventBus
from handler.runtime.context import RunContext, current_run, reset_current_run, set_current_run
from handler.runtime.types import TokenUsage


def test_token_usage_adds():
    a = TokenUsage(10, 4, 0, 3, 1)
    b = TokenUsage(5, 1, 2, 2, 0)
    assert a + b == TokenUsage(15, 5, 2, 5, 1)


async def test_run_context_is_task_local():
    seen: dict[str, str | None] = {}

    async def worker(name: str):
        token = set_current_run(RunContext(run_id=name, thread_key=f"t:{name}"))
        await asyncio.sleep(0)
        ctx = current_run()
        seen[name] = ctx.run_id if ctx else None
        reset_current_run(token)

    await asyncio.gather(worker("a"), worker("b"))
    assert seen == {"a": "a", "b": "b"}
    assert current_run() is None


async def test_bus_delivers_to_thread_and_wildcard():
    bus = RunEventBus()
    async with bus.subscribe("chat:u:1") as q1, bus.subscribe() as qall:
        bus.publish(BusMessage(thread_key="chat:u:1", run_id=None, type="event_queued"))
        assert (await q1.get()).type == "event_queued"
        assert (await qall.get()).thread_key == "chat:u:1"


def test_json_logs_include_extra_fields():
    import io
    import json
    import logging

    from handler.runtime.observability.logging import JsonFormatter

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("extra-test")
    log.addHandler(handler)
    log.propagate = False
    log.warning("run failed", extra={"error": "boom", "retry_in": 10})
    line = json.loads(buf.getvalue())
    assert line["error"] == "boom" and line["retry_in"] == 10 and line["msg"] == "run failed"
