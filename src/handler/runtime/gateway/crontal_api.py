"""Generic Crontal REST access: the agent calls Crontal's API like the UI does.

Instead of one hand-written tool per feature, two tools reach every endpoint:
`crontal_api_get` (reads) and `crontal_api_call` (writes, incl. file upload).
Requests carry a short-lived JWT that Crontal mints for the instance's AI
user (`POST /api/v1/agent-access/token`, agent token required; see
`crontal_identity.py`), so Crontal applies that member's normal permissions.
On chat threads the chatting person is sent as `X-Crontal-On-Behalf-Of`, for
Crontal's audit only. `crontal_api_routes` lists
the available endpoints and `crontal_api_describe` returns one endpoint's spec
(params, JSON body schema with enums, multipart fields, response shape), which
is also appended to 400/422 results so the agent can correct its call.

File responses are saved into the thread's workspace (`downloads/`), and
documents are converted to text (`documents.to_text`) so the agent can read them.

Crontal Files: deleting or trashing files/folders (and emptying the trash)
always needs a person's approval (`destructive_call`, a ToolSpec.always_ask
classifier, so even `crontal_api.*: allow` cannot skip it). Uploads to
`/api/v1/files/upload` carry provenance: `source` (`email` when the workspace
file is a saved email attachment, else `ai_generated`) and `source_detail`
(the thread key, plus mailbox/sender/subject/message id for email), resolved
by the registered upload sources (see `UploadSource`).
"""

from __future__ import annotations

import asyncio
import codecs
import json
import re
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlsplit

import httpx

from .. import documents
from ..context import RunContext
from ..types import Risk
from ..uploads import sanitize_filename
from .base import ToolResult, ToolSpec
from .crontal_identity import (
    ON_BEHALF_OF_HEADER,
    CrontalIdentityClient,
    IdentityUnavailable,
    crontal_identity,
    on_behalf_of,
)

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


# ── destructive calls ─────────────────────────────────────────────────────
# Crontal Files writes that remove files: they need approval on every thread,
# whatever the grants. Crontal itself refuses permanent deletes to the agent;
# moving to the trash is allowed there, but only once a person approved it.
_FILES = ("/api/v1/files",)
_FILES_TRASH_POSTS = re.compile(r"^/api/v1/files/bulk/trash/?$")
_FILES_DELETE_POSTS = re.compile(r"^/api/v1/files/(bulk/delete|trash/empty)/?$")


def destructive_call(args: dict[str, Any]) -> str | None:
    """Why this crontal_api_call deletes Crontal files, or None."""
    method = str(args.get("method", "")).upper()
    path = str(args.get("path", "")).split("?", 1)[0].lower()
    if not _under(path.rstrip("/") or path, _FILES):
        return None
    if method == "DELETE":
        return "deletes Crontal files or folders (moves them to the trash)"
    if method == "POST" and _FILES_TRASH_POSTS.match(path):
        return "moves Crontal files or folders to the trash"
    if method == "POST" and _FILES_DELETE_POSTS.match(path):
        return "permanently deletes Crontal files"
    return None


# ── upload provenance ─────────────────────────────────────────────────────
FILES_UPLOAD = "/api/v1/files/upload"
# (run context, workspace-relative path) -> email details of the attachment
# saved at that path ({mailbox, sender, subject, message_id, ...}) or None.
UploadSource = Callable[[RunContext, str], Awaitable["dict[str, Any] | None"]]


def _form_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _project(value: Any, fields: list[str]) -> Any:
    """Select explicit dot paths, applying the same selection to array items."""
    if isinstance(value, list):
        return [_project(item, fields) for item in value]
    if not isinstance(value, dict):
        return value
    selected: dict[str, Any] = {}
    for field in fields:
        head, _, tail = field.partition(".")
        if head not in value:
            continue
        if not tail:
            selected[head] = value[head]
        elif head not in fields:
            children = [f.partition(".")[2] for f in fields if f.startswith(head + ".")]
            selected[head] = _project(value[head], children)
    return selected


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
                 client: httpx.AsyncClient | None = None,
                 identity: CrontalIdentityClient | None = None,
                 upload_sources: list[UploadSource] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._agent_token = agent_token
        self._workspace_for = workspace_for
        self._client = client or httpx.AsyncClient(timeout=60)
        if identity is None:
            # An explicit client (tests) gets its own identity client; otherwise
            # share the instance's (also used by the prompt and email intake).
            identity = (CrontalIdentityClient(self.base_url, agent_token, client=client)
                        if client is not None
                        else crontal_identity(self.base_url, agent_token))
        self.identity = identity
        # Shared list: integrations (mailbox) register resolvers into it later.
        self._upload_sources = upload_sources if upload_sources is not None else []
        # JWTs by on-behalf-of user ("" = none); all are the AI user's.
        self._jwts: dict[str, tuple[str, float]] = {}
        self._routes: tuple[list[dict[str, Any]], float] | None = None
        self._specs: dict[tuple[str, str], tuple[Any, float]] = {}

    # ── auth ────────────────────────────────────────────────────────────────

    async def _jwt(self, obo: str | None, *, refresh: bool = False) -> str:
        """A JWT of the instance's AI user (never a person's)."""
        key = obo or ""
        cached = self._jwts.get(key)
        if cached and not refresh and cached[1] - time.time() > 60:
            return cached[0]
        try:
            identity = await self.identity.get(refresh=refresh)
        except IdentityUnavailable as exc:
            raise PermissionError(f"No Crontal AI identity for this instance: {exc}") from exc
        headers = {"Authorization": f"Bearer {self._agent_token}",
                   "X-Crontal-User": identity.user_id}
        if obo:
            headers[ON_BEHALF_OF_HEADER] = obo
        response = await self._client.post(
            f"{self.base_url}/api/v1/agent-access/token", headers=headers)
        if response.status_code == 403 and obo:
            # On-behalf-of is audit only: a person Crontal doesn't know (or who
            # left) must not stop the AI user's work.
            return await self._jwt(None, refresh=refresh)
        if response.status_code != 200:
            raise PermissionError(f"Crontal refused a token for its AI user "
                                  f"(HTTP {response.status_code}): {response.text[:200]}")
        body = response.json()
        try:
            expires = datetime.fromisoformat(body["expires_at"]).timestamp()
        except (KeyError, ValueError):
            expires = time.time() + 600
        self._jwts[key] = (body["token"], expires)
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
        obo = on_behalf_of(ctx)
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
            form = dict(args.get("form") or {})
            if path.split("?", 1)[0].rstrip("/") == FILES_UPLOAD:
                form = await self._with_provenance(ctx, str(upload), form)
            kwargs["data"] = {k: _form_value(v) for k, v in form.items()}
        elif "json" in args and args["json"] is not None:
            kwargs["json"] = args["json"]
        try:
            response = await self._send(obo, method, path, kwargs)
        except PermissionError as exc:
            return ToolResult(str(exc), is_error=True)
        except httpx.HTTPError as exc:
            return ToolResult(f"Crontal request failed: {exc}", is_error=True)
        result = await self._result(response, path, workspace, options=args)
        if response.status_code in SPEC_ON_STATUS and isinstance(result.content, dict):
            spec = await self._spec(method, path)
            if isinstance(spec, dict):
                result.content["expected"] = spec
                result.content["hint"] = ("The request did not match the endpoint; "
                                          "fix it using `expected` and retry once.")
        return result

    async def _email_source(self, ctx: RunContext, upload: str) -> dict[str, Any] | None:
        relative = PurePosixPath(upload.replace("\\", "/")).as_posix()
        for resolve in list(self._upload_sources):
            try:
                found = await resolve(ctx, relative)
            except Exception:  # provenance is best effort; never block the upload
                found = None
            if found:
                return found
        return None

    async def _with_provenance(self, ctx: RunContext, upload: str,
                               form: dict[str, Any]) -> dict[str, Any]:
        """Add source/source_detail for a Crontal Files upload (agent values win)."""
        detail = form.get("source_detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail) if detail.strip() else None
            except ValueError:
                return form  # the agent's own value; Crontal validates it
        if detail is not None and not isinstance(detail, dict):
            return form
        email = None if "source" in form else await self._email_source(ctx, upload)
        if "source" not in form:
            form["source"] = "email" if email else "ai_generated"
        merged = {**(email or {}), **(detail or {})}
        merged.setdefault("thread_key", ctx.thread_key)
        form["source_detail"] = merged
        return form

    async def _send(self, obo: str | None, method: str, path: str,
                    kwargs: dict[str, Any]) -> httpx.Response:
        for refresh in (False, True):
            token = await self._jwt(obo, refresh=refresh)
            response = await self._client.request(
                method, f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {token}"}, **kwargs)
            if response.status_code != 401 or refresh:
                return response
        return response

    async def _result(self, response: httpx.Response, path: str,
                      workspace: Path, *, options: dict[str, Any] | None = None) -> ToolResult:
        options = options or {}
        status = response.status_code
        ctype = response.headers.get("content-type", "").split(";")[0].strip()
        if ctype == "application/json" or (not response.content and status < 400):
            try:
                body = response.json() if response.content else None
            except ValueError:
                body = response.text
            original_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)
            metadata: dict[str, Any] = {}
            # Never hide validation errors or other failure details through projection.
            if status < 400 and (fields := options.get("response_fields")):
                metadata = {"projected": True, "full_response_saved_to": self._save(
                    workspace, "response.json", original_text.encode(), path)}
                body = _project(body, fields)
            text = json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)
            budget = max(1000, min(MAX_JSON_CHARS, int(options.get("max_output_chars", MAX_JSON_CHARS))))
            if len(text) > budget:
                saved = self._save(workspace, "response.json", text.encode(), path)
                return ToolResult({"status": status, "truncated": True,
                                   **metadata,
                                   "saved_to": saved,
                                   "preview": text[:budget]}, is_error=status >= 400)
            return ToolResult({"status": status, "body": body, **metadata}, is_error=status >= 400)
        if status >= 400:
            return ToolResult({"status": status, "body": response.text[:2000]}, is_error=True)
        # A file or other non-JSON payload: keep it where the agent can work on it.
        saved = self._save(workspace, _filename(response, path), response.content, path)
        result: dict[str, Any] = {"status": status, "content_type": ctype,
                                  "size": len(response.content), "saved_to": saved}
        if not options.get("include_text", True):
            return ToolResult(result)
        budget = max(1000, min(MAX_TEXT_CHARS, int(options.get("max_output_chars", MAX_TEXT_CHARS))))
        text = await asyncio.to_thread(documents.to_text, workspace / saved,
                                       max_chars=budget + 1)
        if text:
            result["text"] = text[:budget]
            if len(text) > budget:
                result["text_truncated"] = True
        return ToolResult(result)

    @staticmethod
    def _save(workspace: Path, name: str, data: bytes, path: str) -> str:
        target_dir = workspace / "downloads"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        if target.exists():
            target = target_dir / f"{time.time_ns()}-{name}"
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
        if args.get("include_specs"):
            limit = max(1, min(3, int(args.get("limit", 3))))
            matches = [(route, method) for route in routes for method in route["methods"]
                       if method in ("GET", "POST", "PUT", "PATCH", "DELETE")]
            selected = matches[:limit]
            specs = await asyncio.gather(*(self._spec(method, route["path"])
                                           for route, method in selected))
            content = {"routes": [{"method": method, "path": route["path"], "spec": spec}
                                  for (route, method), spec in zip(selected, specs)],
                       "has_more": len(matches) > limit}
            text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            if len(text) > MAX_JSON_CHARS:
                saved = self._save(self._workspace_for(ctx.thread_key), "route-specs.json",
                                   text.encode(), "routes")
                return ToolResult({"truncated": True, "saved_to": saved,
                                   "preview": text[:MAX_JSON_CHARS]})
            return ToolResult(content)
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
        output_options = {
            "response_fields": {"type": "array", "items": {"type": "string"},
                                "maxItems": 50,
                                "description": "Optional JSON body fields (dot paths; applies to arrays). "
                                "Full response is saved to a workspace file. Errors are not projected."},
            "include_text": {"type": "boolean", "description": "Include extracted text for file "
                             "downloads (default true); false saves the file for targeted reading."},
            "max_output_chars": {"type": "integer", "minimum": 1000, "maximum": MAX_JSON_CHARS,
                                 "description": "Optional smaller output budget; files cap at 20000 chars. "
                                 "Truncation is explicit and the complete content remains in a file."},
        }
        return [
            ToolSpec(
                "crontal_api_routes", "crontal_api.routes", Risk.READ,
                "List Crontal's REST API endpoints (methods, path, summary). Use it to find "
                "the endpoint for a task. Set include_specs to get up to 3 matching endpoint "
                "contracts in the same call; use a specific search to narrow results.",
                {"type": "object", "properties": {
                    "search": {"type": "string",
                               "description": "Optional words to look for in path or "
                               "summary (any word matches, best matches first); omit "
                               "for the whole catalogue."},
                    "include_specs": {"type": "boolean", "description": "Include endpoint contracts (default false)."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 3,
                              "description": "Maximum contracts when include_specs=true (default 3)."}}},
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
                "GET any Crontal API endpoint as the instance's AI identity with its "
                "Crontal permissions. JSON comes back directly; files "
                "are saved under downloads/ in your workspace, with document text included.",
                {"type": "object", "properties": {"path": path, "query": query, **output_options},
                 "required": ["path"]},
                self._get),
            ToolSpec(
                "crontal_api_call", "crontal_api.call", Risk.WRITE_EXTERNAL,
                "POST/PUT/PATCH/DELETE a Crontal API endpoint as the instance's AI identity. "
                "Send a JSON body with `json` shaped exactly as crontal_api_describe shows "
                "(unknown fields may be rejected), or upload a workspace file as multipart "
                "(`upload_file`, its field name in `upload_field`, plus extra `form` "
                "fields). A 400/422 result includes the endpoint's spec as `expected`.",
                {"type": "object", "properties": {
                    "method": {"type": "string", "enum": ["POST", "PUT", "PATCH", "DELETE"]},
                    "path": path, "query": query, **output_options,
                    "json": {"type": ["object", "array"], "description": "JSON request body."},
                    "upload_file": {"type": "string",
                                    "description": "Workspace-relative file to upload."},
                    "upload_field": {"type": "string",
                                     "description": "Multipart field for upload_file "
                                     "(default 'file'; see the endpoint's multipart spec)."},
                    "form": {"type": "object", "additionalProperties": True,
                             "description": "Extra multipart form fields for upload_file."}},
                 "required": ["method", "path"]},
                self._call, outward=outward_call, always_ask=destructive_call),
        ]
