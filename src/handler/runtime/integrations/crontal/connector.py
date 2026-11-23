"""Crontal's curated MCP tools, discovered while a tenant's context loads.

The listing (initialize + tools/list on Crontal's agent-mcp) is the same for every
tenant, so the host discovers it once per Crontal URL and keeps it for a TTL
(`CrontalDiscovery`). Each tenant gets its own connector built from that listing,
so its calls carry its own agent token and AI identity.

A failed discovery is not retried by a task: the next context load, or the next run
of a loaded context, tries again once the host-wide backoff has passed (5 s doubling
to 5 minutes). Until then the tenant works without the Crontal tools: chat runs,
and a warning is logged.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any

from ...gateway.crontal import crontal_connector

logger = logging.getLogger("handler.runtime.server")

DEFAULT_TTL_SECONDS = 3600.0
DEFAULT_TIMEOUT_SECONDS = 10.0
FIRST_RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 300.0


def discovery_settings(env: Mapping[str, str]) -> dict[str, float]:
    return {"ttl_seconds": float(env.get("HANDLER_CRONTAL_DISCOVERY_TTL_SECONDS",
                                         str(DEFAULT_TTL_SECONDS))),
            "timeout_seconds": float(env.get("HANDLER_CRONTAL_DISCOVERY_TIMEOUT_SECONDS",
                                             str(DEFAULT_TIMEOUT_SECONDS)))}


@dataclass
class _Listing:
    tools: dict[str, Any] | None = None  # name -> mcp Tool, from the last success
    fetched_at: float = 0.0
    retry_at: float = 0.0  # after a failure: no attempt before this
    delay: float = FIRST_RETRY_SECONDS
    error: str = ""


class CrontalDiscovery:
    """The host's cache of Crontal tool listings, keyed by Crontal URL."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                 clock: Callable[[], float] = monotonic,
                 connect: Callable[[str, str], Any] = crontal_connector) -> None:
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._connect = connect
        self._listings: dict[str, _Listing] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.fetches = 0  # discoveries attempted (tests, logs)

    def _fresh(self, listing: _Listing) -> bool:
        return (listing.tools is not None
                and self._clock() - listing.fetched_at < self.ttl_seconds)

    async def connector(self, url: str, token: str) -> Any | None:
        """A started connector for this tenant's token, or None while Crontal's tools
        can't be listed (and no earlier listing is kept)."""
        listing = self._listings.setdefault(url, _Listing())
        if not self._fresh(listing) and self._clock() >= listing.retry_at:
            lock = self._locks.setdefault(url, asyncio.Lock())
            async with lock:  # one discovery per URL at a time; the rest reuse it
                if not self._fresh(listing) and self._clock() >= listing.retry_at:
                    await self._discover(url, token, listing)
        if listing.tools is None:
            return None
        connector = self._connect(url, token)
        connector.adopt(listing.tools)
        return connector

    async def _discover(self, url: str, token: str, listing: _Listing) -> None:
        self.fetches += 1
        try:
            probe = self._connect(url, token)
            tools = await asyncio.wait_for(probe.discover(), self.timeout_seconds)
            probe.adopt(tools)  # every curated tool is there
        except Exception as exc:  # noqa: BLE001 - kept for the next attempt
            listing.error = f"{type(exc).__name__}: {exc}"
            listing.retry_at = self._clock() + listing.delay
            logger.warning("crontal tool discovery failed", extra={
                "error": listing.error, "retry_in": listing.delay,
                "kept_listing": listing.tools is not None})
            listing.delay = min(listing.delay * 2, MAX_RETRY_SECONDS)
            return
        listing.tools, listing.fetched_at = tools, self._clock()
        listing.retry_at, listing.delay, listing.error = 0.0, FIRST_RETRY_SECONDS, ""
        logger.info("crontal tools discovered", extra={"tools": sorted(tools)})


class CrontalTools:
    """One tenant context's Crontal tools: registered on its gateway once a listing
    is available. `ensure()` is called while the context loads and before each run;
    it costs nothing once registered or while the host backoff runs."""

    def __init__(self, discovery: CrontalDiscovery, url: str | None, token: str | None,
                 gateway: Any, tenant: str,
                 on_registered: Callable[[], None] | None = None) -> None:
        self.discovery, self.url, self.token = discovery, url, token
        self.gateway, self.tenant = gateway, tenant
        self.on_registered = on_registered
        self.registered = False
        self._warned = False

    async def ensure(self) -> bool:
        if self.registered:
            return True
        if not self.url or not self.token:
            if self.url and not self._warned:
                self._warned = True
                logger.error("CRONTAL_URL set but CRONTAL_AGENT_TOKEN missing; Crontal "
                             "tools off", extra={"tenant": self.tenant})
            return True  # nothing to retry
        try:
            connector = await self.discovery.connector(self.url, self.token)
        except Exception:  # noqa: BLE001 - never fails the context or the run
            logger.exception("crontal tools failed to load", extra={"tenant": self.tenant})
            connector = None
        if connector is None:
            if not self._warned:  # once per context; discovery logs each attempt
                self._warned = True
                logger.warning("crontal tools not available yet; the tenant works "
                               "without them", extra={"tenant": self.tenant})
            return False
        self.gateway.register(connector)
        self.registered = True
        logger.info("crontal tools registered", extra={
            "tenant": self.tenant, "tools": [s.name for s in connector.tools()]})
        if self.on_registered is not None:
            self.on_registered()
        return True


# For a context built with no host services (unit tests of a lone integration).
_default_discovery: CrontalDiscovery | None = None


def default_discovery() -> CrontalDiscovery:
    global _default_discovery
    if _default_discovery is None:
        _default_discovery = CrontalDiscovery()
    return _default_discovery
