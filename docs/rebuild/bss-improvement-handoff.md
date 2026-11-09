# BSS sequential improvement handoff

Owner authorized starting the seven items sequentially after reviewing
[the design](bss-improvement-design-2026-11-08.md). Work autonomously, preserve
quality, and update this file at each completed item and before stopping.
The owner reported 41% remaining in their usage window; agents cannot read the
remaining quota. Do not create background jobs merely to keep this work running.

## Current state

- Active item: **2 — CSV writer and durable authenticated download delivery**.
- Runtime deployed: Handler **v53**, code `105f335`, documentation through `a365782`.
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
| 2. CSV tool/delivery | Implementing | Shared gateway tool for both harnesses; durable FileStore artifacts, existing authenticated upload download route and file transcript blocks avoid a frontend deployment |
| 3. RFQ fidelity | Investigation/design documented | Verify unit and shape gaps; implement reviewed mappings without corrupting historical records |
| 4. Connector/draft tools | Pending | Effective capabilities first, draft upsert/reconciliation second |
| 5. Context/caching | Pending | Stable prefix and measured cost/context changes; preserve full-read email gate |
| 6. Route normalization | Pending | Canonicalize braces/Flask/concrete forms for descriptions and cache identity |
| 7. Task outcomes | Pending | Evidence-backed outcomes separate from run execution status |

## Verification/deployment

Handler tests: `PYTHONPATH=src .venv/bin/python -m pytest tests/runtime -q -p no:cacheprovider`.
Baseline **1,216 passed, 2 skipped**. Run focused tests as changes are made and
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
configuration secrets. Backup before deployment. Last backup **b031**.
