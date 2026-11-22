# Integrations

Status: design approved 2026-10-18. The refactor is queued behind the notification-sink and tasks branches.

## Goal

The handler core is app-agnostic. Its built-in inputs and outputs are:
- web chat
- automations
- in-app notifications
- its own UI

Everything specific to an external app, such as Crontal, Gmail or email intake, is an **integration**. An instance turns integrations on, and both the UI and the runtime show only what the enabled integrations contribute. A plain handler instance has no CRONTAL or EMAIL sections anywhere.

## What an integration can contribute

| Contribution | Mechanism |
|---|---|
| **Inputs** | Thread types (`ThreadTypeSpec`: name, lane, visibility, acts-as rule, turn renderer, default instructions) and the HTTP routers that write their events (ingest, callbacks). |
| **Outputs** | Sinks: `name`, `accepts(event)`, `async deliver(payload)`, retry policy. Domain events (`notification.created`, `task.status_changed`, `message.assistant`, …) are fanned out to the accepting sinks through the generic durable `deliveries` outbox, keyed by (sink, idempotency key). |
| **Tools** | Tool sets registered with the gateway, still subject to grants and audit. |
| **Identity** | An optional identity provider: where users come from, how a handler user maps to the app's user, and minting credentials to act as them. At most one per instance. |
| **Settings and prompt** | Settings sections (pydantic models) and fragments added to the persona/instructions. |
| **UI** | Entries in the manifest returned by `GET /api/ui/manifest`: nav items, settings sections, thread types under Settings → Threads, Connections cards. The UI renders only what the manifest lists. |
| **Timer kinds** | Time-driven work (polls, expiries): `timer_kinds()` returns `TimerKind`s whose handlers the host's one timer calls with the tenant's `IntegrationContext` when one of the kind's due rows is due. See "Timer kinds" below. Integrations provide handlers; the host owns the loop. |
| **Lifecycle** | `background_tasks`: long-running coroutines under the instance lifespan. Not for time-driven work (use a timer kind); left only for the Crontal connector's tool discovery, which moves into context loading in role-split step 3. |
| **Dependencies** | `requires = [...]`. Startup fails with a clear error if a required integration is not enabled. |

Each instance builds its integrations, so nothing is shared between instances (see `multi-instance.md`).

## Configuration

- `HANDLER_INTEGRATIONS=crontal,email_intake,google`, or per instance `HANDLER_I_<NAME>__INTEGRATIONS`.
- Each integration reads its own namespaced config. Missing required config fails at startup and names the variable.
- Without `HANDLER_INTEGRATIONS`, an instance runs core only. The migration step below sets it explicitly on the existing instances.

## Packaging

Integrations live in-tree under `src/handler/runtime/integrations/<name>/`. A `handler.integrations` entry-point group for pip-installable integrations is added later, when a second external app needs one. Keep the integration interface free of imports from core internals so that move stays cheap.

## Initial integrations

| Integration | Contains today's | Requires |
|---|---|---|
| `crontal` | `crontal` thread type and `/ingest/crontal`; `crontal_api_*` and the curated `crontal.*` MCP tools; the notification sink (Crontal push) and later the task-status sink; the identity provider (users come only from Crontal); the Crontal persona fragment; `HANDLER_RETURN_TO_ORIGINS` handling | — |
| `email_intake` | `/ingest/email`, the sender gate (blocklist + DKIM), the `email` thread type and its instructions, the Cloudflare Worker docs | — (uses the identity provider for `act_as` if one is present) |
| `google` | Google OAuth, the encrypted connection secrets, the `gmail_*` tools, the Connections card | — |

| `mailbox` | (new, no legacy wiring) IMAP/SMTP mailboxes: the poller, the `mail` thread type, the `mail_*` tools, `/api/mailboxes`, and a Connections card. See `mailbox-integration.md` | — |

## Mailbox integration (IMAP/SMTP)

Added 2026-10-25 (`integrations/mailbox/`, design and operations in `mailbox-integration.md`). It is the first integration with no legacy counterpart, so it exists only where it is listed. On a Crontal-backed instance, use `HANDLER_I_<NAME>__INTEGRATIONS=crontal,mailbox`: setting the list switches Crontal to its integration, which phase 2 proved identical to the legacy wiring. Email intake and Google stay on legacy wiring in every case. Startup warns if `CRONTAL_*` is set but `crontal` is missing from the list.

It contributes the following:
- Thread type `mail`, added as a built-in `ThreadType` so stored threads always parse, with a `threads.mail` settings entry.
- A `mail.received` turn renderer (registered through `run.register_event_renderer`).
- Thread guidance and a tool hint (`instructions.register_guidance` / `register_tool_hint`).
- The `/api/mailboxes` router, the `mail_*` tool set, and the `mailbox.poll` timer kind (one due row per enabled mailbox; no task of its own).
- UI manifest entries for a `connection` card and the `thread_type`.

Its `mail_*` tables are created lazily in the instance's own store, so a core-only instance has none.

## Timer kinds

Added in role-split step 2 (`integrations/base.py` `TimerKind`, `runtime/timer.py`).
Time-driven work is a row in due work: `(tenant, kind, ref, due_at)`, in
`handler_control.due_work` on Postgres with a hub (tenant = schema name), or a `due_work`
table in the tenant's own store (SQLite, or a Postgres store with its own connections).
The host has one `HostTimer`; it claims due rows (`FOR UPDATE SKIP LOCKED` and a renewed
lease on Postgres), resolves the row's tenant, and calls the kind's handler.

```python
class MyIntegration(Integration):
    def timer_kinds(self):
        return [TimerKind("my.poll", self._poll, backfill=self._backfill, timeout_seconds=120)]

    async def _poll(self, ctx, item):      # one firing for item.ref
        ...
        return utcnow() + timedelta(minutes=5)   # next due time; None removes the row

    async def _backfill(self, ctx):        # at every tenant start: make rows match data
        await ctx.store.set_due("my.poll", "thing-1", utcnow(), keep_existing=True)
        await ctx.store.prune_due("my.poll", ["thing-1"])
```

- **Writing rows.** Through the tenant's store, in the same transaction as the work
  where possible: `store.due_statements(kind, ref, due_at | None, keep_existing=...)`
  gives `(sql, params)` pairs with `?` placeholders for the caller's own transaction
  (MailStore uses it in `put_mailbox` and `delete_mailbox`; call `store._due_written()`
  after the commit to wake the timer). `store.set_due(...)` and `store.prune_due(kind,
  keep_refs)` write in their own transaction. `None` as the due time removes the row.
- **Results.** The handler returns the next due time or None. If the work's own
  transaction rewrote the row while the handler ran (its `version` changed), the stored
  due time wins. An exception is recorded on the row (`attempts`, `last_error`) and
  retried with backoff (30 s doubling to an hour); it never stops the timer.
- **Names** are unique per instance and may not reuse a core kind (`schedule.fire`,
  `approval.expire`, `question.expire`, `inputs.release`, `retention.cleanup`).
- **Rows of a kind no longer enabled** for the tenant are removed when they come due;
  the kind's backfill recreates them if it comes back.

## Decisions

1. **Split into three integrations**, not one Crontal bundle: email intake and Gmail are useful without Crontal (e.g. on the personal instance).
2. **Identity:** in an instance with an identity provider (Crontal), users come **only** from that provider. Handler's own user list and per-user logins are disabled; the admin key stays for operators and debugging.
3. **Packaging:** in-tree now; entry points later.

## Migration (refactor plan)

1. Introduce the `Integration` interface, the registry, the manifest endpoint and the generic sink/outbox. The outbox comes from the notification-sink branch.
2. Move the Crontal, email-intake and Google code behind the interface with no change in behaviour. Existing routes, URLs, settings keys and stored data stay the same.
3. Make the UI manifest-driven: Settings → Threads, settings sections, Connections and nav.
4. Make identity provider-driven, with Crontal as the provider. Turn off handler-local users when a provider is present.
5. Set `HANDLER_INTEGRATIONS` on prod (`main`: `crontal,email_intake,google`; `bss`: `crontal`) and on the personal instance (none, or `google` later).
6. Tests: a core-only instance has no Crontal routes, tools, settings or UI; each integration works on its own; dependency errors are reported; the multi-instance host runs instances with different integration sets.
