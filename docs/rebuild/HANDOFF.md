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
- **Old Codex threads.** Threads created before the switch still use Codex: 3 to 7 minutes per turn before the sync fix, much faster now. New chats use `openai_agents`.
- **Gmail connector.** Not built: Google OAuth client, self-hosted Google Workspace MCP and handler-owned OAuth.
- **Authorization.** The authorization system for users and the agent (above) is planned.
- **Pricing.** `prices.yaml` rates are null, so costs show as unpriced.
- **Workspace sync deletions.** Sync never deletes remote files (`known-issues.md` §1).
- **Codex jobs.** Close stdin (`< /dev/null`). Codex's sandbox makes `.git` read-only, so the driver commits its work. The Codex plan is $20 Plus: run at most 2 jobs at a time.
