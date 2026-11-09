# Plans, input limits and spooled email

Multi-tenant Phase 3 (`multi-tenant-design.md`, Section 5). Applies **only to tenants
hosted through the registry** (`tenant-registry.md`). With the registry off, on
SQLite, and in a single-instance deployment, nothing here runs and `put` returns
plain booleans as before.

## Limits

`HANDLER_PLAN_LIMITS` is a JSON object laid over these defaults. A tenant's own
`limits` (platform API) overrides any key. An unknown plan gets `free`'s limits,
never unlimited.

| Key | `free` | `paid` | Meaning |
|---|---|---|---|
| `monthly_inputs` | 50 | none | Metered inputs per UTC calendar month. `null` is unlimited |
| `fix_model` | true | false | Ignore thread, chat and settings model choices |
| `model` | none | none | The fixed model; none means the deployment's default |
| `max_run_tokens` | 200,000 | none | Input + output tokens in one run |
| `max_run_turns` | 15 | none | Model iterations in one run |
| `connected_mail` | false | true | May connect a mailbox or Gmail |

A run that reaches a token or iteration limit stops at a turn boundary and finishes
normally, with a notice in its reply. It isn't a failure and isn't retried. Codex
has no iteration control, so only tokens apply to it.

## What counts

A new input counts once, in the transaction that stores it and only after the
idempotency check, so duplicates and retries never count.

| Event | Over the limit |
|---|---|
| `chat.message` | Refused, not stored: **429** with `{"error": "input_limit_reached", "message", "plan", "usage": {"used", "limit"}, "resets_at"}` |
| `email.received`, `mail.received`, `crontal.notification` | **Stored and held, 2xx**: no run. Email reports `"status": "held"`, Crontal webhooks `"held": true` |
| `automation.fired` | Skipped, one notice per schedule per month. A manual run gets the 429 |

Everything else (`approval.resolved` and other internal events) is never counted, nor
is an event with no thread, such as rejected email. `GET /api/limits/inputs` gives
the composer `{"limited": false}` or `{"limited": true, "plan", "used", "limit",
"held", "resets_at"}`.

A held input is an event with status `held`, plus a row in the tenant's `held_inputs`
holding the thread it will join. It does not stop the tenant sleeping.

## Release

Held inputs are released oldest first, each counting against the current month, for
as long as the allowance lasts. **The rest stay held for the next month; nothing is
discarded.** Release happens at the start of each month (a sleeping tenant's
`next_wake_at` includes the reset), and on a plan change.

A plan change takes effect at once on a running tenant, without waiting for it to
restart, and a sleeping one is woken for it.

*Digest:* there is no separate digest message. Released inputs on the same thread are
read together in that thread's next run. Each still counts as one input. Other
threads run under the plan's run cap, one at a time on `free`.

## Channels

On a plan without `connected_mail`, `POST /api/mailboxes` and
`POST /api/connections/google/start` return **403** with `{"error":
"plan_requires_upgrade", "feature", "plan", "message"}`. The platform API refuses
`mailbox`, `gmail` or `google` in such a tenant's `integrations` with a 409.

## Spooled email

The Cloudflare Worker makes one POST and never retries. If the tenant can't take a
request to its email ingest route (disabled, failed activation, or the host shutting
down), the host checks the signature and timestamp with that tenant's email secret
and stores the raw message in `handler_control.spooled_email`, idempotently, within
the normal size limit. The sender gets a **202** `{"status": "spooled"}`. An invalid
signature or stale timestamp gets a 401 and stores nothing.

When the tenant next runs, each message is replayed through its normal intake,
freshly signed, so the quota gate applies and intake's idempotency key prevents
double counting. A replay that fails stays stored and is retried every five minutes.
Nothing is deleted without a successful replay.

**If the database itself is unreachable, the host can only return 503.** The Worker
doesn't retry, so that message then exists only in the Gmail copy.

## Rollback

All of this adds tables only (`input_usage`, `held_inputs`, `quota_notices` per
tenant, `spooled_email` in the control schema), so earlier images run against a
database this code has touched. An earlier image doesn't route `held` events or
replay spooled mail: both stay stored and are picked up when this image returns.
