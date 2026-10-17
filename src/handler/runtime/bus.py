"""In-process pub/sub for live run events (web UI streaming).

Best effort only: nothing here is durable. The durable record is the store.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .types import utcnow


@dataclass(frozen=True)
class BusMessage:
    thread_key: str
    run_id: str | None
    type: str  # "run_started" | "run_event" | "run_finished" | "event_queued"
    data: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: utcnow().isoformat())


class RunEventBus:
    def __init__(self, max_queue: int = 1000) -> None:
        self._subs: dict[str, set[asyncio.Queue[BusMessage]]] = defaultdict(set)
        self._max_queue = max_queue

    def publish(self, msg: BusMessage) -> None:
        for key in (msg.thread_key, "*"):
            for q in list(self._subs.get(key, ())):
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    pass  # slow subscriber: drop rather than block runs

    @asynccontextmanager
    async def subscribe(self, thread_key: str = "*") -> AsyncIterator[asyncio.Queue[BusMessage]]:
        """Subscribe to one thread's messages, or "*" for all threads."""
        q: asyncio.Queue[BusMessage] = asyncio.Queue(self._max_queue)
        self._subs[thread_key].add(q)
        try:
            yield q
        finally:
            self._subs[thread_key].discard(q)
            if not self._subs[thread_key]:
                self._subs.pop(thread_key, None)
