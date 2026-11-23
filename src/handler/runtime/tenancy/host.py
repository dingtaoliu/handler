"""The registry host's front door: requests to tenants, which are context.

A tenant has no runtime to start or stop (step 3, role-split-design.md): its context
is built on demand into the host's cache (tenancy/contexts.py) and dropped when idle
or changed; runs, timer firings and deliveries are the host loops'. This module keeps
the request side until 3b replaces it with one routed app: routing by `/i/<name>`,
who may cause a context to be built (only a credential that checks out), email a
tenant cannot take (spooled, then replayed), and reacting to registry changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic
from typing import Any
from urllib.parse import parse_qsl

import httpx
from psycopg.errors import UndefinedTable
from starlette._utils import get_route_path
from starlette.responses import JSONResponse

from ..gateway.google_oauth import CALLBACK_PATH
from ..gateway.secret_box import SecretBox
from ..store.sqlite_users import hash_token
from ..web.app import public_shell
from ..web.auth_api import ticket_hash
from ..web.email_ingest import MAX_EMAIL_BYTES, sign_email, verify_email
from ..web.ingest import verify as verify_crontal
from .limits import PlanLimits, upgrade_required
from .plan_guard import plan_refusal
from .contexts import ServedTenant
from .registry import Tenant, TenantRegistry

logger = logging.getLogger("handler.runtime.tenancy")

ROOT_TENANT = "main"
# Connected-mail integrations: a plan without connected mail may not enable them.
PINNED_INTEGRATIONS = frozenset({"mailbox", "gmail", "google"})
_INSTANCE_PATH = re.compile(r"^/i/([a-z][a-z0-9_]{0,31})(/.*)$")
SPOOL_RETRY_SECONDS = 300.0
SPOOL_RETRY_MAX_SECONDS = 86_400.0
SPOOL_MAX_ATTEMPTS = 12
BAD_CREDENTIAL_SECONDS = 60.0
MAX_BAD_CREDENTIALS = 4096
NAME_REFRESH_SECONDS = 60.0
REPLAYED_HEADERS = ("content-type", "x-handler-envelope-from", "x-handler-envelope-to",
                    "x-handler-auth-results", "x-handler-forwarded")


def _limits_key(tenant: Tenant) -> tuple:
    return tenant.plan, json.dumps(tenant.limits, sort_keys=True)


class _Answered(Exception):
    """The response was already sent."""


def _headers(scope) -> dict[str, str]:
    return {key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])}


def _bearer(scope, headers: dict[str, str]) -> str | None:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        return token
    return dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"))).get("access_token")


def _carries_credential(scope) -> bool:
    headers = _headers(scope)
    return bool(_bearer(scope, headers)) or any(
        name in headers for name in ("x-handler-signature", "x-crontal-signature"))


def _is_public_page(path: str) -> bool:
    return path in ("/", "/app", "/app/", "/login") or path.startswith("/app/static/")


def _replaying(body: bytes, receive):
    """A `receive` that gives the already-read body, then defers to the real one."""
    delivered = False

    async def replay():
        nonlocal delivered
        if delivered:
            return await receive()
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay


@dataclass
class _Slot:
    tenant: Tenant
    shell: Any = None  # the public pages, built when first asked for
    limits: Any = None  # PlanLimits for the tenant as last read, resolved on change only
    limits_key: tuple | None = None
    replay: asyncio.Task[Any] | None = None


class TenantHost:
    """The registry host's front: routes each request to its tenant's context.

    Holds what the registry says about every tenant (read at start, on a change
    notice, and every NAME_REFRESH_SECONDS), never a tenant's runtime: contexts are
    the host's cache (tenancy/contexts.py), built on demand and dropped when idle.
    One scanning task replays spooled email and refreshes the tenant list; it reads
    only control tables.
    """

    def __init__(self, registry: TenantRegistry, services: Any, hub: Any, *,
                 scan_seconds: float = 15.0,
                 limits_for: Callable[[Tenant], PlanLimits] | None = None) -> None:
        self.registry = registry
        self.services = services
        self.hub = hub
        self.scan_seconds = scan_seconds
        self._limits_for = limits_for
        self._bad_credentials: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._credential_checks = asyncio.Semaphore(4)
        self._names_read_at = 0.0
        self._slots: dict[str, _Slot] = {}
        self._by_schema: dict[str, _Slot] = {}
        self._scan_now = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._reacting: set[asyncio.Task[Any]] = set()
        services.on_tenant_changed.append(self._changed_notice)

    # --- ASGI ---------------------------------------------------------------
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return
        name, child = self._route(scope)
        slot = self._slots.get(name) if name else None  # never a query: see load()
        if slot is None:
            await self._refuse(scope, receive, send, 404, "instance not found")
            return
        limits = self._plan(slot)
        if limits is not None and not limits.email and self._is_email_ingest(child):
            # Before the spool: on this plan mail is not a channel, so nothing is kept.
            await JSONResponse(upgrade_required("email", limits), 403)(scope, receive, send)
            return
        if get_route_path(child) == "/healthz" and scope["type"] == "http":
            if slot.tenant.enabled:
                await JSONResponse({"ok": True})(scope, receive, send)
            else:
                await self._refuse(scope, receive, send, 404, "instance not found")
            return
        if not slot.tenant.enabled:
            if not await self._spool_email(slot, child, receive, send):
                await self._refuse(scope, receive, send, 404, "instance not found")
            return
        if self._closing:
            if not await self._spool_email(slot, child, receive, send):
                # Shutting down must read as "retry", so senders don't give up on mail.
                await self._refuse(scope, receive, send, 503, "instance unavailable")
            return
        if self._sleeping(slot):
            try:
                receive = await self._admit(slot, child, receive, send)
            except _Answered:
                return
        entered = False
        try:
            async with self.services.tenants.use(slot.tenant.name) as context:
                entered = True
                if context is None:
                    if not await self._spool_email(slot, child, receive, send):
                        await self._refuse(scope, receive, send, 404, "instance not found")
                    return
                await context.app(child, receive, send)
        except Exception:
            if entered:
                raise  # the app failed, not the build
            logger.exception("tenant context build failed", extra={"tenant": slot.tenant.name})
            if not await self._spool_email(slot, child, receive, send):
                await self._refuse(scope, receive, send, 503, "instance unavailable",
                                   headers={"Retry-After": "5"})

    def _plan(self, slot: _Slot) -> PlanLimits | None:
        if self._limits_for is not None and slot.limits is None:
            slot.limits = self._limits_for(slot.tenant)
        return slot.limits

    def _schedules_count(self, slot: _Slot) -> bool:
        limits = self._plan(slot)
        return limits is None or limits.automations

    def _sleeping(self, slot: _Slot) -> bool:
        """No context built: only a credential that checks out may build one."""
        return self.services.cache.cached(slot.tenant.name) is None

    @staticmethod
    def _route(scope) -> tuple[str | None, dict]:
        path = get_route_path(scope)
        match = _INSTANCE_PATH.match(path)
        if match:
            root = scope.get("root_path", "")
            return match.group(1), {**scope, "root_path": f"{root}/i/{match.group(1)}",
                                    "app_root_path": scope.get("app_root_path", root)}
        if path == "/i" or path.startswith("/i/"):
            return None, scope
        return ROOT_TENANT, scope

    @staticmethod
    async def _refuse(scope, receive, send, status: int, detail: str, headers=None) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013 if status == 503 else 1008})
            return
        await JSONResponse({"detail": detail}, status, headers)(scope, receive, send)

    # --- who may wake a sleeping tenant --------------------------------------
    async def _admit(self, slot: _Slot, scope, receive, send):
        """Decide whether this request may start the tenant; returns `receive` to use.

        Only a credential that checks out can wake a tenant. Admin keys and webhook,
        email and OAuth signatures are checked from its sealed secrets; user tokens and
        sign-in tickets by hash in its own table. Public pages are served from here.
        """
        path = get_route_path(scope)
        limits = self._plan(slot)
        refusal = plan_refusal(path, limits) if limits is not None else None
        if refusal is not None:
            await JSONResponse(refusal, 403)(scope, receive, send)
            raise _Answered
        if scope["type"] == "http" and scope["method"] in ("GET", "HEAD") and _is_public_page(path):
            if slot.shell is None:
                slot.shell = public_shell(slot.tenant.name)
            await slot.shell(scope, receive, send)
            raise _Answered
        headers = _headers(scope)
        secrets = self.registry.unseal(slot.tenant)
        method = scope.get("method")
        if method == "POST" and path in ("/ingest/email", "/ingest/crontal", "/login/ticket"):
            body = await self._read_body(receive)
            if body is None:
                await self._refuse(scope, receive, send, 413, "email too large")
                raise _Answered
            receive = _replaying(body, receive)
            if await self._body_checks_out(slot, path, headers, secrets, body):
                return receive
        elif method == "GET" and path == CALLBACK_PATH:
            state = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"))).get("state")
            box = SecretBox.from_env(secrets)
            if state and box is not None and box.verify(state) is not None:
                return receive
        else:
            token = _bearer(scope, headers)
            if token and await self._token_checks_out(slot, token, secrets):
                return receive
        await self._refuse(scope, receive, send, 401, "authentication required")
        raise _Answered

    async def _body_checks_out(self, slot: _Slot, path: str, headers: dict[str, str],
                               secrets: dict[str, str], body: bytes) -> bool:
        if path == "/ingest/email":
            secret = secrets.get("HANDLER_EMAIL_SECRET")
            return bool(secret) and verify_email(secret, headers.get("x-handler-timestamp"),
                                                 headers.get("x-handler-signature"), body)
        if path == "/ingest/crontal":
            secret = secrets.get("HANDLER_WEBHOOK_SECRET")
            return bool(secret) and verify_crontal(secret, headers.get("x-crontal-signature"), body)
        try:
            ticket = json.loads(body).get("ticket")
        except (ValueError, AttributeError):
            return False
        return isinstance(ticket, str) and await self._on_file(
            slot, "sign_in_tickets", "ticket_hash", ticket_hash(slot.tenant.name, ticket))

    async def _token_checks_out(self, slot: _Slot, token: str, secrets: dict[str, str]) -> bool:
        admin_key = secrets.get("HANDLER_ADMIN_KEY", "")
        if admin_key and hmac.compare_digest(token.encode(), admin_key.encode()):
            return True
        return await self._on_file(slot, "user_tokens", "token_hash", hash_token(token))

    async def _on_file(self, slot: _Slot, table: str, column: str, digest: str) -> bool:
        """Look a hashed credential up, at most a few at once and failures remembered."""
        key = (slot.tenant.name, digest)
        now = monotonic()
        if self._bad_credentials.get(key, 0.0) > now:
            return False
        async with self._credential_checks:
            try:
                found = await self.registry.credential_exists(
                    slot.tenant.schema_name, table, column, digest)
            except UndefinedTable:  # never started: it has no such credential
                found = False
        if not found:
            self._bad_credentials[key] = now + BAD_CREDENTIAL_SECONDS
            self._bad_credentials.move_to_end(key)
            while len(self._bad_credentials) > MAX_BAD_CREDENTIALS:
                self._bad_credentials.popitem(last=False)
        return found

    # --- email a tenant can't take right now ---------------------------------
    @staticmethod
    def _is_email_ingest(scope) -> bool:
        path = get_route_path(scope)  # the child scope's root_path strips /i/<name>
        return scope["type"] == "http" and scope["method"] == "POST" and path == "/ingest/email"

    async def _spool_email(self, slot: _Slot, scope, receive, send) -> bool:
        """Keep a signed email the tenant can't take. False if this isn't one to keep.

        The Cloudflare Worker makes one POST and never retries, so a refusal here
        would lose the message from Handler. Checks are those of web/email_ingest.
        A failure to store (database down) leaves the sender a 503.
        """
        if not self._is_email_ingest(scope):
            return False
        secret = self.registry.unseal(slot.tenant).get("HANDLER_EMAIL_SECRET")
        if not secret:
            return False
        headers = {key.decode("latin-1").lower(): value.decode("latin-1")
                   for key, value in scope.get("headers", [])}
        body = await self._read_body(receive)
        if body is None:
            await self._refuse(scope, receive, send, 413, "email too large")
            return True
        if not verify_email(secret, headers.get("x-handler-timestamp"),
                            headers.get("x-handler-signature"), body):
            await self._refuse(scope, receive, send, 401, "invalid signature")
            return True
        if not body.strip():
            await self._refuse(scope, receive, send, 422, "empty email")
            return True
        kept = {key: headers[key] for key in REPLAYED_HEADERS if key in headers}
        try:
            await self.registry.spool_email(slot.tenant.name, hashlib.sha256(body).hexdigest(),
                                            body, kept)
        except Exception:
            logger.exception("could not spool email", extra={"tenant": slot.tenant.name})
            await self._refuse(scope, receive, send, 503, "instance unavailable",
                               headers={"Retry-After": "5"})
            return True
        logger.info("email spooled", extra={"tenant": slot.tenant.name, "size": len(body)})
        await JSONResponse({"status": "spooled"}, 202)(scope, receive, send)
        return True

    @staticmethod
    async def _read_body(receive) -> bytes | None:
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            size += len(chunks[-1])
            if size > MAX_EMAIL_BYTES:
                return None
            if not message.get("more_body"):
                break
        return b"".join(chunks)

    def _start_replay(self, slot: _Slot) -> None:
        if slot.replay is None or slot.replay.done():
            slot.replay = asyncio.create_task(self._replay_spool(slot),
                                              name=f"replay-{slot.tenant.name}")
            slot.replay.add_done_callback(self._log_failure)

    async def _replay_spool(self, slot: _Slot) -> None:
        """Send spooled mail through the tenant's normal intake, quota gate included.

        Each is signed afresh, as its original timestamp has aged out. Intake's
        idempotency key makes a repeat harmless and uncounted.
        """
        name = slot.tenant.name
        secret = self.registry.unseal(slot.tenant).get("HANDLER_EMAIL_SECRET")
        if not secret:
            return
        async with self.services.tenants.use(name) as context:
            if context is None:
                return
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app),
                                         base_url="http://handler", timeout=120) as client:
                for digest in await self.registry.spooled_digests(name):
                    message = await self.registry.spooled_message(name, digest)
                    if message is None:
                        continue
                    body, headers = message
                    timestamp, signature = sign_email(secret, body)
                    try:
                        response = await client.post("/ingest/email", content=body, headers={
                            **headers, "x-handler-timestamp": timestamp,
                            "x-handler-signature": signature})
                    except Exception as exc:  # noqa: BLE001 - stays spooled
                        await self._replay_failed(name, digest, repr(exc))
                        continue
                    if response.status_code < 300:
                        await self.registry.spool_delivered(name, digest)
                    else:
                        await self._replay_failed(name, digest,
                                                  f"{response.status_code}: {response.text}")

    async def _replay_failed(self, name: str, digest: str, error: str) -> None:
        attempts = await self.registry.spool_failed(
            name, digest, error, SPOOL_RETRY_SECONDS, SPOOL_RETRY_MAX_SECONDS, SPOOL_MAX_ATTEMPTS)
        extra = {"tenant": name, "digest": digest, "attempts": attempts, "error": error[:300]}
        if attempts >= SPOOL_MAX_ATTEMPTS:
            logger.error("spooled email gave up after repeated failures; row kept", extra=extra)
        else:
            logger.warning("spooled email replay failed", extra=extra)

    async def load(self) -> None:
        """Know every tenant before serving, so a lookup never needs the database.

        A change made elsewhere (another process, or one this read races) is acted on
        here just as if the platform API had reported it.
        """
        for tenant in await self.registry.list():
            slot, limits_changed, config_changed = self._remember(tenant)
            if limits_changed or config_changed:
                self._react_soon(slot, limits_changed)
        self._names_read_at = monotonic()

    def _slot(self, name: str) -> _Slot | None:
        return self._slots.get(name)

    def _remember(self, tenant: Tenant) -> tuple[_Slot, bool, bool]:
        """Record the tenant as read. Returns its slot, and whether its plan limits and
        its config changed from the last read."""
        slot = self._slots.get(tenant.name)
        if slot is None:
            slot = self._slots[tenant.name] = _Slot(tenant)
            before, before_at = None, None
        else:
            before, before_at = slot.limits_key, slot.tenant.updated_at
        key = _limits_key(tenant)
        limits_changed = before is not None and before != key
        config_changed = before_at is not None and tenant.updated_at != before_at
        slot.tenant = tenant
        if key != slot.limits_key:
            slot.limits_key = key
            slot.limits = self._limits_for(tenant) if self._limits_for is not None else None
        self._by_schema[tenant.schema_name] = slot
        self.services.tenants.serve(ServedTenant(
            tenant.name, tenant.schema_name, hub=self.hub, enabled=tenant.enabled,
            plan=tenant.plan))
        return slot, limits_changed, config_changed

    # --- changes: registry, plan or settings -----------------------------------
    async def tenant_changed(self, name: str) -> None:
        """The platform API changed this tenant (its NOTIFY reaches every process)."""
        tenant = await self.registry.get(name)
        if tenant is None:
            return
        slot, limits_changed, _ = self._remember(tenant)
        self.services.cache.invalidate(name)  # new secrets, config and plan from now on
        await self._react(slot, limits_changed)

    def _changed_notice(self, hub: Any, schema: str) -> None:
        """NOTIFY handler_tenant_changed (any process): re-read the row."""
        slot = self._by_schema.get(schema)
        if hub is not self.hub or slot is None or self._closing:
            return
        task = asyncio.create_task(self.tenant_changed(slot.tenant.name),
                                   name=f"changed-{slot.tenant.name}")
        self._reacting.add(task)
        task.add_done_callback(self._reacted)

    def _react_soon(self, slot: _Slot, limits_changed: bool) -> None:
        self.services.cache.invalidate(slot.tenant.name)
        task = asyncio.create_task(self._react(slot, limits_changed),
                                   name=f"changed-{slot.tenant.name}")
        self._reacting.add(task)
        task.add_done_callback(self._reacted)

    def _reacted(self, task: asyncio.Task[Any]) -> None:
        self._reacting.discard(task)
        self._log_failure(task)

    async def _react(self, slot: _Slot, limits_changed: bool) -> None:
        """A disabled tenant is no longer served (its claims stop at once); a plan
        change is built into a fresh context now, which releases held input and
        resumes schedules the old plan paused (backfill_due)."""
        if not slot.tenant.enabled or not limits_changed:
            return
        try:
            async with self.services.tenants.use(slot.tenant.name):
                pass
        finally:
            await self.registry.request_wake(slot.tenant.name)
            self.services.runner.hint(slot.tenant.name)

    @staticmethod
    def _log_failure(task: asyncio.Task[Any]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("tenant host step failed", exc_info=task.exception())

    # --- the one scanning task ----------------------------------------------
    async def boot(self) -> None:
        """Every enabled tenant is looked at once by the host loops after a start:
        its context build backfills due work, and leftover queued work runs."""
        await self.hub.register_tenants([(slot.tenant.name, slot.tenant.schema_name)
                                         for slot in self._slots.values()
                                         if slot.tenant.enabled])

    async def run(self) -> None:
        while not self._closing:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tenant scan failed")
            self._scan_now.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._scan_now.wait(), self.scan_seconds)

    async def tick(self) -> None:
        if monotonic() - self._names_read_at >= NAME_REFRESH_SECONDS:
            await self.load()  # the one way a tenant made elsewhere becomes known
        for name in await self.registry.tenants_with_spooled_email():
            slot = self._slot(name)
            limits = self._plan(slot) if slot is not None else None
            if slot is not None and (limits is None or limits.email):  # kept until email returns
                self._start_replay(slot)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="tenant-scan")
        self._task.add_done_callback(self._log_failure)

    async def shutdown(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        pending = [s.replay for s in self._slots.values() if s.replay is not None]
        await asyncio.gather(*pending, *self._reacting, return_exceptions=True)


class EnvRouter:
    """ASGI front of an env-defined host: `/i/<name>` (or `/` for main) to that
    tenant's context, built on demand and pinned for the request."""

    def __init__(self, tenants: Any) -> None:
        self.tenants = tenants

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return
        name, child = TenantHost._route(scope)
        if name is None or self.tenants.get(name) is None:
            await TenantHost._refuse(scope, receive, send, 404, "instance not found")
            return
        async with self.tenants.use(name) as context:
            if context is None:
                await TenantHost._refuse(scope, receive, send, 404, "instance not found")
                return
            await context.app(child, receive, send)
