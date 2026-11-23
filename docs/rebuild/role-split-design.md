# Shared host resources: inbox, runner and outbox

Status: **owner decisions settled November 15, 2026 (decision 6 revised November 21); steps 0, 1, 2 and 4 built on
`feat/shared-runtime`, step 3a (the runtime core of step 3) on `feat/shared-runtime-step3a`**
(not deployed; see Build order). First written
November 14, 2026 as a split into separate processes. Rewritten November 15, 2026 after
review with the owner, who narrowed the goal to a single host. Follows
[multi-tenant-design.md](multi-tenant-design.md).

## Goal

A host is a fixed set of resources: processes, memory and database connections. Every
tenant must draw on those resources only while it has work, and fairly by plan.

Today each active tenant holds its own set of resources, whether or not it is using them.
After this change, a tenant is data: schema, settings, secrets, files and memory. It holds
no tasks, objects or connections between runs.

Owner decisions from the November 15 review:

1. **One host.** Inbox, runner and outbox may run in one process. More main processes or
   hosts can be added later (see [Processes](#processes)), but are not needed now.
2. **"Tenant" stays the only term** for the layer above users. There is no separate
   "account".
3. **Approvals and questions stay as they are.** The request is an outbound message.
   The answer comes back as a new event in the same thread.
4. **Production uses only the native harness** (`openai_agents`, the agent loop built on
   the Agents SDK). Production tenants already use it, so enforcing it changes no
   behaviour. Codex remains available to the personal instance.
5. **The three existing instances move to this runtime:** `main`, `bss` and `yeup` run on
   the shared runtime and are imported into the tenant registry, which is switched on
   after build step 3.
6. **A failure never blocks a thread** (revised November 21; first decided November 15
   as parking). Parking was mainly for the site owner to debug. For users it is fine
   to accept a failure: a person can send the message again, and an automation runs on
   its next trigger as usual. A run that fails for good is recorded and its events
   consumed; the thread keeps working. Users and tenant admins get no failure notices;
   the site owner gets the `run failed` alert and can list and replay failed runs
   (notifications.md, "Failed runs").
7. **A side effect with an unknown outcome is never resent automatically.** The call is
   parked and a person is asked, as with an approval.
8. **Fairness:** interactive before background across all tenants; paid before free, with
   aging; reader-pool shares mirror the run caps.
9. **Retention:** failed runs' events, dead deliveries and records of dropped over-limit inputs
   are kept 90 days, then removed by the tenant's normal cleanup.

## Current state

Read on November 15, 2026 on `main` (`6bfb180`).

What each tenant holds while it is active (`server.py` `instance_runtime`):

| Held per tenant | Shared already |
| --- | --- |
| Six long-lived tasks: dispatcher, scheduler, housekeeping, outbox, workspace sync and the Crontal connector, plus the background tasks each integration adds (mailbox pollers and others) | Postgres pool (11 connections plus 1 LISTEN) |
| A FastAPI sub-application at `/i/<name>`, an MCP gateway app, a `RunEventBus`, and settings, memory, upload and notifier objects | `RunGate`: a global run cap, caps per plan, paid tenants first, and aging (`tenancy/run_gate.py`) |
| Its activation lifecycle in `TenantHost`: activation, eviction, a lock and the boot sweep | Document reads: 2 slots for the whole process |

Each housekeeping loop wakes every 15 s to query its own tenant, even when the tenant is
idle. The cost per idle tenant was measured at about 3.6 MB, before counting these
queries and the tasks of any integrations.

What already suits shared workers:

- **Queues are durable in Postgres.** `events` and `deliveries` live in each tenant's
  schema. Threads are claimed with `FOR UPDATE SKIP LOCKED` and a fenced lease (120 s,
  renewed every 30 s), so two workers can never run the same thread.
- **Approvals and questions do not hold a run.** The gateway stores a pending `Approval`
  and the run ends (`gateway/gateway.py`). The decision arrives as an `approval.resolved`
  or `question.answered` event in the same thread (`gateway/questions.py`, `run.py`), and
  any worker can run it. There is no in-memory wait.
- **Chat is already asynchronous.** `POST /api/chat` stores an event and returns at once.
  The reply streams over the thread's SSE feed, which Crontal passes through.
- **The native harness has no shell.** Its file tools resolve every path, symlinks
  included, and reject anything outside the run's workspace (`_confined` in
  `harness/openai_agents.py`). It reaches the gateway over loopback MCP
  (`MCPServerStreamableHttp`), authenticated by a run token.

## Design

### Shape

One main process with three stages. The stages are module boundaries, not processes, and
none of them has per-tenant tasks.

```
inbox ──events──▶ runner ──messages──▶ outbox ──▶ subscribers
(HTTP, ingest,     (claims threads,     (matches          (SSE, Crontal,
 the timer)         loads tenant         subscribers,      push, webhooks)
                    context, runs the    delivers)
                    thread strategy)
                        │
                        └── reader pool (child processes: document, email and image parsing)
```

| Stage | Does | Shared resources it uses |
| --- | --- | --- |
| Inbox | HTTP API, webhook and email ingest, email spool. One **timer** for every tenant's schedules, mail polls and expiries | Request handling and DB connections |
| Runner | Claims due threads across all tenants, loads the tenant's context, applies the thread type's strategy, runs the native harness as an asyncio task | Run slots, reader pool, DB connections |
| Outbox | Matches new messages to subscribers and delivers through a shared pool | Delivery workers, DB connections |

### A request, end to end

1. Crontal forwards `POST /api/chat` with credentials that name the tenant and user.
2. The host resolves the tenant from the credentials and loads its context from the cache.
3. The inbox, in one transaction in the tenant's schema: checks the quota (a free tenant
   over its limit gets a 429), inserts the event under its idempotency key, marks the
   thread pending, and sets the tenant's due time in `handler_control`. Then it wakes the
   runner and returns `{event_id, thread_key}`.
4. The client's SSE stream registers as a live subscriber.
5. The runner picks due tenants by lane, plan and wait time, asks `RunGate` for a slot,
   and claims the thread with every event pending on it.
6. The thread type's strategy chooses the history, lane and prompt sections. The runner
   restores the workspace and builds the tenant's tool view.
7. The run streams steps over the bus. Document parsing goes to the reader pool. An
   approval ends the run and goes out as a message.
8. The reply is stored; the lease, slot and workspace are released and flushed.
9. The outbox delivers the reply to live subscribers, or to push or Crontal if nobody
   was connected.

Other inputs differ only at the edges: the timer writes `automation.fired` events, email
ingest spools first, and Crontal events and approval answers go to their own threads.

### A tenant is context

When the runner claims a thread, or the inbox serves a request, it loads the tenant's
context:

- the registry row and decrypted secrets
- plan limits, settings and grant policy
- a store scoped to its schema (the existing hub and `SET LOCAL search_path`)
- its file store prefix and workspace root
- a gateway view: the core tools plus the tenant's integration tools, filtered by plan and
  grants

Contexts sit in an LRU cache. A `NOTIFY handler_tenant_changed` on any registry or
settings change drops the entry, so writes from the CLI and Crontal are seen too. An idle
tenant costs one cache entry, or nothing once evicted. This replaces `TenantHost`'s
activation lifecycle and the per-tenant sub-application.

### Shared resource pools

Each scarce resource has one pool for the whole host. Each tenant has a fair share by
plan. When other tenants are idle, a busy tenant can use their unused share.

| Resource | Pool | Fairness |
| --- | --- | --- |
| Run slots | `RunGate`, in memory, counting runs. Runs mostly wait on the network, so the cap mainly protects model spend and fairness | Interactive before background across all tenants. Paid before free, with aging. Caps per plan |
| Parsing | The **reader pool**: child processes for document reading, email parsing and image downscaling. This is where memory and CPU spike | A share per tenant inside the pool, so one tenant's hung PDF cannot hold every slot |
| DB connections | The existing shared pool | Unchanged |
| Delivery | One pool of delivery workers | Limits per sink and per tenant, so a tenant whose webhook is down cannot tie up the workers |
| Timers | One timer for every tenant's schedules, polls and expiries | Due work is a row; an idle tenant has no running task |
| Model tokens | The usage ledger and plan limits (already built) | Unchanged |

Parsing is a kind of tool work inside a run, not a separate kind of run. It gets its own
pool because it is CPU-heavy, blocking library code that can briefly use hundreds of MB,
and because a Python thread cannot be cancelled. In a child process, a hung or oversized
parse can be killed: its tool call returns an error, and the run and every other tenant
carry on. This fits the leading R14 hypothesis without proving it.

Runs themselves share the main interpreter, so the main process also needs limits on
what it holds: a size cap on tool results and on files loaded outside the reader pool,
and memory logged per run so a spike can be traced to the run that caused it.

### Thread strategies

The runner chooses how to run a thread from its type. Today this logic is scattered: the
fresh-session rule for automations is a special case in `run.py` and `instructions.py`,
lanes are set at ingest, and models are set in `threads` settings. Each thread type
instead registers one strategy:

| Strategy decides | chat | automation | email / mail | crontal |
| --- | --- | --- | --- | --- |
| History | Full session | Fresh session for each firing, checkpoint in memory; an approval continuation keeps the session (today's `fresh_automation_sessions`) | The email conversation's thread | One thread per entity |
| Lane | Interactive | Background | Background | Background |
| Prompt sections | Chat | Checkpoint guidance | Mail handling | Crontal events |
| Default subscribers | Live connection, push when nobody is connected | Notification, automation dashboard | Notification | Crontal sink |
| A new event after a failed run | Runs normally | Runs normally (fresh session) | Runs normally | Runs normally |

Integrations register strategies for the thread types they add.

### Outbox: subscribers

The outbox matches every new message in a thread against that thread's and tenant's
subscriptions, then delivers it.

| Subscriber | Registered by | Delivered by |
| --- | --- | --- |
| Live connection (SSE) | Opening a thread or task stream; removed when the connection closes | Written to the connection through the in-memory bus. Best effort; a client that reconnects replays stored steps |
| Crontal sink | The integration | The delivery pool, with retries, backoff and dead letters (today's `deliveries`) |
| Push or webhook | User or tenant settings | The delivery pool |

Fallback rule: a chat message that no live connection received within a few seconds goes
to push, or to a Crontal notification until a push client exists.

Agent tools that act on the outside world, such as sending email or calling the Crontal
API, stay inside the run, because the agent needs their result. The outbox carries
notifications and replies, not tool side effects.

Stored messages and run steps are the record. Every subscriber reads from them, so a
delivery can always be retried or replayed.

### Live feed

The live feed stays in memory (`RunEventBus`), since all stages share a process. Two
changes:

- One bus for the host, keyed by tenant and thread, instead of one bus per tenant.
- Text fragments are grouped into stored steps about every 250 ms, so a reconnect replays
  close to what the client already saw.

## Isolation

No layer alone guarantees isolation. Each layer below is enforced in Handler's own code,
so the isolation tests can cover all of them.

| Layer | What separates tenants |
| --- | --- |
| Database | A schema per tenant. Every query runs in a transaction scoped by `SET LOCAL search_path`. The pool's default `search_path` is `handler_control`, so an unscoped query errors instead of reading another tenant's data |
| Routing | The tenant comes from the request's credentials. A `/i/<name>` path is only a hint, and a request whose credentials name another tenant is rejected |
| Tools | One MCP route for the host. The run token names the tenant and thread, so a tool call reaches only its own tenant |
| Files | An S3 prefix and a workspace directory per tenant. Native file tools are confined to the run's workspace |
| Secrets | Encrypted per tenant in the registry |
| Process memory | The tenant travels in a context variable for the whole request or run. The store checks that it matches the schema it scopes to. Every shared structure (context cache, bus, `RunGate`, delivery pool, reader pool) is keyed by tenant |
| Harness | **Native only on production** (owner decision 4). A host setting allows only the native harness, and the registry rejects any other. Codex's sandbox restricts writes but not reads, so it would expose every tenant's files on a shared disk |

Shared on purpose: the OpenAI API key and organisation, logs and the host disk. The
prompt cache key hashes only instructions, tools and model, and the provider's cache
matches exact prefixes, so it carries no content between tenants.

Isolation tests gain three checks: two tenants running at once see none of each other's
data; with N idle tenants the process issues no tenant-scoped queries; a mismatched
tenant context fails at the store.

## Durability

Rule: **every unit of work ends completed, or failed and recorded for the operator,
replayable; a failure never blocks a thread.** Nothing is deleted except inputs over a
free plan's limit, which are recorded and never run (multi-tenant owner decision 3).

| Stage | Today | Change |
| --- | --- | --- |
| Before the inbox commits | Not stored; the sender must resend. Email, mailbox and Crontal events have stable idempotency keys | The Crontal frontend sends a `client_message_id` per chat message, so a resend is not stored twice |
| Waiting for the runner | Stored, pending until claimed | Unchanged |
| During a run | A crash lets the lease expire; the thread is claimed again. Retryable errors get 3 attempts with backoff | Unchanged |
| After the last attempt | The thread is marked `dead`. Its events and inputs are kept, and a failure notice is sent. Only a new chat message or `handler-rt revive` brings it back; new non-chat events queue silently | **Accepted.** The run stays failed with its error, its events are consumed by it, and the thread stays claimable, so new events run normally. A `run_failures` row and one `run failed` ERROR line go to the operator, who can replay the run (`POST /api/runs/{run_id}/replay`); the chat's owner can retry their latest failed run once (`POST /api/runs/{run_id}/retry`). Nobody else is notified. Threads left `dead` by older releases are swept at startup |
| Tool side effects | A retried run repeats calls that changed something outside Handler | Before a tool marked as writing outside Handler runs, store the call under a key from the thread, input events and the call's position. A retry gets the stored result back. A call whose outcome is unknown is parked for a person, never resent |
| Delivery | 10 attempts over several hours, then `dead` with no way to redeliver | A redeliver action for one row or all, an alert, and a slow final retry before it is marked `dead` |
| Workspace files | Uploaded every 300 s and at shutdown; a crash loses up to 5 minutes | Flushed when each run ends |
| Live feed | Replayed from stored steps on reconnect | Unchanged |

## Processes

Not limited to one process. The recommended shape is **one main process plus N reader
children**.

- The main process mostly waits on the network, so asyncio runs many concurrent runs in
  it. Peak measured concurrency was 2.
- CPU-heavy parsing runs in reader children, each able to use its own core. On the
  512 MB Basic dyno, N is about 2 (the main process idles near 140 MB).
- A larger dyno raises N with no design change.

More main processes become worth it only when measured event-loop lag shows the main
process's own CPU is the limit. Claims are already safe across processes. What would
need to change:

| In memory now | With several processes |
| --- | --- |
| `RunGate` shares | A slot table in Postgres, or the cap split evenly between processes |
| Live feed bus | `NOTIFY`, so a run in one process reaches an SSE client on another |
| Context cache invalidation | Already `NOTIFY` |
| The timer | A leader chosen by a Postgres advisory lock |

Each extra main process costs about 140 MB idle and its own pool and `LISTEN` connection
from Handler's budget of 12. More than one dyno also needs workspaces restored from S3 on
whichever dyno a run lands on, which the flush at run end makes safe.

The in-memory pieces stay behind small interfaces, so switching any of them to Postgres
or `NOTIFY` is a contained change.

## Failure behaviour

| Failure | Effect |
| --- | --- |
| Reader child killed or timed out | That tool call returns an error; the run continues or fails on its own terms; other runs are unaffected |
| Main process crashes or runs out of memory | Everything restarts. Leases expire, runs retry, deliveries resume. Browsers reconnect and replay |
| A tenant's webhook is down | Its deliveries back off; the pool keeps serving other tenants |
| A run fails every attempt | The run is recorded as failed and its events consumed; the thread keeps working. The operator is alerted and can replay it |
| Database unreachable | Ingest returns 503; nothing runs |

## Build order

Each step ships and rolls back on its own. Steps 1 to 4 run behind a
`HANDLER_SHARED_RUNTIME` switch that chooses the old `TenantHost` or the new runtime over
the same database; rollback is flipping it.

| Step | Work | Done when |
| --- | --- | --- |
| 0 | Native-only host setting and registry check. Workspace flush when a run ends. `client_message_id` from the Crontal frontend | Codex cannot be enabled on production; a restart loses no finished run's files |
| 1 | Failed runs recorded and replayable, threads never blocked; redeliver for dead deliveries; operator alerts on both | No failure blocks a thread, and every failure reaches the operator with a way to replay |
| 2 | One timer over `handler_control.due_work`; one housekeeping pass, including the 90-day retention cleanup (owner decision 9); one bus for the host; cancel as a stored flag | No per-tenant scheduler, housekeeping or poller tasks remain. **Built** on `feat/shared-runtime-step2` (not deployed; see Progress) |
| 3 | One claim loop across tenants; one host `RunGate` for every tenant, env-defined ones included, ranking interactive before background across tenants (**done**); tenant context cache; a tool view built per run from live entitlements, so a plan change takes effect on the next run (**done**); one routed app and one MCP route; the new integration contract; `TenantHost` lifecycle removed. Then switch on the registry and import `main`, `bss` and `yeup`. Split in two: **3a, the runtime core** (context cache, one claim loop, one outbox loop, `TenantHost` lifecycle removed): **built** on `feat/shared-runtime-step3a`; **3b, the request side** (one routed app and MCP route, routers, tool sets and sinks as context factories, the Crontal connector): next (see Progress) | With 50 idle tenants, the process issues no tenant-scoped queries; a busy tenant cannot delay another's chat; the three tenants run unchanged from the registry |
| 4 | Reader pool as child processes, with email parsing and image downscaling moved in; size caps in the main process; memory logged per run | A synthetic oversized PDF kills only its child, and the tool call reports an error. **Built** on `feat/shared-runtime-readers` (not deployed), with the review follow-up: no idle reader processes, fairness at admission only, an RSS cap per reader. Defaults 1 reader at 220 MB for the 512 MB dyno; measurements in [multi-instance.md](multi-instance.md#memory-budget-measured-november-17-amd64-linux-python-312-release-image). PDF text now comes from PyMuPDF (page blocks) instead of MarkItDown, which needed more than 220 MB even for 2 pages; 2- to 400-page PDFs read at a 164 MB reader peak (on `feat/shared-runtime`). Not behind `HANDLER_SHARED_RUNTIME` |
| 5 | Idempotent tool side effects | A run retried after a crash does not repeat a sent email or a Crontal write |
| 6 | Outbox subscriptions and one delivery pool with per-tenant limits | A failing sink for one tenant does not delay another tenant's deliveries |
| 7 | Thread strategies registered per type; the automation special case moved into its strategy | `run.py` has no checks on thread type |

Progress (branch `feat/shared-runtime`, November 15, 2026):

- **Step 0, Handler parts: built.** `HANDLER_ALLOWED_HARNESSES` (HANDOFF §3) and the
  workspace upload when each run ends. The Crontal `client_message_id` is not done.
- **Step 1: built.** Failed runs accepted and recorded (`run_failures`), the `run failed`
  alert, `GET /api/runs/failed` and replay; the startup sweep of old dead threads;
  `delivery dead` alert and redeliver (notifications.md). No user or admin failure
  notices (owner decision 6, revised November 21). The slow final delivery retry is not
  done.
- **Step 3: partly built** on `feat/shared-runtime-gating` (November 17, 2026, not
  deployed). Done: one host `RunGate` for env-defined instances too (each counted as
  `paid`, as the registry imports them); the gate ranks interactive before background
  across tenants, then paid before free, with aging for both (`HANDLER_RUN_MAX_WAIT_SECONDS`);
  each run's tool view is built from the live plan (`GrantPolicy.excluded`), and the
  gateway refuses calls and approvals the current plan excludes, so a plan change needs
  no restart. Not done: one claim loop, the tenant context cache, one routed app and MCP
  route, the integration contract and removing the `TenantHost` lifecycle. Changes to a
  tenant's integrations or secrets still need its runtime to restart (integration tools,
  routes and sinks are wired at start), and so does gaining Codex on an upgrade (the
  harness is built at start; production allows only the native harness anyway).

- **Step 2: built** on `feat/shared-runtime-step2` (November 22, 2026, not deployed).
  - **Due work.** `store/due_work.py`: rows `(tenant, kind, ref, due_at, version,
    attempts, last_error, lease_owner, lease_until)`, indexed on `due_at`. On a hub:
    `handler_control.due_work` (applied with the control DDL; `tenant` is the schema
    name). SQLite and a Postgres store with its own connections: a `due_work` table in
    the store itself, so the personal instance runs the same code path. Written in the
    same transaction as the work: schedules (create, update, delete, fire), approvals and
    questions (created with an expiry; removed when they leave `pending`), held inputs
    (`inputs.release` at the next month boundary) and mailboxes (`put_mailbox`,
    `delete_mailbox`). Additive only, so v65 runs over it unchanged.
  - **One timer** (`runtime/timer.py` `HostTimer`, in `HostServices` with the host bus
    and the workspace backstop). It claims due rows across tenants (`FOR UPDATE SKIP
    LOCKED`, a 120 s lease renewed while a handler runs), resolves the tenant through
    `Directory.tenant` (env hosts: the running instance's handlers; registry hosts:
    activates a sleeping tenant and counts the firing as in flight; step 3 swaps in the
    context cache there), and calls the kind's handler, which returns the next due time
    or None. A rewrite of the row during the handler (version bump) wins. Failures are
    recorded on the row with backoff (30 s doubling to 1 h) and logged. It sleeps until
    the earliest due time; the stores' in-process hint and `NOTIFY handler_due_work`
    (on the hub's existing LISTEN connection) wake it early; `HANDLER_TIMER_POLL_SECONDS`
    (60) is the backstop poll.
  - **Kinds:** `schedule.fire` (`Scheduler.fire_due`; a missed schedule fires once,
    then moves to its next slot from now, as before; `run_forever` removed),
    `approval.expire` (`Gateway.expire_due`), `question.expire`
    (`QuestionService.expire_due`), `inputs.release` (`PostgresCore.release_held_due`:
    due work at the month boundary, written when an input is held; a tenant start and a
    plan change also release), `retention.cleanup` (`retention.py`, daily per tenant,
    first an hour after a tenant first starts), and the integration kind `mailbox.poll`.
  - **Retention (decision 9):** deletes, older than 90 days, `run_failures` rows (the
    replay record; the run, its messages and its events stay), dead `deliveries`, and
    `over_limit` event records except email types. Held inputs, the email spool and
    mailbox mail are never touched.
  - **Housekeeping removed.** The grants reload is a settings listener in the process
    plus a version check before each run (catches a save made elsewhere); a NOTIFY-based
    invalidation comes with step 3's context cache.
  - **Backfill** at every tenant start (`backfill_due`, also on a plan change):
    idempotent; covers rows written by v65 (schedules, pending expiries, held inputs, the
    retention row) and removes rows whose work is gone.
  - **Integration contract, timer part:** `Integration.timer_kinds()` (integrations.md,
    "Timer kinds"). The mailbox poller is ported. `background_tasks` remains only for the
    Crontal connector (tool discovery with retry), which is not a timer and moves into
    context loading in step 3.
  - **Workspace sync:** one `WorkspaceBackstop` for the host retries failed run-end
    uploads every `HANDLER_SYNC_INTERVAL_SECONDS`; no database queries.
  - **One bus:** `RunEventBus` keyed by (tenant, thread); each runtime holds a
    `TenantBus` view that stamps and filters by tenant.
  - **Cancel:** there was no cancel API before. `POST /api/runs/{run_id}/cancel` (anyone
    who can see the thread) stores `runs.cancel_requested_at` (additive column) and wakes
    the run in this process; the run reads the flag at each heartbeat and at once on a
    wake, ends `cancelled` and consumes its events; the thread keeps working.
  - **Checks:** with idle tenants (8 env-defined with the mailbox integration, or 3 in
    the registry) no scheduler, housekeeping, workspace-sync or poller task exists, and
    Postgres's statement log shows only the timer's control-schema reads
    (`store_pg/test_idle_tenants.py`). The dispatcher's 30 s idle claim and the outbox's
    poll (when a sink is registered) are still per tenant until steps 3 and 6.

- **Step 3a: built** on `feat/shared-runtime-step3a` (November 22, 2026, not deployed;
  details and test results in [step3a-progress.md](step3a-progress.md)). No feature
  switch: rollback is the Heroku release, and every schema change is additive.
  - **A tenant is context** (`tenancy/contexts.py`). `build_tenant_context` (was
    `instance_runtime`'s body) builds the store on the hub, settings, grants, live
    limits, gateway, executor, sinks, timer handlers and the HTTP sub-app, and starts no
    long-lived task. `ContextCache`: LRU (`HANDLER_TENANT_CACHE_SIZE`, 32) with a TTL
    (`HANDLER_TENANT_CACHE_TTL_SECONDS`, 600 s), a context pinned while in use, retired
    by `invalidate` (generation counter) and closed when its last user leaves (flushes
    workspaces, detaches the store). Invalidated by `NOTIFY handler_tenant_changed`
    (payload: schema), sent in the writer's transaction by registry writes and settings
    saves; a process skips its own notices (it invalidated in process). Grants reload
    moves here; the version check before each run stays as a backstop. A tenant on its
    own store (SQLite, a lone Postgres instance) is pinned: only its process hears it.
  - **One claim loop** (`runner.py` `HostRunner`). Due tenants come from
    `handler_control.tenant_state` (created by the hub now, env tenants included;
    additive columns `schema_name`, `wake_version`, `next_delivery_at`,
    `delivery_version`). Every writer marks `next_wake_at` in its own transaction
    (`MARK_WAKE_SQL` in the hub store's `_notify`). Per due tenant: one `work_state`
    query, admission through the host `RunGate` in rank order (interactive first, then
    paid, fewer running, wait, with aging), `claim()` as before, each run an asyncio task
    with the context pinned. A tenant with nothing claimable is written back with its
    next look (a retry's `available_at`, a live lease's `lease_until`), only if no writer
    came in between (`wake_version`). Hints: stores' in-process `WorkSignal`, `NOTIFY
    handler_work_shared`; backstop poll `HANDLER_CLAIM_POLL_SECONDS` (5). Locally the
    same record lives in memory (`wakes.py` `LocalWakes`). `Dispatcher` is this loop
    over one store.
  - **One outbox loop** (`sinks.py` `HostOutbox`): due tenants from
    `next_delivery_at` (marked by enqueue, retry, redeliver), at most
    `HANDLER_OUTBOX_BATCH` (20) rows per tenant per round, `HANDLER_OUTBOX_CONCURRENCY`
    (4) tenants at once, round robin; sinks from the tenant's context.
  - **One MCP app for the host:** run tokens are host-wide and carry their tenant's
    gateway and name; a token presented under another tenant's URL is refused.
  - **`TenantHost` lifecycle removed:** activation, eviction, the per-tenant lock, the
    boot and delayed sweeps, `HANDLER_MAX_ACTIVE_TENANTS`, the idle stop and the
    mailbox pinning. It keeps the request side (routing, credential admission, spooled
    email and its replay, reacting to registry changes). At boot every tenant is due
    once. `RegistryTimerDirectory` and the env `TenantDirectory` wiring are replaced by
    `HostTimerDirectory` over the cache. Env hosts route through `EnvRouter`.
  - **Remains for 3b:** one routed app instead of a FastAPI sub-app per context; the
    MCP route keyed by token alone (the app is already one); routers, tool sets and
    sinks as tenant-context factories; Crontal tool discovery in context loading (it
    is still a start-up task per context until it succeeds, cancelled when the context
    closes); then the registry switch-on and import of `main`, `bss` and `yeup`.

The tenant registry (multi-tenant stage 2) is switched on only at the end of step 3, so
production never runs the activation lifecycle this design removes.

Future work: automatic slow retries of dead deliveries, with exponential backoff.

## Technical choices

Agreed with the owner on November 15, 2026 unless marked as a default.

| Choice | Decision |
| --- | --- |
| Request routing | Keep `/i/<name>` URLs so Crontal doesn't change. The name is only a hint; a request whose credentials name another tenant is rejected |
| Tool gateway | One MCP route for the host; the run token names the tenant and thread. Loopback MCP stays |
| Integration contract (default) | **Integrations provide handlers; the host owns loops.** A background task (a coroutine that loops forever per tenant, such as the mailbox poller) becomes a timer job: the integration registers a handler for a kind such as `mailbox.poll`, the host's timer calls it with the tenant context when a `due_work` row is due, and the handler writes the next due time. Tool discovery (the Crontal connector) moves into context loading, cached and retried on the next load, and can be shared across tenants. Routers are registered once and resolve the tenant per request; tool sets and sinks become factories that take the tenant context. The mailbox and Crontal integrations are ported |
| Due work | A `handler_control.due_work` table (tenant, kind, reference, due time), written in the same transaction as the work it describes |
| Context cache invalidation | `NOTIFY handler_tenant_changed`, even in one process |
| Reader children (default) | `forkserver`; pass file paths, not bytes; a memory limit per child (`RLIMIT_AS`); a timeout and kill; recycle after N tasks |
| Idempotency key for tool calls | Thread, input event ids, tool name and a hash of the canonical arguments; applies to tools that write outside Handler (`Risk.WRITE_*`). The call's position is not enough: a retried run can make different calls in a different order. An intent row is written before the call runs and completed after; a retry that finds a completed row gets its result, and one that finds an intent without a result has an unknown outcome and parks for a person |
| Workspace flush | When each run ends |
| Personal instance and SQLite | The same code path, as a host with one tenant |
| Migration and rollback | `HANDLER_SHARED_RUNTIME` chooses the old `TenantHost` or the new runtime over the same database |
| Metrics per tenant | Queue wait, run time, reader-slot wait, memory per run and failed-run counts |
| Proof before release | A load test with about 50 synthetic tenants covering the step 3, 4 and 6 checks |
| Dyno size | The 512 MB Basic dyno until step 4's measurements show otherwise |
| Push channel for the chat fallback | A Crontal notification until a push client exists |
