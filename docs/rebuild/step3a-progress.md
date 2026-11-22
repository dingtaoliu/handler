# Shared runtime step 3a: progress

Branch `feat/shared-runtime-step3a` (from `feat/shared-runtime` at 8d52be4). Brief: the
runtime core of step 3 in [role-split-design.md](role-split-design.md): a tenant context
cache, one claim loop across tenants, one outbox loop, and `TenantHost`'s activation
lifecycle replaced by the cache. 3b (after this) does the request side.

## Done

- (nothing yet)

## Next

1. One MCP app for the host: run tokens carry their tenant's gateway; no per-tenant MCP
   lifespan.
2. `tenant_state` in the hub's control DDL (wake columns, env tenants too); every writer
   of pending work marks the wake in its own transaction; `NOTIFY handler_tenant_changed`.
3. `TenantContext` built without tasks, in an LRU cache with TTL and invalidation.
4. `HostRunner`: one claim loop over due tenants, through the host `RunGate`.
5. `HostOutbox`: one delivery loop across tenants, round robin.
6. `TenantHost` lifecycle removed; env hosts route through the cache too.
7. Tests (idle 50, fairness, crash/lease, isolation, invalidation, connections) and docs.
