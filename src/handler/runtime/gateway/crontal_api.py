"""Generic Crontal REST access: the agent calls Crontal's API like the UI does.

Instead of one hand-written tool per feature, two tools reach every endpoint:
`crontal_api_get` (reads) and `crontal_api_call` (writes, incl. file upload).
Requests carry a short-lived user JWT that Crontal mints for the user the
agent acts for (`POST /api/v1/agent-access/token`, agent token required), so
Crontal applies that user's normal permissions. `crontal_api_routes` lists
the available endpoints and `crontal_api_describe` returns one endpoint's spec
(params, JSON body schema with enums, multipart fields, response shape), which
is also appended to 400/422 results so the agent can correct its call.

File responses are saved into the thread's workspace (`downloads/`), and
documents are converted to text (`documents.to_text`) so the agent can read them.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

import httpx

from .. import documents
from ..context import RunContext
from ..types import Risk
from ..uploads import sanitize_filename
from .base import ToolResult, ToolSpec

API_PREFIX = "/api/v1/"
# Never reachable by the agent (refused, and left out of crontal_api_routes):
# its own plumbing and site-admin tools.
_HIDDEN = ("/api/v1/agent-access", "/api/v1/agent-mcp", "/api/v1/handler",
           "/api/v1/admin", "/api/v1/marketplace/query", "/api/v1/files/stats")
MAX_JSON_CHARS = 60_000
MAX_TEXT_CHARS = 20_000
ROUTES_TTL_SECONDS = 600
SPEC_TTL_SECONDS = 600
SPEC_ON_STATUS = (400, 422)
_WORD = re.compile(r"[a-z0-9]+")


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _hidden(path: str) -> bool:
    # A plain prefix test (not per segment): over-hiding is the safe side.
    return path.startswith(_HIDDEN)


# ── outward calls ─────────────────────────────────────────────────────────
# Writes that put the acting merchant's data in front of someone outside it.
# On threads fed by outside input (email, crontal notifications) these always
# need approval (gateway/policy.py); writes that stay inside the merchant don't.
# Default deny: a write is "inside" only under _INSIDE_WRITES and not matched
# by _OUTWARD_WRITES, so new Crontal areas start out as outward.
_INSIDE_WRITES = (
    # Not /api/v1/supply_listing: listings are active (on the public marketplace)
    # as soon as they are created, so writes there are outward.
    "/api/v1/rfq", "/api/v1/demand_listing", "/api/v1/bid",
    "/api/v1/contact", "/api/v1/files", "/api/v1/operations", "/api/v1/notification",
    "/api/v1/inventory", "/api/v1/products",
)
_OUTWARD_WRITES = [
    (re.compile(r"^/api/v1/rfq/[^/]+/share"), "emails or shares an RFQ"),
    (re.compile(r"^/api/v1/bid/[^/]+/lock$"), "submits a bid to the buyer"),
    (re.compile(r"^/api/v1/bid/token/[^/]+/revisions$"), "revises a submitted bid"),
    (re.compile(r"^/api/v1/bid/[^/]+/respond$"), "answers a supplier's bid"),
    # Belt and braces for future routes inside the areas above.
    (re.compile(r"(share|invite|publish|submit|send|email|message|public|token)", re.I),
     "shares, sends or publishes"),
]
# Writes whose `status` in the body decides: anything but draft is visible
# to other merchants (publishing generates the share link; closing notifies).
_STATUS_WRITES = re.compile(r"^/api/v1/rfq(/[^/]+)?$")


def outward_call(args: dict[str, Any]) -> str | None:
    """Why this crontal_api_call leaves the merchant, or None if it stays inside."""
    method = str(args.get("method", "")).upper()
    path = str(args.get("path", "")).split("?", 1)[0]
    if CrontalApiTools._check_path(path) is not None:
        return "has an invalid path"
    if method == "GET":
        return None
    if not _under(path, _INSIDE_WRITES):
        return f"writes to {path}, which reaches people outside the merchant"
    for pattern, why in _OUTWARD_WRITES:
        if pattern.search(path):
            return why
    if _STATUS_WRITES.match(path):
        for source in (args.get("json"), args.get("form")):
            status = source.get("status") if isinstance(source, dict) else None
            if status is not None and str(status).strip().lower() != "draft":
                return f"sets an RFQ's status to {status!r} (visible to suppliers)"
    return None


def crontal_base_url(url: str) -> str:
    """https://host/api/v1/agent-mcp → https://host (the API's origin)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _safe_name(name: str) -> str:
    """A bare file name; Unicode kept (see uploads.sanitize_filename)."""
    return sanitize_filename(name, default="download")


def _filename(response: httpx.Response, path: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    # Prefer RFC 5987 filename*= (UTF-8, percent-encoded): Flask's plain filename=
    # is an ASCII fallback with every non-ASCII character dropped.
    if match := re.search(r"filename\*\s*=\s*([\w-]+)'[^']*'([^;]+)", disposition):
        try:
            codecs.lookup(match.group(1))
            encoding = match.group(1)
        except LookupError:
            encoding = "utf-8"
        return _safe_name(unquote(match.group(2).strip().strip('"'), encoding=encoding,
                                  errors="replace"))
    match = re.search(r"filename\s*=\s*\"?([^\";]+)", disposition)
    return _safe_name(match.group(1) if match else unquote(path.rstrip("/").split("/")[-1]))


def _rank(routes: list[dict[str, Any]], words: set[str]) -> list[dict[str, Any]]:
    """Routes matching any search word, most matched words first.

    A search word matches a path/summary word sharing its start, either way
    ("file" matches "files", "uploaded" matches "upload", but "file" does not
    match "profile"), so multi-word queries still find routes.
    """
    def matches(word: str, token: str) -> bool:
        return token.startswith(word) or (len(token) >= 4 and word.startswith(token))

    scored = []
    for index, route in enumerate(routes):
        text = f"{route['path']} {route.get('summary', '')}".lower()
        tokens = set(_WORD.findall(text))
        hits = sum(1 for word in words if any(matches(word, t) for t in tokens))
        if hits:
            scored.append((-hits, index, route))
    scored.sort(key=lambda item: item[:2])
    return [route for _, _, route in scored]


class CrontalApiTools:
    def __init__(self, base_url: str, agent_token: str, *,
                 workspace_for: Callable[[str], Path],
                 client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._agent_token = agent_token
        self._workspace_for = workspace_for
        self._client = client or httpx.AsyncClient(timeout=60)
        self._jwts: dict[str, tuple[str, float]] = {}
        self._routes: tuple[list[dict[str, Any]], float] | None = None
        self._specs: dict[tuple[str, str], tuple[Any, float]] = {}

    # ── auth ────────────────────────────────────────────────────────────────

    @staticmethod
    def _user(ctx: RunContext) -> str | None:
        return ctx.acting_as or ctx.user_id

    async def _jwt(self, user: str, *, refresh: bool = False) -> str:
        cached = self._jwts.get(user)
        if cached and not refresh and cached[1] - time.time() > 60:
            return cached[0]
        response = await self._client.post(
            f"{self.base_url}/api/v1/agent-access/token",
            headers={"Authorization": f"Bearer {self._agent_token}", "X-Crontal-User": user})
        if response.status_code != 200:
            raise PermissionError(f"Crontal refused a token for user {user} "
                                  f"(HTTP {response.status_code}): {response.text[:200]}")
        body = response.json()
        try:
            expires = datetime.fromisoformat(body["expires_at"]).timestamp()
        except (KeyError, ValueError):
            expires = time.time() + 600
        self._jwts[user] = (body["token"], expires)
        return body["token"]

    # ── requests ────────────────────────────────────────────────────────────

    @staticmethod
    def _check_path(path: str) -> str | None:
        if not isinstance(path, str) or not path.startswith(API_PREFIX):
            return f"path must start with {API_PREFIX}"
        route = path.split("?", 1)[0]
        # Only plain paths: no encoded characters, dot segments, empty segments
        # or backslashes that the server could resolve to a hidden route.
        if ("%" in route or "\\" in route or "//" in route
                or any(seg in (".", "..") for seg in route.split("/"))):
            return "path must be a plain path (no %-escapes, //, . or .. segments)"
        if _hidden(route.lower()):
            return "that endpoint is not available to the agent"
        return None

    async def _request(self, ctx: RunContext, method: str, args: dict[str, Any]) -> ToolResult:
        user = self._user(ctx)
        if not user:
            return ToolResult("No Crontal user for this run", is_error=True)
        path = args.get("path", "")
        if problem := self._check_path(path):
            return ToolResult(problem, is_error=True)
        workspace = self._workspace_for(ctx.thread_key)
        kwargs: dict[str, Any] = {"params": args.get("query") or None}
        if upload := args.get("upload_file"):
            source = (workspace / upload).resolve()
            if workspace.resolve() not in source.parents or not source.is_file():
                return ToolResult(f"upload_file not found in the workspace: {upload}",
                                  is_error=True)
            field = str(args.get("upload_field") or "file")
            kwargs["files"] = {field: (source.name, source.read_bytes())}
            kwargs["data"] = {k: str(v) for k, v in (args.get("form") or {}).items()}
        elif "json" in args and args["json"] is not None:
            kwargs["json"] = args["json"]
        try:
            response = await self._send(user, method, path, kwargs)
        except PermissionError as exc:
            return ToolResult(str(exc), is_error=True)
        except httpx.HTTPError as exc:
            return ToolResult(f"Crontal request failed: {exc}", is_error=True)
        result = await self._result(response, path, workspace)
        if response.status_code in SPEC_ON_STATUS and isinstance(result.content, dict):
            spec = await self._spec(method, path)
            if isinstance(spec, dict):
                result.content["expected"] = spec
                result.content["hint"] = ("The request did not match the endpoint; "
                                          "fix it using `expected` and retry once.")
        return result

    async def _send(self, user: str, method: str, path: str,
                    kwargs: dict[str, Any]) -> httpx.Response:
        for refresh in (False, True):
            token = await self._jwt(user, refresh=refresh)
            response = await self._client.request(
                method, f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {token}"}, **kwargs)
            if response.status_code != 401 or refresh:
                return response
        return response

    async def _result(self, response: httpx.Response, path: str,
                      workspace: Path) -> ToolResult:
        status = response.status_code
        ctype = response.headers.get("content-type", "").split(";")[0].strip()
        if ctype == "application/json" or (not response.content and status < 400):
            try:
                body = response.json() if response.content else None
            except ValueError:
                body = response.text
            text = json.dumps(body, ensure_ascii=False, default=str)
            if len(text) > MAX_JSON_CHARS:
                saved = self._save(workspace, "response.json", text.encode(), path)
                return ToolResult({"status": status, "truncated": True,
                                   "saved_to": saved,
                                   "preview": text[:MAX_JSON_CHARS]}, is_error=status >= 400)
            return ToolResult({"status": status, "body": body}, is_error=status >= 400)
        if status >= 400:
            return ToolResult({"status": status, "body": response.text[:2000]}, is_error=True)
        # A file or other non-JSON payload: keep it where the agent can work on it.
        saved = self._save(workspace, _filename(response, path), response.content, path)
        text = await asyncio.to_thread(documents.to_text, workspace / saved,
                                       max_chars=MAX_TEXT_CHARS + 1)
        result: dict[str, Any] = {"status": status, "content_type": ctype,
                                  "size": len(response.content), "saved_to": saved}
        if text:
            result["text"] = text[:MAX_TEXT_CHARS]
            if len(text) > MAX_TEXT_CHARS:
                result["text_truncated"] = True
        return ToolResult(result)

    @staticmethod
    def _save(workspace: Path, name: str, data: bytes, path: str) -> str:
        target_dir = workspace / "downloads"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        if target.exists():
            target = target_dir / f"{int(time.time())}-{name}"
        target.write_bytes(data)
        return str(target.relative_to(workspace))

    async def _catalog(self) -> list[dict[str, Any]]:
        if self._routes is None or time.time() - self._routes[1] > ROUTES_TTL_SECONDS:
            response = await self._client.get(
                f"{self.base_url}/api/v1/agent-access/routes",
                headers={"Authorization": f"Bearer {self._agent_token}"})
            response.raise_for_status()
            self._routes = (response.json()["routes"], time.time())
        return self._routes[0]

    async def _list_routes(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        try:
            routes = await self._catalog()
        except httpx.HTTPError as exc:
            return ToolResult(f"Could not list Crontal routes: {exc}", is_error=True)
        routes = [r for r in routes if not _hidden(str(r.get("path", "")).lower())]
        if words := set(_WORD.findall((args.get("search") or "").lower())):
            routes = _rank(routes, words)
        return ToolResult([f"{','.join(r['methods'])} {r['path']}"
                           + (f" — {r['summary']}" if r.get("summary") else "")
                           for r in routes])

    async def _spec(self, method: str, path: str) -> Any:
        """One route's spec from Crontal (cached); a string when unavailable."""
        key = (method.upper(), path.split("?", 1)[0])
        cached = self._specs.get(key)
        if cached and time.time() - cached[1] <= SPEC_TTL_SECONDS:
            return cached[0]
        try:
            response = await self._client.get(
                f"{self.base_url}/api/v1/agent-access/routes/describe",
                params={"method": key[0], "path": key[1]},
                headers={"Authorization": f"Bearer {self._agent_token}"})
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

    async def _describe(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        method = str(args.get("method", "")).upper()
        path = args.get("path", "")
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            return ToolResult("method must be GET, POST, PUT, PATCH or DELETE", is_error=True)
        if problem := self._check_path(path):
            return ToolResult(problem, is_error=True)
        spec = await self._spec(method, path)
        return ToolResult(spec, is_error=not isinstance(spec, dict))

    async def _get(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        return await self._request(ctx, "GET", args)

    async def _call(self, ctx: RunContext, args: dict[str, Any]) -> ToolResult:
        method = str(args.get("method", "")).upper()
        if method not in ("POST", "PUT", "PATCH", "DELETE"):
            return ToolResult("method must be POST, PUT, PATCH or DELETE", is_error=True)
        return await self._request(ctx, method, args)

    def tools(self) -> list[ToolSpec]:
        path = {"type": "string", "description": "API path starting with /api/v1/, "
                "with path parameters filled in (e.g. /api/v1/rfq/12)."}
        query = {"type": "object", "description": "Query-string parameters.",
                 "additionalProperties": True}
        return [
            ToolSpec(
                "crontal_api_routes", "crontal_api.routes", Risk.READ,
                "List Crontal's REST API endpoints (methods, path, summary). Use it to find "
                "the endpoint for a task, then crontal_api_describe for its exact contract.",
                {"type": "object", "properties": {
                    "search": {"type": "string",
                               "description": "Optional words to look for in path or "
                               "summary (any word matches, best matches first); omit "
                               "for the whole catalogue."}}},
                self._list_routes),
            ToolSpec(
                "crontal_api_describe", "crontal_api.describe", Risk.READ,
                "Show one Crontal endpoint's contract: path and query params, the JSON body "
                "schema (required fields, types, allowed enum values, nested objects such "
                "as product_info and its per-shape shape_attributes), multipart fields for "
                "uploads, and the response shape. Call it before a write you haven't made "
                "in this thread.",
                {"type": "object", "properties": {
                    "method": {"type": "string",
                               "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                    "path": {"type": "string", "description": "Path as listed by "
                             "crontal_api_routes, or with ids filled in."}},
                 "required": ["method", "path"]},
                self._describe),
            ToolSpec(
                "crontal_api_get", "crontal_api.get", Risk.READ,
                "GET any Crontal API endpoint as the user you work for (same data and "
                "permissions they have in the Crontal UI). JSON comes back directly; files "
                "are saved under downloads/ in your workspace, with document text included.",
                {"type": "object", "properties": {"path": path, "query": query},
                 "required": ["path"]},
                self._get),
            ToolSpec(
                "crontal_api_call", "crontal_api.call", Risk.WRITE_EXTERNAL,
                "POST/PUT/PATCH/DELETE a Crontal API endpoint as the user you work for. "
                "Send a JSON body with `json` shaped exactly as crontal_api_describe shows "
                "(unknown fields may be rejected), or upload a workspace file as multipart "
                "(`upload_file`, its field name in `upload_field`, plus extra `form` "
                "fields). A 400/422 result includes the endpoint's spec as `expected`.",
                {"type": "object", "properties": {
                    "method": {"type": "string", "enum": ["POST", "PUT", "PATCH", "DELETE"]},
                    "path": path, "query": query,
                    "json": {"type": ["object", "array"], "description": "JSON request body."},
                    "upload_file": {"type": "string",
                                    "description": "Workspace-relative file to upload."},
                    "upload_field": {"type": "string",
                                     "description": "Multipart field for upload_file "
                                     "(default 'file'; see the endpoint's multipart spec)."},
                    "form": {"type": "object", "additionalProperties": True,
                             "description": "Extra multipart form fields for upload_file."}},
                 "required": ["method", "path"]},
                self._call, outward=outward_call),
        ]
