# BSS Handler improvements: decisions and proposed design

Status: **initial authorized implementation/analysis sequence delivered**.
See [the handoff](bss-improvement-handoff.md) for deployed releases, validation,
verification limits and remaining investigative/product follow-ups.
Progress and takeover instructions: [BSS work handoff](bss-improvement-handoff.md).

Owner responses on November 8, 2026 supersede the earlier review's proposed
priorities and XLSX recommendation. This document records those decisions,
additional read-only schema findings, and a proposed implementation contract.
The owner subsequently asked to start the items sequentially and document work
for another agent. Item 1 remains investigation first; do not repair production
records without establishing the cause and a concrete repair scope.

Evidence: [recent chat review](../audits/bss-chat-review-2026-11-08.md), covering
nine other BSS chat threads and 61 runs. Database checks below were read-only.

## Decisions

| Item | Owner direction | Treatment in this design |
| --- | --- | --- |
| 1. Incomplete RFQ confirmed | Note for further investigation | Open investigation BSS-001; defer repair and implementation |
| 2. Spreadsheet generation | Implement a CSV writing tool for compatibility | Design `write_spreadsheet` producing CSV, plus working artifact delivery |
| 3. RFQ specification fidelity | Investigate the inquiry/model gap more deeply | Source-to-model comparison below; proposed schema and mapping changes |
| 4. Mailbox capabilities | Determine prompt versus tooling fix | Primarily tooling, supported by short capability-aware instructions |
| 5. Context cost | Design reuse of an initial system-prompt cache | Stable instruction prefix, provider caching, and task context management |
| 6. Route syntax | Fix | Normalize route descriptions and cache identities |
| 7. Task success reporting | Agreed | Separate execution status from evidence-backed task outcome |

## 1. Investigation note: incomplete RFQ confirmation (BSS-001)

**Open, investigation only.** Stirlings/Eddie RFQ 562 imported 16 lines; the
agent's source review counted 102 and notified the user about the missing 86.
The subsequent live database check found 16 listings, `intake_status=confirmed`,
and `intake_message=NULL`. Who confirmed it is unknown. Do not infer that the
agent performed the confirmation.

Investigate:

1. Reconcile all 102 source rows independently, including destination sections,
   notes and zero-quantity rows; establish the exact missing rows.
2. Identify the confirmation request, actor and timing through available request
   logs/audit records. Establish whether the warning was visible at confirmation.
3. Establish why extraction returned only 16: source preparation, truncation,
   model output, post-processing, or another limit. Existing limits are 60,000
   input characters and 100 extracted items, but those limits alone do not
   establish why this response stopped at 16.
4. Check whether a bid or subsequent workflow used the partial RFQ.
5. Propose completeness reconciliation and persistent warnings after the cause
   is established. Current confirmation checks only for at least one listing
   and clears the intake warning.

Deliverable: an incident timeline, verified row reconciliation and a scoped
repair proposal. Preserve source files and existing records. No reset,
re-extraction, line insertion, or status change is part of this document.

## 2. CSV spreadsheet writing and delivery

### Proposed tool contract

`write_spreadsheet(path, columns, rows, overwrite=false)` produces a `.csv` in
the thread workspace. Native OpenAI and Codex should share one implementation.
This is a deterministic writer, with no extra model call or general code execution.

| Input | Contract |
| --- | --- |
| `path` | Workspace-confined relative `.csv` path, including resolved symlink checks |
| `columns` | Ordered column definitions: machine key, displayed heading, scalar type |
| `rows` | Ordered objects keyed by column key; reject unknown keys, nested values and type mismatches |
| `overwrite` | Default false; explicit replacement prevents accidental artifact loss |

Use Python's CSV serializer, comma delimiter, correct quote/newline escaping,
CRLF record endings and UTF-8 with BOM for common desktop Excel compatibility.
Preserve Unicode. Missing values become blank cells; distinguish missing from
zero. Date columns use ISO dates. Numeric values use decimal points without
locale-specific grouping; identifiers remain text in the CSV data.
Do not evaluate formulas. The implementation prefixes formula-looking text and
leading control characters with an apostrophe and reports `literalized_cells`.
Typed negative numeric cells stay numeric. This also applies to phone numbers
beginning with `+`; CSV readers may display the apostrophe. Source data remains
unchanged. This explicit export convention avoids silently treating data as formulas.

Return a compact receipt: workspace path, MIME type, row/column counts, byte
count and content hash. Never echo the full table back into the agent context.
Choose explicit size/row bounds during implementation and return actionable
errors rather than truncating the exported data. Larger outputs can use a later
server-side export-from-records operation instead of making the model repeat
thousands of existing business rows in tool arguments.

### Compatibility limits and delivery

CSV is the agreed initial format. It has one table, not workbook tabs, styles,
merged cells or a portable formula model. A quotation CSV can contain computed
weights/totals and blank price columns, but cannot promise that entering prices
will automatically recalculate totals across Excel and Google Sheets. Multiple
tables require separate CSV files. Spreadsheet applications may infer dates or
drop leading zeros on open; identifier-sensitive imports should use text columns.
Do not add Excel-specific `sep=` rows or formula wrappers as a compatibility fix.

Writing is separate from persistence and delivery. The proposed flow is:

1. Write and validate the CSV locally.
2. Persist it through the existing Crontal upload/artifact mechanism when the
   task calls for a saved file; record the returned file ID.
3. Render a file-ID-based attachment/download action using Crontal's authenticated
   download service. Do not generate `sandbox:` links or bare API URLs that
   depend on the browser supplying an agent's bearer credentials.
4. Report `generated`, `saved`, or `ready_to_download` from actual receipts.
   Upload success alone does not verify a browser download.

Acceptance: Chinese text, commas, quotes, embedded newlines, blanks, zeros,
identifiers and numeric precision survive parse/write round trips; open/import
the representative CSVs in Excel and Google Sheets; click the attachment as a
normal authorized Crontal user; confirm existing access boundaries are respected.
The separate historic file-list click failure still needs browser reproduction.

## 3. Inquiry requirements versus Crontal's actual model

### Existing structure

A private received RFQ already has buyer identity/reference, received date,
source channel/file, destination, dates and intake status. Each demand listing
has a product, quantity/unit, price target, delivery date/address, payment terms,
quality requirements and original `source_text`. A stainless-steel product has
grade, shape, standard, condition, finish and JSONB `shape_attributes`.

Shape attributes are not unrestricted JSON at the API boundary. The received-RFQ
service validates them against the active `shape_definition` catalogue. That
catalogue is database-backed and extensible; the initial problem is not a lack
of JSON storage capacity.

Read-only inspection found **16 active stainless-steel shapes**. Relevant entries:

| Shape | Allowed attributes relevant here |
| --- | --- |
| `tube_straight` | `od`, `wt`, `length_type`, `fixed_length`, `end_finish`, `ndt`, `coating_or_lining` |
| `pipe_seamless` | `od`, `wt`, `schedule` |
| `pipe_welded` | `od`, `wt`, `schedule` |
| Square/rectangular hollow section | No active matching shape; `square_bar` is a solid bar and is not a substitute |

### Concrete source-to-record comparison

The following checks use the actual stored RFQ 563/564 listings, their original
tool inputs, and the live catalogue, rather than the fallback Python definitions.

| Buyer requirement | Current saved representation | Gap and consequence | Proposed treatment |
| --- | --- | --- | --- |
| RFQ 563: SHS 100×100×5 mm, 6 m, 22 pcs | Listing 578: `tube_straight`, `wt=5`, `fixed_length=6`; square size in name/source/quality notes | No width, height or hollow-section shape; structured geometry cannot distinguish the section reliably | Add square/rectangular hollow-section definitions with width, height, wall and length fields; map explicitly |
| RFQ 563: SHS 50×50×2 and 30×30×2 mm | Listings 579/580 have the same mapping pattern | Same gap for two further sizes | Same section model; preserve separate lines and quantities |
| RFQ 563: 1/2-inch Sch10S pipe, 6 m, 72 pcs; route unspecified | Listing 581: `tube_straight`, OD 21.34, WT 2.11; schedule in prose | No suitable route-unknown pipe shape; `tube_straight` cannot hold schedule or nominal size | Represent pipe family separately from optional manufacturing route, or define a supported route-unknown pipe shape; retain NPS and schedule |
| RFQ 563: conflicting “1/4 (1/2) Inch”, 33 pcs | Listing 582 retains original text and HOLD note, without inferred OD/WT | Correct abstention, but the ambiguity is only free text in this direct-create path | Persist a structured clarification flag linked to the original wording; do not infer a size |
| RFQ 564: 4-inch seamless Sch80S, 380 m | Listing 583: `pipe_seamless`, only `schedule=80S`; NPS in name/quality text | Nominal size is not a supported pipe attribute; schedule alone does not identify the dimensions | Add nominal size/system and explicit units; fill OD/WT only from verified source or an identified conversion standard |
| RFQ 564: bevelled ends, EN10204 3.1 MTC | End finish and MTC stored in quality text/name | `end_finish` exists for straight tube but not pipe; requirements are searchable only as prose | Add common pipe end-finish fields; keep narrative requirements and optionally typed certificate requirements |
| RFQ 564: CIF Qatar | Country plus quality text/source; no dedicated Incoterm in received-create header | Exact trade term and named port are not structured; Qatar alone is not a named port | Model trade term and named place when downstream quoting needs them; keep unspecified port unresolved |
| All 6 m fixed-length lines | `fixed_length=6`, listing unit `pcs` | Shape metadata says number but has no unit; quantity unit does not define length unit | Specify canonical dimensional units and preserve source units. Investigate consumers before calling existing values wrong or converting them |

The retries preserved useful wording; these are losses of **structured fidelity**,
not evidence that the original facts were deleted. Both RFQs remain needs-review.

### Proposed boundary between model changes and agent changes

1. **Catalogue/model:** define tubular geometry and consistent fields for
   dimensions, nominal sizes, end finishes and lengths. Initial attribute
   additions can use the existing catalogue/JSONB mechanism; new header fields,
   provenance or review flags may require schema migrations. Check UI rendering,
   matching, quote generation and exports together before finalizing names.
2. **API/tool contract:** expose the relevant live shape definitions and units
   before writes. Return precise mapping failures; validate locally or through
   a non-writing validation endpoint before a create call.
3. **Agent mapping:** never use a solid bar for a hollow section, invent a welded
   versus seamless route, or drop required fields just to obtain HTTP 201.
   Return a field-level mapping report: structured, preserved as text, unresolved.
   Unsupported products can remain explicitly unclassified and needs-review;
   the current direct-create schema permits an absent shape when no attributes
   are supplied.
4. **Provenance/review:** preserve original wording and identifiers separately
   from normalized values. Extraction has draft confidence flags, but the
   direct-create listing schema does not accept equivalent field flags; design
   a common persisted review representation. Source sheet/row and multi-document
   references are a useful next step, distinct from item 1's deferred incident.

Acceptance: RFQs equivalent to 563/564 retain all buyer facts with an explicit
mapping status, use correct shapes, distinguish unknown from inferred dimensions,
and round-trip through API/UI/quote/export without silently changing units.
Do not repair existing records until the model and mappings are reviewed.

Relevant Crontal sources: `schemas/received_rfq_schemas.py`,
`services/received_rfq_service.py`, `services/shape_definition_service.py`,
`models/shape_definition.py`, `models/product_types/stainless_steel.py`,
`models/listing_types/demand_listing.py` under `backend/src/api_server/`.

## 4. Mailbox issue: primarily tooling, with supporting prompt changes

A prompt can steer the agent to an available connector; it cannot give IMAP
the missing draft operations or guarantee one draft per inquiry.

| Layer | Proposed change |
| --- | --- |
| Capability discovery | Resolve available connections for the effective user/mailbox; distinguish OAuth configured from Gmail connected |
| Tool listing | Offer tools appropriate to that connection, with deterministic ordering per capability profile |
| Authorization | Continue checking credentials and permissions at invocation; hidden tools are not an authorization mechanism |
| IMAP draft tools | List/read/update drafts and reconcile Handler draft IDs with server mailbox UID/UIDVALIDITY; handle replace-by-append servers explicitly |
| Duplicate prevention | Persist one draft identity per normalized inquiry/reply purpose; upsert rather than create repeatedly; resolve copies across mailboxes through message IDs and inquiry references |
| Sender/signature | Resolve the mailbox and saved signature from the actual recipient/routing preference; do not guess based on connector name |
| Prompt | Short current capability summary; use IMAP for these mailboxes and say when an operation is unavailable |

Existing duplicate drafts may have been manually created or created by different
workflows; origin still needs verification. Do not automatically delete historical
drafts. A future deletion tool must preserve the user's deletion-approval policy.

Acceptance: disconnected Gmail is not attempted for an IMAP task; the same
inquiry in two mailboxes has one intended reply draft; edits retain identity and
the correct sender/signature; SMTP send is separately authorized and verified.

## 5. Prompt reuse and context cost

### What is already happening

The contact-group follow-up used 826,910 aggregate input tokens across eight
model requests, including 721,277 cached tokens (**87.2%**). Its individual
requests still carried roughly 102–106k tokens. It already benefited from
provider caching; adding a local system-prompt cache alone cannot remove that
history or its cached-input charges.

Handler already places many stable instruction components before changing
memory and uses provider Conversations for chat history. Current compaction
starts at approximately 200k context tokens, above this example. Scheduled
firings already start fresh sessions under the efficiency setting. Preserve
that behavior and let each job explicitly save its next-run checkpoint.

### Provider behavior verified against official documentation

OpenAI caches matching rendered prefixes, including tool definitions. A local
string cache saves application work but is not a provider token-cache handle.
For current GPT-6.1 Sol, cache routing is automatic; `prompt_cache_key` can
partition accounting. Responses supports explicit prefix breakpoints and
30-minute or 12-hour minimum lifetimes. Longer retention has a higher write
rate, so it is not automatically cheaper. Reused tokens still occupy context
and incur cached-input charges. Keep supplying the required instructions;
cache keys do not replace prompt content or session state.
Source: [official OpenAI prompt caching documentation](https://developers.openai.com/api/docs/guides/prompt-caching).

### Proposed architecture

| Layer | Contents and lifecycle |
| --- | --- |
| Compiled instruction template | Versioned common rules/persona and deterministic tool definitions; same exact content reused for compatible chat and automation profiles |
| Provider prefix cache | Reuse the compiled prefix naturally across eligible requests; evaluate an explicit boundary at the common developer content |
| Per-run suffix | Thread-type instructions, current actor, current time, relevant memory, job checkpoint, new task and permissions-derived capability summary |
| Active task state | Decisions, business IDs, source references, verified outcomes and unresolved questions; retrieve full originals when needed |
| Durable originals | Full chat/tool records, files and original email bodies remain available outside the compact model view |

Template identity should include instance, persona/rules revision, tool-schema
and capability-profile revision, and model-relevant settings. Keep timestamps,
run IDs, user text and changing memory outside the reusable common content.
Cache the compiled text locally only for render efficiency; invalidate it on
relevant changes. A provider cache key is an accounting/partitioning hint, not
a cross-tenant access control or a stored conversation ID.

Use compact, stable capability profiles rather than exposing every disconnected
integration just to preserve cache hits. Different profile/tool schemas may not
share a prefix; measure this tradeoff instead of assuming all chats and jobs
can share one identical prompt. Do not share another user's private task state.

Start with normal implicit caching plus a stable prefix. Evaluate a 12-hour
boundary only for genuinely repeated shared content. Do not add cache-warming
automations: they spend tokens even when no user work follows. Check installed
SDK support before choosing the exact request shape; its current type declarations
lag the official documentation for some retention options. Inspect serialized
requests to verify where SDK developer instructions and tool schemas actually land.

### Reduce context and calls as well as improving reuse

* Provide header/contact-focused mail queries for contact extraction. This is
  a purpose-specific view, not a replacement summary of an unread email body.
* Retain the user's full-read gate: summarize mail only after the system has
  read its complete body; preserve amounts, changes, commitments and quoted
  history. Keep force-full access to every original.
* At a verified task boundary, checkpoint the resulting IDs, decisions and
  unresolved issues. For grouping contacts, the active task needs the 19 IDs,
  not every earlier email used to find them.
* Evaluate earlier provider compaction or a verified task-state transition.
  Do not silently rotate sessions, drop raw history, or assume a summary retains
  every detail. If a recap is insufficient, retrieve the source. Set the threshold
  from measurements rather than changing the current 200k default in this plan.
* Add batch membership operations and normalize route lookup to cut model
  round trips. The existing 19 membership writes were already issued together;
  do not mistake them for 19 separate model requests.

Track per request: full input, cached input, cache writes by retained lifetime
where the provider exposes them, output, cost, model, latency, instruction
revision/profile and task. Distinguish prompt-component estimates from actual
provider billing; cache attribution by component is an estimate without provider
breakdown. Cache-write/read/ordinary categories must not be charged twice.

Acceptance: replay representative multi-turn tasks, a fresh conversation and a
fresh automation under the same instance/profile; measure cache reuse across
them without sharing history. Compare request counts, context sizes, total model
cost including checkpoint/compaction cost, and answer/task quality. Test a cold
cache and a changed persona/tool schema. No model downgrade is proposed here.

## 6. Normalize route syntax

Normalize `/contact/list/{contact_list_id}/contact/{contact_id}`,
the equivalent Flask `<int:...>` template, and a matching concrete path to the
same registered route for description and schema caching. Preserve methods and
API version; use catalogue converter information and specificity rather than
blind text substitution. Handle trailing slashes and query strings consistently.
Unknown or ambiguous paths should return a precise lookup error.

Keep description-template normalization separate from executing a request:
the write tool must still require concrete validated IDs, and route resolution
must not broaden access to hidden endpoints. A future stable operation ID can
reduce syntax dependence further but is not required for this initial fix.

Affected areas: Handler `gateway/crontal_catalog.py`, `gateway/crontal_api.py`,
and Crontal `controllers/agent_access_controller.py`. Acceptance includes the
exact failed contact and file examples, mixed parameter converters, static
routes versus variable routes, method mismatches and genuinely absent routes.

## 7. Report execution and task outcome separately

Keep `run.status` for execution lifecycle. Add a separately persisted task record
spanning runs, approvals and asynchronous jobs, with outcomes:
`in_progress`, `awaiting_approval`, `awaiting_review`, `blocked`, `partial`,
`completed`, or `canceled`. A run ending normally must not set task completion.

Completion evidence should reference actual receipts: created record IDs,
expected/actual item counts, saved artifact IDs, or SMTP success. Record limits
and verification level accurately: SMTP acceptance is not proof of recipient
delivery, and a saved file is not proof of a working browser download.

Persist linked run/job IDs, current outcome, concise reason, pending work,
verification evidence and update time. Derive deterministic evidence from tool
results where possible; an agent-written assertion alone is not verification.
Propagate asynchronous and approval results to the originating task. RFQ import
coverage checks belong to item 1's later investigation, not an implicit repair
authorized by this reporting design.

Carry tool error flags through gateway, MCP, SDK, stored transcript, usage ledger
and UI. Distinguish an error followed by successful recovery from an unresolved
failure. Validate against disconnected Gmail, failed/retried upload, partial
extraction, pending approval and a genuinely completed file/task. Historical run
outcomes should remain unknown unless there is sufficient evidence to classify
them; do not backfill success just because a run says `completed`.

## Proposed work boundaries after review

1. Keep BSS-001 open for investigation; no data repair in the initial tool work.
2. Implement CSV creation and authenticated delivery as one usable vertical slice.
3. Review the RFQ field comparison and agree on canonical units/shape names before
   modifying the catalogue, APIs or existing records.
4. Implement capability-aware mail tools, route normalization and task outcomes
   in independently reviewable changes.
5. Baseline prompt layout/cost, implement stable template reuse, then evaluate
   task checkpoints and compaction against quality regressions.

All acceptance criteria above are planned checks, not tests run for an
implementation. This change consists only of documentation and read-only research.
