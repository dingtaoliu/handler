# Plans, input limits, free tenants and spooled email

Multi-tenant Phases 3 and 4 (`multi-tenant-design.md`, Sections 5 and 6). Applies **only to tenants
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
| `model` | `gpt-6.1-terra` | none | The fixed model; none means the deployment's default |
| `max_run_tokens` | 200,000 | none | Input + output tokens in one run |
| `max_run_turns` | 15 | none | Model iterations in one run |
| `email` | false | true | Inbound email of any kind. Off, it is refused and **not stored** |
| `connected_mail` | false | true | May connect a mailbox or Gmail |
| `automations` | false | true | Schedules run, and can be created |
| `console` | false | true | The admin console and its APIs (below) |
| `allow_codex` | false | true | The Codex harness may run |
| `strict_grants` | true | false | The grant floor below |
| `max_upload_bytes` | 100 MB | none | Total chat uploads kept |
| `max_workspace_bytes` | 200 MB | none | Total workspace files the agent or tools write |

`max_runs` belongs only in a tenant's own `limits` (the run gate). In
`HANDLER_PLAN_LIMITS` it is refused at startup; set per-plan run caps in
`HANDLER_PLAN_RUN_CAPS`.

**Free tenants get chat and Crontal threads only:** no email of any kind and no
automations, until they move to a paid plan.

A run that reaches a token or iteration limit stops at a turn boundary and finishes
normally, with a notice in its reply. It isn't a failure and isn't retried. Codex
has no iteration control, so only tokens apply to it.

## What counts

A new input counts once, in the transaction that stores it and only after the
idempotency check, so duplicates and retries never count.

| Event | Over the limit |
|---|---|
| `chat.message` | Refused, not stored: **429** with `{"error": "input_limit_reached", "message", "plan", "usage": {"used", "limit"}, "resets_at"}` |
| `email.received`, `mail.received`, `crontal.notification` | **Stored and held, 2xx**: no run. Email reports `"status": "held"`, Crontal webhooks `"held": true`. Email only on a plan with `email` |
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
double counting. A replay that fails stays stored and is retried with backoff
(5 minutes, doubling, at most a day apart). After 12 failures it stops retrying and
logs an error, and the row is kept. Set `next_attempt_at` to `now()` to try again.
Nothing is deleted without a successful replay. Spooled mail for a tenant now on a
plan without email is kept, not replayed.

**If the database itself is unreachable, the host can only return 503.** The Worker
doesn't retry, so that message then exists only in the Gmail copy.

## Rollback

All of this adds tables only (`input_usage`, `held_inputs`, `quota_notices` per
tenant, `spooled_email` in the control schema), so earlier images run against a
database this code has touched. An earlier image doesn't route `held` events or
replay spooled mail: both stay stored and are picked up when this image returns.

## Free tenants (Phase 4)

Every refusal below is a **403** with `{"error": "plan_requires_upgrade", "feature",
"plan", "message"}`. `feature` is one of `email`, `automations`, `console`,
`mailbox` or `gmail`.

**Email.** Signed mail to a free tenant's ingest route gets `feature: "email"`. It is
not stored, held, spooled or processed, including while the tenant is disabled or
failing to start, and an upgrade processes nothing from before it. The spool still
applies to every plan that has email. The platform API refuses `email_intake`,
`mailbox`, `gmail` or `google` in a free tenant's `integrations` with a 409.

**Automations.** `/api/automations` and everything under it, including manual runs,
get `feature: "automations"`. The `automation_*` tools are not offered, and the grant
policy denies `automation.*` in any case. Existing schedules of a downgraded tenant
are kept unchanged but do not fire and do not count towards `next_wake_at`. They
resume on upgrade.

**Codex.** Never built for a free tenant, whatever the settings, thread type, thread,
chat request or `HANDLER_DEFAULT_HARNESS` say. New threads use the native harness. An
existing Codex thread is moved to it permanently at its next run, dropping its Codex
session.

**No admin console.** Crontal calls Handler with the instance admin key on behalf of
merchant owners, so a role check would not hold. Instead, only the chat panel's routes
answer, for any caller:

- `/healthz`, `/mcp`, `/ingest/*`
- `/api/chat` and `/api/chat/options`
- `/api/limits/inputs`, `/api/me`, `/api/users` and `/api/users/<id>`
- `/api/threads…`, `/api/runs/…`, `/api/tasks…`
- `/api/approvals…`, `/api/questions…`, `/api/notifications…`, `/api/uploads…`
- `/api/mailboxes…` and `/api/connections/google…` (each answers 403 for its own feature)

Everything else, including `/app`, `/login`, `/login/ticket`, sign-in tickets,
`/api/settings…`, logs, tools, audit, memory, usage reports and user tokens, gets
`feature: "console"`. The list fails closed, so a new route starts out refused.

**Grants.** Settings can only tighten this floor:

| Call | Chat thread | Thread fed by outside input (Crontal) |
|---|---|---|
| Reads (`crontal_api.get`, routes, describe) | Automatic | Automatic |
| Writes kept in Handler (memory save, notify, questions, titles) | Normal rules | Normal rules and the untrusted floor |
| `crontal_api.call` inside the merchant (draft RFQ, contact, listing) | Approval | Approval |
| `crontal_api.call` that leaves the merchant (share, publish, submit, send) | Approval | **Denied** |
| Destructive calls (Crontal file trash and delete) | Approval | **Denied** |
| Anything that runs code (`exec`) | **Denied** | **Denied** |
| `automation.*` | **Denied** | **Denied** |

**Storage.** Checked at write time; over the limit the write fails with a **413**
`{"error": "storage_limit_reached", "scope": "upload" | "workspace", "limit_bytes",
"message"}`. The agent's tools return the same message as a tool error. Inbound
attachments and Crontal notifications are always stored, and then count against the
limit.

