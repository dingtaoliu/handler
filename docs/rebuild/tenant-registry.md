# Tenant registry, wake on demand and run limits

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
| `HANDLER_TENANT_IDLE_SECONDS` | 600 | Idle time before a tenant stops |
| `HANDLER_TENANT_SCAN_SECONDS` | 15 | Host scan interval, also the latest a durable wake is noticed |
| `HANDLER_TENANT_BOOT_GRACE_SECONDS` | 120 | When the second boot sweep runs |
| `HANDLER_GLOBAL_MAX_RUNS` | 6 | Runs at once, whole process |
| `HANDLER_PLAN_RUN_CAPS` | `free:1,paid:3` | Runs at once per tenant, by plan. An unknown plan gets the lowest |
| `HANDLER_RUN_MAX_WAIT_SECONDS` | 60 | After this a waiting free tenant ranks with paid ones |

A tenant's `limits.max_runs` overrides its plan cap. Each tenant's own
`HANDLER_MAX_CONCURRENT` and lane capacities still apply beneath these.

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

Body fields, all optional: `plan`, `limits`, `integrations`, `config`, `secrets`.
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
conflict returns 409. Changes to a running tenant apply when it next restarts,
which happens as soon as it is quiescent.

## Lifecycle

A tenant runs only while it has a reason to. It **activates** on an HTTP request,
a notification for its schema, `next_wake_at` being reached, or at boot if
`active_since` is set. A tenant with `mailbox`, `gmail` or `google` in
`integrations` is pinned active.

It **deactivates** after the idle period when it has no in-flight run, open request
or SSE stream, due event or due outbox row, and its workspaces flushed cleanly.
Then, in order: the runtime stops, `next_wake_at` is written, `active_since` is
cleared. `next_wake_at` is the earliest of schedules, outbox retries, thread retries
and approval or question expiry.

Everything that starts or stops one tenant holds that tenant's lock, so it can never
have two runtimes. A request or notification arriving during a stop waits, then
starts a fresh runtime. Locks are per tenant, so a slow stop delays no one else.
Activations run at most four at a time. A clean shutdown leaves `active_since` set,
so a restart resumes those tenants.

## How queued work on a sleeping tenant is always found

| Source | Guarantee |
|---|---|
| HTTP (chat, email intake, Crontal webhooks) | The request activates the tenant first. If that fails it gets 503 with `Retry-After`, nothing was accepted, and a durable retry is recorded |
| Another process (CLI, a store with its own connections) | Writes `next_wake_at` **in the same transaction as the work**, so the wake can't be lost. The notification only makes it faster |
| Schedules, retries, expiries | `next_wake_at`, written at stop |
| Listener outage | The durable wake above; reconnecting prompts an immediate scan |
| Crash | `active_since` still set, restarted at boot |
| Anything else, such as an older image writing during a release overlap | Each inactive tenant's tables are swept at boot, and once more after the boot grace |

Only those two sweeps look inside a sleeping tenant. Otherwise the host's single
scan reads `handler_control` alone, so idle tenants issue no queries.

## Rollback

Registry mode changes no tenant tables. To leave it, unset
`HANDLER_TENANT_REGISTRY` or restore an earlier image; `handler_control` is ignored
and should be left in place. Keep the `HANDLER_INSTANCES` and `HANDLER_I_*` config
in place while the registry is on, because rolling back reads them again: changes
made only through the platform API (rotated secrets, for example) are not seen, and
tenants created there are unreachable, with their data intact, until the registry
is turned back on.
