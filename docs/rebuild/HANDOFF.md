# Handoff: handler rebuild

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
| Event queue, threads, leases | `store/` (SQLite + Postgres mixins), `dispatcher.py` | Thread types: chat, gmail, automation, crontal. One harness session per thread |
| Run lifecycle | `run.py` (`RunExecutor`, `_plan`) | Model/effort come from the newest chat message's override, then the thread, then per-type settings, then instance defaults. Lost sessions recover by replaying a recap of the transcript |
| **Harnesses** | `harness/codex.py`, `harness/openai_agents.py` | See §3 |
| Gateway | `gateway/` | MCP server at `/mcp/`; per-run tokens; `GrantPolicy` (exact action → glob → risk default); approvals; audit |
| Crontal access | `gateway/crontal_api.py` (generic REST), `gateway/crontal.py` (older curated MCP tools) | See §4 |
| Memory, settings, prompts | `memory.py`, `settings.py`, `instructions.py`, `web/settings_api.py` | Settings are versioned in the store. Thread-type `harness`/`model`/`reasoning_effort` are enums (dropdowns). `GET /api/settings/{s}` returns `defaults`; `GET /api/settings/prompt?thread_type=` returns the rendered prompt |
| Workspace sync | `workspaces.py`, `files.py` | Runs only mark a thread dirty; a background flush uploads every `HANDLER_SYNC_INTERVAL_SECONDS` (300) and again on SIGTERM. Uploads skip harness caches (`.tmp/` etc.), send only files whose content hash changed, and run 16 transfers in parallel |
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

## 4. Crontal integration (both repos)

**Crontal (`/Users/dannyliu/dev/repos/hephaestus`, `main`, deployed as `hephaestus-backend-test`):**
- `controllers/agent_access_controller.py`:
  - `POST /api/v1/agent-access/token` (agent token + `X-Crontal-User`) mints a 15-minute user JWT with `actor: agent`;
  - `GET /api/v1/agent-access/routes` is the API catalog.
- `controllers/agent_mcp_controller.py`: the older curated MCP tools.
- `services/handler_service.py`: entitlement, signed webhook outbox, and the `/api/v1/handler/*` proxy. Entitlement is `is_site_admin_user`, which needs stored role `site_admin` **and** an email in `SITE_ADMIN_EMAILS`; the feature is gated by `HANDLER_ENABLED`.
- Frontend: Handler settings tab (`SchemaForm`, defaults, rendered prompt) and the chat panel (model/effort selects).

**Handler side:**
- The agent reaches all of Crontal through `crontal_api_routes`, `crontal_api_get` and `crontal_api_call`.
  - Calls run with the user's own JWT, so Crontal applies that user's permissions.
  - File responses are saved to `downloads/` in the workspace, with document text extracted.
  - Uploads take a workspace file through `upload_file`.
- `crontal_api.*` and `crontal.*` are granted `allow` on the Heroku instance, because the owner wants full access for now. A proper authorization system for users and the agent is planned; the `actor: agent` claim is the hook for it.
- Webhooks come in at `/ingest/crontal` (HMAC `X-Crontal-Signature`) and create `crontal:{entity_type}:{entity_id}` threads.

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

## 7. In flight (2026-10-18, end of session)

Working mode: the main session acts as architect and hands implementation to background sub-agents in git worktrees. The lead merges into `rebuild`/`main`, runs tests and deploys.

**Done today and deployed:**
- **Native-harness tool arguments.** Tool calls in native-harness transcripts now show their arguments.
- **Pricing.** `prices.yaml` has official rates, and the double-billed cache writes are fixed.
- **Default labels.** Settings show the real default model and effort, e.g. "Default (gpt-6.1-sol)".
- **Crontal storage.** 20 file rows with no S3 object were deleted after backup `b015`, and Crontal now refuses to start outside dev/test without S3.
- **Email intake** (`docs/rebuild/email-intake.md`), live on `crontal-agent`:
  - Cloudflare Worker `crontal-email-intake` (Cloudflare account `yz2936@nyu.edu`) is deployed.
  - The routing rule for `contact@crontal.ai` now points to the Worker. To roll back, change the rule's action to forward to `crontal.contacts@gmail.com`.
  - `email.act_as=1` (Danny, Brava Canada).
  - **`email.allowed_senders` is EMPTY**, so every email is stored and ignored. Needs the client's sender addresses.
  - Not yet verified end to end. Next: a test email should still reach the Gmail inbox.
- **Gmail connector** (`docs/rebuild/gmail-connector.md`), live:
  - Google project `crontal-test`, an agentic OAuth client, published without verification, scope `gmail.modify`.
  - `crontal.contacts@gmail.com` is connected on `crontal-agent`.
  - Handler-side `return_to` for connecting from Crontal is live (`HANDLER_RETURN_TO_ORIGINS=https://crontal.ai`).

**Decided:**
- **Instances are scoped to merchants.** Brava Canada (merchant 2, Danny) runs instance `main`, which is today's `crontal-agent`. Brava Stainless Steel (merchant 1, Eric, `yz2936@nyu.edu`) runs instance `bss`.
- **All instances share one dyno.** One process hosts every instance under `/i/<name>/`, and the root instance is also served at `/`.

**Open branches (unmerged; check whether each finished before using it):**
- hephaestus `feat/handler-google-connection` (`f1a86ac`): the Google account card in Crontal's Handler settings. Merge it together with the next item.
- hephaestus `feat/handler-instances`: the `handler_instance` registry (merchant → base_url plus secrets), a CLI, merchant-based entitlement, routing for the proxy and the outbox, and agent tokens limited to their own merchant. Base URLs may include a path prefix.
- handler `feat/multi-instance` (worktree `../handler-wt-multi`): the multi-instance host, `HANDLER_INSTANCES`, `HANDLER_I_<NAME>__<VAR>`, and the runbook in `docs/rebuild/multi-instance.md`.

**Separate app `crontal-agent-bss`:**
- It was provisioned (schema `handler_bss`, prefix `handler/bss/`, persona adapted to the merchant) and is now **scaled to 0**.
- Its secrets are in the session scratchpad file `bss_instance_secrets.env`, which may be gone. They are also in `heroku config -a crontal-agent-bss`.
- Delete the app (with the owner's OK) once `bss` runs inside `crontal-agent`.

**Rollout order:**
1. Deploy the multi-instance handler with only `main` and check nothing changed.
2. Add the `HANDLER_I_BSS__*` vars and redeploy.
3. Back up the database, deploy Crontal (migrate), then register `main` (root URL) for merchant 2 and `bss` (`…/i/bss`) for merchant 1.
4. Check Handler as Danny and as Eric.
5. Remove Crontal's legacy global `HANDLER_*` vars.

**Infrastructure notes:**
- Instances share Crontal's Postgres (Essential-1: one database user, 20 connections) and one Bucketeer key, so isolation is enforced only by the app. For a hard boundary, move to Standard-0 with a user per schema and a bucket per instance.
- Rotate `hephaestus-backend-test`'s Bucketeer, OpenAI and database credentials: they were printed in a session log.

## 8. Continuation: multi-instance release prepared (2026-10-18)

The in-flight branches in §7 are now implemented and merged:

- Handler `feat/multi-instance` implementation `dc3fb65`, merged to `rebuild`.
  Runbook: `docs/rebuild/multi-instance.md`. Runtime suite: **664 passed,
  2 skipped**. Per-instance configuration uses `HANDLER_I_<NAME>__<ENV_VAR>`.
- Crontal merchant registry `ddad985` and Google connection card `e2faefb`
  are merged and pushed to `main`. Backend suite passed 227 tests plus 22
  subtests; after merging Google, the combined focused suite passed 54 tests.
  Frontend build and targeted ESLint passed.
- Shared database backup **b017** completed successfully.
- Containers are prepared for `crontal-agent` and `hephaestus-backend-test`.
  **No release/configuration/registry mutation has been performed.**
- The Crontal deployment skill explicitly requires confirmation before release.
  Continue with the rollout order in §7 after the owner confirms. Detailed
  Crontal checklist: `docs/handler-instances-rollout-2026-10-18.md`.
- Protected scratch configuration snapshots/payload are under
  `/tmp/crontal-rollout` (directory 700, files 600). Never print their contents.
  They can be reconstructed from Heroku if absent. The prepared BSS payload
  reuses its existing keys, `handler_bss` schema and `handler/bss/` prefix,
  changes public URL to the shared host's `/i/bss`, and limits each Handler
  Postgres pool to 3 connections (plus each store's LISTEN connection).
- Keep main's public URL at the root: its Google OAuth callback is already
  registered there. BSS intentionally inherits neither main Google configuration
  nor main email intake secret. A direct Handler browser login is needed once
  when switching to named-instance browser storage keys.
- Legacy outbox rows without an instance ID are skipped after registry
  activation; they must never be inferred/rerouted into another merchant.
- Register both merchant rows in one database transaction to avoid a partial
  switch from legacy fallback. After checks, remove only Crontal's deprecated
  global URL/admin/webhook/agent-token variables, keeping HANDLER_ENABLED.

## 9. Released: two Crontal merchants on one dyno (2026-10-18)

Owner approved both deployments. This supersedes the pending status in §8.

- `crontal-agent`: image `f19dcb728aea` released in v28; current **v30** runs
  `HANDLER_INSTANCES=main,bss`. One Basic web dyno is up. Both pools cap at 3
  connections each, plus their LISTEN connections.
- Crontal: image `8d1de39aad7e` released in v104; current **v105** removes the
  obsolete global HANDLER_URL/admin/webhook/agent credentials. HANDLER_ENABLED
  remains on. Migration is now `r3n4o5p6q7s8`; backup is **b017**.
- Merchant 2/user 1 (Danny) routes to main at the root. Merchant 1/user 2 (Eric)
  routes to BSS at `/i/bss`. Registry activation inserted both rows atomically.
- Live proxy/persona/threads/Google checks passed for both users, and repeated
  after legacy globals were removed. Each agent token can mint a JWT only for
  its own merchant. Both Crontal MCP discovery calls passed. Webhook signature
  checks passed without creating events. Cross-instance admin keys return 401;
  unknown instance routes return 404.
- Main's Gmail connection and root Google callback are preserved. BSS has no
  inherited Google/email configuration; its existing persona/schema/S3 prefix
  are preserved. BSS user `2` is provisioned as admin in its own store only.
- Crontal's served frontend matches tested bundle `index-UN7gf22n.js` exactly.
  Final log review found no recent runtime errors. Open SSE streams were
  cancelled during the bounded HTTP drain on restarts, followed by completed
  workspace sync and clean shutdown; pre-registration BSS connector 401 retries
  resolved with registry activation.
- The separate `crontal-agent-bss` app remains scaled to zero, not deleted.
- The Crontal deployment skill now covers Handler and coordinated releases;
  existing user authorization persists through releases and migrations.
- Full evidence: hephaestus `docs/handler-instances-rollout-2026-10-18.md`.

## 10. Chat activity visibility fixed and released (2026-10-18)

User reported automations completed but their work was not visible in Chat.
Handler **v31**, image `36f4a50bfe80`, deploys `accf30e` + `0bdb6e7`:

- Chat keeps one instance-wide, permission-filtered SSE subscription even when
  a thread is selected. Background work marks its thread with new activity and
  a badge/toast without moving the user's selected thread.
- Active thread run steps show assistant text and tool progress before the
  final transcript commits. Reconnect, focus and five-second visible-page
  refresh recover durable activity; final transcript replaces previews.
- Automations use their names, include scheduled prompts, and show successful
  notify message text directly in the transcript. Own-user notification events
  produce a toast; another recipient's event does not.
- Chat initially opens the most recent/active listed thread once. New still
  explicitly starts a blank conversation.
- Transcript API serves the newest 200 messages chronologically, fixing threads
  freezing on their first 200 entries. Store defaults remain oldest-first for
  harness recap/replay; no schema migration.
- Validation: **666 passed, 2 skipped**, JS syntax/behavior tests, and headless
  Chrome rendering with isolated mock APIs (named automation, visible reminder,
  running assistant/tool activity, global SSE request). No test model calls.
- Live app.js matches tested source byte-for-byte. Root/main/BSS health and
  cross-instance auth checks passed; main Gmail and BSS isolation preserved.
