# Recent BSS chat review — 2026-11-08

Read-only review of nine recent BSS chat threads, excluding the previously
investigated Stirlings inline-image incident. The sample spans November 4–8,
2026 and contains 61 runs, all recorded as `completed`. Production records and
tool inputs/results were read directly; no customer records or settings changed.
These chat runs predate v53's PDF transport fix. Older approval examples also
predate the recent approval/efficiency changes; they are not evidence those
historical policies remain active.

The sample records 15,806,679 aggregate input tokens and $7.0217271 in model cost
estimates. These are cumulative per-request counts, including repeated and cached
context, not unique document tokens. They exclude external/hosted service charges.

## 1. Incomplete RFQ import can become confirmed (highest priority)

Thread `chat:2:e46e346117be41bb8a52c23f2e327ce2`, November 8:
`run_33d6993408024bc5` created RFQ 562 from Eddie's original spreadsheet via
`POST /api/v1/rfq/received/extract`. The follow-up automation
`sch_e8b767d6241e401a` correctly identified only 16 imported lines against 102
source lines (63 Fremantle, 20 Brisbane, 19 Melbourne), notified user 2, and
recorded the missing 86 lines. It stopped after reporting rather than completing
the import. The extraction result itself said additional rows were omitted due
to source length.

Independent live database checks during this review found RFQ 562 **confirmed**,
with **16 demand listings** and `intake_message=NULL`. The source-line count is
from the agent's source review; the present listing count and status were checked
directly in the business database. Who confirmed it was not established.

Current Crontal `received_rfq_service.confirm_received_rfq` checks only that
extraction is no longer running and at least one listing exists, then clears
`intake_message`. The extraction service has a 60,000-character input cap and
100-item result cap, with no chunked completion; a 102-line RFQ exceeds the latter
even if the model returns every line. Those limits are confirmed code facts, not
proof of the precise reason this particular response stopped at 16.

Proposed fix: process spreadsheet rows in bounded chunks; keep row/destination
provenance and expected/imported counts; require reconciliation or an explicit
recorded exception before confirmation; retain coverage warnings after review.
Give incomplete imports a durable task that can resume, rather than only a memory
note and terminal notification. Avoid blindly rerunning extraction and duplicating
existing rows.

## 2. Generated files are not reliably delivered to the user

Thread `chat:2:6788b67c22b1486cb14eda04895fb5f0`, November 4–6:
`run_c2b42e755f69438d` generated the Morris quotation as Excel XML and linked to
`sandbox:/exports/MORRIS_S261009_WA1007_Metric_Quotation_20261104.xml`.
It was later uploaded as Crontal file 892. The user repeatedly reported download
and file-click failures. Another generated prospect list was CSV (file 925).
The agent eventually suggested a raw `/api/v1/files/892` browser URL while
acknowledging it had not verified browser authentication.

Current native `write_file` only writes UTF-8 text; it cannot produce a binary
XLSX workbook. The frontend Markdown renderer has no `sandbox:` artifact resolver,
and the file download service explicitly supplies a bearer header. A successful
agent-side API fetch therefore does not establish that the suggested browser
link works. The separate reported file-row click failure was not reproduced in
a browser and its exact frontend cause remains unverified.

Proposed fix: add a constrained XLSX writer and first-class artifact/file links
that use Crontal's authenticated download handler. Test the user's click path,
not just storage success or API status. Use durable task/file references so
continuing in another chat does not require the user to reconstruct the task.

## 3. Structured RFQ data loses useful dimensions

Same November 8 business thread, `run_1a145f8d9bd540ac`: both Amardeep creates
initially returned HTTP 400 for unsupported `shape_attributes`.
The retry created RFQs 563 and 564 successfully (five and one listings).
For RFQ 563 the retry removed square-section width, height and section shape;
those facts survive in product names, `source_text` and quality notes instead.
For RFQ 564 nominal size and bevelled ends moved into prose, leaving only the
schedule in structured shape attributes. Both RFQs remain `needs_review`.

This is recoverable information, not evidence the facts disappeared entirely.
However, filters, matching and later quote generation cannot depend on structured
dimensions that were never stored. Square sections also use `tube_straight`
because the agent lacked a suitable supported representation.

Proposed fix: expose valid shapes/attributes before writes, support square/hollow
section geometry, and report explicit mapping gaps. Preserve unsupported facts
and review holds rather than silently treating API acceptance as full fidelity.

## 4. Large mail results remain in context during unrelated follow-ups

Thread `chat:2:93d4dc895fcf442a9dd62de799dd2691`, November 8:

| Operation | Model requests | Aggregate input tokens | Cached input tokens | Estimated model cost |
| --- | ---: | ---: | ---: | ---: |
| List grade/standard folder | 5 | 102,206 | 79,094 | $0.0684 |
| Find and create 19 Stirlings contacts | 10 | 692,378 | 614,031 | $0.2898 |
| Group those existing contacts | 8 | 826,910 | 721,277 | $0.3486 |

The group operation carried 101,595–105,612 input tokens per request despite
needing existing contact IDs. Earlier contact research returned a 72 KB mail
read and multiple 18–39 KB search pages. Group-membership output was already
projected to small receipts. The 19 membership writes were issued together,
so this was **not** 19 separate model turns. Most requests in the group operation
were route discovery, intermediate decisions and checkpointing.

Proposed fix: contact/header-focused search that preserves an explicit path to
the full originals; context compaction at verified task boundaries; and a batch
membership operation. Preserve the user's existing rule that unread email bodies
must not be substituted with summaries. Benchmark the actual model context and
request count; reducing response bytes alone will not remove older context.

## 5. Agent-visible connector capabilities are misleading

`gmail_search` failed because Gmail was not connected in the Lucas search and
the Stirlings quotation analysis. In the duplicate-draft thread
`chat:2:bfdef4698fcb4a11b55a5cce02e4d216`, `gmail_list_drafts` hit the same condition
while the user's actual mailboxes were IMAP/SMTP connections.

Current MCP tool listing exposes the registered tool catalog without a
per-user connection filter. Gmail registration depends on OAuth configuration,
which is different from the requesting user having connected Gmail.
The IMAP tool set can create/send drafts but lacks agent tools to list, update
or delete them. Thus the user's reported duplicate drafts could not be inspected
or replaced. The precise origin of both drafts was not independently established;
saving a preference to make one draft does not provide deterministic deduplication.

Proposed fix: advertise effective per-user capabilities, gate unavailable Gmail
tools, and add IMAP draft listing/updating with stable message IDs and a
cross-mailbox inquiry key. Keep the user's deletion approval policy intact.

## 6. Route syntax creates avoidable discovery retries

The contact-group run described a route using `{contact_list_id}` and received
"No Crontal endpoint matches". It later succeeded using the same route as
`<int:contact_list_id>`. The file-organization run had the same problem describing
`PATCH /api/v1/files/{file_id}`.

The backend matcher currently accepts Flask templates or concrete paths, while
the compact prompt catalog renders parameters with braces. This is a contract
mismatch between tools, not an absent endpoint.

Proposed fix: normalize brace, Flask and concrete forms to a canonical route
identity for descriptions and caching; preferably expose stable operation IDs.

## 7. Successful run status does not measure task completion

All 61 sample runs were `completed`, including runs whose tool calls failed or
whose deliverable remained unusable/incomplete. The duplicate-draft Gmail result
contained "Gmail is not connected" while the stored tool result had
`is_error=false` and the step was `ok`. Other newer Gmail errors were classified
as errors, so this is not evidence every current error path is broken.

Proposed fix: keep execution status separate from task outcome, and record
`blocked`, `partial`, `awaiting_review`, and verified completion with linked
artifacts or business records. Ensure structured tool error flags survive the
MCP/SDK/transcript/reporting path. Report recovery separately from failure:
the two missing-path uploads in `run_731ebd1f3f244b2e` were correctly retried and
ultimately succeeded, so they are not evidence of lost archived attachments.

Recommended order: incomplete-import/confirmation integrity; working spreadsheet
delivery; schema fidelity; connector/draft capabilities; then context size and
route normalization. No implementation or deployment was performed for this review.
