# Handoff: handler rebuild

## 2026-11-22 shared runtime step 3b (not deployed)

- **Branch `feat/shared-runtime-step3b`** (from `feat/shared-runtime` at d3f2eae). Details,
  numbers and the test list: [step3b-progress.md](step3b-progress.md). No schema change and
  no feature switch: rollback is the previous Heroku release.
- **What changed:** one routed HTTP app for the host (routers registered once, the request's
  tenant in a context variable); the tenant comes from the credentials and `/i/<name>` is a
  hint (another tenant's admin key: 401 before any build); a tenant's store refuses work for
  another tenant; one MCP route `/mcp/` keyed by the run token (harnesses now get it;
  `/i/<name>/mcp/` still takes that tenant's tokens); the integration contract's request side
  (`routers(TenantRoutes)` once per host, `serves_routes`, `load`; `background_tasks`
  removed); Crontal tool discovery in context loading with a host-wide cache; a context-cache
  race fixed (a fresh build could be closed under its waiters when tenants outnumber slots).
- **Registry switch-on (runbook stage 1) is ready:** rehearsed on production's shape
  (`store_pg/test_registry_switch_on.py`) and with `deploy/registry-switch-on.sh` against a
  local server. Procedure and the exact config production must set: tenant-registry.md,
  "Switching the registry on in production". It needs this branch deployed first.
- **New settings** (defaults fit): `HANDLER_CRONTAL_DISCOVERY_TTL_SECONDS` (3600),
  `HANDLER_CRONTAL_DISCOVERY_TIMEOUT_SECONDS` (10).
- **Watch after deploy:** `crontal tools discovered` once per Crontal URL and `crontal tools
  registered` per tenant; no `TenantMismatch`; `/healthz` and `/i/{main,bss,yeup}/healthz` 200;
  main's admin key 401 on `/i/bss`.

## 2026-11-21 shared runtime Release 2 (Handler v65)

- **Handler v65** (`main` at `133cf3f`, merging `feat/shared-runtime`; image
  `cca5ffc258b4`, 2.15 GB). Backup **b045**. **Rollback: v64** (no schema change). Shipped
  about 30 minutes after v64 at the owner's request; v64 had run 13 of 13 runs to completion
  with no R14 before it.
- **Contents:** the reader pool (document, email and image parsing in child processes, one
  reader at a 220 MB RSS cap by default, one heavy library family per reader), PDF text
  through PyMuPDF instead of MarkItDown, tool-result and file size caps, per-run peak memory
  logs, one host `RunGate` (6 runs at once, 3 per tenant; no gate variables are set in
  production), interactive before background across tenants, per-run tool views from the
  live plan, and duplicate chat messages returning their original thread.
- **Measured on the built image** (amd64, disposable storage, fake key): idle server 158 MB
  with no reader processes; reader peaks 146 to 184 MB (PDFs of 30, 100 and 400 pages all
  160 MB); every PDF/docx/xlsx sequence read with no refusals.
- **Verified:** suite on the merge 1,676 passed / 5 skipped; `/healthz` and
  `/i/{main,bss,yeup}/healthz` 200, unknown instance 404, no key 401; three instances
  serving; no ERROR, R14 or traceback after start.
- **Watch next:** R14 over the coming days against the days before; `document read` logs
  (`family`, `reader_replaced`, `reader_recycled`) and "too large to read" refusals;
  `run memory` logs with `run_peak_rss_mb` and `reader_peak_rss_mb`. Ordinary files hitting
  the cap is a reason to revisit the reader count or the dyno size.

## 2026-11-21 shared runtime Release 1 (Handler v64)

- **Handler v64** (`main` at `26ae390`, merging `feat/shared-runtime-r1` at `e0140e9`; image
  `278fc4df5576`, 2.15 GB). v63 set `HANDLER_ALLOWED_HARNESSES=openai_agents`. Backup
  **b043**. **Rollback: v62** (v62 ignores the new `run_failures` table and the variable,
  but would mark threads dead again; the next upgrade sweeps them).
- **Contents:** see [shared-runtime-release-handoff.md](shared-runtime-release-handoff.md)
  Release 1: native-only harness guard, workspace upload at run end, accepted failure
  (`run failed` alert, `run_failures`, `GET /api/runs/failed`, Replay, user retry for
  chat), dead-delivery redeliver, lowercase `.pdf` upload names, and retries for transient
  provider errors.
- **Verified:**
  - `/healthz` and `/i/{main,bss,yeup}/healthz` return 200; an unknown instance returns 404;
    no key returns 401, and main's admin key returns 401 on BSS.
  - The startup sweep logged 13 `run failed` lines for BSS (`failed_before_upgrade`); no
    thread is dead in any schema; `run_failures` holds BSS's 13.
  - No harness errors at startup.
  - Suite on merged `main`: 1,631 passed / 4 skipped. The image started cleanly in a local
    smoke run.
- **BSS backlog replayed:** the 12 mail failures (40 events, November 9 to 20) replayed
  oldest first via `POST /i/bss/api/runs/{id}/replay`: all 12 runs **completed**, none
  failed again (drained 15:25 to 15:37 EST). The November 11 chat
  (`run_4b8520ffe33042ff`) was not replayed.
- **Crontal v127** (Release 3, same day): `main` at `c3146ef` (chat `client_message_id`,
  client conversation ids, red failure icon with Retry via
  `POST /api/v1/handler/threads/<key>/runs/<run_id>/retry`), shipped together with the
  owner-approved, previously undeployed `08d2d1f` (merchant quotation PDF templates,
  migration `z2quote20261115`). Image `bbd01eee1a5c`; backup **b044**; maintenance mode
  20:40:03 to 20:40:38 UTC for the migration; Alembic now `z2quote20261115`; Yeup (298)
  and Brava (232) have their templates. Backend suite 759 passed / 1 skipped on the
  release tree. Only H80 503s during maintenance; proxy calls 200 afterwards.
  **Rollback: v126**, then `alembic downgrade y1hfree20261109` only if the column must go
  (v126 ignores it). Tag `deployed/hephaestus-backend-test-v127`.
- **Not yet checked by a person:** the failure icon and Retry in Crontal's panel (covered by
  component tests only).
- **Codex threads:** `handler_crontal` has 3 old chat threads on `codex` (user 1, October 18);
  a message there now fails the run.

## 2026-11-21 production run failures (Release 1 line, `feat/shared-runtime-r1`)

- **Uppercase `.PDF` attachments killed runs.** Native `read_file` uploaded a PDF under its
  own name (`Bill Of Lading - SHAJ81315.PDF`); the Responses API matches input-file types
  on the lowercase extension and refused the next model call with a non-retryable 400
  ("Expected context stuffing file type to be a supported format"): 9+ BSS mail threads.
  Fixed in `harness/pdf_files.py`: uploads are named `<stem>.pdf` (`input_file_name`), and
  the upload name is in the cache key, so references cached under an old name are
  re-uploaded. That is the only place Handler sends an input file; chat `{"type": "file"}`
  blocks are transcript-only (the model reads attachments from the workspace). Threads
  that already failed need a replay to run again.
- **Transient provider errors were final.** A plain `openai.APIError` ("Our servers are
  currently overloaded. Please try again later.", status-less, from a stream) killed two mail
  threads on their first attempt. `harness/openai_agents.py` `_error` now also retries
  429/408/409/5xx status errors, status-less errors whose code or message says
  overload/unavailable, the 400 "Another process is currently operating on this
  conversation" (waits at least `CONVERSATION_BUSY_RETRY_SECONDS` = 30 s, via the new
  `HarnessError.retry_after`), and `ModelBehaviorError` "did not produce a final response"
  (at most 2 attempts, via `HarnessError.max_attempts`). Other 400s stay final.

## 2026-11-14 prompt-cache release (Handler v62)

- **Handler v62** (`7c7fde2`, image `2d47da22e2c1`, 2,151,625,279 bytes), on top of the
  multi-tenant v61. Backup **b042**. **Rollback: v61** (`8e201677a077`). The schema change
  is additive (`threads.context_hash`), so v61 runs on the migrated schema unchanged.
- **Why:** the OpenAI Agents SDK derives a `prompt_cache_key` from each conversation, so
  nothing was ever cached across runs: 0 cached tokens on all 134 BSS first requests of
  new threads. Synthetic tests on gpt-6.1 show the cache matches at block boundaries,
  not at the first differing token:
  - any change inside `instructions` gives 0 cached (tools and instructions form one block);
  - a `developer`-role input merges into that block;
  - a `user`-role first input keeps the tools and instructions cached;
  - a change mid-history falls back to the instructions boundary.

  Memory index, linked memory and speaker lived in the instructions, so any memory change
  anywhere in an instance made every open chat fully cold.
- **Change (`c3bc3de` + `c19d68e`):**
  - Instructions are byte-identical for each thread type.
  - Memory index, linked memory and speaker go in an append-only user-role
    `<handler_context>` item. It is sent on a session's first turn and again only when it
    changes, tracked by `threads.context_hash`, which is reset by a new session or a
    compaction.
  - `prompt_cache_key` = hash(instructions + tools + model + harness).
  - The deprecated `prompt_cache_retention` setting is removed (gpt-5.5+ are 24h only).
  - Codex is unchanged.
- **Verified:**
  - `/healthz`, `/i/{main,bss,yeup}/healthz` return 200; an unknown instance returns 404.
  - Cross-instance admin keys and requests with no key return 401.
  - The column exists in all three schemas, and startup is clean.
  - Crontal proxy calls return 200.
  - The BSS preview shows instructions without the memory index or speaker; tokens per
    type: chat 3,840, email 4,988, automation 3,871; context about 1.25k.
  - Suite: 1,550 passed / 4 skipped.
- **Measure next:** cached tokens on first requests of new threads (baseline 0/134), and
  cold chat turns within 30 minutes (baseline 27). Watch for memory text at user-role
  authority being treated as a request.
- Analysis: hephaestus `docs/handler-chat-cost-audit-2026-11-14.md`; unmerged branches
  `audit/chat-context-2026-11-14` and `docs/trigger-action-design`.

## 2026-11-14 multi-tenant release (stage 1: everything switched off)

**Picking this up? Start with [multi-tenant-handoff.md](multi-tenant-handoff.md)**: state,
owner decisions, next steps, rollback and working notes.

Design: [multi-tenant-design.md](multi-tenant-design.md) (owner decisions in its
Decisions table); next architecture step: [role-split-design.md](role-split-design.md)
(proposed, not approved). Branch `feat/multi-tenant` merged to `main` at `25a3284`.

- **Handler v61** (`25a3284`, image `8e201677a077`, 2,151,614,156 bytes); config-only
  **v60** set `HANDLER_MERCHANT_ID=2`, `HANDLER_I_BSS__HANDLER_MERCHANT_ID=1`,
  `HANDLER_I_YEUP__HANDLER_MERCHANT_ID=298`. **Crontal v126** (`0767312`, image
  `e3766cca9a9d`, 746,877,878 bytes); Alembic `z1coverage20261112` →
  **`y1hfree20261109`**, run under maintenance mode (about 1 minute).
- **Backup b041** (schema and data). Schema-only dump, config and release snapshots in
  `~/dev/backups/2026-11-14-pre-multi-tenant/` (mode 600, outside the repos).
- **Rollback:** Handler **v59** (`ef3e1ff02d4e`), Crontal **v125** (`9423d0c63689`).
  Git tags `deployed/crontal-agent-v59` (`8ca3d29`), `deployed/hephaestus-backend-test-v125`
  (`63a3fa9`), and `pre-multi-tenant` in each repo. The old images were tested against the
  migrated schema; downgrading the migration deletes pending merchant-level outbox rows.
- **Live now:** shared Postgres pool (11 + 1 LISTEN for the process), `handler_control`
  schema with per-tenant schema versions, empty quota tables in each tenant schema,
  per-run and per-document-read memory logs, a process-wide cap of 2 document reads
  (`HANDLER_DOC_READ_MAX_CONCURRENT`), one Crontal webhook per merchant-wide event.
- **Still off:** tenant registry (`HANDLER_TENANT_REGISTRY` unset), plans enforcement for
  the three env-defined tenants (Crontal marks merchants 1, 2, 298 paid; 14 others free),
  every merchant's cutover switch, free pilot. Hold the registry until the role split
  (role-split-design.md, open decision 1) unless the owner decides otherwise.
- **Verified:** `/healthz`, `/i/main`, `/i/bss`, `/i/yeup` 200; unknown name 404; each
  admin key 200 only on its own tenant (401 on the other two, 401 with no key);
  Postgres connections **12/20** (was 16/20); `handler_control.tenant_schema_versions`
  has core and mailbox rows for all three schemas; Crontal `/api/v1/time` 200 and live
  proxy traffic (status, approvals, settings, prompt, `agent-access/routes/describe`)
  all 200 with no app errors after maintenance ended.
- **Watch next:** a few days of memory logs (R14 cause) and connection counts before
  switching anything on. `HANDLER_MAX_CONCURRENT` is still 20; the review suggested 3,
  but check each instance's lane limits first.

## Active: BSS sequential improvements

**Latest November 13: automation dashboard shipped.** Handler **v59**
(`8ca3d29`, image `ef3e1ff02d4e`, 2,151,476,534 bytes), Crontal **v125**
(`63a3fa9`, image `9423d0c63689`, 741,395,931 bytes), backup **b040**.
No schema/config changes; Alembic remains **z1coverage20261112**. Rollbacks are
Handler **v58** / Crontal **v124**. `/app/automations` appears in Crontal's sidebar
for Handler users: all schedules plus retained deleted history, current model,
run/failure counts, last/next run, tokens and cumulative estimated cost. SQL
aggregation avoids history caps and run/request double counting; unknown prices
stay marked. Read-only queries refresh every 30 seconds while visible.
**1,267 Handler tests / 4 skipped**, **685 Crontal tests / 22 subtests**, 34
frontend tests, build/i18n and synthetic desktop/mobile browser checks passed.
Live instance health/auth, both user proxies, schedule isolation and independent
SQL totals reconciled; schedule definitions remain unchanged. Full contract and
release evidence: sibling Crontal `docs/automation-dashboard-2026-11-13.md`.
Current read-only verification scripts/config snapshots are protected under
`/tmp/automation-dashboard-release/`. Older release notes below are historical.

**Latest November 12:** Crontal **v124** (`dab8c97`, image `4bc02f43ac46`,
741,381,019 bytes), backup **b039**, migration **z1coverage20261112**, rollback
Crontal **v123**. Handler remains **v58**. Spreadsheet row-coverage enforcement,
partial confirmation/quote gates and confirmation attribution are live; historical
RFQ562/quote232 are unchanged. **684 backend tests / 22 subtests**, frontend/i18n,
PostgreSQL migration and live health/ownership/isolation/proxy checks passed.
The tubular catalogue/preflight follow-up shipped in Crontal v123. Two real
Dovecot tests passed (default Handler suite now 1,261 passed / 4 skipped).
The refreshed cost audit has a real cohort and does not demonstrate overall
savings; a new hourly inquiry-filing automation is the next measured target.
See the current [BSS takeover notes](bss-improvement-handoff.md) and
[updated cost audit](../audits/bss-cost-followup-2026-11-09.md). Release history below
is retained as historical evidence.

The owner authorized sequential work on the seven recent-chat findings, with
durable takeover notes. Start with [bss-improvement-handoff.md](bss-improvement-handoff.md)
for current progress and [the design](bss-improvement-design-2026-11-08.md) for
contracts and constraints. Item 1 is investigation first; preserve production
records while establishing why the incomplete import was confirmed.

CSV export tool and durable chat downloads released as **v54**, runtime `4339dd6`,
image `d38375a1d3b3`, backup **b032**, rollback **v53**. Full suite **1,230 passed,
2 skipped**; live synthetic BSS artifact persistence/download and all three
instance health/auth checks passed. See [csv-spreadsheets.md](csv-spreadsheets.md).

Mail capability filtering and draft tooling released as **v55**, runtime `9c5c030`,
image `1cabdadd96ac`, backup **b033**, rollback **v54**. Full suite **1,242 passed,
2 skipped**; effective BSS user 2 inventory and all three health/auth checks passed.
See [mail-capabilities-and-drafts.md](mail-capabilities-and-drafts.md) for limits
and interruption recovery; no production SMTP or draft mutation was used in tests.

Context reuse released as **v56**, runtime `39baef4`, image `647f2f3e823d`,
backup **b034**, rollback **v55**. **1,245 passed/2 skipped**; all instances healthy.
Live BSS chat/automation share a 3,613-token common prefix; see
[context-reuse.md](context-reuse.md). Provider savings are not yet measured.

Route normalization released as **v57** (`4668eab`, image `626e2f1c4455`),
backup **b035**, rollback **v56**. **1,247 passed/2 skipped**; all instances healthy.
Live file/contact brace aliases resolve to registered contracts. See
[route-normalization.md](route-normalization.md).

Business outcomes released as Handler **v58** (`b0a7971`, image `c72d79a1a0fa`)
and Crontal **v122** (`0c7a849`, image `5f523db6bc51`), backup **b037**, rollback
Handler **v57** / Crontal **v121**. **1,261 passed/2 skipped**, new outcome modules
Pyright clean; 113 Crontal integration tests, frontend build and i18n passed.
All instance health/isolation/schema checks and both users' Crontal proxies passed;
new frontend bundle verified. See [task-outcomes.md](task-outcomes.md) and the
[latest instruction token/% breakdown](../audits/bss-prompt-components-2026-11-09.md).
Initial authorized sequence delivered; investigative/schema/cost-measurement
follow-ups are recorded in the BSS handoff. No historical RFQ repair was made.

## 2026-11-08 native PDF file transport

Native OpenAI `read_file` now uploads intact PDFs and returns file references,
including all pages by default. Explicit page selections remain PDFs; legacy
`as_images=true` no longer produces inline PNGs for PDFs. Upload references are
content/API-scope cached in the isolated, synced harness home. CLI PDF rendering
is unchanged. See `document-reader.md` for transport and retention details.

Reason: large inline PNG tool outputs reproduced provider HTTP 500s when listing
and resuming Conversations. A live test through the new tool used a 12.2 MB,
15-page scanned PDF: every amount and the total were correct; stored history
listing and a follow-up both succeeded. No original customer documents were used.
Validation: **1,216 passed, 2 skipped**, including 8 new PDF transport cases;
PDF preparation/upload modules pass Pyright. The harness has existing unrelated
type errors. Released as **v53** on `crontal-agent`, runtime **105f335** on `main`,
image **517ff8a5e633**, backup **b031**, rollback **v52**. Container smoke verified
the actual native tool preserves 15 PDF pages and returns an `input_file` reference.
Main/BSS/Yeup health and authenticated settings returned 200; unauthenticated
settings returned 401 on all three instances. Existing inline-image conversation
items are not rewritten by this release.

## 2026-11-08 document-reader refactor

The shared reader is now a `documents/` package starting with an explicit file-type
router and separate text, converted-document, spreadsheet, PDF, image and archive
readers. Existing `documents.read`, `to_text`, `handler-read`, tool schema and output
behavior are preserved. See `docs/rebuild/document-reader.md` for module boundaries,
extension routing, preview semantics and validation. Scans still use model vision.
Validation: **1,208 passed, 2 skipped**, 35 focused reader tests, zero Pyright errors,
and 86 differential cases identical to the old implementation. Released as **v51**
on `crontal-agent`, runtime `b6cc927` on `main`, image `efe7caf5b8fe`, backup **b029**;
rollback **v50**. Main/BSS/Yeup health, authenticated tools/reporting and 401 checks
passed. The image also passed native CSV/image/scanned-PDF, workspace boundary and
installed `handler-read` smoke tests.

## 2026-11-08 detailed usage ledger

Request/tool-call metering, immutable user/thread attribution, admin reporting and
console tables are documented in `docs/audits/detailed-usage-ledger-2026-11-08.md`.
Released on `crontal-agent` as **v50**, runtime `745bd2b`, backup **b028**; rollback
**v49**. Final combined suite: **1,198 passed, 2 skipped**. All three instance health
and reporting checks passed. Detailed data starts at this release; older run totals
remain available through the
existing usage API. Costs are model-token estimates only: hosted/external fees are
excluded, and hypothetical tool-context estimates are separate and non-additive.

## 2026-11-08 token efficiency update

Owner-approved implementation and validation are documented in
`docs/audits/token-efficiency-implementation-2026-11-08.md`; the baseline audit is
`docs/audits/token-efficiency-2026-11-08.md` and component measurements are in
`docs/audits/prompt-components-2026-11-08.md`.

The rebuilt runtime now supports full-read-gated email summaries (never automatically
mark historical mail read), paged/batched mail reads, fresh automation firings with
memory checkpoints, stable prompt components with token/% preview, compact results,
opt-in terminal approval receipts, and bounded Luna/Terra helpers under the existing
main model's verification. Live `efficiency` settings can disable fresh sessions or
helpers. Released on `crontal-agent` as **v48**, runtime commit `17324c3`, after backup **b026**; rollback **v47**. Main/BSS persona version 3 corrects stale identity wording. All three Crontal instance health/tool/settings checks passed. The main model remains Sol. Legacy Handler and the personal deployment are
unchanged. Read the implementation document before changing these contracts.

**Follow-up (branch `feat/route-catalog-in-prompt`, not merged or deployed):** a compact Crontal
API route catalog (routes of common resources, paths without `/api/v1`, plus full contracts that
fit the cap) is now the last stable prompt component, `crontal_route_catalog`, cached per instance
for 1 h with lazy refresh and a non-blocking fallback to discovery tools. Settings
`efficiency.route_catalog`, `route_catalog_resources`, `route_catalog_max_chars` (7000) and
`route_catalog_full_specs`. Default ~1,703 tokens on a realistic fixture (1,359 lines only). This
supersedes the 2026-10-25 (2b) "pull on demand only" decision below; see the implementation document.

Updated 2026-10-18 by the driver (Claude Code), for whoever picks this up next. Read this first, then `docs/agent-runtime-design.md` (the design) and the package briefs in `docs/rebuild/` (`phase1.md` … `phase4-ui.md`). Known problems are in `docs/rebuild/known-issues.md`.

## 1. What this project is

Handler is an **agent runtime layered on agent harnesses**. It does not write its own agent loop: a harness runs each turn. Handler owns everything around that:
- durable, event-driven threads;
- the tool gateway (MCP server, grants, approvals, audit);
- shared memory and settings;
- schedules;
- connectors;
- observability and cost;
- storage.

It runs in two places:
- **Personal instance** on macbook-server (the owner's agent).
- **Crontal instance** on Heroku (`crontal-agent`). It serves the site admins of Crontal, the steel procurement marketplace in the `hephaestus` repo, as a premium add-on.

Repo and code layout:
- Repo: `/Users/dannyliu/dev/repos/handler` (GitHub `dingtaoliu/handler`), branch **`rebuild`**. Every commit is pushed and deployed.
- All runtime code is in `src/handler/runtime/`. The legacy modules elsewhere in `src/handler/` still run the old personal instance on port 8000, untouched.
- Tests: `PYTHONPATH=src .venv/bin/python -m pytest tests/runtime -q -p no:cacheprovider`. Currently **539 passed, 2 skipped**; keep it green.

## 2. Architecture as built

| Area | Where | Notes |
|---|---|---|
| Event queue, threads, leases | `store/` (SQLite + Postgres mixins), `runner.py` (the host claim loop; `dispatcher.py` is it over one store) | Thread types: chat, gmail, automation, crontal. One harness session per thread |
| Run lifecycle | `run.py` (`RunExecutor`, `_plan`) | Model/effort come from the newest chat message's override, then the thread, then per-type settings, then instance defaults. Lost sessions recover by replaying a recap of the transcript |
| **Harnesses** | `harness/codex.py`, `harness/openai_agents.py` | See §3 |
| Gateway | `gateway/` | MCP server at `/mcp/`; per-run tokens; `GrantPolicy` (exact action → glob → risk default); approvals; audit |
| Crontal access | `gateway/crontal_api.py` (generic REST), `gateway/crontal.py` (older curated MCP tools) | See §4 |
| Memory, settings, prompts | `memory.py`, `settings.py`, `instructions.py`, `web/settings_api.py` | Memory kinds, links, `memory_search`, run-start memory and lost-save recovery: `memory-kinds.md`. Settings are versioned in the store. Thread-type `harness`/`model`/`reasoning_effort` are enums (dropdowns). `GET /api/settings/{s}` returns `defaults`; `GET /api/settings/prompt?thread_type=` returns the rendered prompt |
| Workspace sync | `workspaces.py`, `files.py` | A run marks its thread dirty and, when it ends (completed or failed), starts a background upload of that thread (`WorkspaceSync.flush_soon`); neither the reply nor the run slot waits for it. A failed upload stays dirty for the host's one workspace backstop (`WorkspaceBackstop`, every `HANDLER_SYNC_INTERVAL_SECONDS`, 300; role-split step 2); the shutdown flush on SIGTERM also waits for uploads under way and covers runs cancelled at shutdown. Uploads skip harness caches (`.tmp/` etc.), send only files whose content hash changed, and run 16 transfers in parallel |
| Web UI | `web/static/app/` (full UI), `login.html`, `timeline.html` | Tabs: chat (model/effort selects, attachments), memory, automations, activity, usage, approvals, notifications, settings (prompt preview, "Default (…)" labels), tools, files, logs, connections, users |
| Admin APIs | `web/admin_api.py`, `uploads.py` | Memory, automations, usage, tools, uploads, logs, connections |
| Auth | `auth.py`, `web/auth_api.py` | Admin key (+ `X-Handler-User` to act as a user), per-user tokens, `/login` |
| Legacy import | `migrate_legacy.py` (`handler-rt import-legacy`) | Used once for the personal instance: persona, memory, users, conversations |

## 3. Harnesses

- **`codex`** runs `codex exec --json` as a subprocess, with an isolated `CODEX_HOME` and session resume.
  - The Docker image must ship **both** `codex` and `codex-code-mode-host`; without the second, every tool call fails.
  - The personal instance uses ChatGPT login through `~/.codex/auth.json`, linked into each home.
- **`openai_agents`** is native and in-process, built on the OpenAI Agents SDK (no subprocess).
  - Session: an OpenAI Conversation (`conversation_id`).
  - Tools: handler's MCP gateway, OpenAI's hosted `WebSearchTool`, and `list_files`/`read_file`/`write_file` limited to the workspace. There is deliberately **no shell**.
  - Auth: `OPENAI_API_KEY` or `CODEX_API_KEY`. SDK tracing is off.
- **Selecting a harness:**
  - Settings → Threads picks the harness per thread type. It's read live when a thread is created, so no restart is needed; existing threads keep their harness.
  - `HANDLER_DEFAULT_HARNESS` seeds a fresh instance.
  - Codex is optional; the server starts with whichever harnesses are available.
  - **`HANDLER_ALLOWED_HARNESSES`** (host setting, comma-separated; unset allows all, so the personal instance keeps Codex). Production sets `openai_agents` (role-split-design.md, owner decision 4). It is read once from the host's own environment and applies to every instance and registry tenant; `HANDLER_I_<NAME>__…` and tenant config cannot widen it. With it set:
    - a harness not listed is never constructed;
    - a `HANDLER_DEFAULT_HARNESS` outside it stops startup, and the platform API and registry refuse such tenant config (422 / validation error);
    - saving or rolling back `threads` settings that name it returns 422; unset types default to an allowed harness, and the form offers only allowed ones;
    - a thread whose stored harness is not allowed is not run and not switched: its run fails at once with no retry and is recorded as a failed run with the reason (the thread stays open; see notifications.md, "Failed runs"). Startup logs an ERROR when saved `threads` settings still name one.

## 4. Crontal integration (both repos)

**Crontal (`/Users/dannyliu/dev/repos/hephaestus`, `main`, deployed as `hephaestus-backend-test`):**
- `controllers/agent_access_controller.py`:
  - `POST /api/v1/agent-access/token` (agent token) mints a 15-minute JWT with `actor: agent` for the instance's **AI identity** only (see below); `X-Crontal-User` naming anyone else is 403;
  - `GET /api/v1/agent-access/identity` returns that AI user (`user_id`, `display_name`, `merchant_name`);
  - `GET /api/v1/agent-access/routes` is the API catalog.
- `controllers/agent_mcp_controller.py`: the older curated MCP tools.
- `services/handler_service.py`: entitlement, signed webhook outbox, and the `/api/v1/handler/*` proxy. Entitlement is `is_site_admin_user`, which needs stored role `site_admin` **and** an email in `SITE_ADMIN_EMAILS`; the feature is gated by `HANDLER_ENABLED`.
- Frontend: Handler settings tab (`SchemaForm`, defaults, rendered prompt) and the chat panel (model/effort selects).

**AI identity (2026-10-25, branch `feat/ai-identity` in both repos):** each Crontal `HandlerInstance` has one AI user (`user.kind='ai'`, "<merchant> AI", a member of the merchant, no login). The agent acts in Crontal **only** as it, on every thread type (chat, email, crontal, automation, gmail); it can no longer act as people. `gateway/crontal_identity.py` discovers the AI user (`/agent-access/identity`, cached 10 min, shared per instance) and the persona prompt names it. Chat runs send the chatting person as `X-Crontal-On-Behalf-Of` (audit only; dropped if Crontal refuses it). Handler-side identity (`acting_as`: approvals, memory/automation attribution, failure notices) is unchanged.

**Handler side:**
- The agent reaches all of Crontal through `crontal_api_routes`, `crontal_api_get` and `crontal_api_call`.
  - Calls run with the AI user's JWT, so Crontal applies that member's permissions.
  - File responses are saved to `downloads/` in the workspace, with document text extracted.
  - Uploads take a workspace file through `upload_file`.
- `crontal_api.*` and `crontal.*` are granted `allow` on the Heroku instance, because the owner wants full access for now. A proper authorization system for users and the agent is planned; the `actor: agent` claim is the hook for it.
- Webhooks come in at `/ingest/crontal` (HMAC `X-Crontal-Signature`) and create `crontal:{entity_type}:{entity_id}` threads.
- Legacy AI chats are carried over with `POST /api/threads/import` (bare admin key; the owner must already exist, so provision with `POST /api/users` first; history only, no input counted). See `legacy-chat-import.md`.

## 5. Deployments

**Heroku `crontal-agent`** (team `crontal`, one Basic dyno, container stack):
- Uses Crontal's Postgres (schema `handler_crontal`) and Bucketeer (prefix `handler/crontal/`).
- Config:
  - `CODEX_API_KEY`, copied from Crontal's `OPENAI_API_KEY`;
  - `HANDLER_DEFAULT_HARNESS=openai_agents`;
  - `HANDLER_MAX_CONCURRENT=20`;
  - `HANDLER_RT_MODEL=gpt-6.1-sol`, `HANDLER_RT_EFFORT=low`;
  - `CRONTAL_URL=https://hephaestus-backend-test-d7a0a92e4255.herokuapp.com/api/v1/agent-mcp`;
  - `HANDLER_ADMIN_KEY`, `HANDLER_WEBHOOK_SECRET`, `CRONTAL_AGENT_TOKEN`.
- The persona (Crontal system prompt) is stored in settings: edit it in Crontal → Handler settings, or `PUT /api/settings/persona`.
- Deploy:
  ```
  docker build --platform linux/amd64 -t registry.heroku.com/crontal-agent/web .
  docker push registry.heroku.com/crontal-agent/web
  heroku container:release web -a crontal-agent
  ```
- The image symlinks `/bin/sh` → bash so that `handler-rt` becomes PID 1 and gets SIGTERM (see the Dockerfile comment).

**Crontal `hephaestus-backend-test`:** deploy with the repo's `/deploy` skill, or the same docker flow. Take `heroku pg:backups:capture` first and run alembic after the release.

**macbook-server (personal):**
- Code in `~/repos/handler-rt` (rebuild), data in `~/.handler-rt/`, port 8100, launchd job `com.handler.rt`.
- Uses the Codex harness with ChatGPT login.
- Update: `git pull && uv sync && launchctl kickstart -k gui/$(id -u)/com.handler.rt`.
- The legacy instance on :8000 must keep running untouched.

## 6. Open items

- **Missing Crontal files.** Crontal's file records with `local_files/...` keys (ids 1, 34, 265, 298, 331, 332) are missing from S3; they were probably uploaded to dyno disk before Bucketeer was attached. Downloads 404 in the UI too. Investigate `hephaestus-web-app`, and make sure all uploads go to S3.
- **Native harness transcripts.** Tool calls show their output but not their arguments in the UI.
- **Old Codex threads.** Threads created before the switch still use Codex. Decided (2026-10-18): this is fine. A thread keeps the harness it was created with; no migration.
- **Gmail connector.** Not built: Google OAuth client, self-hosted Google Workspace MCP and handler-owned OAuth.
- **Authorization.** The authorization system for users and the agent (above) is planned. Not a priority yet (2026-10-18).
- **Pricing.** `prices.yaml` rates are null, so costs show as unpriced.
- **Workspace sync deletions.** Sync never deletes remote files (`known-issues.md` §1). To be fixed, but low priority (2026-10-18).
- **Codex jobs.** Close stdin (`< /dev/null`). Codex's sandbox makes `.git` read-only, so the driver commits its work. The Codex plan is $20 Plus: run at most 2 jobs at a time.
- **Crontal notification list should load in pages (backlog, 2026-10-18).** The bell/inbox component in Crontal's frontend (`NotificationBell` / `notification-dropdown`) should show only the most recent N (e.g. 10) with a "See more" control that loads older pages (cursor/limit API), instead of keeping every notification in the component. It must still scale once handler pushes notifications into Crontal.

## 7. Current state (updated 2026-10-18 19:45 by the lead)

Working mode: the main session acts as architect and hands implementation to background sub-agents in git worktrees. The lead merges into `rebuild`/`main`, runs tests and deploys with the hephaestus `deploy` skill.

**Live:** handler `crontal-agent` @ `6e1db05` (v35+); Crontal `hephaestus-backend-test` v110 @ `25d45a4`.
- **Instances, all on one dyno:** `main` (Brava Canada, merchant 2) at `/`, `bss` (Brava Stainless Steel, merchant 1) at `/i/bss`. Crontal routes requests through its `handler_instance` registry. The separate `crontal-agent-bss` app has been deleted.
- **Notifications:** pushed from handler into Crontal through the generic durable `deliveries` outbox and the Crontal sink. Crontal stores them as `handler_notification` rows and receipts (migration `s4o5p6q7r8t9`).
- **Tasks:** the `/api/tasks` API, handler's Tasks tab, and a Tasks tab in Crontal's agent panel. The debug trace (`steps`) is admin-only in the tasks API.
- **Chat images:** paste, drop or pick images in Crontal's panel. Both harnesses get them as vision input, with copies kept in the workspace.
- **Gmail:** read, search, attachments and drafts, plus `gmail_send` / `gmail_send_draft`. Sends always need approval (`explicit_allow_only`), and the approval pins the exact content. The `google_workspace_mcp` sidecar and Google's preview MCP were evaluated and rejected for now; see `gmail-connector.md` §4.
- **Email intake:** `contact@crontal.ai` goes through the Cloudflare Worker to `/ingest/email`. Senders are gated by a **blocklist** (intentional, by the owner's decision) plus DKIM. It works in Crontal as the instance's AI user (it acted as user 1 before `feat/ai-identity`).
- **Credentials:** the Postgres credentials were rotated on 2026-10-18.

**Next:**
1. **Integrations refactor** (`docs/rebuild/integrations.md`, approved). **Phase 1 is merged** (interface, registry, per-instance `HANDLER_INTEGRATIONS`, `GET /api/ui/manifest`; no behaviour change). The owner approved its decisions: the package is `src/handler/runtime/integrations/`; legacy wiring stays until each integration takes over; `HANDLER_INTEGRATIONS` is not inherited by other instances; `chat`/`automation` are reserved thread types. **Phase 2 step 1 (`crontal`) is merged.** Once `HANDLER_INTEGRATIONS` is set on an instance, that instance gets Crontal only from the integration, so leaving `crontal` out of the list removes it. The variable is NOT set on prod yet, so legacy wiring is still active there. **Next:** phase 2 `email_intake`, then `google`, then core-only tests (see `docs/rebuild/integrations-progress.md`). The ordered plan is in `docs/rebuild/integrations-progress.md`.
2. **Native read-only Drive tools** (recommended over MCP). The owner must add `drive.readonly`, enable the Drive API and reconnect. Waiting on the owner's go-ahead.

**Owner's batch, 2026-10-25** (worked through background agents; status here):
- **Done:** the bss Google OAuth `redirect_uri_mismatch`. The owner added `…/i/bss/oauth/google/callback` to the shared OAuth client, and Google now accepts it. Eric still has to connect, which completes the end-to-end test.
- **Crontal chat panel auto-scroll:** fixed in hephaestus `ChatWindow.tsx`; it follows new messages only while the reader is at the bottom and shows a "Jump to latest" button otherwise. Committed on hephaestus `main`, not deployed yet; the owner tests after deploy.
- **(1) Merchant invites (hephaestus).** A user invites a coworker by email. Crontal pre-creates the account linked to the same merchant and emails a link; the invitee sets a username and password. Implement invites now. Multi-user-per-merchant implications (merchant-to-merchant chat as the merchant, roles, ownership of RFQs/bids/notifications) stay a design doc for now.
- **(2a) 10/23 run failures.** Review the recent runs on `main` and `bss`. One is `BadRequestError 400: No tool output found for function call call_e9ad…` (the native harness's OpenAI Conversation was left with a function call that has no output).
  - *Found (2026-10-25):* main `chat:1:b847…` hit an openai-agents 0.12.2 bug: the SDK tracks sent items by `id(obj)`, so a reused address dropped a tool output. It is fixed upstream in >=0.18. bss `chat:2:2c66…` broke when the dyno restarted mid-tool (v36). Neither thread recovers, because the run is marked `dead`, nothing ever clears that, and the user message is lost. Also found: binary `.xls` dumped into the context (396 KB, NUL bytes then broke a jsonb write), runs stuck at `running`, 0 tokens recorded on failed runs, Conversations growing unbounded (300k+ input tokens per run), a scanned PDF unreadable, an uploaded doc ignored, and Crontal returning 500 instead of 400 on POST /message/chat. A fix branch is in progress (SDK bump, conversation repair, dead reset, run sweeps, NUL strip, usage on failure, truncation, a revive CLI). Revive the two threads after deploy.
- **(2b) Crontal API descriptions.** The `/agent-access/routes` catalog has only method, path and a one-line summary, with no request or response shapes. Decide between injecting the schema into the prompt and pulling it on demand.
- **(2c) Base file tooling.** The native `read_file` is UTF-8 only. Needs a one-stop reader for all common formats (xlsx, docx, pdf, pptx, csv…; `markitdown` is already a dependency), a way to show workspace images to the model, and writing of common formats.
- **(4) Handler admin portal sign-in from Crontal.** `/app` asks for the instance admin key, which isn't usable from Crontal. Crontal site admins should get a one-click, short-lived sign-in into their merchant's instance.
- **Decisions the driver made while the owner was away (2026-10-25), for review:** (2b) API schemas are pulled on demand: a compact catalog, a per-route describe tool, the schema returned automatically on 4xx validation errors, and a short usage note in the prompt; they are not injected into every context and not cached in memory (it would go stale). (2c) Writing non-text formats is on hold until the owner picks an approach. The scroll fix is committed on hephaestus `main` but not deployed.
- **(3) Token and cost tracking.** In Crontal's own AI agent (not Handler), the admin view lists messages but tokens in and out are all 0. Fix that, then surface Handler's usage and cost (from `/api/usage` per instance) in Crontal's admin portal so all AI spend is visible in one place. `prices.yaml` rates are still null.

**Deployed 2026-10-25:**
- **Crontal v113** (`main` @ 9365c70): scroll fix, merchant invites, AI spend, console sign-in, and the agent API specs with `/agent-access/routes/describe`. Migration `t5p6q7r8s9u0` was applied after backup b020.
- **Handler v40** (`rebuild` @ 8dfc9ee): sign-in tickets, the document reader, the run-reliability fixes (openai-agents 0.24, conversation repair, dead-thread revive, abandoned-run sweep, compaction at 200k) and `crontal_api_describe`.
- On startup, bss marked `run_1d6749a2c7c54b47` and `run_d4da6695cee44305` abandoned.
- Both dead threads were revived with `--reset-session --discard-pending` (main `chat:1:b847…`, bss `chat:2:2c66…`).
- Rollback targets: Crontal v111, Handler v37.
- Deleted prod user 166 (`daniel.dingtao.liu@hotmail.com`, Test Merchant 2) at the owner's request, so they can test invites.
- Ops notes:
  - `heroku run` hangs locally; use `heroku run:detached` and then `heroku logs --dyno run.N`.
  - `docker push` to Heroku once hung for over an hour; kill it and push again.
  - macOS has no `timeout`.
- Open decisions: how to write non-text formats (recommended: structured `write_xlsx`/`write_docx` tools); keep `pillow-heif` (26 MB); whether existing JWTs should be rejected for inactive users; the open questions in hephaestus `docs/design/multi-user-merchants.md`.
- Handler-side follow-ups from the AI-spend work: `/api/usage` by_user, cache-write tokens, day-by-model breakdown, `GET /api/prices`, paged scans.

**Parked 2026-10-25: agent sandbox** (the owner wants one so the agent can take on complex tasks we never built for). Requirements the agreed definition covers:
1. No secrets.
2. Filesystem isolation.
3. Network limits.
4. Process isolation.
5. Resource limits.
6. Lifecycle, meaning setup and cleanup, which is hygiene and not the security boundary.

Measured on a Heroku dyno: same uid as Handler, `unprivileged_userns_clone=0` so bwrap fails, and Codex already runs with `danger-full-access` on `crontal-agent`. Code must not run on the Handler dyno. Recommended: option B, a separate sandbox dyno per instance from the same image, with no secrets and no DB, driven over HTTP behind a `Sandbox` interface. E2B can be swapped in later. Awaiting the owner's go-ahead.

**Built 2026-10-25, not merged: IMAP/SMTP mailbox integration** (branch `feat/mailbox-integration`, `docs/rebuild/mailbox-integration.md`; Crontal side on hephaestus `feat/handler-mailboxes`). Enable per instance with `HANDLER_I_<NAME>__INTEGRATIONS=crontal,mailbox`. It supports several mailboxes per instance, connected from Crontal. First mailbox: `sales@yeup-tube.com` (chengmail.cn: IMAP 143 STARTTLS or 993 TLS, SMTP 587 STARTTLS or 465). Reachable from Heroku. The server has no IDLE, so Handler polls. Yeup is Crontal merchant 298, and the owner is choosing which instance gets it.

**Deployed 2026-10-25 evening:**
- A parallel session released Crontal v114/v115 and Handler v41/v42: the tenant-leak fix, the email-intake lockdown, AI identity (migration `u6q7r8s9t0v1`), and the mailbox code.
- The driver then released **Handler v43** (`rebuild` @ 4637d76): `mail:` threads are now untrusted, so the lockdown floor also applies to connected mailboxes. Backup b022 was taken first. Rollback targets: Crontal v115, Handler v42.
- Verified: each instance's prompt names its own AI user (main is "Brava Canada AI", user 331; bss is "Brava Stainless Steel AI", user 332).
- The mailbox integration is not enabled on any instance yet.
- `feat/private-user-spaces` is still unmerged. It conflicts with `rebuild` (run.py, settings_api.py, instructions.py) and changes AI Spend usage reads, mailbox thread visibility and the console sign-in view.

**2026-10-26 01:13 UTC: mailbox integration enabled** on both instances (`HANDLER_INTEGRATIONS=crontal,mailbox`, `HANDLER_I_BSS__INTEGRATIONS=crontal,mailbox`, release v44). Crontal now comes from the integration, not the legacy wiring. Email intake and Google are unaffected; main's Gmail is still active. Each instance's prompt still names its own AI user. No mailboxes are connected yet.

**2026-10-27 13:02 UTC: `yeup` instance** for Shanghai Yeup Tube Co., Ltd (Crontal merchant 298; Lisa Lee is user 265) at `/i/yeup`, release v45. Schema `handler_yeup`, prefix `handler/yeup/`, integrations `crontal,mailbox`, same model/harness as bss. Crontal registry #34, AI user 364 ("Shanghai Yeup Tube Co., Ltd AI"). Verified: health, admin keys isolated across instances (401), the agent token mints only for 364 (Lisa and bss's AI are 403), and the prompt names the Yeup AI user. `sales@yeup-tube.com` goes on **yeup** (not bss); Lisa connects it from Crontal → Handler settings. DB: Crontal's Postgres role allows 20 connections; 12 were in use before yeup, which can add up to 4.

**Deployed 2026-10-28 01:19–01:25 UTC:** Handler **v46** (`rebuild` @ 8fc08fc) and Crontal **v116** (`main` @ 2bb985e). Backup b023 first; migration `v7r8s9t0u1w2` (received RFQs) applied, all 20 existing RFQs became `marketplace`. Rollback targets: Handler v45, Crontal v115.
- Handler: `GET /api/approvals?thread_key=`; approval requests logged in the transcript (`approval_request` event block); `ask_user` questions (`docs/rebuild/ask-user.md`, `questions` table created at startup, `waiting_answer` task status).
- Crontal: received RFQs (sellers record RFQs from buyers outside Crontal), date-only values shown on the right day, approval and question cards inline in the Handler chat panel, Tasks show `waiting_answer`.
- Verified: health on `/`, `/i/main`, `/i/bss`, `/i/yeup`; `/i/nope` 404; the new routes answer; Crontal health. No real agent turn was run, so approval and question cards are only tested against mocked Handler responses.
- Seen before the deploy: H12 timeouts at 01:08 UTC on Crontal's Handler proxy and notification routes. Likely long-lived proxied SSE streams exhausting gunicorn's 2x4 threads; to investigate.

**Waiting on the owner:**
- Rotate the OpenAI key (`OPENAI_API_KEY` on Crontal, `CODEX_API_KEY` on `crontal-agent`) and the Bucketeer keys.
- Send a test email to `contact@crontal.ai` to confirm the Gmail copy still arrives.

**Deployed 2026-11-08 11:23–11:26 UTC:** Handler **v47** (`rebuild` @ 524422d) and Crontal **v118** (`main` @ 594539d). Backup b025 first; migration `z0f1i2l3e4s5` (files drive) applied. Rollback targets: Handler v46, Crontal v117.
- Handler:
  - `GET /api/threads?type=&exclude_type=`;
  - memory kinds, links, `memory_search`, run-start memory, and `memory.save` unasked on untrusted threads (`memory-kinds.md`);
  - `contact@` intake copies under the read-only `intake` account;
  - Crontal file deletes always need approval (`always_ask`), and uploads carry provenance.
- Crontal:
  - agent panel split into Conversations / Inbox / Tasks, and tool calls ordered after the user message;
  - "Saved RFQs" renamed to Inquiries (default tab), with a quote button per inquiry;
  - Drive-style Files: `folder_path` and folder rows on `file`, provenance, trash; agent tokens can only trash.
- Verified: all instance health checks, cross-instance 401, the bss chat filter, Crontal health, the file backfill (29 rows, none lost).
- bss lost memory saves recovered: 45 expired and 6 denied, executed via `/api/admin/memory-saves/recover`. The resulting topics have duplicate names (e.g. five topics for ZM Tubes PO 30595); merging them is pending owner approval.
- Not deployed yet: Crontal `fix/inquiry-background-extraction` (Read RFQ reads in the background; migration `a1i2n3q4r5x6`). The cause: extraction took more than 30 s, and the browser and Heroku cut the request off.

**Deployed 2026-11-08 12:56–13:05 UTC:** Handler **v49** (`rebuild` @ 19c1450) and Crontal **v119** (`main` @ 0cb7c11). Backup b027 first; Crontal migration `a1i2n3q4r5x6` (inquiry background extraction) applied; `GUNICORN_THREADS=16` set on Crontal. Rollback targets: Handler v48, Crontal v118.
- Handler: compact Crontal route catalog in the stable prompt (`efficiency.route_catalog*`, ~1.7k tokens; only 13% of bss runs call Crontal, so keep it small); chat `context_links` (page context -> run-start memory); mail-intake plan doc (`mail-intake-plan.md`, not built).
- Crontal: Read RFQ reads in the background (`intake_status` extracting/extraction_failed, retry, line editor); Files My files/Trash tabs; grouped tool calls in chat; bounded DB pool (2+2 per process; with Handler's 12 the 20-connection DB is exactly full: no headroom for extra processes), streams release DB sessions and drop dead clients within ~15 s; cached thread list; page context chip for new chats.
- bss memory tidied by hand (46 -> 36 topics, 26 linked); backup in the session scratchpad only.
- Verified: Handler instance health/404, catalog present in prompt; Crontal health, new bundle served, DB 17/20 after restart.

**Backlog:**
- **Mail intake: keep everything, wake less.** Plan in `mail-intake-plan.md` (owner review 2026-11-08, not started).
- Paginate Crontal's notification list (see §6).
- Encrypt the secrets in Crontal's `HandlerInstance` registry.
- `/api/runs/{id}` still returns steps to non-admins; make them admin-only.
- Retention for run history.
- The failure-notice rate limit is kept in memory.

**Ops notes:**
- `heroku run` from a background shell can hang after it finishes. Check `alembic_version` directly.
- Prune Docker's build cache regularly; it once filled the disk at about 50 GB.
