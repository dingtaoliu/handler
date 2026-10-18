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
   hourly) and after the last attempt marks the row `dead` and logs
   `delivery dead-lettered`. A `DeliveryError(retryable=False)` dead-letters
   at once.

A sink implements `name`, `retry`, `accepts(event)` and `async deliver(payload)`.
The outbox is sink-agnostic; other events (e.g. `task.status_changed`) only
need an emit call and a sink that accepts them.

Inspect stuck deliveries in the instance's schema:
`SELECT sink, key, status, attempts, last_error FROM deliveries WHERE status <> 'delivered'`.
To retry a dead row, set `status = 'pending', next_attempt_at = now()`.

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

## Automatic failure notifications

When a background (non-chat) run fails with no retry left (the thread goes
dead), `FailureNotifier` notifies the thread's acting user plus the instance's
enabled `admin` users, for example:
`Automation run failed and will not retry: Daily digest (automation:daily). HarnessError: …`.
Retries that are still scheduled notify nobody, and each thread notifies at
most once per hour (in memory; a restart resets the window).
