"""Background task: discover Crontal's curated MCP tools, then expose them.

Retries with backoff until Crontal is reachable or the instance stops.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ...gateway.crontal import crontal_connector

logger = logging.getLogger("handler.runtime.server")


async def run_crontal_connector(crontal_url: str | None, token: str | None, gateway: Any,
                                stop: asyncio.Event) -> None:
    if not crontal_url or not token:
        if crontal_url:
            logger.error("CRONTAL_URL set but CRONTAL_AGENT_TOKEN missing; Crontal tools off")
        return
    connector = crontal_connector(crontal_url, token)
    delay = 5.0
    while not stop.is_set():
        try:
            await connector.start()
            gateway.register(connector)
            logger.info("crontal tools registered",
                        extra={"tools": [s.name for s in connector.tools()]})
            return
        except Exception as exc:
            logger.warning("crontal connector not ready, retrying",
                           extra={"error": str(exc), "retry_in": delay})
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
            delay = min(delay * 2, 300.0)
