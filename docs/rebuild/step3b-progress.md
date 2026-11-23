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

Suite: 1737 passed, 5 skipped.

## Next

- Isolation tests for the routed app (cross-tenant keys, tokens, run tokens; threads,
  uploads, streams under either URL); discovery tests.
- Registry switch-on rehearsal (test and operator procedure).
- Load test: 50 registry tenants.
- Docs: role-split-design, tenant-registry, multi-instance, integrations, HANDOFF.
