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

`HANDLER_DB_POOL_MAX` (default 5) sets the pool maximum per instance and supports
scoped overrides. Each Postgres store also holds a LISTEN connection. Two default
instances can therefore consume up to 12 database connections; reserve capacity
for Crontal's web processes and maintenance within the database plan's limit.
Run concurrency is **per instance**, so add both limits when sizing the dyno.

## Notifications to Crontal

Each instance pushes its notifications to its own Crontal deployment through a
durable outbox when its `CRONTAL_URL`, `CRONTAL_AGENT_TOKEN` and
`HANDLER_WEBHOOK_SECRET` are set (for BSS, the `HANDLER_I_BSS__…` variants).
Deliveries carry `X-Handler-Instance: <name>` (`main` for the root instance)
and are signed with that instance's webhook secret, which must match the
secret registered for it in Crontal. See `notifications.md`.

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
