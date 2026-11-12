# BSS sequential improvement handoff

Owner authorized starting the seven items sequentially after reviewing
[the design](bss-improvement-design-2026-11-08.md). Work autonomously, preserve
quality, and update this file at each completed item and before stopping.
The owner reported 41% remaining in their usage window; agents cannot read the
remaining quota. Do not create background jobs merely to keep this work running.

## Current state

- The authorized initial sequence and the tubular/coverage follow-ups are
  delivered. Historical repair, page-batched document continuation and further
  measured cost optimization remain follow-ups; see the current notes below.
- Handler **v58**, code `b0a7971`, image `c72d79a1a0fa` (2,151,469,157 bytes).
- Crontal **v124**, runtime `dab8c97`, image `4bc02f43ac46` (741,381,019 bytes).
- Latest backup **b039**; current Crontal rollback **v123**, Handler stays **v58**. Protected config
  snapshots and read-only verification scripts are in `/tmp/bss-coverage-release/`
  (directory mode 0700, configs 0600). Never commit/print those configs.
- Handler repo `/Users/dannyliu/dev/repos/handler`, branch `main`; Crontal repo
  `/Users/dannyliu/dev/repos/hephaestus`, branch `main`. Runtime/frontend commits
  pushed. Release-note commits are docs only and need no new container release.
- Preserve unrelated Handler `.claude/`, `docs/incidents/` and
  `docs/rebuild/multi-tenant-design.md`. Reinspect worktrees before future edits.

## Item status and next steps

| Item | State | Next action |
| --- | --- | --- |
| 1. Incomplete RFQ | Initial investigation complete; follow-ups open | See [investigation](../audits/bss-rfq562-investigation.md): independently verified 102 rows, only first 16 imported, downstream draft quote 232; actor/exact omission mechanism unknown; no production repair |
| 2. CSV tool/delivery | Deployed v54 | See [CSV tool](csv-spreadsheets.md). 1,230 passed/2 skipped; 17 focused checks; new modules Pyright clean. Live synthetic S3 artifact downloaded through BSS HTTP endpoint with correct Unicode/values and owner; cleaned up. Tool advertised with allow decision; all three instances healthy |
| 3. RFQ fidelity | Deployed Crontal v123; coverage safeguards v124 | Additive pipe/SHS/RHS geometry, source dimension/piece-length units, non-writing mapping validation and filtered catalogue; future spreadsheet row reconciliation/confirmation gates. See November 12 evidence below. No historical product/RFQ repair |
| 4. Connector/draft tools | Deployed v55 | See [mail tooling](mail-capabilities-and-drafts.md). 1,242 passed/2 skipped; latest focused 37 passed; drafts module Pyright clean. Live BSS user 2 inventory hides disconnected Gmail, exposes new draft tools; all three health/auth checks passed. No customer drafts changed or SMTP messages sent for testing |
| 5. Context/caching | Deployed v56 | Released v56; all three health/auth checks passed. At v56 BSS chat/automation shared a 3,613-token stable prefix; latest v58 metrics are below. See [context reuse](context-reuse.md). 1,245 passed/2 skipped; prompt modules Pyright clean. Effective per-run tool guidance, common prefix, bounded local compilation, prefix metrics and optional mail field projection. Implicit caching/200k compaction unchanged; cost savings still require comparable production measurements |
| 6. Route normalization | Deployed v57 | See [route normalization](route-normalization.md); 55 focused checks and 1,247 full suite passed, 2 skipped; module Pyright clean |
| 7. Task outcomes | Deployed Handler v58 + Crontal v122 | See [task outcomes](task-outcomes.md). Persisted reports, receipt facts, linked runs/approvals/RFQ progress; UI separates execution from outcome. Unknown historical outcomes stay unknown. New reporting does not prove source coverage by itself |

## Verification/deployment

Handler tests: `PYTHONPATH=src .venv/bin/python -m pytest tests/runtime -q -p no:cacheprovider`.
Current default **1,261 passed, 4 skipped**; two new opt-in real Dovecot tests passed separately. Run focused tests as changes are made and
the complete suite before a release. New modules should pass Pyright using
`--pythonpath .venv/bin/python`; the native harness already has unrelated errors.
Commit/push finished work and follow `HANDOFF.md` deployment instructions.
Do not deploy partial unverified behavior. No deployment is part of item 1's
read-only investigation alone.

Production config: capture `heroku config --json -a crontal-agent` internally;
never print values. PostgreSQL `DATABASE_URL`, BSS schema
`HANDLER_I_BSS__HANDLER_DB_SCHEMA`. Use read-only transactions for investigation.
Recent audit snapshots: `/tmp/bss-chat-review-20261108/{01..09}.json` (sensitive
customer content; do not commit full dumps). These are conveniences, not durable
dependencies: record findings and reproduce read-only queries as needed.

Latest release health checks use `/i/{main,bss,yeup}/healthz` and authenticated
`/api/settings/threads`; unauthenticated settings must return 401. Use
`heroku releases` to inspect version; avoid `releases:info`, which can expose
configuration secrets. Backup before deployment. Latest backup **b039**; current Crontal rollback **v123**, Handler unchanged **v58**. Excel/Sheets interactive imports and browser-click testing have
not been run; API isolation and compatibility serialization were tested.

## Final rollout evidence (November 9)

- Full Handler suite: **1,261 passed / 2 skipped**; new outcome modules Pyright
  clean. New receipt/approval/async tests passed on SQLite and PostgreSQL.
- Crontal: **113 relevant backend tests passed**; frontend production build and
  English/Chinese i18n check passed. Docker reused unchanged backend layers.
  Alembic upgrade was a no-op; current revision `x9ledger20261104 (head)`.
- All three Handler health/auth checks passed; wrong-instance admin key returned
  401 and unknown instance 404. New tools and all four schema tables verified.
- Both Crontal users 1 and 2 received new outcome fields through the authenticated
  proxy (200). Historical samples remain `unknown`. Served production JavaScript
  contains the new panel and “Run completed” label. No interactive browser test.
- Read-only live Crontal contracts resolve the exact file/contact brace aliases
  that failed before. No business writes, customer draft changes, SMTP sends or
  model turns were used for rollout testing.
- BSS chat instructions: **4,959 local estimated tokens**, with a **3,764-token
  common prefix (75.9%)** shared with automation. See the complete
  [component counts and percentages](../audits/bss-prompt-components-2026-11-09.md).
  Tool schemas/history are excluded. Do not claim a production cost reduction yet.

## November 12 continuation and takeover

- Crontal v123 (`f2d766a`, image `d4fc1df55f8b`, backup b038) delivered the
  tubular catalogue/unit/preflight changes. All **665 backend tests / 22
  subtests**, frontend build/i18n and PostgreSQL migration checks passed. Existing
  custom metadata/inactive definitions were preserved. Typed commercial fields
  outside that scope remain unsupported; retain source/quality text rather than
  invent supported structured fields.
- Crontal **v124** (`dab8c97`, image `4bc02f43ac46`, 741,381,019 bytes), backup
  **b039**, migration **z1coverage20261112 (head)**, adds deterministic CSV/XLS/XLSX
  inventories, bounded row batches, strict reconciliation, exact source quantity/
  unit preservation, transactional line/evidence persistence, confirmation/quote
  gates and trusted confirmation attribution. Full evidence is opt-in; ordinary
  reads return a compact summary. See [coverage design/status](rfq-coverage-next-design.md)
  and sibling Crontal `docs/rfq-source-coverage-work.md` for contracts and limits.
- **684 backend tests / 22 subtests**, frontend build/i18n and disposable
  PostgreSQL upgrade/downgrade/upgrade passed. Live health, ownership 200/404/401,
  new frontend bundle, all Handler instances' health/isolation, and both users'
  proxies passed. Historical RFQs562–564, their lines and quote232 are unchanged;
  historical coverage/confirmation audit remains NULL. No new customer model turn
  or mail was used for verification. Crontal rollback **v123**; keep additive
  audit columns. Handler runtime remains **v58** with no new runtime release.
- Read-only XLS reproduction found all sections in the 27,202-character converted
  source, plus the original 1,556-character pasted context. It does not reproduce
  a 60k truncation. AgentLog331 identifies **gpt-5.6-luna**, 15,140 input / 6,363
  output tokens and BSS AI extraction identity, not the confirmation actor.
  Historical raw request/response/finish reason remain unavailable. Protected
  repair inventory prepares 16 preserved + 86 proposed rows, not an executed repair.
- Two opt-in real Dovecot 2.4.4 TLS tests passed with disposable synthetic mailboxes:
  draft reuse/update, Unicode/signature/attachment preservation, unrelated deleted
  draft retained, external edits refused, no SMTP transport. Default Handler suite
  **1,261 passed / 4 skipped**; no runtime changes/redeploy needed for these tests.
- The November 9 zero-cohort observation is superseded: **1,238 requests / 190
  distinct BSS runs**, **$19.6387** estimated model cost since v56. Same-model
  chat cost/run increased; Terra mail cost/run is roughly flat. No overall saving
  can be claimed. Four bounded helpers cost **$0.0737**. A new hourly inquiry-filing
  schedule contributes **60 runs / 451 requests / $3.6312**; it was not disabled.
  [Updated measurements and tool-token percentages](../audits/bss-cost-followup-2026-11-09.md).
- HTTP202 audit found only existing received-RFQ create/retry business jobs; their
  resource IDs already have a task-outcome adapter. Legacy MCP 202 is transport
  acknowledgement. No extra poller or job adapter was added.

## Follow-up work for a future agent

1. Keep BSS-001 open: exact historical omission/confirmation mechanism remains
   unresolved. Do not assert the 60k cap caused it or invent the confirmation
   actor. No raw historical provider payload was retained. A repair needs reviewed
   products/source links and quote provenance, using the protected local inventory
   only as preparation. RFQ562/quote232 were not modified.
2. New spreadsheet coverage is preventive. Document extraction still has the
   existing 12-page scanned-PDF and 100-item caps, now blocked when partial. Design
   page-batched continuation and reviewed in-place partial recovery separately;
   do not add an override that silently accepts missing source. Handler's native
   PDF reader already preserves all pages and is a separate implementation.
3. Investigate the new hourly automation's empty/redundant firings and repeated
   API/memory reads before changing behavior. Preserve pending work/amendments and
   verified receipts. Use outcome/task complexity matched cohorts; keep full unread
   mail and full-read-gated summaries. The main model/200k compaction threshold were
   not changed in this follow-up. No warming jobs or extra agents were introduced.
