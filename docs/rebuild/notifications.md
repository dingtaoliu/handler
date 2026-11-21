# Notifications and output sinks

Notifications are the first **output** of the integrations architecture: the
core emits domain events, and integrations plug in sinks that deliver them.

## Flow

1. `notify` (tool) or an automatic notification calls `Notifier.send`
   (`runtime/notifications.py`). It stores one `notifications` row per local
   recipient, publishes `notification` on the bus (Handler UI, Crontal panel
   toasts), and emits a `notification.created` `DomainEvent` with a fresh
   notification id (`ntf_…`).
2. `Outbox.emit` (`runtime/sinks.py`) adds one row per accepting sink to the
   durable `deliveries` table (`store/deliveries.py`, SQLite and Postgres),
   keyed by (sink name, event key). Emitting the same event twice is a no-op.
3. The `outbox` background task claims due rows (a 60-second lease on
   `next_attempt_at`; `FOR UPDATE SKIP LOCKED` on Postgres) and calls
   `sink.deliver(payload)`. On failure it retries with the sink's
   `RetryPolicy` (default 10 attempts: 10 s, 30 s, 2 min, 10 min, 30 min, then
   hourly) and after the last attempt marks the row `dead`. A
   `DeliveryError(retryable=False)` dead-letters at once. Each dead row logs
   one ERROR line, `delivery dead` (fields `tenant`, `sink`, `key`, `attempts`,
   `error`), for a log-based operator alert. Nobody else is notified: the
   row is kept for redeliver (below).

A sink implements `name`, `retry`, `accepts(event)` and `async deliver(payload)`.
The outbox is sink-agnostic; other events (e.g. `task.status_changed`) only
need an emit call and a sink that accepts them.

Dead deliveries (admin only):

- `GET /api/deliveries` lists dead rows (`?status=pending|delivered|` for others).
- `POST /api/deliveries/redeliver` with `{"sink": "crontal", "key": "ntf_…"}`
  requeues one; without `key`, every dead row of that sink. Requeued rows get a
  fresh set of attempts and are due at once.
- CLI: `handler-rt delivery redeliver <sink> [--key <key>]`.

Automatic slow retries of dead rows are future work (role-split-design.md).

## Failed runs

Owner decision (November 21): a failure never blocks a thread, and users and
tenant admins get no failure notices. A person can retry their failed chat
message once (see "User retry" below) or send it again; an automation runs on its
next trigger. Failure signals go to the site owner only.

A run that fails with no retry left, or with an error that is never retried (a
harness this host does not allow, an unknown harness), is **accepted**:

- The run stays `failed` with its error, and the user's input messages stay in
  the conversation record.
- The events it took are marked consumed by it (`events.consumed_by_run` = the
  run id), so they never run again by themselves.
- The thread is not blocked: attempts and backoff reset, `last_error` stays for
  debugging until a run succeeds, and the thread stays claimable. Events that
  arrived during the run, and any new one, run normally. `threads.dead` is
  never set.
- One `run_failures` row records it (thread, type, error, attempts, event ids,
  failed time).
- One ERROR log line `run failed` (fields `tenant`, `thread_key`,
  `thread_type`, `run_id`, `attempts`, `error`, `event_ids`) for the operator's
  log-based alert. A failure that will be retried logs the WARNING
  `run failed, will retry` instead.
- Chat shows the error inline as before: the stream's `run_finished` event has
  `status: failed` and the error, and Handler's conversation view shows
  "Run failed (send your message again to retry): …". Crontal's chat panel
  shows a red failure icon with Retry instead (below).

Operator tools (admin only):

- `GET /api/runs/failed?limit=&before=`: terminal failures, newest first, with
  run id, thread key, type and title, error, attempts, failed time, event ids
  and whether it was replayed; `next_cursor` pages back.
- `POST /api/runs/{run_id}/replay`: makes that run's events pending again on
  its thread, so they run once more. 409 if already replayed or if the run is
  not a terminal failure; 404 for an unknown run.
- CLI: `handler-rt run failed [--limit N]`, `handler-rt run replay <run_id>`.
  `handler-rt thread revive <key>` still clears a thread's attempts and backoff.
- Web UI: a Failed runs table with Replay on Activity, for admins.

### User retry

`POST /api/runs/{run_id}/retry` (no body) re-runs a failed chat message for the
person chatting. It does what replay does, so it is not a new message: the same
events run again, the user message is not saved twice, and nothing is counted
against the plan's monthly inputs (inputs are counted only when `put` stores a
new event, `_charge_input` in `store/postgres.py`; replay and retry only clear
`consumed_by_run` on events already stored). Replay and retry share one flag
(`run_failures.replayed_at`; a retry records `replayed_by = "retry:<user>"`), so
a failed run is re-run at most once, by either.

Allowed for a principal acting as user U (a user token, or the admin key with
`X-Handler-User: U`) when all hold:

- the run has a `run_failures` row not yet replayed or retried;
- its thread is a `chat` thread owned by U (`chat:<U>:…`);
- it is the thread's latest run, and nothing else is queued or running on the
  thread (checked in the same transaction as the re-queue, so a retry can't run
  out of order with a newer message).

Responses:

| Status | When | Body |
|---|---|---|
| 202 | Re-queued | `{"run_id", "thread_key", "status": "queued"}` |
| 400 | Admin key without `X-Handler-User` | `detail` |
| 404 | Unknown run, or a chat thread that isn't U's (an admin user too) | `{"detail": "run not found"}` |
| 409 | Not a terminal failure | `{"detail": "run is not a terminal failure"}` |
| 409 | Already retried or replayed | `{"detail": "run was already replayed or retried"}` |
| 409 | Not the latest run, or newer work queued or running | `{"detail": "run is not the latest on its thread"}` |
| 409 | Not a chat thread | `{"detail": "only chat runs can be retried"}` |

Shared (non-chat) threads get 409, not 404: every member can already see them
and their runs, so the answer confirms nothing; the operator re-runs those with
replay. On success the thread's stream gets `event_queued` (as a new chat
message does) with `data: {"event_id", "event_ids", "retry_of": <run_id>}`, then
`thread_updated`; then the usual `run_started` … `run_finished` of the new run.
Admin replay publishes the same two events.

What the client reads to show and enable Retry:

- **Live**, `run_finished` on the thread's stream: `run_id`, `data.status ==
  "failed"`, `data.error`, `data.retryable` (true only for an accepted failure on
  a chat thread with nothing else queued; false while `data.retry_in` is set,
  because the runtime retries by itself) and `data.event_ids` (the events the
  run took).
- **On load**, `GET /api/threads/{key}/runs` (newest first): every run has
  `retryable`; at most one is true, the latest, when it is `status: "failed"`,
  has an unused failure and nothing else is queued. `trigger_event_ids` are its
  events.
- **Which message**: in `GET /api/threads/{key}/messages`, the failed run's user
  message is the `role: "user"` message whose `raw.event_id` is in the run's
  `trigger_event_ids` (`data.event_ids` on the stream). Match on the event id,
  not `run_id`: after an automatic retry the message is saved under the first
  attempt's run.
- Show the icon and Retry on that message when `retryable` is true; on 202, or
  on any `event_queued` or `run_started` for the thread, hide Retry and show
  progress. A 409 means the run can't be retried any more: reload the runs.

Threads parked by releases before this one (`threads.dead`) are swept at each
runtime start, before any work is claimed: `dead` is cleared, their queued
events are consumed by `failed-before-upgrade` so stale work does not suddenly
run, each gets a `run_failures` row (under the thread's latest run) to replay,
and each logs one `run failed` line with `failed_before_upgrade: true`. The
sweep only touches dead threads, so it is idempotent.

Email is never lost to a failure: the events and stored mail stay in the
database and can be replayed, and the shared host's email spool deletes a
spooled message only once intake accepted it, never because a run failed.

## Crontal sink

`integrations/crontal/sink.py` registers only when the instance is
Crontal-connected: `CRONTAL_URL`, `CRONTAL_AGENT_TOKEN` and
`HANDLER_WEBHOOK_SECRET` are all set (per instance, see `multi-instance.md`).

- `POST <Crontal origin>/api/v1/handler/callbacks/notifications`.
- Body: `id`, `instance` (`main` for the root/single instance), `recipients`
  (Handler user ids, which are Crontal user ids, or `"all"`), `message`,
  `thread_key`, `run_id`, `thread_type`, `thread_title`, `created_at`.
- Headers: `X-Handler-Signature: t=<unix>,v1=<HMAC-SHA256>` over `"<t>." + body`
  with the instance's `HANDLER_WEBHOOK_SECRET` (the `/ingest/crontal` scheme in
  reverse; `web/ingest.sign`), and `X-Handler-Instance: <name>`. Every attempt
  is re-signed, because Crontal allows 5 minutes of clock skew.
- 2xx is success (201 new, 200 duplicate). 400/413/422 dead-letter; anything
  else (401 during secret rotation, 404 before Crontal ships the endpoint,
  5xx, timeouts) retries.
- Crontal resolves `"all"` to all active users of the instance's merchant and
  drops recipients outside that merchant. It is idempotent on
  (instance, notification id) and never forwards these notifications back.

## Crontal events coming in (`/ingest/crontal`)

Crontal signs each webhook with `X-Crontal-Signature`. There are two body shapes:

| | Per-user note (no `scope`) | Merchant-wide event (`"scope": "merchant"`) |
|---|---|---|
| Body | `id`, `recipient_user_id`, `type`, `entity_type`, `entity_id`, `data`, `created_at` | The same, with `"recipient_user_id": null` and `id` the merchant event id |
| Idempotency key | `crontal:<id>` | `crontal:merchant:<id>`, never colliding with a note id |
| Thread | `crontal:<entity_type>:<entity_id>`, else `crontal:notification:<id>` | The same entity thread, else `crontal:notification:merchant-<id>` |
| Owner and acting user | The recipient | None: a shared merchant thread |
| Metered inputs | One per recipient | **One** for the whole merchant |

Use the merchant-wide shape for anything Crontal sends to all of a merchant's users
(RFQ and bid events), so five users mean one input, not five. Any other `scope`
value is refused with a 422. Bodies without `scope` behave exactly as before, so old
and new Crontal both work.

A thread with no owner behaves like any shared thread:

- Every merchant user sees it, and its tasks, approvals and questions, through the
  proxy (`can_see_thread` restricts only `chat:` threads).
- Its runs act as the instance's AI identity alone. No `X-Crontal-On-Behalf-Of` is
  sent, as for all background threads.
- Failure notices go to the instance admins only.

A thread created by a per-user note keeps that first owner; later merchant-wide events
join it unchanged. Over a plan's limit, either shape is held with a 2xx
(`plan-limits.md`).
