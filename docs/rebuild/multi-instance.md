# Multiple instances on one dyno

`handler-rt serve` can host independent runtimes behind one port in one process.
Set `HANDLER_INSTANCES=main,bss`. Each name must match `[a-z][a-z0-9_]{0,31}`,
be unique, and include `main`. With this unset the existing single-instance
configuration and root URLs remain supported.

| Instance | HTTP prefix | Postgres schema | S3 prefix | Scratch data |
|---|---|---|---|---|
| main | `/i/main`, also `/` | existing `HANDLER_DB_SCHEMA` | existing `HANDLER_FILE_PREFIX` | existing `HANDLER_RT_DATA_DIR` |
| bss | `/i/bss` | `handler_bss` by default | `handler/bss/` by default | `<main-data-dir>/instances/bss` |

The root alias uses the **same** main runtime and MCP lifetime. Unknown `/i/*`
paths return 404. Instances own their store, bus, dispatcher, scheduler, gateway,
run tokens, user tokens, settings, memory, uploads and workspaces. Identical user,
thread, run and event ids in different instances do not share records. This is
application namespace isolation inside a shared process, database and OS account.

## Configuration

Main retains the existing unprefixed variables. Override an instance variable
with `HANDLER_I_<UPPERCASE_NAME>__<ORIGINAL_ENV_NAME>`, for example:

```sh
HANDLER_INSTANCES=main,bss
HANDLER_DB_SCHEMA=handler_crontal
HANDLER_FILE_PREFIX=handler/crontal/
HANDLER_I_BSS__HANDLER_ADMIN_KEY=<unique-bss-admin-key>
HANDLER_I_BSS__HANDLER_WEBHOOK_SECRET=<unique-bss-webhook-secret>
HANDLER_I_BSS__CRONTAL_URL=https://bss.example/api/v1/agent-mcp
HANDLER_I_BSS__CRONTAL_AGENT_TOKEN=<bss-agent-token>
HANDLER_I_BSS__HANDLER_PUBLIC_URL=https://handler.example/i/bss
HANDLER_I_BSS__HANDLER_MAX_CONCURRENT=5
```

Additional instances inherit infrastructure (`DATABASE_URL`, Bucketeer credentials,
file-store selection), model/harness defaults and API keys. They do **not** inherit
main's admin key, webhook/email secrets, encryption key, Google settings, Crontal
URL/token, public URL, return-to origins, user list, Codex auth file, harness env
file or log path. Configure those explicitly for each instance that needs them.
Credentials are resolved into per-instance environment dictionaries; runtime setup
never swaps `os.environ`. A harness env file is read into that instance's snapshot,
with explicit environment values taking precedence. Codex receives its allowlisted
snapshot and the native harness gets its explicit API key.

Multi-instance startup rejects missing/reused admin keys, repeated data directories,
shared schemas on the same database, overlapping S3 prefixes, and reused configured
webhook/email/encryption secrets. Keep the main schema `handler_crontal` and prefix
`handler/crontal/` to preserve existing records; do not migrate or copy main data.

## Database connections

With `HANDLER_INSTANCES` set, all instances on one database share one pool and one
LISTEN connection owned by the host: **11 + 1 = 12 connections for the whole
process, however many instances it hosts**. `HANDLER_DB_SHARED_POOL_MAX` (default
11) sets the pool size. It is a host setting and cannot be scoped to an instance.
Instances on a different `DATABASE_URL` get their own 11 + 1 for that database.

`HANDLER_DB_POOL_MAX` (default 5) now applies only to a single instance, with
`HANDLER_INSTANCES` unset, and to one-off CLI commands. Hosts ignore it and its
scoped overrides. Leave any existing value in place: a rollback to an image from
before shared connections reads it as a per-instance size, so raising it to 11
would make that image open 44 connections. A CLI command holds up to 2 connections
while it runs, on top of the 12. Single-instance and SQLite deployments use no
shared pool and create no control schema.

Isolation on a shared connection:

- Every store transaction starts with `SET LOCAL search_path TO <schema>`. All pool
  connections are transactional, because `SET LOCAL` does nothing in autocommit.
- The pool's default `search_path` is `handler_control`, which has no tenant tables,
  so unscoped SQL fails with `undefined_table` instead of reaching a tenant.
- Auto-prepared statements are off on the shared pool: `SELECT *` would fail wherever
  two tenants' columns differ in order. Prepared statements cannot be re-enabled.
- Nothing may keep session state on a pool connection: no plain `SET`, session
  advisory locks or temp tables. New code must also not hold a store transaction
  across slow work, since 11 connections serve every tenant.

`handler_control.tenant_schema_versions` records a hash of each tenant's applied DDL
per component (`core`, `mailbox`), so a start with current schemas runs one `SELECT`
per tenant and no DDL. To force a re-apply, delete that tenant's row. The control
schema is created automatically and needs no migration.

Notifications use one channel, `handler_work_shared`, with the schema name as the
payload. Run concurrency is **per instance**, so add it across instances when sizing
the dyno.

## Memory and document reads

Documents are decoded inside the server process, whose memory every instance
shares. `HANDLER_DOC_READ_MAX_CONCURRENT` (default 2) caps simultaneous reads
across the **whole process**, not per instance; it cannot be scoped to an instance.
A read waits for a slot, and a read whose caller timed out keeps its slot until
the decoding actually finishes. Codex's `handler-read` runs in its own process and
is not counted.

Each read logs `document read` (source, file type and size, wait, duration, memory
before and after) and each run logs `run memory` at its start and end. Fields:
`rss_mb` (or `rss_mb_before`/`rss_mb_after`), `peak_rss_mb` (high-water mark since
the process started) and `swap_mb`; `active_runs` and `active_reads` show what else
was running. Use these to find what drives a dyno over its quota before changing
anything else. Memory is per process, so several runs in flight share one figure.

## Notifications to Crontal

Each instance pushes its notifications to its own Crontal deployment through a
durable outbox when its `CRONTAL_URL`, `CRONTAL_AGENT_TOKEN` and
`HANDLER_WEBHOOK_SECRET` are set (for BSS, the `HANDLER_I_BSS__…` variants).
Deliveries carry `X-Handler-Instance: <name>` (`main` for the root instance)
and are signed with that instance's webhook secret, which must match the
secret registered for it in Crontal. See `notifications.md`.

## Crontal AI identity

Each instance's `CRONTAL_AGENT_TOKEN` acts in Crontal only as that instance's AI
user, which Crontal creates per registered instance (Crontal's
`docs/handler-instances.md` → *AI identity*). Handler discovers it with
`GET /api/v1/agent-access/identity` and caches it per (Crontal URL, token), so
instances on one host never share an identity. Check after deploy: each
instance's prompt preview (`GET /api/settings/prompt?thread_type=chat`) names its
own merchant's AI user.

## URLs and OAuth

Use `https://handler.example/i/bss` as BSS's Handler base URL in the associated
Crontal deployment. Its proxy, webhook destination and API requests append their
paths to that base. Existing main callers can continue using the root base URL.
Internal harness MCP URLs use the respective `/i/<name>/mcp/` route.

Browser login, assets, API calls, downloads, SSE and timeline links preserve their
instance prefix. Tokens and UI preferences use separate browser storage keys for
each instance; main's root alias and `/i/main` share the same main storage keys.
Enabling named mode introduces named storage keys, so existing browser sessions
sign in again once.

`HANDLER_PUBLIC_URL` is used verbatim: keep main's existing root public URL and
registered Google callback unchanged. For BSS Google OAuth configure its own
Google client and encryption key, and set:

```sh
HANDLER_I_BSS__HANDLER_PUBLIC_URL=https://handler.example/i/bss
HANDLER_I_BSS__GOOGLE_OAUTH_CLIENT_ID=<client-id>
HANDLER_I_BSS__GOOGLE_OAUTH_CLIENT_SECRET=<client-secret>
HANDLER_I_BSS__HANDLER_SECRET_KEY=<stable-bss-encryption-key>
```

Register `https://handler.example/i/bss/oauth/google/callback` with Google. If no
public URL is configured, callbacks derive the external origin plus mounted prefix.
OAuth completion redirects back to the same instance. Return-to allowlists remain
per instance through `HANDLER_I_BSS__HANDLER_RETURN_TO_ORIGINS`.

## Startup, shutdown and verification

The host starts each instance's MCP manager and background services once before
accepting traffic. A startup failure closes stores and unwinds started instances.
On SIGTERM, Uvicorn allows HTTP requests two seconds to close (including SSE), then
signals all instance owner tasks together. They flush workspaces, drain runs and
flush again within concurrent 25-second budgets, before closing stores and MCP.

Run the runtime suite before deploying:

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/runtime -q -p no:cacheprovider
```

After release check `/healthz`, `/i/main/healthz`, `/i/bss/healthz`; verify a main
admin token returns 401 at BSS and vice versa; verify BSS's settings and chat show
only BSS records. Check each Crontal instance's Handler proxy and signed webhook
against its matching destination. Review logs for startup or shutdown exceptions.
Application JSON logging remains process-wide; the main log file is the default
process log sink.

To roll back, restore the previous image/config with `HANDLER_INSTANCES` unset.
Main continues using its original schema and S3 prefix. BSS's schema and files
remain intact for a later retry; do not remove them during a runtime rollback.

Shared connections change no tenant tables, so an image from before them runs
unchanged against the same database and ignores `handler_control`; leave that schema
in place. While old and new dynos overlap they do not hear each other's
notifications, since an old dyno sends only on its per-schema channel. Work is not
lost: each dispatcher's idle poll claims it within 30 seconds.
