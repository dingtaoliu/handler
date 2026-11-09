"""Crontal's API catalog, shared by the crontal_api tools and the prompt.

`CrontalRouteCatalog` fetches and caches `GET /api/v1/agent-access/routes`
(the route list) and `/routes/describe` (one route's contract) for one
instance (Crontal URL + agent token). `crontal_api_routes`/`describe` read
through it, and `prompt_text()` renders a compact catalog for the stable part
of the instructions so agents don't spend a model round trip on discovery:

- one line per route under the configured resource prefixes (`METHOD path —
  summary`, paths shown without the `/api/v1` prefix, which the header tells
  the agent to prepend), grouped by resource; other routes stay discoverable
  through crontal_api_routes;
- full contracts for a configured list of common routes, in that priority
  order, while they fit `max_chars` (all selected route lines are always
  kept; when the lines alone exceed the cap the catalog is left out).

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
SUMMARY_CHARS = 48
API_PREFIX = "/api/v1"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
# Never reachable by the agent (refused, and left out of the catalog):
# its own plumbing and site-admin tools.
HIDDEN_PREFIXES = ("/api/v1/agent-access", "/api/v1/agent-mcp", "/api/v1/handler",
                   "/api/v1/admin", "/api/v1/marketplace/query", "/api/v1/files/stats")
# Contract keys already shown in the route's heading line.
_HEADING_KEYS = ("method", "path", "summary", "has_schema")
_PARAM = re.compile(r"<(?:\w+(?:\([^)]*\))?:)?(\w+)>")

# Resources whose routes get catalog lines, from BSS usage since Oct 25
# (received RFQs and files dominate; operations, RFQs, bids follow).
DEFAULT_RESOURCES = [
    "/api/v1/rfq/received", "/api/v1/rfq", "/api/v1/bid", "/api/v1/files",
    "/api/v1/operations", "/api/v1/notification", "/api/v1/contact",
    "/api/v1/demand_listing",
]
# Default full contracts, most important first (fewer fit a smaller cap).
DEFAULT_FULL_SPECS = [
    "GET /api/v1/rfq/received",
    "POST /api/v1/rfq/received",
    "POST /api/v1/files/upload",
    "GET /api/v1/files/browse",
]
# Summary words that add nothing to a route's method and path.
_GENERIC = set("""get list all create new update delete remove a an the by id for of
specific one its your their this that to in on with and or from existing authenticated
merchant caller calling owning owner only can user belonging""".split())
_WORDS = re.compile(r"[a-z0-9]+")
_TRAILING = {"a", "an", "the", "for", "of", "to", "by", "with", "and", "or", "in", "on",
             "from", "its", "your", "their", "are", "is", "be", "that", "which", "so",
             "you", "can", "calling", "authenticated", "one"}


def hidden(path: str) -> bool:
    # A plain prefix test (not per segment): over-hiding is the safe side.
    return path.lower().startswith(HIDDEN_PREFIXES)


def canonical_route(method: str, path: str, routes: Iterable[dict[str, Any]]) -> str:
    """Resolve description aliases only; never rewrite an executing request.

    Template names must match the published contract. Concrete values are matched
    conservatively for standard converters; unknown converters go to Crontal.
    """
    path = path.split("?", 1)[0]
    candidates = [str(r["path"]) for r in routes
                  if method in (r.get("methods") or []) and not hidden(str(r["path"]))]
    if hidden(path):
        raise ValueError("That endpoint is not available to the agent")
    if path in candidates:
        return path
    template = _PARAM.sub(r"{\1}", path).rstrip("/")
    aliases = [p for p in candidates if _PARAM.sub(r"{\1}", p).rstrip("/") == template]
    if len(aliases) == 1:
        return aliases[0]
    if len(aliases) > 1:
        raise ValueError("Ambiguous route description; use the exact crontal_api_routes path")
    if any(c in path for c in "{}<>"):
        raise ValueError(f"No published {method} route matches {path}; see crontal_api_routes")
    matches = []
    converters = {"int": r"[0-9]+", "float": r"[0-9]+\.[0-9]+",
                  "string": r"[^/]+", "default": r"[^/]+", "path": r"[^/].*?",
                  "uuid": r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"}
    for candidate in candidates:
        pattern, cursor, static = "", 0, 0
        for param in _PARAM.finditer(candidate):
            converter = param.group(0)[1:-1].partition(":")[0] if ":" in param.group(0) else "default"
            if converter not in converters:
                break  # Let the backend resolve custom/parameterized converters.
            literal = candidate[cursor:param.start()]
            static += len(literal)
            pattern += re.escape(literal) + converters[converter]
            cursor = param.end()
        else:
            tail = candidate[cursor:].rstrip("/")
            pattern += re.escape(tail)
            if re.fullmatch(pattern, path.rstrip("/")):
                matches.append((static + len(tail), candidate))
    if matches:
        best = max(score for score, _ in matches)
        winners = {p for score, p in matches if score == best}
        if len(winners) == 1:
            return winners.pop()
        raise ValueError("Ambiguous route description; use the exact crontal_api_routes path")
    return path  # Backend remains authoritative for concrete/unknown routes.


def resource_prefix(entry: str) -> str | None:
    """A normalized `/api/v1/...` path prefix; None when malformed."""
    entry = entry.strip().rstrip("/")
    if (not entry.startswith(API_PREFIX + "/") or any(c.isspace() for c in entry)
            or "<" in entry or "{" in entry):
        return None
    return entry


def _under(path: str, prefixes: Iterable[str]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def spec_key(entry: str) -> tuple[str, str] | None:
    """`"GET /api/v1/rfq"` -> ("GET", "/api/v1/rfq"); None when malformed."""
    method, _, path = entry.strip().partition(" ")
    method, path = method.upper(), path.strip()
    if method not in METHODS or not path.startswith("/api/v1/"):
        return None
    return method, path


def _summary(text: str) -> str:
    """The first sentence of Crontal's summary, cut at a word boundary to
    SUMMARY_CHARS characters (no dangling article/preposition, no period)."""
    text = " ".join((text or "").split())
    text = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].rstrip(".")
    if len(text) <= SUMMARY_CHARS:
        return text
    words = text[:SUMMARY_CHARS + 1].split(" ")[:-1]
    text = " ".join(words)
    if text.count("(") > text.count(")"):  # don't leave a parenthesis open
        text = text[:text.rindex("(")].rstrip()
    words = text.split(" ")
    while words and words[-1].lower().strip(",;:—-") in _TRAILING:
        words.pop()
    return " ".join(words).rstrip(",;:—-")


def _stem(word: str) -> str:
    return word[:-1] if word.endswith("s") and len(word) > 3 else word


def _informative(path: str, summary: str) -> bool:
    """Whether the summary says more than the method and path: a word that is
    neither generic nor (a form of) a path word. "Get a bid by ID" on
    GET /bid/{bid_id} says nothing more; "Lock and submit a bid" does."""
    path_words = {_stem(w) for w in _WORDS.findall(path.lower().replace("_", " "))}
    for word in map(_stem, _WORDS.findall(summary.lower())):
        if word in _GENERIC or word in path_words:
            continue
        if any(len(p) >= 4 and (word.startswith(p) or p.startswith(word)) for p in path_words):
            continue
        return True
    return False


def _display(path: str) -> str:
    """`/api/v1/rfq/<int:rfq_id>` -> `/rfq/{rfq_id}` (the header says to prepend)."""
    return _PARAM.sub(r"{\1}", path).removeprefix(API_PREFIX)


def _group(path: str) -> str:
    parts = path.removeprefix("/api/v1/").split("/")
    return parts[0] or "other"


def render_route_lines(routes: Iterable[dict[str, Any]],
                       resources: Iterable[str] | None = None) -> str:
    """Route lines grouped by resource; deterministic for the same routes.
    With `resources`, only routes under those path prefixes."""
    prefixes = None if resources is None else [p for p in map(resource_prefix, resources) if p]
    groups: dict[str, list[str]] = {}
    for route in sorted(routes, key=lambda r: (str(r.get("path", "")),
                                               list(r.get("methods") or []))):
        path = str(route.get("path", ""))
        methods = [m for m in route.get("methods") or [] if m in METHODS]
        if not methods or not path.startswith("/api/v1/") or hidden(path):
            continue
        if prefixes is not None and not _under(path, prefixes):
            continue
        summary = _summary(str(route.get("summary") or ""))
        if not _informative(path, summary):
            summary = ""
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
    "# Crontal API catalog (common resources)\n"
    "Paths omit the /api/v1 prefix: always prepend it, e.g. /rfq/received is "
    "/api/v1/rfq/received. Fill {name} with ids. Contracts are crontal_api_describe "
    "output minus method/path/summary (success_status 200 unless shown).")


def render_catalog(routes: list[dict[str, Any]], specs: dict[tuple[str, str], Any],
                   full_specs: Iterable[str], max_chars: int,
                   resources: Iterable[str] | None = None) -> str | None:
    """The prompt text; None when even the route lines exceed `max_chars`."""
    text = CATALOG_HEADER + "\n\n" + render_route_lines(routes, resources)
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
        if hidden(key[1]):
            return "That endpoint is not available to the agent"
        try:
            key = (key[0], canonical_route(key[0], key[1], await self.routes()))
        except ValueError as exc:
            return str(exc)
        except httpx.HTTPError:
            # Discovery outages must not block exact/concrete descriptions.
            if any(c in path for c in "{}<>"):
                return "Route catalog unavailable; retry discovery before describing a template"
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

    async def _render(self, max_chars: int, full_specs: tuple[str, ...],
                      resources: tuple[str, ...] | None) -> tuple[str | None, bool]:
        """(text, complete); incomplete when a wanted contract failed to load."""
        routes = await self.routes()
        listed = {(m, str(r.get("path"))) for r in routes for m in r.get("methods") or []}
        wanted = [k for k in dict.fromkeys(filter(None, map(spec_key, full_specs)))
                  if k in listed and not hidden(k[1])]
        results = await asyncio.gather(*(self.spec(m, p) for m, p in wanted))
        text = render_catalog(routes, dict(zip(wanted, results)), full_specs, max_chars,
                              resources)
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

    async def prompt_text(self, *, max_chars: int, full_specs: Iterable[str],
                          resources: Iterable[str] | None = None) -> str | None:
        """The rendered catalog, or None when unavailable (never raises).

        Fresh copy: returned as is. Stale copy: returned while a background
        refresh runs. None yet: waits up to `wait_seconds` for the first fetch
        (it keeps running for later runs), except shortly after a failure.
        """
        key = (int(max_chars), tuple(full_specs),
               None if resources is None else tuple(resources))
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
