# BSS sequential improvement handoff

Owner authorized starting the seven items sequentially after reviewing
[the design](bss-improvement-design-2026-11-08.md). Work autonomously, preserve
quality, and update this file at each completed item and before stopping.
The owner reported 41% remaining in their usage window; agents cannot read the
remaining quota. Do not create background jobs merely to keep this work running.

## Current state

- Active item: **4 — effective connector capabilities and draft tooling**.
- Runtime deployed: Handler **v54**, code `4339dd6`, image `d38375a1d3b3`.
- Handler repo `/Users/dannyliu/dev/repos/handler`, branch `main`.
- Crontal repo `/Users/dannyliu/dev/repos/hephaestus`; existing unrelated edits
  may be present. Inspect and preserve them before any changes.
- Unrelated Handler `.claude/` stays untouched. Existing untracked
  `docs/incidents/` contains prior PDF incident diagnostics; preserve it.
- Initial audit and design were untracked documents before this work began.

## Item status and next steps

| Item | State | Next action |
| --- | --- | --- |
| 1. Incomplete RFQ | Initial investigation complete; follow-ups open | See [investigation](../audits/bss-rfq562-investigation.md): independently verified 102 rows, only first 16 imported, downstream draft quote 232; actor/exact omission mechanism unknown; no production repair |
| 2. CSV tool/delivery | Deployed v54 | See [CSV tool](csv-spreadsheets.md). 1,230 passed/2 skipped; 17 focused checks; new modules Pyright clean. Live synthetic S3 artifact downloaded through BSS HTTP endpoint with correct Unicode/values and owner; cleaned up. Tool advertised with allow decision; all three instances healthy |
| 3. RFQ fidelity | Requested deeper analysis complete | Live definitions and actual stored lines compared in design section 3. No matching hollow-section shape; missing pipe attributes and unit metadata. Model/schema migration and historical repairs remain separate reviewed follow-ups |
| 4. Connector/draft tools | Implemented; releasing | See [mail tooling](mail-capabilities-and-drafts.md). Owner/scopes-filtered tools; tracked and live-server draft reads; atomic duplicate prevention; guarded tracked-draft editing with UIDPLUS and reconciliation states. No independent-delete tool or automatic historical cleanup. Full suite 1,242 passed/2 skipped; latest focused 37 passed; new drafts module Pyright clean (gateway has an existing jsonschema typing error) |
| 5. Context/caching | Pending | Stable prefix and measured cost/context changes; preserve full-read email gate |
| 6. Route normalization | Pending | Canonicalize braces/Flask/concrete forms for descriptions and cache identity |
| 7. Task outcomes | Pending | Evidence-backed outcomes separate from run execution status |

## Verification/deployment

Handler tests: `PYTHONPATH=src .venv/bin/python -m pytest tests/runtime -q -p no:cacheprovider`.
Current **1,242 passed, 2 skipped**. Run focused tests as changes are made and
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
configuration secrets. Backup before deployment. Last backup **b032**; rollback
for v54 is v53. Excel/Sheets interactive imports and browser-click testing have
not been run; API isolation and compatibility serialization were tested.
