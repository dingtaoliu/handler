# Inbox, worker and outbox roles: design

Status: **proposed, not approved for work**. Written November 14, 2026 at the owner's
request, as a follow-on to [multi-tenant-design.md](multi-tenant-design.md). Nothing here
is built.

## Goal

Handler has three stages: inbox, agent processing and outbox. The owner wants:

1. A reliable, performant queue in front of each stage.
2. Each stage able to run as its own process, so a failure in one does not take down the
   others, and each can be scaled on its own when Handler moves to larger hosts.
3. Agent processing done by a shared pool of workers that claim work from the inbox and
   load the tenant's context for each job, instead of one long-running loop per tenant.
4. **All three roles able to run on a single dyno**, which is the default deployment.

## Current state

Read on November 14, 2026 on branch `feat/multi-tenant`.

| Stage | Queue today | Runs where |
| --- | --- | --- |
| Inbox | `events` table per tenant schema. Idempotent insert, routed to a thread, `pending_since` set. `NOTIFY handler_work_shared` with the schema name | HTTP handlers (`web/app.py`, `web/ingest.py`, `web/email_ingest.py`), mailbox poller, scheduler |
| Agent | Threads claimed with `FOR UPDATE SKIP LOCKED` and a lease. An expired lease is reclaimed, so a crashed run is retried | One `Dispatcher` task per active tenant (`server.py` `instance_runtime`), capped by `RunGate` |
| Outbox | `deliveries` table per tenant schema, with backoff and dead letters | One outbox task per active tenant |

The queues are already durable and transactional with the data. **No new queue technology
is needed**: Postgres stays the queue. At Handler's volume (hundreds to tens of thousands
of events a day) it is fast enough, and an event can never disagree with its thread state.
Revisit only if claim latency or `NOTIFY` volume becomes measurable.

What is missing is process separation. Everything runs in one process, and each active
tenant owns long-lived tasks: dispatcher, scheduler, outbox, housekeeping, workspace sync,
Crontal connector and integration pollers (`server.py`, around line 955). That is why the
`TenantHost` has to start and stop tenants.

Three couplings tie the stages to one process:

| Coupling | Where | Why it blocks a split |
| --- | --- | --- |
| Run events to browsers | `RunEventBus` (`bus.py`), consumed by SSE in `web/app.py` and `web/tasks_api.py` | In memory: a run in another process is invisible to the web process |
| Agent tools | The harness calls the gateway at `http://<local>:<port>/i/<name>/mcp/` (`server.py` ~814, `run.py` ~633) | The gateway is mounted in the web app and holds the run tokens |
| Workspaces | `workspace_sync.restore` at run start (`run.py` ~619); changed files upload every 300 s and on shutdown | Safe on one disk; on another host a run could restore files up to 5 minutes stale |

## Design

### Roles

One codebase, one image. `handler-rt serve --roles web,worker,io` chooses what a process
runs. The default is all three.

| Role | Runs | Writes | Scales with |
| --- | --- | --- | --- |
| `web` (inbox) | HTTP API, Crontal proxy target, webhook and email ingest, email spool, platform API, SSE relay, static UI | `events`, approvals and question answers, settings | Request volume |
| `worker` (agent) | Global claim loop, `RunGate`, tenant context cache, harnesses, the tool gateway, document-reader process pool, workspace restore and flush | Messages, run steps, `deliveries`, usage | Concurrent runs: this is where larger hosts go |
| `io` (outbox and producers) | Delivery of `deliveries`, mailbox pollers, the automation scheduler, housekeeping (approval and question expiry) | Delivery status; new `events` from mail and schedules | Connected mailboxes, delivery volume |

Mail pollers and the scheduler only create inbox events, so they move to `io`. That is what
lets a worker be stateless: nothing tenant-specific runs while a tenant is idle.

**Rule: roles communicate only through Postgres** (tables, plus `LISTEN`/`NOTIFY` for
latency), even when they share a process. There is no in-process shortcut, so the code
path tested in the single-process default is the same one that runs split.

### A tenant becomes context, not a runtime

A tenant is no longer started and stopped. A worker that claims a thread loads that
tenant's context:

- registry row and decrypted secrets
- plan limits, settings and grant policy
- a store scoped to its schema (the existing hub and `SET LOCAL search_path`)
- its file store prefix and workspace root

Contexts live in an LRU cache in each process, invalidated by a `NOTIFY handler_tenant_changed`
on any registry or settings change. An idle tenant costs nothing but a cache entry that
eventually expires. This replaces most of `TenantHost`'s lifecycle code: activation,
deactivation, eviction, the per-tenant lock and the boot sweep.

Kept from the current branch: schema per tenant, the hub and scoping rules, the registry,
the platform API, the quota gate, plan guards, the email spool, isolation tests and
credential-gated routing.

### Global claim loop

1. Any process that writes work (an event, a retry, a schedule, an expiry) also updates
   `handler_control.tenant_state.next_wake_at` in the same transaction. This already
   happens for writers outside a running tenant; it becomes the rule for all writers.
2. A worker reads due tenants from `tenant_state` (one indexed query), ordered by plan
   priority and wait time, then claims threads inside each tenant's schema with the
   existing `claim()`.
3. `NOTIFY handler_work_shared` (payload: schema) wakes workers immediately. A poll of
   `tenant_state` every few seconds covers missed notifications.

Fairness across several worker processes cannot live in memory. `RunGate`'s global and
per-tenant caps move to a `handler_control.run_slots` table: a slot row is taken in the
claim transaction and released when the run ends, and the lease expiry frees slots from a
crashed worker.

### Cross-process paths that replace in-memory ones

| Path | Today | Design |
| --- | --- | --- |
| Run events to SSE | `RunEventBus` in memory | The worker writes the event (most are already stored as run steps) and sends `NOTIFY handler_run_events` with schema, run id and sequence. The web process subscribes once and fans out to its SSE clients, reading the row when the payload is large. `NOTIFY` payloads are limited to 8,000 bytes, so they carry ids, not content. Reconnecting clients replay from the stored sequence |
| Agent tools | Gateway mounted in the web app | The gateway runs **in the worker**, on a loopback port per worker process, next to the runs and their tokens. The web process no longer serves `/mcp/` for harnesses |
| Approvals and questions | Decided through the web API | The web process writes the decision and sends `NOTIFY`. A waiting run in the worker resumes on the notification, with a poll as backup. **To verify:** how the gateway waits today, since an in-memory future would not survive the split |
| Cancelling a run | Web API to the in-process run | A cancel flag on the run row plus `NOTIFY`; the worker checks it between steps |
| Settings and registry changes | Read live in-process | `NOTIFY handler_tenant_changed`; caches drop the entry |
| Workspaces | Restore at start, upload every 300 s | Unchanged on one host. Before any multi-host split, a run also flushes its changed files when it ends, so the next run on any host restores current files |

### Running all three on a single dyno

Two modes, chosen by configuration, with no code difference:

| Mode | How | Isolation | Memory |
| --- | --- | --- | --- |
| **A. One process, all roles** (default) | `--roles web,worker,io` | Code-level only: a crash or an out-of-memory error still stops all roles, as today | One interpreter, about 140 MB idle locally. Fits the current 512 MB Basic dyno |
| **B. Three processes, one dyno** | A small supervisor (`handler-rt serve --split`) starts one process per role, restarts a role that exits, and keeps `web` bound to `$PORT` | A crashed worker or io process is restarted alone; ingest keeps accepting and queued work waits | Three interpreters, roughly 3 × 140 MB idle before any run. Needs a 1 GB dyno |

Then mode C, separate dynos per role, is a `heroku.yml` change. It also needs the
workspace flush-on-end above, and SSE served only by `web` dynos.

Document reading moves to a small process pool owned by the worker in every mode. A large
PDF can then exhaust only that child process, which matches the leading R14 hypothesis
without proving it.

### Connection budget

| Mode | Pools | Total |
| --- | --- | --- |
| A | One hub: 11 pooled + 1 LISTEN | 12, unchanged |
| B or C | web 3 + io 2 + worker 4 pooled, plus 1 LISTEN each | 12 |

Each process needs its own `LISTEN` connection, so splitting costs pooled connections.
Mode C with more than one worker dyno exceeds 12 and needs a larger Postgres plan or a
connection pooler (PgBouncer in transaction mode works with `SET LOCAL`, because the
scope lives inside the transaction).

### Failure behaviour

| Failure | Mode A | Modes B and C |
| --- | --- | --- |
| Worker crashes or runs out of memory | Everything restarts; leases expire and runs retry | Only the worker restarts; ingest and delivery continue; threads show "queued" |
| io process crashes | Everything restarts | Deliveries and mail polls pause and resume; nothing is lost |
| web process crashes | Everything restarts | Runs continue; browsers reconnect and replay events |
| Database unreachable | All roles stop; ingest returns 503 | Same |

The UI must show "queued" rather than appearing to hang whenever a worker is not running.

## Build order

Each step ships and rolls back on its own.

| Step | Work | Done when |
| --- | --- | --- |
| 0 | Release the current branch's Phase 1 (shared connections, memory logs) as planned | Running in production; memory data collecting |
| 1 | Run events through `NOTIFY` and stored rows; approvals, questions, cancel and cache invalidation through the database | The suite passes with web and worker as separate processes in tests; SSE replay works across a restart |
| 2 | Gateway in the worker; global claim loop; `run_slots`; tenant context cache; producers moved to `io` | No per-tenant long-lived tasks remain; idle tenants issue no queries; a busy tenant cannot starve another across two worker processes |
| 3 | Document reader in a process pool | A synthetic oversized PDF kills only the reader child, and the run reports an error |
| 4 | `--split` supervisor (mode B) | Killing the worker process leaves ingest accepting, and queued work completes after restart |
| 5 | Workspace flush on run end; mode C configuration | Two worker processes with separate data directories pass the workspace tests |

## Open decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | Whether to switch on the registry (multi-tenant stage 2) before this refactor | Release Phase 1 and the Crontal branch with switches off now. Hold stage 2 until step 2 here, so production never runs the tenant start/stop lifecycle that this design removes. The cost is a later free-tier pilot |
| 2 | Dyno for mode B | Decide from Phase 0 memory logs. Mode A on the 512 MB Basic dyno until then |
| 3 | When to move to mode C | When worker CPU or memory, not request volume, is the measured limit |
