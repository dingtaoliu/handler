# BSS sequential improvement handoff

Owner authorized starting the seven items sequentially after reviewing
[the design](bss-improvement-design-2026-11-08.md). Work autonomously, preserve
quality, and update this file at each completed item and before stopping.
The owner reported 41% remaining in their usage window; agents cannot read the
remaining quota. Do not create background jobs merely to keep this work running.

## Current state

- Active item: **7 — persisted task outcomes, evidence and continuation links**.
- Runtime deployed: Handler **v57**, code `4668eab`, image `626e2f1c4455`.
- Handler repo `/Users/dannyliu/dev/repos/handler`, branch `main`.
- Crontal repo `/Users/dannyliu/dev/repos/hephaestus`; existing unrelated edits
  may be present. Inspect and preserve them before any changes.
- Unrelated Handler `.claude/` stays untouched. Existing untracked
  `docs/incidents/` contains prior PDF incident diagnostics; preserve it.
- Initial audit and design were untracked documents before this work began.
- Item 7 implementation is verified and preparing release in Handler and five Crontal frontend files.
  See [task-outcomes.md](task-outcomes.md). Final full suite passed 1,261/2 skipped; new modules Pyright clean,
  both SQLite and PostgreSQL receipt/approval/async cases passed. Commit/push
  and deploy Handler first, then Crontal. Crontal build + i18n passed,
  113 relevant backend tests passed; current Crontal v121 image f343a05cd85a.
  No production business records changed; v57 all three health/auth checks passed.

## Item status and next steps

| Item | State | Next action |
| --- | --- | --- |
| 1. Incomplete RFQ | Initial investigation complete; follow-ups open | See [investigation](../audits/bss-rfq562-investigation.md): independently verified 102 rows, only first 16 imported, downstream draft quote 232; actor/exact omission mechanism unknown; no production repair |
| 2. CSV tool/delivery | Deployed v54 | See [CSV tool](csv-spreadsheets.md). 1,230 passed/2 skipped; 17 focused checks; new modules Pyright clean. Live synthetic S3 artifact downloaded through BSS HTTP endpoint with correct Unicode/values and owner; cleaned up. Tool advertised with allow decision; all three instances healthy |
| 3. RFQ fidelity | Requested deeper analysis complete | Live definitions and actual stored lines compared in design section 3. No matching hollow-section shape; missing pipe attributes and unit metadata. Model/schema migration and historical repairs remain separate reviewed follow-ups |
| 4. Connector/draft tools | Deployed v55 | See [mail tooling](mail-capabilities-and-drafts.md). 1,242 passed/2 skipped; latest focused 37 passed; drafts module Pyright clean. Live BSS user 2 inventory hides disconnected Gmail, exposes new draft tools; all three health/auth checks passed. No customer drafts changed or SMTP messages sent for testing |
| 5. Context/caching | Deployed v56 | Released v56; all three health/auth checks passed. BSS chat/automation share a 3,613-token stable prefix. See [context reuse](context-reuse.md). 1,245 passed/2 skipped; prompt modules Pyright clean. Effective per-run tool guidance, common prefix, bounded local compilation, prefix metrics and optional mail field projection. Implicit caching/200k compaction unchanged; cost savings still require comparable production measurements |
| 6. Route normalization | Deployed v57 | See [route normalization](route-normalization.md); 55 focused checks and 1,247 full suite passed, 2 skipped; module Pyright clean |
| 7. Task outcomes | Verified, preparing release | Existing tasks are run projections and infer succeeded from any finished run. Add separate persisted outcome/receipt/run links; keep legacy execution filters compatible; no historical success backfill. New model reports must cite actual tool receipt facts, and pending approvals/errors/async extraction block completion |

## Verification/deployment

Handler tests: `PYTHONPATH=src .venv/bin/python -m pytest tests/runtime -q -p no:cacheprovider`.
Current **1,247 passed, 2 skipped**. Run focused tests as changes are made and
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
configuration secrets. Backup before deployment. Last backup **b035**; rollback
for v57 is v56. Excel/Sheets interactive imports and browser-click testing have
not been run; API isolation and compatibility serialization were tested.
