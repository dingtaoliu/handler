# Multi-tenant Handler and free-tier migration: design

Status: **in implementation on branch `feat/multi-tenant`; nothing deployed**. The
owner reviewed this direction in conversation on November 8, 2026, asked for this
document, then asked for it to be implemented on a branch and settled the three
[open decisions](#decisions). A production release still needs the owner's approval.
Production follows `main`; the `rebuild` branch has been merged and deleted.

Spans two repositories: this one (Handler) and `../hephaestus` (Crontal).

## Owner requirements

1. Fit more Handler instances on the current Heroku Basic dyno. Usage per
   merchant is light, and the native loop has a low memory requirement.
2. Multi-tenant Handler with a shared connection pool and clear isolation between
   tenants, including incoming channel and thread messages.
3. Remove the split between free users and Handler users. Agent and tool
   improvements are currently built twice.
4. Move free users to Handler with a limit on the number of inputs, counted across
   thread types (email, messages and so on).

## Before you start

- Read `AGENTS.md`, `docs/rebuild/HANDOFF.md`, `docs/rebuild/multi-instance.md`,
  and Crontal's `docs/handler-instances.md`.
- **Confirm the branch before deploying.** `main` contained all of `rebuild` plus 6
  more commits on November 8, while Crontal's `/deploy` skill still names `rebuild`
  for Handler. Ask the owner which one production should follow.
- This checkout may have uncommitted work from other sessions (`HANDOFF.md`,
  `bss-improvement-handoff.md`, `docs/incidents/`). Work in a worktree.
- In Crontal worktrees the shell inherits `VIRTUAL_ENV` for the main checkout. Set
  `PDM_IGNORE_ACTIVE_VENV=1` on every pdm command and confirm that
  `api_server.__file__` resolves inside the worktree.
- Local Crontal is not connected to Handler: `handler.enabled` is false, no
  `HANDLER_*` settings exist in `.env.local`, and there are no `handler_instance`
  rows. It shows the old agent panel until that is set up. Port 8000 on the
  owner's machine is the legacy bot and must keep running untouched.
- Take `heroku pg:backups:capture` before any production release that changes
  schemas. Handler and Crontal share one Postgres database.
- **Never drop inbound email.** Owner rule: cost controls may delay mail, never
  discard it, and everything is stored at ingest.

## Current state and evidence

Measured or read on November 8, 2026 unless noted.

### Architecture

`handler-rt serve` already hosts every instance in one process on one port.
`server.py:create_host` mounts a sub-application at `/i/<name>`. Production runs
`HANDLER_INSTANCES=main,bss,yeup` on one Basic dyno (`crontal-agent`). Ports are
not a constraint.

Each instance owns, through `server.py:instance_runtime`: a `PostgresStore`
(`AsyncConnectionPool` plus one LISTEN connection), run event bus, dispatcher,
scheduler, housekeeping, outbox, workspace sync, MCP gateway, settings, memory,
uploads, integration background tasks (mailbox poller, Crontal connector).
Instances are declared with environment variables; adding one means new Heroku
config and a restart.

Isolation today: a Postgres schema (`handler_<name>`), S3 prefix
(`handler/<name>/`), data directory and separate secrets, enforced at startup by
`instance_configs`. Crontal maps one merchant to one instance through
`handler_instance` (base URL, admin key, webhook secret, agent token hash, AI user).

### What limits capacity

| Resource | Evidence | Conclusion |
| --- | --- | --- |
| Postgres connections | `HANDLER_DB_POOL_MAX=3` plus 1 LISTEN per instance, so 3 × 4 = 12. Crontal's budget is 8 (`db/db.py` comment). Plan cap is 20 (`essential-1`). `pg:info` showed 16/20 | **The current 3-instance limit** |
| Memory at idle | 145 MB at 1 instance, 150 at 3, 179 at 10, 250 at 30: about 3.6 MB per instance | Not a constraint |
| CPU at idle | Negligible at 30 instances over 30 seconds | Not a constraint |
| Memory under work | 17 R14 errors, all between 23:13 and 23:19 UTC, in a log sample spanning 23:13 to 00:26 UTC. The dyno is Basic (512 MB) | **Second limit, active in production** |
| Run concurrency | `HANDLER_MAX_CONCURRENT=20`, per instance. Peak BSS concurrency over 7 days was only 2 | The setting is far above what the dyno can hold |
| Postgres tables | 160/4000 used. 33 tables per tenant schema, so about 116 more tenants | Cap on schema-per-tenant |

Limits of this evidence:

- Idle numbers came from a throwaway local process on macOS using SQLite with no
  integrations. They exclude Postgres pools and Linux differences. Measure on the
  dyno before relying on them for sizing.
- **The cause of the R14s is unknown.** Only 5 BSS runs fell in the surrounding
  window; `main` and `yeup` were not checked. The leading hypothesis is document
  reading, which runs inside the main process through `asyncio.to_thread`
  (`harness/openai_agents.py`, `gateway/crontal_api.py`). Treat it as unproven.

### Usage and accounts

- Crontal: 17 merchants, 14 human users, 3 AI users, no user without a merchant.
- Handler runs: `crontal` 82 total (0 in the last 7 days), `bss` 637 (475),
  `yeup` 6 (5).
- Old built-in agent: 26 AI chats ever, 5 in the last 30 days.
- Crontal has no plan or tier concept. Free users are limited by
  `AGENT_DAILY_MESSAGE_LIMIT` (50 messages per user per day, chat only;
  `services/auth.py`).
- Panel selection: `frontend/src/components/ai-agent/AgentPanel.tsx` shows
  `HandlerChatPanel` if `useHandlerStatus` is true, else the old `AIAgentPanel`.
  The server side is `handler_service.resolve_target`.

## Design

One process, one shared connection pool, one Postgres schema per merchant, and
tenant runtimes that exist only while they have work.

### 1. Shared connections

Replace the per-store pool and LISTEN with one process-wide pair owned by the host.

- `PostgresStore` currently sets `search_path` through connection `options`. It
  must instead run `SET LOCAL search_path TO <schema>` as the first statement of
  each transaction on a shared pool connection.
- Fail closed: the pool's default `search_path` must be a schema with no tenant
  tables (for example `handler_control`), so a code path that forgets to scope
  errors out rather than reading another tenant's data.
- `_connection()` in `store/postgres.py` is the intended chokepoint. **Audit every
  use of the pool**, including `postgres_domains.py`, `usage.py`, `deliveries.py`,
  `tasks.py`, `thread_metadata.py` and `integrations/mailbox/store.py`. `SET LOCAL`
  has no effect in autocommit, so check any autocommit path too.
- Notifications: today each schema has a channel named `<CHANNEL>_<schema>`. Move
  to one channel with the schema name as the payload. The host's listener sets the
  matching active store's `_wake`, or marks an inactive tenant for activation.
- Schema setup: `_setup` applies every `schema_pg*.sql` under a global advisory
  lock on first use. Add a per-tenant schema version in the control schema and skip
  DDL when current, so activation stays cheap.
- Connection budget: 11 pool plus 1 LISTEN, keeping Handler within its existing
  budget of 12 regardless of tenant count. Keep single-instance and SQLite
  deployments working unchanged, including the personal instance.

### 2. Tenant registry

A new `handler_control` schema replaces `HANDLER_INSTANCES` as the source of truth.

`tenants`: name (`m<merchant_id>` for new tenants, which satisfies the existing
name pattern), schema, S3 prefix, enabled, plan, limits (JSON), integrations,
per-tenant config such as model and effort, encrypted secrets (reuse
`gateway/secret_box.py`), timestamps.

`tenant_state`: `active_since`, `next_wake_at`, schema version.

- Existing env-defined instances are imported once. `main`, `bss` and `yeup` keep
  their schemas, prefixes and URLs. `main` keeps its root alias.
- A platform API, authenticated with a new key separate from any tenant admin key,
  provides idempotent create, update, disable and enable by merchant id. Creating a
  tenant needs no restart.
- Preserve every uniqueness and overlap check in `instance_configs`.

### 3. Wake on demand

A tenant's runtime exists only while it has a reason to.

- Replace static `host.mount` calls with a routing ASGI app: look up the tenant
  from `/i/<name>`, activate it if needed, then forward. Unknown names remain 404.
- **Activate on:** an HTTP request, a notification for its schema, `next_wake_at`
  reached, or at boot if `active_since` was left set by a crash.
- **Deactivate after** an idle period (start with 10 minutes) when there are no
  in-flight runs, pending events, open SSE streams or undelivered outbox rows.
  Flush workspaces first, then write `next_wake_at` from the earliest schedule or
  outbox retry and clear `active_since`.
- Tenants with always-polling integrations (mailbox, Gmail) stay pinned as active.
- Keep the owner-task structure in `create_host`: MCP uses AnyIO cancel scopes, so
  each tenant must enter and exit in the same task. Deactivation signals that
  tenant's own stop event instead of the host-wide one.
- A single host task scans `tenant_state` for due wake-ups, so idle tenants issue
  no queries.

### 4. Fair scheduling across tenants

`max_concurrent` and lane capacity are per instance today. Add process-wide limits
above them: a global run cap sized to the dyno, per-plan tenant caps (start with 1
or 2 for free), paid work first, and a strict global cap on simultaneous document
reads. A busy tenant must not be able to starve another.

### 5. Plans and input limits

Crontal is the source of truth for a merchant's plan and pushes plan and limits
through the platform API.

**Metering point.** All inputs pass through `EventQueue.put`. Add a quota gate
around it. Check callers including `web/app.py` (chat), `web/ingest.py` (Crontal
notifications), `web/admin_api.py`, `scheduler.py`, and the mail integrations.
Audit for others.

- Count only metered event types: chat messages, email, mailbox mail, Crontal
  notifications and automation runs. Internal events such as `approval.resolved`
  (`thread_router`) are not inputs.
- Count inside the same transaction as the insert, after the idempotency check, so
  duplicates and retries never count.
- `put` returns a boolean today. The gate needs a third outcome for "over limit".

**Behaviour at the limit:**

| Input | Behaviour |
| --- | --- |
| Chat | Refused with a clear error, usage and reset time. Replaces the composer's current message meter |
| Email, mailbox mail, Crontal notifications | Stored, no run. Processed at reset or on upgrade, as a digest where sensible. `put` with `thread=None` may fit; verify |
| Automations | Skipped, with a notice to the user |

**Cost limits.** An input count alone does not bound spend, since one email can
trigger a long run. Free tenants also get a fixed model and per-run token and
iteration limits. The usage ledger (`usage_records`, `run_attributions`) gives cost
per run for tuning limits.

### 6. Hardening for untrusted tenants

Current tenants are known customers. Free signups are not, and email is prompt
injection input.

- Codex harness disabled for free tenants. It runs with `danger-full-access` on the
  dyno, and code must not run there (see `HANDOFF.md`).
- `crontal_api.*` is granted `allow` on the Heroku instance today. Free tenants
  need a stricter default grant policy.
- No Handler admin console for free tenants.
- Tenant isolation tests must cover SSE, the run event bus, MCP run tokens, uploads
  (note `UploadStore.get` globs by file id under a tenant's root), workspaces and
  the outbox.
- Per-tenant limits on upload and workspace storage.

### 7. Moving free merchants and retiring the old agent

- Provision a tenant on a merchant's first agent use, not at signup, so unused
  accounts consume no tables. Reuse the `handler_instance` row and AI identity
  creation behind `flask handler-instance add`
  (`handler_instance_cli.py`, `services/ai_identity_service.py`).
- A per-merchant switch controls the cutover.
- Compare the old agent's tools (`backend/src/api_server/agent/tools/`: demand
  listings, shape definitions, OCR, file read/write) with what Handler can do
  through `crontal_api.*`. Close gaps before removal.
- Old chats stay readable. Remove `AIAgentPanel` and the agent loop only after all
  merchants have moved.

## Why not a `tenant_id` column on shared tables

| | Schema per tenant (chosen) | Shared tables with `tenant_id` |
| --- | --- | --- |
| Change size | Connection scoping, registry, activation | 33 tables plus about 120 SQL statements in the Postgres stores, and the SQLite store's interface |
| Failure mode | Wrong scope errors out | A missed filter leaks data, unless row-level security is added |
| New tenant cost | About 33 tables, and DDL | One row |
| Tenant ceiling | About 116 more on `essential-1` | Effectively none |
| Cross-tenant work | Per schema | One query |

Revisit if the tenant count approaches about 80, or if the owner's growth target
exceeds what schemas allow (open decision 3). If shared tables become necessary,
use Postgres row-level security with `FORCE ROW LEVEL SECURITY`.

## Beyond one dyno

The run event bus and SSE are in process, so a second web dyno needs either
tenant-to-dyno pinning or a shared bus. A 1 GB dyno is the simpler next step. Claims
already use `FOR UPDATE SKIP LOCKED`, so the dispatcher itself is not the blocker.

## Build order

Phases 0 to 2 do not depend on the open decisions.

| Phase | Work | Done when |
| --- | --- | --- |
| 0. Stop the R14s | Log process memory per run and per document read. Find the cause. Lower run concurrency. Cap document reads, moving them to a subprocess if they prove to be the cause (`handler-read` exists for Codex) | No R14 errors across a normal week, with memory per run on record |
| 1. Shared connections | Section 1 | Total Handler connections stay at or below 12 with 10 test tenants under load. Existing three tenants unchanged. A scoping test proves an unscoped query fails |
| 2. Registry and wake on demand | Sections 2 to 4 | A tenant is created with no restart. Idle tenants issue no queries. Crash recovery and scheduled wake-ups are tested. Dyno memory measured at 30 registered tenants |
| 3. Plans and limits | Section 5 | Each input type behaves per the table above. Duplicates don't count. Over-limit email is stored and later processed, proven by test |
| 4. Free cutover | Sections 6 and 7 | Isolation tests pass. A pilot of free merchants runs for a week within limits. The old agent is then removed |

Deploy Handler before Crontal when Crontal depends on new routes. Each phase must
ship and roll back on its own. After each release, follow the checks in
`multi-instance.md`, including that one tenant's admin token returns 401 on another.

## Decisions

Settled by the owner on November 8, 2026.

| # | Decision | Outcome | Needed by |
| --- | --- | --- | --- |
| 1 | Free limit amount, period and scope | 50 inputs per month "for now", so it must be configurable. Per merchant and per calendar month is this document's proposed scope; the owner specified only the amount and period | Phase 3 |
| 2 | Channels on the free plan | Chat, Crontal notifications and the forwarding intake address. Connected mailboxes and Gmail stay paid | Phase 3 |
| 3 | Tenant target for the next year | Under about 100: schema per tenant, as designed here | Phase 2 |
