# Business task outcomes and receipt evidence

Item 7 separates the execution lifecycle from the result of the requested work.
A finished model run is still `run.status=completed`; it does not imply the task
was completed. The existing `/api/tasks` run projection and execution filters
remain compatible. Each item additionally exposes `execution_status` and a
`task_outcome` record. Historical runs without a record show `unknown`.

## Persistence and scope

Both SQLite and PostgreSQL add four tables at startup: `task_outcomes`,
`task_run_links`, `task_receipts` and `task_job_links`. No Crontal Alembic migration
or historical success backfill is required. Task identity is derived from the
trigger event batch, so retries share a task. Approval/question continuations
reuse their originating task when all events belong to the same one; new user
turns and scheduled firings start separate tasks. Mixed independent events are
not silently combined with an older task. Linked run IDs remain explicit.

The gateway records compact deterministic receipt facts for calls, including
validation failures, denied/expired approvals and actual approved execution.
These facts retain IDs, machine statuses, counts, hashes and bounded scalar
metadata; email bodies, document text, descriptions and full tool payloads are
not copied into this ledger. Successful dictionary results carry
`handler_receipt_id`; `task_get_outcome` provides recent receipts when needed.
It returns the last 50 plus older unresolved errors, avoiding an unbounded context
replay. Existing full tool output/history storage contracts are unchanged.

`task_report_outcome` records `in_progress`, `awaiting_approval`,
`awaiting_review`, `blocked`, `partial`, `completed` or `canceled`, with a reason,
remaining work and checks referencing actual receipt fields. Completed reports
require successful matching facts and no remaining work, unresolved errors,
pending approvals/answers or unfinished extraction. Failed-call recovery is
matched by tool and operation identity (method/path or the relevant message,
draft, file or schedule), so unrelated success does not erase a failure.
Changed payloads may recover the same validation failure. New tool activity after
a completed report requires renewed review. This tool only reports: it neither
approves/cancels actions nor repairs business data.

A normally finished run with no explicit outcome becomes `awaiting_review`.
Failed/interrupted runs show blocked. Pending approvals or questions override
completion. Process loss is checked against the latest linked run, so a dead
worker does not leave a misleading completed outcome. Reporting failures after
an action are logged and never converted into a retryable business-action error.

## Asynchronous work

Crontal received-RFQ receipts link by merchant-local RFQ ID. Subsequent tool reads
of that RFQ update the original task's job state, even from another task in the
same merchant instance. `extracting` remains in progress, `extraction_failed` is
blocked, and `needs_review` awaits review. Even `confirmed` does not establish
source coverage or automatically complete the task. The actual latest receipt
reference is retained. No polling automation or production data repair is added.
Unknown asynchronous 202/pending receipts remain unverified until the same
operation has a terminal receipt. Other services need explicit resource adapters
before automatic cross-task job correlation can be claimed.

## Verification boundary

A matching count/ID/status is server-verified against the stored tool response.
The server cannot establish that an agent chose every correct success criterion
or that the upstream service extracted all source rows. Instructions explicitly
require source coverage/count comparisons, not merely a created record ID.
The API labels verification as receipt facts only; correctness and source scope
still need agent review. SMTP acceptance is not recipient delivery, and creating
an artifact is not proof of a browser download. Unknown or unverified work must
remain awaiting review; no outcome can replace the RFQ coverage investigation.

MCP error metadata remains intact in native SDK results. Codex MCP `isError`,
transport errors and nonzero command exit codes now normalize to error events;
recorder steps, transcript blocks and the tool usage ledger retain that flag.
Historical false error flags are not rewritten.

## UI and validation

Handler's console and Crontal's English/Chinese task panels show execution and
business outcome separately. The old `succeeded` execution label now reads
“Run completed” with a neutral tone. Completed business outcomes get the positive
badge; unknown historical outcomes never get a completed-task badge.

Tests cover both database backends, count mismatches/foreign receipts, normal
execution without verification, same-operation retries versus unrelated success,
real gateway approval execution and denial, multi-run continuation identity,
RFQ extraction progress/16-versus-102 coverage, generic async receipts, and error
propagation into steps/transcripts/usage. Crontal proxy regression tests and
frontend production build/i18n checks are also required before rollout.

Reporting adds two tool schemas, a short instruction component and compact receipt
IDs; a report may require a model round trip. `task_get_outcome` is optional when
the preceding result already contains the receipt. This is a quality/visibility
tradeoff, not a claimed token saving. No additional agent, model downgrade,
recurring automation or model-based outcome classifier is added.
