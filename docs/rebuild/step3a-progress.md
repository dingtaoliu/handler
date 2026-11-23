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

## Next

- Fairness, isolation, invalidation-by-NOTIFY and 50-tenant connection-budget tests.
- Full suite green; docs (role-split step 3 row, tenant-registry, multi-instance,
  multi-tenant-design, dispatcher notes).
