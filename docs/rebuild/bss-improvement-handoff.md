# BSS sequential improvement handoff

Owner authorized starting the seven items sequentially after reviewing
[the design](bss-improvement-design-2026-11-08.md). Work autonomously, preserve
quality, and update this file at each completed item and before stopping.
The owner reported 41% remaining in their usage window; agents cannot read the
remaining quota. Do not create background jobs merely to keep this work running.

## Current state

- The authorized initial implementation/analysis sequence is delivered.
  Investigative/product follow-ups below remain open.
- Handler **v58**, code `b0a7971`, image `c72d79a1a0fa` (2,151,469,157 bytes).
- Crontal **v122**, code `0c7a849`, image `5f523db6bc51` (741,323,571 bytes).
- Backup **b037**; rollback Handler **v57**, Crontal **v121**. Protected config
  snapshots and read-only verification scripts are in `/tmp/bss-item7-release/`
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
| 3. RFQ fidelity | Requested deeper analysis complete | Live definitions and actual stored lines compared in design section 3. No matching hollow-section shape; missing pipe attributes and unit metadata. Model/schema migration and historical repairs remain separate reviewed follow-ups |
| 4. Connector/draft tools | Deployed v55 | See [mail tooling](mail-capabilities-and-drafts.md). 1,242 passed/2 skipped; latest focused 37 passed; drafts module Pyright clean. Live BSS user 2 inventory hides disconnected Gmail, exposes new draft tools; all three health/auth checks passed. No customer drafts changed or SMTP messages sent for testing |
| 5. Context/caching | Deployed v56 | Released v56; all three health/auth checks passed. At v56 BSS chat/automation shared a 3,613-token stable prefix; latest v58 metrics are below. See [context reuse](context-reuse.md). 1,245 passed/2 skipped; prompt modules Pyright clean. Effective per-run tool guidance, common prefix, bounded local compilation, prefix metrics and optional mail field projection. Implicit caching/200k compaction unchanged; cost savings still require comparable production measurements |
| 6. Route normalization | Deployed v57 | See [route normalization](route-normalization.md); 55 focused checks and 1,247 full suite passed, 2 skipped; module Pyright clean |
| 7. Task outcomes | Deployed Handler v58 + Crontal v122 | See [task outcomes](task-outcomes.md). Persisted reports, receipt facts, linked runs/approvals/RFQ progress; UI separates execution from outcome. Unknown historical outcomes stay unknown. New reporting does not prove source coverage by itself |

## Verification/deployment

Handler tests: `PYTHONPATH=src .venv/bin/python -m pytest tests/runtime -q -p no:cacheprovider`.
Current **1,261 passed, 2 skipped**. Run focused tests as changes are made and
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
configuration secrets. Backup before deployment. Last backup **b037**; rollback
Handler v57 / Crontal v121. Excel/Sheets interactive imports and browser-click testing have
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

## Follow-up work for a future agent

1. Keep BSS-001 open: identify who confirmed RFQ562 and recover raw extraction
   request/response if possible. The initial investigation proves 102 source rows
   versus 16 stored, but not the exact model omission mechanism. Do not repair the
   historical RFQ/quote without separately reviewed scope and provenance.
2. Review the RFQ shape/attribute/unit/provenance proposals with the owner before
   selecting migrations or backfilling records. Item 3 authorized deeper analysis.
3. Compare detailed request/cache/token/tool-result ledger measurements on similar
   tasks. Evaluate earlier checkpoints/compaction only with source coverage and
   multi-turn quality tests; 200k compaction and the main model remain unchanged.
4. Test draft replacement against a dedicated real mailbox (fake IMAP/SMTP tests
   passed); do not use customer mail for a smoke test.
5. Extend async resource adapters beyond received RFQs if needed. Unknown jobs
   stay unverified. Receipt facts cannot establish complete source coverage or
   recipient delivery; keep those verification limits explicit.
