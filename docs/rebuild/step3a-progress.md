# Shared runtime step 3a: progress

Branch `feat/shared-runtime-step3a` (from `feat/shared-runtime` at 8d52be4). Brief: the
runtime core of step 3 in [role-split-design.md](role-split-design.md): a tenant context
cache, one claim loop across tenants, one outbox loop, and `TenantHost`'s activation
lifecycle replaced by the cache. 3b (after this) does the request side.

## Done

1. **Wake records** (`store/postgres_hub.py` `TENANT_STATE_DDL`, `wakes.py`):
   `handler_control.tenant_state` is created by the hub (env tenants too), with additive
   columns `schema_name`, `wake_version`, `next_delivery_at`, `delivery_version`. Every
   writer of pending work marks `next_wake_at` in its own transaction (hub `_notify`,
   `MARK_WAKE_SQL`); deliveries mark `next_delivery_at` (`MARK_DELIVERY_SQL`).
2. **Tenant-changed notices:** registry writes and settings saves send
   `NOTIFY handler_tenant_changed` (payload: schema); the hub skips its own backends'
   notices (handled in process).
3. **Context cache** (`tenancy/contexts.py`): `TenantContext`, `ContextCache` (LRU,
   TTL, pinned while in use, generation-based invalidation), `HostTenants` directory.
4. **One claim loop** (`runner.py` `HostRunner`) through the host `RunGate`;
   `Dispatcher` is that loop over one store.
5. **One outbox loop** (`sinks.py` `HostOutbox`), round robin by batch.
6. **One MCP app for the host**: run tokens carry their tenant's gateway and name;
   a token under another tenant's URL is refused.
7. **`instance_runtime` → `build_tenant_context`** (no long-lived task); `HostServices`
   holds cache, loops, timer, MCP; env hosts route through `EnvRouter`; `TenantHost`
   keeps only the request side. Activation, eviction, the per-tenant lock, the boot
   sweep, the active-tenant cap and pinning are gone.
8. Tests moved to the context model: registry, free plan, plan limits, review fixes,
   multi-instance, integrations; crash and lease reclaim; idle 50 (env and registry).

9. New checks: `test_host_runner.py` (fairness: with the gate full a chat goes before
   another tenant's background flood; a free flood cannot starve a paid tenant; two
   tenants run at once; a disabled tenant is not claimed), `store_pg/test_host_loops.py`
   (two tenants running at once share no store, bus, workspace, sink, context or
   token; a plan change and a disable from another process apply through
   `NOTIFY handler_tenant_changed`; settings saved elsewhere drop the context; 50
   tenants under load stay within 12 connections), `store_pg/test_idle_tenants.py` (50
   idle env and registry tenants: no per-tenant task, no cached context after the TTL,
   only control-schema statements), `test_tenant_registry.py` (a crashed run's lease
   is the next wake and the thread is reclaimed, dead run swept as abandoned).
10. Writer audit (every writer marks `tenant_state` in its transaction through the hub
    store's `_notify`): `put` (chat, ingest, Crontal, email, approval and question
    answers via `emit_event`, timer handlers' events), schedule firing, held-input
    release, `complete` (remaining events), `fail` with retry (`available_at`: the
    loop writes the retry time back), terminal `fail`, `replay_failed_run` (operator
    replay and user retry), `revive_thread`, `sweep_dead_threads`; deliveries:
    `enqueue_delivery`, retrying `delivery_failed`, `redeliver`; registry:
    `set_enabled`, `request_wake`, `spool_email` bump `wake_version`.
11. Docs: role-split-design (step 3 row, progress), tenant-registry, multi-instance,
    multi-tenant-design status, HANDOFF.

Full suite: 1736 passed, 5 skipped (baseline 1745: about 35 obsolete lifecycle tests
removed, about 26 added).

## Next (3b)

- One routed app instead of a FastAPI sub-app per context; MCP keyed by token alone.
- Routers, tool sets and sinks as tenant-context factories.
- Crontal tool discovery in context loading (still a start-up task per context until
  it succeeds; cancelled when the context closes).
- Registry switch-on and import of `main`, `bss` and `yeup`.
