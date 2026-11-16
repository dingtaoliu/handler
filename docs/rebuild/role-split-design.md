# Shared host resources: inbox, runner and outbox

Status: **owner decisions settled November 15, 2026; steps 0 and 1 built on
`feat/shared-runtime`** (not deployed; see Build order). First written
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
6. **Parked work waits for a person for now.** A parked thread is shown with its error and
   a Retry action, its tenant's admins are told and the operator is alerted. Automatic
   retries of parked threads are future work, with exponential backoff.
7. **A side effect with an unknown outcome is never resent automatically.** The call is
   parked and a person is asked, as with an approval.
8. **Fairness:** interactive before background across all tenants; paid before free, with
   aging; reader-pool shares mirror the run caps.
9. **Retention:** parked events, dead deliveries and records of dropped over-limit inputs
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
| A new event on a parked thread | Retry now | Queue and notify | Queue and notify | Queue and notify |

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

Rule: **every unit of work ends either completed or parked**, with a reason, a notice to
someone and a way to retry. Nothing is deleted except inputs over a free plan's limit,
which are recorded and never run (multi-tenant owner decision 3).

| Stage | Today | Change |
| --- | --- | --- |
| Before the inbox commits | Not stored; the sender must resend. Email, mailbox and Crontal events have stable idempotency keys | The Crontal frontend sends a `client_message_id` per chat message, so a resend is not stored twice |
| Waiting for the runner | Stored, pending until claimed | Unchanged |
| During a run | A crash lets the lease expire; the thread is claimed again. Retryable errors get 3 attempts with backoff | Unchanged |
| After the last attempt | The thread is marked `dead`. Its events and inputs are kept, and a failure notice is sent. Only a new chat message or `handler-rt revive` brings it back; new non-chat events queue silently | **Parked**, visible in the UI and admin console with the error and a Retry action. The tenant's admins are told and the operator is alerted. The thread strategy decides what a new event does. Automatic retries with exponential backoff are future work |
| A poison event | One event failing every attempt parks the whole thread | On the last attempt, retry the events one at a time and park only the one that fails |
| Tool side effects | A retried run repeats calls that changed something outside Handler | Before a tool marked as writing outside Handler runs, store the call under a key from the thread, input events and the call's position. A retry gets the stored result back. A call whose outcome is unknown is parked for a person, never resent |
| Delivery | 10 attempts over several hours, then `dead` with no way to redeliver | A redeliver action for one row or all, an alert, and a slow final retry before parking |
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
| A run fails every attempt | The thread is parked and its people are told; a person retries it |
| Database unreachable | Ingest returns 503; nothing runs |

## Build order

Each step ships and rolls back on its own. Steps 1 to 4 run behind a
`HANDLER_SHARED_RUNTIME` switch that chooses the old `TenantHost` or the new runtime over
the same database; rollback is flipping it.

| Step | Work | Done when |
| --- | --- | --- |
| 0 | Native-only host setting and registry check. Workspace flush when a run ends. `client_message_id` from the Crontal frontend | Codex cannot be enabled on production; a restart loses no finished run's files |
| 1 | Parked threads with the Retry action; redeliver for dead deliveries; notices and alerts on both | No work stops without a notice and a way to retry |
| 2 | One timer over `handler_control.due_work`; one housekeeping pass; one bus for the host; cancel as a stored flag | No per-tenant scheduler, housekeeping or poller tasks remain |
| 3 | One claim loop across tenants; tenant context cache; one routed app and one MCP route; the new integration contract; `TenantHost` lifecycle removed. Then switch on the registry and import `main`, `bss` and `yeup` | With 50 idle tenants, the process issues no tenant-scoped queries; a busy tenant cannot delay another's chat; the three tenants run unchanged from the registry |
| 4 | Reader pool as child processes, with email parsing and image downscaling moved in; size caps in the main process; memory logged per run | A synthetic oversized PDF kills only its child, and the tool call reports an error |
| 5 | Idempotent tool side effects | A run retried after a crash does not repeat a sent email or a Crontal write |
| 6 | Outbox subscriptions and one delivery pool with per-tenant limits | A failing sink for one tenant does not delay another tenant's deliveries |
| 7 | Thread strategies registered per type; the automation special case moved into its strategy | `run.py` has no checks on thread type |

Progress (branch `feat/shared-runtime`, November 15, 2026):

- **Step 0, Handler parts: built.** `HANDLER_ALLOWED_HARNESSES` (HANDOFF §3) and the
  workspace upload when each run ends. The Crontal `client_message_id` is not done.
- **Step 1: built.** Parked threads with notices, the `thread parked` alert and Retry;
  `delivery dead` alert, admin notice and redeliver (notifications.md). The poison-event
  split and the slow final delivery retry are not done.

The tenant registry (multi-tenant stage 2) is switched on only at the end of step 3, so
production never runs the activation lifecycle this design removes.

Future work: automatic retries of parked threads and deliveries, with exponential backoff.

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
| Idempotency key for tool calls | Thread, input events and the call's position in the run; applies to tools that write outside Handler (`Risk.WRITE_*`) |
| Workspace flush | When each run ends |
| Personal instance and SQLite | The same code path, as a host with one tenant |
| Migration and rollback | `HANDLER_SHARED_RUNTIME` chooses the old `TenantHost` or the new runtime over the same database |
| Metrics per tenant | Queue wait, run time, reader-slot wait, memory per run and parked counts |
| Proof before release | A load test with about 50 synthetic tenants covering the step 3, 4 and 6 checks |
| Dyno size | The 512 MB Basic dyno until step 4's measurements show otherwise |
| Push channel for the chat fallback | A Crontal notification until a push client exists |
