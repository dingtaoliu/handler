# Tasks: background work view

A **task** is one run of a non-chat thread (`automation`, `email`, `crontal`,
`gmail`, and any future non-chat type), or a non-chat thread whose events are
waiting for a worker ("queued"). Tasks add no tables: they are derived from
`runs`, `threads`, `events`, `messages`, `run_steps` and `approvals`
(`store/tasks.py`, `tasks.py`, `web/tasks_api.py`). Every route works under an
instance prefix (`/i/<name>/api/tasks`) like the rest of the API.

## Status

| Status | Derived from |
|---|---|
| `queued` | Thread has unconsumed routed events, is not dead, and no live lease. Includes a retry after a failure (`retry_at`, `attempts`, `error`) |
| `running` | Run row `running` and the thread's lease is still held by that run (same lease version, not expired) |
| `waiting_approval` | Run is not failed/running, and one of its approvals is still `pending` |
| `succeeded` | Run `completed` (and no pending approval) |
| `failed` | Run `failed`/`cancelled`, or a `running` row whose lease lapsed (worker died: `error` says it was interrupted) |
| `dead` | Failed run that is the latest run of a thread marked dead (retries exhausted or not retryable) |

Approvals do not block a run: the gated tool returns "pending approval" and the
run finishes. The task then shows `waiting_approval` until a human decides; the
decision queues an `approval.resolved` event, which becomes a new task.

## API

`GET /api/tasks?type=&status=&since=&cursor=&limit=`

- `type`, `status`: comma-separated lists (`status=failed,dead`). Unknown values
  are 422. `chat` is not a task type.
- `since`: ISO-8601 with timezone; runs started (or queued) at or after it.
- `limit`: 1..200 (default 50) runs per page. `next_cursor` is set while more
  runs may follow; pass it back as `cursor`.
- Queued tasks are not runs: they lead the **first** page only (up to 50) and do
  not count toward `limit`.

```json
{"items": [{
  "id": "run_ab12…",              // run id, or "queued:<thread_key>"
  "run_id": "run_ab12…",          // null when queued
  "thread_key": "email:<root-message-id>",
  "thread_type": "email",
  "thread_title": "Fwd: Quote request",   // thread_names → summary → fallback
  "trigger_summary": "Email from Alice <alice@buyer.example>: Quote request",
  "trigger_events": [{"id": "evt_…", "type": "email.received", "source": "email",
                      "received_at": "…"}],
  "status": "succeeded",
  "run_status": "completed",       // raw runs.status (null when queued)
  "harness": "openai_agents", "model": "gpt-6.1-sol",
  "queued_at": "…", "started_at": "…", "finished_at": "…",
  "duration_ms": 8123,             // running tasks: elapsed so far
  "cost_usd": 0.0123,              // null = unpriced
  "usage": {"input_tokens": 1200, "cached_input_tokens": 0, "cache_write_tokens": 0,
            "output_tokens": 300, "reasoning_tokens": 0, "total_tokens": 1500},
  "summary": "I replied with …",  // last assistant text, ≤300 chars
  "error": null,
  "pending_approval_ids": [],
  "retry_at": null, "attempts": 0  // queued items only
}], "next_cursor": "eyJ…"}
```

Trigger summaries (first trigger event, plus "(+N more)"):

| Event | Summary |
|---|---|
| `email.received` | `Email from <original sender>: <original subject>`; falls back to the forwarder and the forward's subject |
| `automation.fired` | the automation's name; `(run manually)` for Run now |
| `crontal.notification` | `<entity_type> <entity_id> · <notification type>`, e.g. `rfq 42 · bid_submitted` |
| `approval.resolved` | `Approval <status>: <tool>` |
| other | `<type>: <name/subject/title/text>` or `<type> from <source>` |

`GET /api/tasks/{run_id}` returns one item. For **admin principals** (admin key,
or a user with role `admin`) it adds `steps`, the debug trace, sorted by time:

- `llm_call`: `model`, `usage`, `cost_usd`, `latency_ms` (approximate: usage is
  reported when the call ends, so this is the gap since the previous step);
- `tool_call`: `data.input` / `data.output` previews, `status`, `duration_ms`;
- `assistant_text`, `reasoning`, `error`, `retry`, …: as recorded;
- `approval` (from the approvals table): `data.args`, `action`, `decided_by`.

Strings in `data` are truncated to 2,000 characters (`… [N chars]`). Members get
the item without `steps`. Chat runs are 404 here even for admins: use
`/api/runs/{id}` for chat.

Non-chat threads are shared with all users of the instance, so any
authenticated user may list tasks.

## Live updates

The bus carries `task_updated` messages (`data` is the task item, without
`steps`):

- `running` when a run starts and its final status when it finishes
  (`RunExecutor`);
- `queued` (a small item: `id`, `thread_key`, `thread_type`, `status`,
  `event_type`, `queued_at`) whenever an event is routed to a non-chat thread.
  The stores call `on_routed` after `put` / `fire_schedule` commit; the server
  points it at the instance bus.

They arrive on the existing `/api/stream`. `GET /api/tasks/stream` is the same
SSE stream filtered to `task_updated` only (no chat content), for proxies such
as Crontal. Treat a message as a hint and refetch: approval decisions change an
older run's status without their own message (the resulting `approval.resolved`
event produces a `queued` one).

## Web UI

The **Tasks** tab (formerly Activity) lists tasks with type and status filters,
"Load more" paging and live refresh. Selecting a task shows its header (status,
trigger, model, duration, cost, error), **Open transcript** (the thread in Chat)
and, for admins, the step timeline; click a step for its data. Tasks waiting for
approval link to the Approvals tab. Ignored email still shows below.

## Gaps

- Retention: tasks live as long as runs, steps and messages; nothing prunes them.
- `/api/runs/{id}` (used by Chat for live progress) still returns steps to any
  user who can see the thread; only the tasks API restricts the trace to admins.
- Queued items are a snapshot (the first page only) and have no stable id across
  the transition to running; match them by `thread_key`.
- `latency_ms` for model calls is an approximation (see above).
