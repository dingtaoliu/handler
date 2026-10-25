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
- **Crontal notification list should load in pages (backlog, 2026-10-18).** The bell/inbox component in Crontal's frontend (`NotificationBell` / `notification-dropdown`) should show only the most recent N (e.g. 10) with a "See more" control that loads older pages (cursor/limit API), instead of keeping every notification in the component. It must still scale once handler pushes notifications into Crontal.

## 7. Current state (updated 2026-10-18 19:45 by the lead)

Working mode: the main session acts as architect and hands implementation to background sub-agents in git worktrees. The lead merges into `rebuild`/`main`, runs tests and deploys with the hephaestus `deploy` skill.

**Live:** handler `crontal-agent` @ `6e1db05` (v35+); Crontal `hephaestus-backend-test` v110 @ `25d45a4`.
- **Instances, all on one dyno:** `main` (Brava Canada, merchant 2) at `/`, `bss` (Brava Stainless Steel, merchant 1) at `/i/bss`. Crontal routes requests through its `handler_instance` registry. The separate `crontal-agent-bss` app has been deleted.
- **Notifications:** pushed from handler into Crontal through the generic durable `deliveries` outbox and the Crontal sink. Crontal stores them as `handler_notification` rows and receipts (migration `s4o5p6q7r8t9`).
- **Tasks:** the `/api/tasks` API, handler's Tasks tab, and a Tasks tab in Crontal's agent panel. The debug trace (`steps`) is admin-only in the tasks API.
- **Chat images:** paste, drop or pick images in Crontal's panel. Both harnesses get them as vision input, with copies kept in the workspace.
- **Gmail:** read, search, attachments and drafts, plus `gmail_send` / `gmail_send_draft`. Sends always need approval (`explicit_allow_only`), and the approval pins the exact content. The `google_workspace_mcp` sidecar and Google's preview MCP were evaluated and rejected for now; see `gmail-connector.md` §4.
- **Email intake:** `contact@crontal.ai` goes through the Cloudflare Worker to `/ingest/email`. Senders are gated by a **blocklist** (intentional, by the owner's decision) plus DKIM. It acts as user 1.
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

**Waiting on the owner:**
- Rotate the OpenAI key (`OPENAI_API_KEY` on Crontal, `CODEX_API_KEY` on `crontal-agent`) and the Bucketeer keys.
- Send a test email to `contact@crontal.ai` to confirm the Gmail copy still arrives.

**Backlog:**
- Paginate Crontal's notification list (see §6).
- Encrypt the secrets in Crontal's `HandlerInstance` registry.
- `/api/runs/{id}` still returns steps to non-admins; make them admin-only.
- Retention for run history.
- The failure-notice rate limit is kept in memory.

**Ops notes:**
- `heroku run` from a background shell can hang after it finishes. Check `alembic_version` directly.
- Prune Docker's build cache regularly; it once filled the disk at about 50 GB.
