"""Crontal's API catalog, shared by the crontal_api tools and the prompt.

`CrontalRouteCatalog` fetches and caches `GET /api/v1/agent-access/routes`
(the route list) and `/routes/describe` (one route's contract) for one
instance (Crontal URL + agent token). `crontal_api_routes`/`describe` read
through it, and `prompt_text()` renders a compact catalog for the stable part
of the instructions so agents don't spend a model round trip on discovery:

- one line per route (`METHOD path — summary`), grouped by resource;
- full contracts for a configured list of common routes, in that priority
  order, while they fit `max_chars` (all route lines are always kept; when the
  lines alone exceed the cap the catalog is left out).

The text is byte-stable while Crontal is unchanged (sorted, compact sorted
JSON, no timestamps), so it stays in the cached prompt prefix. It is rendered
at most once per `ttl` (1 h) and refreshed lazily: a stale copy is served while
a background refresh runs, and a run never waits longer than `wait_seconds`
for a first fetch. Without a catalog the prompt falls back to discovery tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)

ROUTES_PATH = "/api/v1/agent-access/routes"
DESCRIBE_PATH = "/api/v1/agent-access/routes/describe"
ROUTES_TTL_SECONDS = 600
SPEC_TTL_SECONDS = 600
PROMPT_TTL_SECONDS = 3600
PROMPT_WAIT_SECONDS = 3.0
FAILURE_RETRY_SECONDS = 60
SUMMARY_CHARS = 120
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
# Never reachable by the agent (refused, and left out of the catalog):
# its own plumbing and site-admin tools.
HIDDEN_PREFIXES = ("/api/v1/agent-access", "/api/v1/agent-mcp", "/api/v1/handler",
                   "/api/v1/admin", "/api/v1/marketplace/query", "/api/v1/files/stats")
# Contract keys already shown in the route's heading line.
_HEADING_KEYS = ("method", "path", "summary", "has_schema")
_PARAM = re.compile(r"<(?:\w+(?:\([^)]*\))?:)?(\w+)>")

# Default full contracts, most important first (fewer fit a smaller cap):
# what agents use most, i.e. received/own RFQs, quotes, files, line items,
# contacts and notifications.
DEFAULT_FULL_SPECS = [
    "GET /api/v1/rfq/received",
    "GET /api/v1/rfq/received/<int:rfq_id>",
    "POST /api/v1/rfq/received",
    "GET /api/v1/rfq",
    "GET /api/v1/rfq/<int:rfq_id>",
    "GET /api/v1/bid/merchant/<int:merchant_id>",
    "GET /api/v1/files/browse",
    "GET /api/v1/files/search",
    "POST /api/v1/files/upload",
    "PATCH /api/v1/files/<int:file_id>",
    "POST /api/v1/demand_listing",
    "GET /api/v1/contact",
    "GET /api/v1/notification",
]


def hidden(path: str) -> bool:
    # A plain prefix test (not per segment): over-hiding is the safe side.
    return path.lower().startswith(HIDDEN_PREFIXES)


def spec_key(entry: str) -> tuple[str, str] | None:
    """`"GET /api/v1/rfq"` -> ("GET", "/api/v1/rfq"); None when malformed."""
    method, _, path = entry.strip().partition(" ")
    method, path = method.upper(), path.strip()
    if method not in METHODS or not path.startswith("/api/v1/"):
        return None
    return method, path


def _summary(text: str) -> str:
    """Crontal's summary, cut at a word boundary to SUMMARY_CHARS characters."""
    text = " ".join((text or "").split())
    if len(text) <= SUMMARY_CHARS:
        return text
    return text[:SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


def _display(path: str) -> str:
    return _PARAM.sub(r"{\1}", path)


def _group(path: str) -> str:
    parts = path.removeprefix("/api/v1/").split("/")
    return parts[0] or "other"


def render_route_lines(routes: Iterable[dict[str, Any]]) -> str:
    """Route lines grouped by resource; deterministic for the same routes."""
    groups: dict[str, list[str]] = {}
    for route in sorted(routes, key=lambda r: (str(r.get("path", "")),
                                               list(r.get("methods") or []))):
        path = str(route.get("path", ""))
        methods = [m for m in route.get("methods") or [] if m in METHODS]
        if not methods or not path.startswith("/api/v1/") or hidden(path):
            continue
        summary = _summary(str(route.get("summary") or ""))
        line = f"{','.join(sorted(methods))} {_display(path)}" + (f" — {summary}" if summary else "")
        groups.setdefault(_group(path), []).append(line)
    return "\n".join(f"[{name}]\n" + "\n".join(lines) for name, lines in sorted(groups.items()))


def render_spec(method: str, path: str, spec: dict[str, Any]) -> str:
    body = {k: v for k, v in spec.items() if k not in _HEADING_KEYS}
    if body.get("success_status") == 200:
        body.pop("success_status")
    return (f"### {method} {_display(path)}\n"
            + json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


CATALOG_HEADER = (
    "# Crontal API catalog\n"
    "Every endpoint you can call with crontal_api_get/crontal_api_call, grouped by "
    "resource. Fill {name} path parameters with ids. Contracts below are exactly what "
    "crontal_api_describe returns (minus method/path/summary; success_status 200 "
    "unless shown).")


def render_catalog(routes: list[dict[str, Any]], specs: dict[tuple[str, str], Any],
                   full_specs: Iterable[str], max_chars: int) -> str | None:
    """The prompt text; None when even the route lines exceed `max_chars`."""
    text = CATALOG_HEADER + "\n\n" + render_route_lines(routes)
    if len(text) > max_chars:
        return None
    blocks: list[str] = []
    budget = max_chars - len(text) - len("\n\n## Contracts")
    seen: set[tuple[str, str]] = set()
    for entry in full_specs:
        key = spec_key(entry)
        if key is None or key in seen:
            continue
        seen.add(key)
        spec = specs.get(key)
        if not isinstance(spec, dict) or not spec.get("has_schema", True):
            continue
        block = render_spec(key[0], key[1], spec)
        if len(block) + 2 <= budget:  # greedy in priority order; skip what doesn't fit
            blocks.append(block)
            budget -= len(block) + 2
    if blocks:
        text += "\n\n## Contracts\n\n" + "\n\n".join(blocks)
    return text


class CrontalRouteCatalog:
    """Fetches and caches Crontal's route list and route contracts."""

    def __init__(self, base_url: str, agent_token: str, *,
                 client: httpx.AsyncClient | None = None,
                 prompt_ttl: float = PROMPT_TTL_SECONDS,
                 wait_seconds: float = PROMPT_WAIT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self._agent_token = agent_token
        self._client = client or httpx.AsyncClient(timeout=60)
        self.prompt_ttl = prompt_ttl
        self.wait_seconds = wait_seconds
        self._routes: tuple[list[dict[str, Any]], float] | None = None
        self._specs: dict[tuple[str, str], tuple[Any, float]] = {}
        # Rendered prompt text by (max_chars, full specs): (text or None, rendered at).
        self._prompts: dict[tuple, tuple[str | None, float]] = {}
        self._refreshing: dict[tuple, asyncio.Task] = {}
        self._failed_at: dict[tuple, float] = {}

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._agent_token}"}

    async def routes(self) -> list[dict[str, Any]]:
        """The raw route list (cached ROUTES_TTL_SECONDS); raises httpx errors."""
        if self._routes is None or time.time() - self._routes[1] > ROUTES_TTL_SECONDS:
            response = await self._client.get(f"{self.base_url}{ROUTES_PATH}",
                                              headers=self._headers)
            response.raise_for_status()
            self._routes = (response.json()["routes"], time.time())
        return self._routes[0]

    async def spec(self, method: str, path: str) -> Any:
        """One route's contract (cached); a string when unavailable."""
        key = (method.upper(), path.split("?", 1)[0])
        cached = self._specs.get(key)
        if cached and time.time() - cached[1] <= SPEC_TTL_SECONDS:
            return cached[0]
        try:
            response = await self._client.get(
                f"{self.base_url}{DESCRIBE_PATH}",
                params={"method": key[0], "path": key[1]}, headers=self._headers)
        except httpx.HTTPError as exc:
            return f"Could not describe {key[0]} {key[1]}: {exc}"
        if response.status_code == 404:
            spec: Any = (f"No Crontal endpoint matches {key[0]} {key[1]}; "
                         "see crontal_api_routes.")
        elif response.status_code != 200:
            return (f"Could not describe {key[0]} {key[1]} "
                    f"(HTTP {response.status_code}): {response.text[:200]}")
        else:
            spec = response.json()
        self._specs[key] = (spec, time.time())
        return spec

    # ── prompt ──────────────────────────────────────────────────────────────

    async def _render(self, max_chars: int, full_specs: tuple[str, ...]
                      ) -> tuple[str | None, bool]:
        """(text, complete); incomplete when a wanted contract failed to load."""
        routes = await self.routes()
        listed = {(m, str(r.get("path"))) for r in routes for m in r.get("methods") or []}
        wanted = [k for k in dict.fromkeys(filter(None, map(spec_key, full_specs)))
                  if k in listed and not hidden(k[1])]
        results = await asyncio.gather(*(self.spec(m, p) for m, p in wanted))
        text = render_catalog(routes, dict(zip(wanted, results)), full_specs, max_chars)
        if text is None:
            logger.warning("crontal route catalog omitted: route lines exceed max_chars",
                           extra={"max_chars": max_chars})
        # spec() caches answers (incl. 404) and returns "Could not ..." for failures.
        complete = not any(isinstance(r, str) and r.startswith("Could not") for r in results)
        return text, complete

    async def _refresh(self, key: tuple) -> str | None:
        try:
            text, complete = await self._render(*key)
        except Exception as exc:  # noqa: BLE001 - logged; the prompt falls back
            self._failed_at[key] = time.time()
            logger.warning("crontal route catalog fetch failed", extra={"error": str(exc)})
            raise
        rendered_at = time.time()
        if not complete:  # a contract failed to load: retry soon, not in an hour
            rendered_at -= max(0.0, self.prompt_ttl - FAILURE_RETRY_SECONDS)
        self._prompts[key] = (text, rendered_at)
        self._failed_at.pop(key, None)
        return text

    def _start_refresh(self, key: tuple) -> asyncio.Task:
        task = self._refreshing.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._refresh(key))

            def done(finished: asyncio.Task, key: tuple = key) -> None:
                if self._refreshing.get(key) is finished:
                    del self._refreshing[key]
                if not finished.cancelled():
                    finished.exception()  # retrieved: failures are logged in _refresh

            task.add_done_callback(done)
            self._refreshing[key] = task
        return task

    async def prompt_text(self, *, max_chars: int, full_specs: Iterable[str]) -> str | None:
        """The rendered catalog, or None when unavailable (never raises).

        Fresh copy: returned as is. Stale copy: returned while a background
        refresh runs. None yet: waits up to `wait_seconds` for the first fetch
        (it keeps running for later runs), except shortly after a failure.
        """
        key = (int(max_chars), tuple(full_specs))
        cached = self._prompts.get(key)
        now = time.time()
        if cached is not None:
            if now - cached[1] > self.prompt_ttl and not self._recently_failed(key, now):
                self._start_refresh(key)
            return cached[0]
        if self._recently_failed(key, now) and key not in self._refreshing:
            return None
        task = self._start_refresh(key)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self.wait_seconds)
        except Exception:  # noqa: BLE001 - timeout or fetch error: fall back
            return None

    def _recently_failed(self, key: tuple, now: float) -> bool:
        failed = self._failed_at.get(key)
        return failed is not None and now - failed < FAILURE_RETRY_SECONDS


_SHARED: dict[tuple[str, str], CrontalRouteCatalog] = {}


def crontal_route_catalog(base_url: str, agent_token: str) -> CrontalRouteCatalog:
    """The shared catalog for this Crontal and agent token (one per instance)."""
    key = (base_url.rstrip("/"), hashlib.sha256(agent_token.encode()).hexdigest())
    if key not in _SHARED:
        _SHARED[key] = CrontalRouteCatalog(base_url, agent_token)
    return _SHARED[key]
