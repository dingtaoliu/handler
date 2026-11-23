# Shared runtime step 3b: progress

Branch `feat/shared-runtime-step3b` (from `feat/shared-runtime` at d3f2eae: v65 plus
step 2, the legacy chat import and step 3a). Brief: the request side of step 3 in
[role-split-design.md](role-split-design.md), then readiness to switch the tenant
registry on in production and import `main`, `bss` and `yeup`.

## Done

1. **Store isolation check** (`store/postgres.py`): every transaction of a tenant's
   store checks `context.current_tenant()` (set per request, run, timer firing and
   context build) against the store's tenant, and raises `TenantMismatch` before any
   statement on a mismatch. Probed across the whole suite: never fires.
2. **One routed app** (`web/host_app.py`, `tenancy/routing.py`). `HostServices.app` is
   built once; every router is registered once with `TenantLocal` proxies that read
   the request tenant's object from its context (`TenantContext.parts`). The front
   (`tenancy/host.py`) serves each request inside `serving(context)` (a context
   variable plus `current_tenant`). Routes some tenants lack are route groups
   (`email_ingest`, `integration:<name>`) refused with 404 by `TenantGuard`, which
   also applies the plan's refusals (was `ConsoleGuard` and a per-tenant
   `/api/mailboxes` route). `context.app` is now a `TenantApp` view, not an app. The
   per-slot public shell is gone: a sleeping tenant's pages are the host app served
   with no context (only public pages are reachable that way).
3. **Credentials name the tenant:** `HostTenants` keeps each tenant's admin key
   digest; the front refuses another tenant's admin key with 401 before any build
   (env and registry hosts). Tokens, tickets, signatures and OAuth state are checked
   against the hinted tenant's own, as before.
4. **One MCP route:** `/mcp/` at the host root resolves the tenant from the run
   token alone; harnesses are now given `http://127.0.0.1:<port>/mcp/`. `/i/<name>/mcp/`
   still works for that tenant's tokens (401 for another's). MCP requests never build
   a context.
5. **Integration contract, request side** (`integrations/base.py`, `registry.py`):
   `routers(TenantRoutes)` is called once per host on an unconfigured instance
   (`host_routers`, duplicates refused across every registered integration);
   `serves_routes(ctx)` says whether a tenant is served them; `load(ctx)` is
   awaited during context loading and called again before a run while it returns
   False. `background_tasks` and `BackgroundTask` are removed. Crontal and mailbox
   are ported; legacy Crontal wiring is served the crontal integration's
   `/ingest/crontal`.
6. **Crontal discovery in context loading** (`integrations/crontal/connector.py`):
   `CrontalDiscovery` caches the agent-mcp listing per URL for the host
   (`HANDLER_CRONTAL_DISCOVERY_TTL_SECONDS`, 3600; attempt timeout
   `HANDLER_CRONTAL_DISCOVERY_TIMEOUT_SECONDS`, 10), with a host-wide backoff after a
   failure (5 s doubling to 300 s) and the last listing kept. Each tenant's
   connector is built from it with its own token. A tenant without tools yet logs a
   warning once and works; the next context load or run retries.

7. **Discovery tests** (`crontal/test_discovery.py`): one listing for 20 tenants at
   once with each connector on its own token, TTL expiry, host-wide backoff and
   recovery, an expired listing kept when rediscovery fails, a missing curated tool
   and a slow Crontal count as failures, a tenant's tools registered once available.
8. **Switch-on rehearsal** (`store_pg/test_registry_switch_on.py`,
   `store_pg/production_shape.py`), on a real loopback port: production's env shape
   (`main`/`handler_crontal`, `bss` with `crontal,mailbox`, `yeup`) seeded with users,
   tokens, chats, schedules, memory, approvals and a BSS mailbox; registry on; every
   data table's row count unchanged; registry rows match (schemas, S3 prefixes,
   merchant ids, plan `paid`, sealed admin keys, webhook secrets, agent tokens, Google
   client, BSS email secret, config); old URLs, keys and tokens see the same data;
   Crontal's proxy calls (provisioning, sign-in ticket, panel reads, multipart upload,
   SSE stream, chat with attachment and context links, duplicate resend, failed run
   retry, signed webhook); the harness listed its tools over `/mcp/` with its run
   token; a free tenant through `PUT /platform/tenants/500` (schema `handler_m500`,
   Terra, 50 inputs, no email, automations, console or mailboxes; chat works while
   Crontal discovery fails, tools appear on a later run; legacy chat import uncounted);
   registry off keeps everything (the free tenant 404s, data intact); on again, it is
   back.

9. **Routed app isolation** (`store_pg/test_routed_isolation.py`, env and registry
   hosts, real loopback port, the same user id in every tenant): every admin key and
   user token against every tenant URL (main's two URLs included) and the SSE
   `?access_token=` form: 200 only on its own tenant, else 401; a thread, upload and
   stream of main are invisible to bss under either of main's URLs (401 with bss's
   credentials, 404 or empty under bss's own URL, and bss's stream for the same thread
   key carries nothing of main's run); run tokens at `/mcp/` (any tenant's token) and
   `/i/<name>/mcp/` (only its own); sign-in tickets and webhook signatures bound to
   their tenant; unknown tenants 404 on every route; route groups per tenant; public
   pages and health by the hint alone.

## Next

- Operator procedure for the switch-on (tenant-registry.md).
- Load test: 50 registry tenants.
- Docs: role-split-design, tenant-registry, multi-instance, integrations, HANDOFF.
