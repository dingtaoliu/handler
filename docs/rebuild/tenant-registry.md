# Tenant registry, wake on demand and run limits

Monthly input limits, free-plan cost limits and spooled email are in
[plan-limits.md](plan-limits.md).

Multi-tenant Phase 2 (`multi-tenant-design.md`, Sections 2 to 4). **Opt-in:** set
`HANDLER_TENANT_REGISTRY=1`. Without it, env-defined hosting (`multi-instance.md`)
is unchanged, and single-instance, SQLite and personal deployments never reach
this code.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `HANDLER_TENANT_REGISTRY` | off | `1` hosts tenants from the database |
| `HANDLER_PLATFORM_KEY` | required | Bearer key for `/platform/*`. Never an admin key, never in a tenant's environment |
| `HANDLER_TENANT_SECRET_KEY` | required | Seals tenants' secrets. Changing it makes them unreadable and stops startup |
| `HANDLER_TENANT_CACHE_SIZE` | 32 | Tenant contexts kept built (LRU); a context in use is never closed, so the cache may briefly exceed it |
| `HANDLER_TENANT_CACHE_TTL_SECONDS` | 600 | An unused context is closed after this |
| `HANDLER_CLAIM_POLL_SECONDS` | 5 | The claim loop's backstop poll of `tenant_state` (signals wake it at once) |
| `HANDLER_OUTBOX_POLL_SECONDS` | 5 | The outbox loop's backstop poll |
| `HANDLER_OUTBOX_BATCH` / `HANDLER_OUTBOX_CONCURRENCY` | 20 / 4 | Deliveries per tenant per round; tenants delivered at once |
| `HANDLER_TENANT_SCAN_SECONDS` | 15 | Spool replay interval (the host's request-side scan) |
| `HANDLER_GLOBAL_MAX_RUNS` | 6 | Runs at once, whole process |
| `HANDLER_PLAN_RUN_CAPS` | `free:1,paid:3` | Runs at once per tenant, by plan. An unknown plan gets the lowest |
| `HANDLER_RUN_MAX_WAIT_SECONDS` | 60 | After this a waiting free tenant ranks with paid ones, and waiting background work goes ahead of chats (once; its wait then restarts) |
| `HANDLER_ALLOWED_HARNESSES` | all | Harnesses the host may run, comma-separated; production sets `openai_agents`. Never in a tenant's environment. A tenant whose `HANDLER_DEFAULT_HARNESS` is outside it is refused (see HANDOFF §3) |

When slots are contended, the gate admits interactive work (chat) before background
work (automations, email, Crontal events) across all tenants; then paid before free;
then the tenant with fewer runs; then the longest wait. A tenant's `limits.max_runs` overrides its plan cap. Each tenant's own
`HANDLER_MAX_CONCURRENT` and lane capacities still apply beneath these. The three
run-gate settings apply with the registry off too: env-defined instances share the same
host gate, each counted as `paid` with no `max_runs` override.

## Import

On the first start with the registry on, the instances in `HANDLER_INSTANCES` are
copied in once. They keep their names, schemas, S3 prefixes, URLs and data
directories, and `main` keeps its root alias. Per-tenant values become `config`,
and anything named like a credential is sealed. Set
`HANDLER_I_<NAME>__HANDLER_MERCHANT_ID` beforehand to link an instance to its
Crontal merchant; otherwise the platform API can't find it and would create a
second tenant for that merchant.

**After the import, the registry wins.** `HANDLER_INSTANCES` and `HANDLER_I_*` are
ignored, except to go back to env-defined hosting (see Rollback). Values passed only
as CLI flags are not imported, apart from host, port, data dir, database and schema.

## Platform API

`Authorization: Bearer $HANDLER_PLATFORM_KEY`. All writes are safe to repeat.

| Call | Effect |
|---|---|
| `PUT /platform/tenants/{merchant_id}` | Creates tenant `m<merchant_id>` (201), or changes the supplied fields (200). No restart |
| `POST /platform/tenants/{merchant_id}/disable` | Stops the tenant at once, including its open requests. Its URLs return 404. Queued work is kept |
| `POST /platform/tenants/{merchant_id}/enable` | Allows it again and runs whatever queued |
| `GET /platform/tenants`, `GET /platform/tenants/{merchant_id}` | Read, never including secrets |

Body fields, all optional: `plan`, `limits` (see `plan-limits.md`), `integrations`,
`config`, `secrets`.
A `null` value in `config` or `secrets` removes that key. Only these keys are
accepted:

- `config`: `HANDLER_RT_MODEL`, `HANDLER_RT_EFFORT`, `HANDLER_DEFAULT_HARNESS`,
  `HANDLER_MAX_CONCURRENT`, `HANDLER_PUBLIC_URL`, `HANDLER_RETURN_TO_ORIGINS`,
  `HANDLER_RT_USERS`, `CRONTAL_URL`.
- `secrets`: `HANDLER_ADMIN_KEY`, `HANDLER_WEBHOOK_SECRET`, `HANDLER_EMAIL_SECRET`,
  `HANDLER_SECRET_KEY`, `CRONTAL_AGENT_TOKEN`, `GOOGLE_OAUTH_CLIENT_ID`,
  `GOOGLE_OAUTH_CLIENT_SECRET`.

If `HANDLER_ADMIN_KEY` is omitted at creation one is generated and returned once,
as `admin_key`. Every check `instance_configs` makes runs on each write, and a
conflict returns 409. A `HANDLER_DEFAULT_HARNESS` that is unknown or outside
`HANDLER_ALLOWED_HARNESSES` returns 422. A change retires the tenant's cached context
(`NOTIFY handler_tenant_changed`, heard by every process): the next request, run, firing
or delivery builds a fresh one with the new secrets, config, integrations and plan.
Work in progress finishes on the old context, which follows the new plan's limits at
once (one `LiveLimits` per tenant).

## Lifecycle: a tenant is context (shared runtime step 3)

A tenant has no runtime to start or stop. Its **context** (store on the shared hub,
settings and grants, live limits, gateway, executor, sinks, timer handlers, HTTP
sub-app) is built on demand into the host's LRU cache (`tenancy/contexts.py`) and
closed when it is unused for the TTL, pushed out by the size, or retired by a change.
Building starts no long-lived task: runs, timer firings and deliveries belong to the
host's loops (`runner.py`, `sinks.py` `HostOutbox`, `timer.py`), MCP is one host app.
A context in use (a run, a firing, a delivery pass, an open request or SSE stream) is
pinned; a retired one closes when its last user leaves. Closing flushes workspaces
and detaches the store; nothing of the tenant remains.

`tenant_state` (`handler_control`, created by the hub) records when each tenant next
needs the claim loop (`next_wake_at`, `wake_version`) and the outbox loop
(`next_delivery_at`, `delivery_version`). Every writer of pending work marks it in its
own transaction. At boot every enabled tenant is marked due once, so each is looked
at once (its build backfills due work, and work a restart left runs); `active_since`
is no longer written. There is no activation, eviction lock, active-tenant cap or
pinning: a connected mailbox is a timer row, not a reason to stay built.

## What may build a tenant's context

Tenant names like `m123` are guessable, so a request alone does not start a tenant:

- `/i/<name>/healthz` (and `/healthz` for `main`) is answered by the host without
  waking it.
- The sign-in page and app shell (`/`, `/app`, `/app/static/*`, `/login`) are served by
  the host from the package; their data calls carry credentials. On a plan without
  the console they are refused (`plan-limits.md`).
- Anything else wakes the tenant only with a credential that checks out:
  - the instance admin key;
  - a user token (`Authorization` or SSE `?access_token=`) or a sign-in ticket, found
    by hash in the tenant's own table;
  - a valid email or Crontal webhook signature, or a valid OAuth `state`.

  Otherwise it gets a 401 and nothing starts. Failed credential lookups are remembered
  for a minute and run at most four at a time.
- A request without a credential never builds a context (it may use one already
  built).
- Tenant names are held in memory: an unknown name costs no query. The list is
  reloaded once a minute, so a tenant created by another process is known within a
  minute.

## How queued work is always found

| Source | Guarantee |
|---|---|
| HTTP (chat, email intake, Crontal webhooks) | The request builds the context first. If that fails it gets 503 with `Retry-After` and nothing was accepted. Email is instead spooled durably and acknowledged |
| Every writer (this process, the CLI, another process) | Marks `tenant_state.next_wake_at` **in the same transaction as the work** (`MARK_WAKE_SQL`), so the wake can't be lost; `NOTIFY handler_work_shared` only makes it faster |
| Thread retries, crashed runs | After each look the claim loop writes back the earliest of a retry's `available_at` and a live lease's `lease_until`; a lease a crash left expires and the thread is claimed again |
| Schedules, expiries, mail polls, held-input release | Host timer rows (`due_work`, step 2) |
| Deliveries | `next_delivery_at`, marked by enqueue, retry and redeliver |
| Listener outage | The durable marks above, read by the backstop polls; reconnecting looks at every tenant |
| A failed context build | The tenant stays due and is retried at the next pass |
| Anything else, such as an older image writing during a release overlap | Every tenant is looked at once at boot; v65's NOTIFY still reaches the claim loop |

Otherwise the host's loops read `handler_control` alone, so idle tenants issue no
tenant-scoped queries (`store_pg/test_idle_tenants.py`, 50 tenants).

## Rollback

Registry mode changes no tenant tables. To leave it, unset
`HANDLER_TENANT_REGISTRY` or restore an earlier image; `handler_control` is ignored
and should be left in place. Keep the `HANDLER_INSTANCES` and `HANDLER_I_*` config
in place while the registry is on, because rolling back reads them again: changes
made only through the platform API (rotated secrets, for example) are not seen, and
tenants created there are unreachable, with their data intact, until the registry
is turned back on.
