"""Wake on demand: one runtime per tenant, existing only while it has a reason to.

The TenantHost is the ASGI app in front of every tenant and the owner of each
tenant's lifecycle. All starts and stops of one tenant run under that tenant's
lock, so there is never more than one runtime for it, and a request or
notification that arrives during a stop waits and then starts a fresh one. Locks
are per tenant, so a slow stop holds up no other tenant.

Work on an inactive tenant is found by any of: its HTTP request; a notification for
its schema; `tenant_state.next_wake_at`, which a process outside this host writes
in the same transaction as the work and a stop writes from the tenant's schedules
and retries; and a sweep of each tenant's own tables at boot and once more after
the boot grace, for writes that predate any of these.
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
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
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
from .registry import Tenant, TenantRegistry

logger = logging.getLogger("handler.runtime.tenancy")

ROOT_TENANT = "main"
# Integrations that may poll for input. A tenant is pinned awake only while one
# of them has something to poll: an enabled connected mailbox. Gmail has no
# poller (it is read on demand), so it never pins a tenant.
PINNED_INTEGRATIONS = frozenset({"mailbox", "gmail", "google"})
POLLING_INTEGRATIONS = frozenset({"mailbox"})
_INSTANCE_PATH = re.compile(r"^/i/([a-z][a-z0-9_]{0,31})(/.*)$")
ACTIVATION_RETRY_SECONDS = 30.0
SPOOL_RETRY_SECONDS = 300.0
SPOOL_RETRY_MAX_SECONDS = 86_400.0
SPOOL_MAX_ATTEMPTS = 12
DEFAULT_MAX_ACTIVE = 40
BAD_CREDENTIAL_SECONDS = 60.0
MAX_BAD_CREDENTIALS = 4096
NAME_REFRESH_SECONDS = 60.0
REPLAYED_HEADERS = ("content-type", "x-handler-envelope-from", "x-handler-envelope-to",
                    "x-handler-auth-results", "x-handler-forwarded")


def _limits_key(tenant: Tenant) -> tuple:
    return tenant.plan, json.dumps(tenant.limits, sort_keys=True)


class AtCapacity(Exception):
    """Every active-tenant place is held by a tenant that can't be stopped now."""


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
class _Runtime:
    app: Any
    stop: asyncio.Event
    owner: asyncio.Task[None]


@dataclass
class _Slot:
    tenant: Tenant
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    runtime: _Runtime | None = None
    activation: asyncio.Task[Any] | None = None
    deactivation: asyncio.Task[Any] | None = None
    replay: asyncio.Task[Any] | None = None
    in_flight: set[asyncio.Task[Any]] = field(default_factory=set)
    last_activity: float = field(default_factory=monotonic)
    stale: bool = False  # its config changed; restart once quiescent
    recover: bool = False  # a stop could not record its state
    shell: Any = None  # the public pages, built when first asked for
    limits: Any = None  # PlanLimits for the tenant as last read, resolved on change only
    limits_key: tuple | None = None
    # What the running runtime was started with or last updated to.
    applied_key: tuple | None = None
    applied_at: Any = None

    polls: bool = False  # last seen with an enabled connected mailbox

    @property
    def pinned(self) -> bool:
        return self.polls and self.may_poll

    @property
    def may_poll(self) -> bool:
        return bool(POLLING_INTEGRATIONS.intersection(self.tenant.integrations or ()))

    def touch(self) -> None:
        self.last_activity = monotonic()


RuntimeFactory = Callable[[Tenant], Any]  # an async context manager yielding the app


class TenantHost:
    def __init__(self, registry: TenantRegistry, runtime_for: RuntimeFactory, *,
                 idle_seconds: float = 600.0, scan_seconds: float = 15.0,
                 boot_grace_seconds: float = 120.0, concurrent_activations: int = 4,
                 limits_for: Callable[[Tenant], PlanLimits] | None = None,
                 max_active: int = DEFAULT_MAX_ACTIVE) -> None:
        self.registry = registry
        self._runtime_for = runtime_for
        self.idle_seconds = idle_seconds
        self.scan_seconds = scan_seconds
        self.boot_grace_seconds = boot_grace_seconds
        self._limits_for = limits_for
        self.max_active = max_active
        self._occupied: set[str] = set()  # running or being started
        self._bad_credentials: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._credential_checks = asyncio.Semaphore(4)
        self._names_read_at = 0.0
        self._starting = asyncio.Semaphore(concurrent_activations)
        self._slots: dict[str, _Slot] = {}
        self._by_schema: dict[str, _Slot] = {}
        self._scan_now = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False

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
        task = asyncio.current_task()
        slot.in_flight.add(task)  # before any await: a stop must see this request
        credentialed = _carries_credential(child)
        try:
            if self._sleeping(slot):
                try:
                    receive = await self._admit(slot, child, receive, send)
                except _Answered:
                    return
            try:
                runtime = None if self._closing else await self._runtime(slot)
            except Exception as exc:
                if not isinstance(exc, AtCapacity):
                    logger.exception("tenant activation failed", extra={"tenant": slot.tenant.name})
                if not await self._spool_email(slot, child, receive, send):
                    await self._refuse(scope, receive, send, 503, "instance unavailable",
                                       headers={"Retry-After": "5"})
                return
            if runtime is None:
                if await self._spool_email(slot, child, receive, send):
                    return
                # Shutting down must read as "retry", so senders don't give up on mail.
                status, detail = ((503, "instance unavailable") if self._closing
                                  else (404, "instance not found"))
                await self._refuse(scope, receive, send, status, detail)
                return
            if credentialed:  # an anonymous probe must not keep a tenant awake
                slot.touch()
            await runtime.app(child, receive, send)
        finally:
            slot.in_flight.discard(task)
            if credentialed:
                slot.touch()

    def _plan(self, slot: _Slot) -> PlanLimits | None:
        if self._limits_for is not None and slot.limits is None:
            slot.limits = self._limits_for(slot.tenant)
        return slot.limits

    def _schedules_count(self, slot: _Slot) -> bool:
        limits = self._plan(slot)
        return limits is None or limits.automations

    @staticmethod
    def _sleeping(slot: _Slot) -> bool:
        return slot.runtime is None or slot.lock.locked()

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
        task = asyncio.current_task()
        slot.in_flight.add(task)
        name = slot.tenant.name
        try:
            try:
                runtime = await self._runtime(slot)
            except AtCapacity:
                return  # the next scan tries again
            secret = self.registry.unseal(slot.tenant).get("HANDLER_EMAIL_SECRET")
            if runtime is None or not secret:
                return
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.app),
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
        finally:
            slot.in_flight.discard(task)
            slot.touch()

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
            if limits_changed or config_changed or (slot.runtime and not tenant.enabled):
                task = asyncio.create_task(self._react(slot, limits_changed, config_changed),
                                           name=f"changed-{tenant.name}")
                task.add_done_callback(self._log_failure)
        self._names_read_at = monotonic()

    def _slot(self, name: str) -> _Slot | None:
        return self._slots.get(name)

    def _remember(self, tenant: Tenant) -> tuple[_Slot, bool, bool]:
        """Record the tenant as read. Returns its slot, and whether its plan limits and
        its config changed from what its runtime runs with (or, asleep, from the last read)."""
        slot = self._slots.get(tenant.name)
        if slot is None:
            slot = self._slots[tenant.name] = _Slot(tenant)
        key = _limits_key(tenant)
        before = slot.applied_key if slot.runtime is not None else slot.limits_key
        limits_changed = before is not None and before != key
        config_changed = slot.runtime is not None and tenant.updated_at != slot.applied_at
        slot.tenant = tenant
        if key != slot.limits_key:
            slot.limits_key = key
            slot.limits = self._limits_for(tenant) if self._limits_for is not None else None
        self._by_schema[tenant.schema_name] = slot
        return slot, limits_changed, config_changed

    # --- lifecycle: every transition holds the tenant's lock ----------------
    async def _runtime(self, slot: _Slot) -> _Runtime | None:
        if slot.runtime is not None and not slot.lock.locked():
            return slot.runtime  # no await before the caller counts as in flight
        return await asyncio.shield(self._activation(slot))

    def _activation(self, slot: _Slot) -> asyncio.Task[Any]:
        """The tenant's one activation; shared, so a caller leaving can't abandon a start."""
        if slot.activation is None or slot.activation.done():
            slot.activation = asyncio.create_task(self._activate(slot),
                                                  name=f"activate-{slot.tenant.name}")
            slot.activation.add_done_callback(self._log_failure)
        return slot.activation

    @staticmethod
    def _log_failure(task: asyncio.Task[Any]) -> None:
        if not task.cancelled() and isinstance(task.exception(), AtCapacity):
            logger.warning("tenant not started: at the active tenant limit")
        elif not task.cancelled() and task.exception() is not None:
            logger.error("tenant lifecycle step failed", exc_info=task.exception())

    async def _activate(self, slot: _Slot) -> _Runtime | None:
        async with slot.lock:
            if slot.runtime is not None or not slot.tenant.enabled or self._closing:
                return slot.runtime
            name = slot.tenant.name
            try:
                await self._reserve_capacity(slot)
            except AtCapacity:
                with suppress(Exception):
                    await self.registry.activation_failed(name, ACTIVATION_RETRY_SECONDS)
                raise
            async with self._starting:
                # Before the start: a crash from here on is recovered at boot.
                await self.registry.mark_active(name)
                try:
                    slot.runtime = await self._start(slot)
                except BaseException:
                    self._occupied.discard(name)
                    with suppress(Exception):
                        await self.registry.activation_failed(name, ACTIVATION_RETRY_SECONDS)
                    raise
            slot.recover = False
            slot.touch()
            logger.info("tenant activated", extra={"tenant": name})
            return slot.runtime

    async def _reserve_capacity(self, slot: _Slot) -> None:
        """Claim one of the active-tenant places, stopping quiescent LRU tenants if needed."""
        tried: set[str] = set()
        while True:
            if len(self._occupied) < self.max_active:
                self._occupied.add(slot.tenant.name)  # no await since the check
                return
            victims = [s for s in self._slots.values()
                       if s.runtime is not None and not s.pinned and not s.in_flight
                       and not s.lock.locked() and s.tenant.name not in tried]
            if not victims:
                raise AtCapacity(slot.tenant.name)
            victim = min(victims, key=lambda s: s.last_activity)
            tried.add(victim.tenant.name)
            await self._deactivate(victim)

    async def _start(self, slot: _Slot) -> _Runtime:
        tenant = await self.registry.get(slot.tenant.name) or slot.tenant
        self._remember(tenant)
        slot.applied_key, slot.applied_at = _limits_key(tenant), tenant.updated_at
        ready = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()

        async def own_runtime() -> None:
            # MCP uses AnyIO cancel scopes: enter AND exit the runtime in this task.
            try:
                async with self._runtime_for(tenant) as app:
                    ready.set_result(app)
                    await stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    raise

        owner = asyncio.create_task(own_runtime(), name=f"tenant-{tenant.name}")
        owner.add_done_callback(self._log_failure)
        return _Runtime(await ready, stop, owner)

    async def _quiescent(self, slot: _Slot, runtime: _Runtime) -> bool:
        """No in-flight run, open request or SSE stream, due work or unflushed files."""
        parts = runtime.app.state.lifecycle
        if slot.in_flight or parts.dispatcher.active_count:
            slot.touch()
            return False
        due = await parts.store.earliest_work(list(parts.outbox.sinks),
                                              schedules=self._schedules_count(slot))
        if due is not None and due <= parts.store._clock():
            return False
        if parts.workspace_sync.dirty:
            await parts.workspace_sync.flush()
            if parts.workspace_sync.dirty:
                logger.warning("tenant stays active: workspace flush failed",
                               extra={"tenant": slot.tenant.name})
                return False
        return not slot.in_flight

    async def _deactivate(self, slot: _Slot, *, force: bool = False) -> bool:
        async with slot.lock:
            runtime = slot.runtime
            if runtime is None:
                return False
            if force:
                for task in list(slot.in_flight):
                    task.cancel()
            elif not await self._quiescent(slot, runtime):
                return False
            name, schema = slot.tenant.name, slot.tenant.schema_name
            sinks = list(runtime.app.state.lifecycle.outbox.sinks)
            runtime.stop.set()
            await asyncio.gather(runtime.owner, return_exceptions=True)
            slot.runtime, slot.stale = None, False
            self._occupied.discard(name)
            try:
                # The wake goes first: if we die before active_since clears, the
                # next boot still activates this tenant.
                await self.registry.write_next_wake(name, schema, sinks,
                                                    schedules=self._schedules_count(slot))
                await self.registry.clear_active(name)
            except Exception:
                slot.recover = True
                logger.exception("tenant stop could not be recorded", extra={"tenant": name})
                return False
            logger.info("tenant deactivated", extra={"tenant": name})
            return True

    def _start_deactivation(self, slot: _Slot) -> None:
        if slot.deactivation is None or slot.deactivation.done():
            slot.deactivation = asyncio.create_task(self._deactivate(slot),
                                                    name=f"deactivate-{slot.tenant.name}")
            slot.deactivation.add_done_callback(self._log_failure)

    # --- platform API hooks -------------------------------------------------
    async def tenant_changed(self, name: str) -> None:
        tenant = await self.registry.get(name)
        if tenant is None:
            return
        slot, limits_changed, config_changed = self._remember(tenant)
        await self._react(slot, limits_changed, config_changed)

    async def _react(self, slot: _Slot, limits_changed: bool, config_changed: bool) -> None:
        if not slot.tenant.enabled:
            await self._deactivate(slot, force=True)
            return
        if config_changed:
            slot.stale = True  # new config and secrets take effect on the next start
        if limits_changed:
            await self._apply_limits(slot)

    async def _apply_limits(self, slot: _Slot) -> None:
        """A plan change frees held input now; the rest of a restart can wait for quiescence."""
        if self._limits_for is None:
            return
        runtime = slot.runtime
        if runtime is None or slot.lock.locked():
            # Starts with the new plan; its start (backfill_due) releases the backlog.
            await self.registry.request_wake(slot.tenant.name)
            self._activation(slot)
            return
        parts = runtime.app.state.lifecycle
        parts.limits.current = self._plan(slot)
        slot.applied_key = slot.limits_key
        await parts.store.release_held_inputs()
        await parts.backfill_due()  # schedules kept on a plan without them resume now

    # --- the host timer's view of tenants -------------------------------------
    def timer_directory(self, hub: Any) -> "RegistryTimerDirectory":
        return RegistryTimerDirectory(self, hub)

    # --- the one scanning task ----------------------------------------------
    async def run(self) -> None:
        await self.boot()
        resweep_at = monotonic() + self.boot_grace_seconds
        while not self._closing:
            try:
                await self.tick()
                if resweep_at is not None and monotonic() >= resweep_at:
                    resweep_at = None
                    await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tenant scan failed")
            self._scan_now.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._scan_now.wait(), self.scan_seconds)

    async def boot(self) -> None:
        for slot in list(self._slots.values()):
            tenant = slot.tenant
            if tenant.enabled and slot.may_poll:
                await self._refresh_polling(slot)
            if tenant.enabled and (tenant.active_since is not None or slot.pinned):
                self._activation(slot)
        self.registry.hub.on_unattached = self._notified
        self.registry.hub.on_listening = self._scan_now.set
        await self.sweep()

    async def sweep(self) -> None:
        """Look inside each inactive tenant once, for work no signal told us about."""
        for slot in list(self._slots.values()):
            if slot.runtime is not None or not slot.tenant.enabled:
                continue
            try:
                if await self.registry.sweep_tenant(slot.tenant.name, slot.tenant.schema_name,
                                                    schedules=self._schedules_count(slot)):
                    self._activation(slot)
            except UndefinedTable:
                pass  # created but never started: nothing in it yet
            except Exception:
                logger.exception("tenant sweep failed", extra={"tenant": slot.tenant.name})

    async def _refresh_polling(self, slot: _Slot) -> None:
        """Whether this tenant now has a mailbox to poll (one query, its own schema)."""
        if not slot.may_poll:
            slot.polls = False
            return
        try:
            slot.polls = await self.registry.has_polling_mailbox(slot.tenant.schema_name)
        except UndefinedTable:
            slot.polls = False  # the mailbox tables appear with the first mailbox
        except Exception:
            logger.exception("could not check mailboxes", extra={"tenant": slot.tenant.name})

    def _notified(self, schema: str) -> None:
        slot = self._by_schema.get(schema)
        if slot is not None and slot.tenant.enabled and not self._closing:
            self._activation(slot)

    async def tick(self) -> None:
        if monotonic() - self._names_read_at >= NAME_REFRESH_SECONDS:
            await self.load()  # the one way a tenant made elsewhere becomes known
        for name, seen in await self.registry.due():
            slot = self._slot(name)
            if slot is None:
                continue
            if slot.runtime is not None and not slot.lock.locked():
                await self.registry.clear_wake(name, seen)  # its dispatcher polls for itself
                slot.runtime.app.state.lifecycle.store._wake.set()
            else:
                self._activation(slot)
        for name in await self.registry.tenants_with_spooled_email():
            slot = self._slot(name)
            limits = self._plan(slot) if slot is not None else None
            if slot is not None and (limits is None or limits.email):  # kept until email returns
                self._start_replay(slot)  # activates the tenant first if it is asleep
        for slot in list(self._slots.values()):
            if slot.runtime is not None and slot.may_poll and not slot.lock.locked():
                await self._refresh_polling(slot)  # pin on a new mailbox, unpin on the last
        for slot in self._slots.values():
            if not slot.tenant.enabled:
                continue
            if slot.runtime is None:
                if slot.pinned or slot.recover:
                    self._activation(slot)
            elif slot.stale or (not slot.pinned
                                and monotonic() - slot.last_activity >= self.idle_seconds):
                self._start_deactivation(slot)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="tenant-scan")
        self._task.add_done_callback(self._log_failure)

    async def shutdown(self) -> None:
        """Stop every runtime together. active_since stays set, so a restart resumes them."""
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        slots = list(self._slots.values())
        pending = [t for s in slots for t in (s.activation, s.deactivation, s.replay) if t is not None]
        await asyncio.gather(*pending, return_exceptions=True)
        for slot in slots:
            if slot.runtime is not None:
                slot.runtime.stop.set()
        results = await asyncio.gather(
            *(s.runtime.owner for s in slots if s.runtime is not None), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.error("tenant lifecycle failed", exc_info=result)


class RegistryTimerDirectory:
    """Tenants as the host timer sees them on a registry host.

    Due rows of every enabled tenant are claimed from the hub's queue (the control
    schema only). A row of a sleeping tenant activates it, as a request would; the
    firing counts as in flight, so the tenant can't stop under it. Step 3 replaces
    activation with the tenant context cache; this is the one place to change.
    """

    def __init__(self, host: TenantHost, hub: Any) -> None:
        self._host = host
        self._queue = hub.due_queue()

    def register(self, target: Any) -> None:
        pass  # the running runtime's target is found through its slot

    def unregister(self, target: Any) -> None:
        pass

    def sources(self) -> list[tuple[Any, list[str]]]:
        schemas = [schema for schema, slot in self._host._by_schema.items()
                   if slot.tenant.enabled and slot.tenant.schema_name == schema]
        return [(self._queue, schemas)]

    @asynccontextmanager
    async def tenant(self, queue: Any, key: str):
        slot = self._host._by_schema.get(key)
        if slot is None or not slot.tenant.enabled or self._host._closing:
            yield None
            return
        task = asyncio.current_task()
        slot.in_flight.add(task)  # before any await: a stop must see this firing
        try:
            runtime = await self._host._runtime(slot)
            yield None if runtime is None else runtime.app.state.timer_target
        finally:
            slot.in_flight.discard(task)
