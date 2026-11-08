# Detailed usage ledger — 2026-11-08

Adds durable tracking by tool call, thread, user, model, UTC date, run and trigger.
The original `/api/usage` endpoint remains compatible; detailed reports use new
ledger rows only and do not fabricate historical attribution.

## Recorded data

- An immutable run-attribution snapshot captures instance, thread owner, initiating
  user, attributed task user, trigger types and event IDs. Chat work belongs to the
  triggering user; background work uses the configured actor/thread owner. Mixed
  user batches and unowned work remain shared. Approval/question continuations keep
  original task attribution while recording the responder separately.
- Each native completed/incomplete/failed Responses request with returned usage is
  persisted as it arrives, including response ID, actual/requested model, service
  tier, status, timestamps, token/cache/reasoning counts, harness, estimated USD cost
  and the selected pricing rates/date/context tier. SDK-only compaction or residual
  usage remains explicitly marked fallback, with no invented response ID.
- Request IDs deduplicate before charging totals. Helper model requests carry the
  actual tool-call ID/name and source response ID. Main agent requests remain in an
  explicit `__agent__` bucket rather than being charged again to every tool.
- Each tool invocation stores its call ID, generating response ID, name, start/end,
  status, text input/output token estimates, sanitized text bytes and image count.
  No prompt or tool body is copied into the ledger.
- Tool output cost estimates assume exactly one uncached text read at the run model
  rate. They exclude image tokens and repeated/cached history and are never added
  to direct model costs. Ordinary tools have no direct model charge unless they
  actually invoke a metered model (for example `bounded_model_task`).

## Reporting

All new routes require instance admin authentication; user impersonation is denied.
Filters include ISO `since`/`until` (inclusive/exclusive UTC), `days` (default 30),
`thread_key`, `user_id`, `tool_name`, `run_id`, plus `model` on model-record reports.

- `GET /api/usage/records`: paged request records with immutable attribution and
  pricing snapshots, plus safe source-request/timestamp-quality fields.
- `GET /api/usage/tool-calls`: paged individual tool-call metrics and attribution.
- `GET /api/usage/breakdown?group_by=user,thread,tool,model,date,trigger`: aggregate
  over any unique selection of those dimensions. Costs count once in each view.
- `GET /api/usage/tools`: direct model spend plus separate tool-context estimates.

The console Usage page adds user/thread/tool cost tables and a separate tool-output
estimate table. Shared/unknown attribution and partially priced totals are explicit.

## Limits

Costs are estimates using the configured standard model token rates, not invoice
reconciliation. Unsupported returned service tiers and unknown prices retain tokens
but have unpriced costs. Hosted-tool per-call fees (including web search) and
external service charges are excluded. Pricing snapshots preserve historical rate
selection even if the configured table changes later.

Capture cannot recover provider usage never delivered before a hard disconnect.
Tool calls interrupted before a finish event retain their last observed running
state. Deferred approved external actions use the existing gateway audit; the tool
ledger records the model invocation, not a second invocation on later approval.
Codex/other harnesses remain at their available usage granularity, explicitly tagged
`harness_reported`; native Responses calls provide request-level metering.

Reports page through all matching records without silently truncating totals.
They currently aggregate in memory; SQL aggregates/keyset pagination are a future
optimization for very large histories. Per-instance database schemas isolate data.

## Validation and rollout

Final combined runtime suite: **1,198 passed, 2 skipped**, including the concurrent
v49 changes, real SQLite/Postgres ledger tests, native harness metering and the
individual tool-call endpoint. JavaScript syntax and Docker image imports/ledger
contracts passed before release.
A real, isolated native Sol request persisted exactly one provider-reported record
with response ID, default tier, 4,851 input / 5 output tokens, and a pricing snapshot
($0.012176 estimated). The temporary OpenAI Conversation was deleted. No production
thread or customer conversation was modified by this smoke test.

Database backup: `b028` on `hephaestus-backend-test`. Previous live release at
rollout preparation: `v49` (rollback target). Schema changes are additive.

Released as **v50** on `crontal-agent` (image `3fe31c8d12ee`, runtime commit
`745bd2b`, including merged remote `rebuild` changes through `e29c629`). All three
instances (`main`, `bss`, `yeup`) passed health, records, tool-calls, combined
breakdown, tools and legacy usage endpoint checks. Missing/incorrect admin keys
returned 401. Rollback: v49; backup: b028.
