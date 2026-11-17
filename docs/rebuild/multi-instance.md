# Multiple instances on one dyno

To define tenants in the database and run each only while it has work, see
[tenant-registry.md](tenant-registry.md). Everything below describes env-defined
hosting, which stays the default.

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

Parsing runs in the **reader pool** (`documents/pool.py`): child processes shared by
the whole host, for document reads (`read_file`, bounded tasks, PDF uploads,
download previews), email parsing (`/ingest/email`, mailbox ingest and attachment
fetches) and attachment image downscaling. Codex's `handler-read` runs in its own
process and is not counted.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HANDLER_DOC_READ_MAX_CONCURRENT` | 1 | Reader processes, for the **whole host**, not per instance. 1 fits the 512 MB Basic dyno; see the budget below |
| `HANDLER_READER_MAX_RSS_MB` | 220 | A reader whose RSS goes over this (sampled every 200 ms) is killed; the call fails with "… is too large to read" and the reader is replaced. 0 turns it off |
| `HANDLER_READER_TIMEOUT_SECONDS` | 120 | A call running longer has its process killed; the tool call gets an error |
| `HANDLER_READER_MEMORY_MB` | 0 (off) | `RLIMIT_AS` per reader, Linux only (macOS doesn't enforce it). Off by default: a reader with PyMuPDF and MarkItDown loaded reserves well over 1 GB of address space, so a limit near 2 GB already crashed an ordinary large-PDF read. Only a loose backstop |
| `HANDLER_READER_MAX_TASKS` | 50 | Calls before a reader is replaced |
| `HANDLER_READER_MAX_RESULT_MB` | 64 | Largest result a reader returns; larger is an error |
| `HANDLER_READER_IDLE_SECONDS` | 60 | An unused reader stops after this |
| `HANDLER_TOOL_RESULT_MAX_CHARS` | 200,000 | A gateway tool result over this is cut, with a marker |
| `HANDLER_MAIN_FILE_MAX_MB` | 50 | A workspace file a tool loads whole in the main process (Crontal upload, email attachment) is refused over this |

- **Failures stay in their call.** A timeout, a caller giving up (e.g. a gateway tool
  timeout), a reader that crashes or runs out of memory: that process is killed or
  reaped and replaced, the call raises `ReaderFailed` (a `ReadError`, so tools report
  `error: …`; previews report `text_error` / `extract_error`), and every other read
  carries on.
- **Fair by tenant, at admission only.** A running read is never stopped for another
  tenant. A free slot goes to the waiting tenant holding the fewest slots, oldest call
  first; while another tenant is waiting, a tenant holding its share
  (ceil(slots / active tenants)) can't start another read. A tenant alone may use
  every slot. The tenant is the instance name, carried in a context variable that each
  instance sets for its tasks and requests (`context.current_tenant`).
- **Nothing runs while idle.** The fork server preloads only the reader modules; a
  reader imports PyMuPDF, MarkItDown, openpyxl or Pillow when a call needs them. The
  fork server and resource tracker start with the first read and stop with the last
  idle reader, `HANDLER_READER_IDLE_SECONDS` after the last read.

### Memory budget (measured November 17, amd64 Linux, Python 3.12, release image)

Whole process tree: main process (the host, idle, SQLite) + fork server + resource
tracker + readers. RSS in MB.

| | Before the follow-up (preload, 2 readers, no RSS cap) | Now (1 reader, cap 220) |
| --- | --- | --- |
| Idle, before any read | 154 (main only) | 154 (main only) |
| 60 s after the last read | 395 (main 157, fork server 212, tracker 26) | 157 (main only) |
| Every reader busy on a 400-page PDF and a 60,000-row xlsx | 779 RSS / 503 PSS, and the PDF reader crashed under `RLIMIT_AS` | 440 RSS / 400 PSS (main 157, server 45, tracker 26, reader 212); the PDF read failed "too large to read", the xlsx read succeeded |
| First read, cold (PDF / xlsx) | 2.7 s / about 2.3 s (fork server start with preload) | 1.1–1.2 s / 1.0–1.2 s |
| Warm read (PDF / xlsx) | 48 / 12 ms | 45 / 11 ms |

With no cap, the 400-page PDF's text conversion took one reader to **6 GB** over two
minutes. Reader peaks per file with no cap: xlsx (60,000 rows) 73 MB; HTML (5,000
paragraphs) 152 MB; PDF text through MarkItDown (pdfminer) 244 MB for 2 pages, 311 MB
for 10, 597 MB for 30, 1.57 GB for 100. PyMuPDF's own text extraction of the same
100- and 400-page PDFs added nothing measurable to its 155 MB import (0.3 s and 1.1 s).
A reader that has imported PyMuPDF alone is about 145 MB; with MarkItDown, about 200 MB.

- **512 MB Basic dyno:** 1 reader with a 220 MB cap keeps main (about 155 idle, more in
  runs) + fork server 45 + tracker 26 + reader 220 near 450 MB. The cost: PDF text
  conversion through MarkItDown needs more than 220 MB even for a 2-page PDF, so PDF
  text previews (downloads, email attachments, bounded tasks) fail with
  "too large to read" until PDF text extraction moves to PyMuPDF. `read_file` on a PDF
  uploads the file to the model and parses nothing, so chat reading of PDFs is
  unaffected; xlsx, CSV, Word, HTML and images fit.
- **1 GB dyno:** 2 readers at 300 MB (measured busy tree 584 RSS / 516 PSS), or 3 at
  220 MB, stay under about 900 MB with room for the main process.
- A read still waiting for a slot, or still running, after 5 seconds is logged every
  5 seconds (`document read waiting for a slot` or `document read still running`,
  with `tenant`, `active_reads`, `tenant_reads` and `limit`).

Each read logs `document read` (source, tenant, file type and size, wait, duration,
outcome, the reader's memory before and after and its peak during the call, the
reader's pid and exit code, and `main_rss_mb`). Each run logs `run memory` at its
start and end; the end line adds `run_peak_rss_mb` (the main process's highest RSS
while the run was going, sampled each second; None without `/proc`) and
`reader_peak_rss_mb` (the highest reader peak among its reads). Other fields:
`rss_mb`, `peak_rss_mb` (high-water mark since the process started) and `swap_mb`;
`active_runs` and `active_reads` show what else was running. Memory is per process,
so several runs in flight share one main-process figure.

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
