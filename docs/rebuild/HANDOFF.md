# Handoff: handler rebuild

Written 2026-10-17 by the previous driver (Claude Code) for the next driver (Codex). Read this first, then `docs/agent-runtime-design.md` (the design) and `docs/rebuild/phase1.md`, `phase2.md`, `phase3.md` (work-package briefs with the rules every package follows).

## 1. What this project is

Handler is being rebuilt as an **agent runtime layered on vendor harnesses**. Codex is the default harness; Claude Code is optional. Handler does not implement its own agent loop. It owns:
- durable event-driven threads;
- the tool gateway (MCP server, grants, approvals, audit);
- shared memory and settings;
- schedules;
- connectors;
- observability and cost;
- storage backends.

Crontal (the `hephaestus` repo) is the first application built on top of it. It's sold as a premium add-on, and the first instance serves Crontal's hardcoded admin users.

- Repo: `/Users/dannyliu/dev/repos/handler` (GitHub `dingtaoliu/handler`).
- **All new code lives in `src/handler/runtime/`.** The legacy modules elsewhere in `src/handler/` are untouched until cutover; they still run the owner's personal instance from `main`.
- **Branch `rebuild`** (pushed) holds all the work. `main` = pre-rebuild code + the design doc.

## 2. Current state (rebuild @ `a327629`)

**Phases 1 and 2 are merged, pushed, and verified live with real Codex.** The full suite is **333 passed, 1 skipped** (the skip is an opt-in live Codex test).

| Area | Code | Status |
|---|---|---|
| Contracts | `runtime/types.py`, `context.py`, `store/base.py`, `harness/base.py`, `gateway/base.py`, `bus.py` | Fixed. Change deliberately and update every implementation |
| Event queue, threads, leases | `store/sqlite.py` (`SQLiteCore`), `store/postgres.py` (`PostgresCore`) | Done, SQLite + Postgres |
| Dispatcher, run lifecycle | `dispatcher.py`, `run.py` (`RunExecutor`, `RunPlan`) | Done |
| Codex harness | `harness/codex.py` | Done. Runs `codex exec --json --ignore-user-config`, isolated `CODEX_HOME`, session resume, `approval_policy="never"`, MCP servers via `-c` overrides with `default_tools_approval_mode="approve"` |
| Observability | `observability/` (step recorder, raw sink, price table, JSON logs) | Done. `prices.yaml` has null rates on purpose until real prices are verified |
| Web API + timeline | `web/app.py`, `web/static/timeline.html` | Done. **No auth yet** (package L adds it) |
| Gateway | `gateway/` (policy, tokens, gateway, MCP server, approvals API) | Done; mounted at `/mcp/` |
| Schedules | `scheduler.py`, `tools/automation.py`, `store/*schedules*` | Done |
| Memory / settings / notify | `memory.py`, `settings.py`, `instructions.py`, `tools/notify.py`, `web/settings_api.py` | Done |
| Postgres domain tables, connections | `store/postgres_domains.py`, `store/sqlite_connections.py` | Done |
| Upstream MCP connectors | `gateway/upstream.py`, `gateway/credentials.py` | Done, not yet wired into the server config |
| File storage | `files.py` (local + S3/Bucketeer) | Done, not yet used by the executor (package N) |
| Server | `server.py` (`python -m handler.runtime.server serve ...`) | Wires all of the above for SQLite or Postgres (`--database-url`) |

### Phase 3 packages (driver update, 2026-10-17)

Packages L, M and N have been completed and reviewed; final integration results
are recorded below. Their original worktrees remain under `handler-wt/`.

- **L — Auth/users:** hashed user tokens, admin key/acting user, user/token CLI,
  API authorization, private chat visibility and authenticated SSE. Open `/login`
  for the browser login shell; `/timeline` itself requires authentication.
  SSE connections recheck credentials, and server access logging is disabled to
  avoid logging query tokens. Admin-key impersonation uses the selected user's role.
- **M — Packaging:** pinned/checksummed Codex image, non-root container, schema
  isolation and schema-specific notifications, environment configuration, and
  deployment instructions in `deploy/README.md`. Container defaults
  `HANDLER_MAX_CONCURRENT=1` to cap runs across both lanes. Outside the container,
  use `--max-concurrent 1` for the same behavior.
- **N — Durability:** sanitized workspace/home backups, restore, conservative
  one-time missing-session retry with a recap and RETRY step. No extra recovery
  retry after tool/output progress. Known lease loss/cancellation skips uploads.
  `HANDLER_FILE_STORE=s3` and `HANDLER_FILE_PREFIX=handler/crontal/` select Bucketeer;
  default local backups under `<data-dir>/files` need persistent disk.

Remaining deployment caveats: hosted sandbox compatibility still needs a real
Heroku check; keep `workspace-write` rather than silently disabling isolation.
FileStore sync uploads do not remove remote files deleted locally. Arbitrarily
named files containing secrets are not detected by the filename exclusions.
The Google OAuth client JSON was absent at review time. No live model calls or
cloud provisioning were performed during this driver pass.

**Known issues:** see `docs/rebuild/known-issues.md`: workspace sync never deletes remote files, and Codex's sandbox can't run in unprivileged containers (likely Heroku). Both need a decision before onboarding non-admin users.

## 3. What's next, in order

1. ~~L, M, N integration~~ **Done 2026-10-17:** all three merged into `rebuild`; full suite 447 passed, 2 skipped (run outside the Codex sandbox, which blocks localhost binding).
2. **Google Workspace spike.** The owner chose a **self-hosted open-source Google Workspace MCP server** (e.g. `taylorwilsdon/google_workspace_mcp`) over Composio, to avoid vendor lock-in. Handler should own OAuth, and tool servers should accept a bearer token per request (design §6.1–6.2). The owner has GCP project `crontal-test` (number 447602706589) and was creating a Web OAuth client with these redirect URIs:
   - `http://localhost:8100/oauth/google/callback`
   - `http://localhost:8000/oauth2callback`

   The client JSON will be at `~/.handler-rt/google_oauth_client.json`. **Never commit it or print its secret.** The spike answers:
   - (a) can that server accept a token from handler instead of running its own OAuth flow;
   - (b) unverified "In production" OAuth app behavior for under 100 users (Testing status expires refresh tokens after 7 days);
   - (c) can it send email, not just draft.

   Then build a generic OAuth2 connect flow in handler (Google first; Microsoft and Slack later as config), a Gmail poller event source (`gmail.received`), and wire `UpstreamConnector` into the server config.
3. **Deploy to Heroku: ask the owner before creating anything.** The target is a new app `crontal-agent` (one Basic dyno, about $7/month) attached to `hephaestus-backend-test`'s Postgres (schema `handler_crontal`) and Bucketeer (own prefix). The owner must decide the OpenAI key; the recommendation is a dedicated key with a monthly spend cap, set as `CODEX_API_KEY`. **The hosted service must use an API key, never the owner's ChatGPT login.** Measure memory on the dyno: a Codex turn peaked at about 214 MB locally.
4. **Crontal integration (phase 3c, in the hephaestus repo):**
   - Crontal hosts an MCP server (RFQs, bids, inventory, files) authenticated as an agent principal through a token endpoint (the `broker` credential provider).
   - A webhook outbox sends `crontal.notification` events, keyed by entity.
   - An entitlement flag routes premium users to handler; the built-in agent stays for everyone else.
   - A native settings tab calls handler's settings API through Crontal's backend.
   - Provisioning goes through handler's admin API.

   Design §12 has the details. Hooks are deliberately not built yet.
5. Later gaps (design §5–§9):
   - `delegate` tool and async `task.completed`
   - `thread.idle` memory consolidation
   - skills sync
   - Claude Code harness
   - container sandbox
   - real model prices
   - personal macbook-server cutover from the legacy code

### Status 2026-10-17 (evening)

- **Hosted sandbox mode** done: `HANDLER_CODEX_SANDBOX`, an environment allowlist, and a Codex shell env policy (`known-issues.md` §2). Live secret-stripping check pending Codex quota.
- **Phase 3c (Crontal integration)** built and verified end-to-end locally (real Crontal + real handler, two processes): MCP tool discovery, signed webhook delivery with retry and idempotency, and the Crontal proxy (status, chat, threads).
  - Handler side is on `rebuild`.
  - Crontal side is on hephaestus branch `handler-integration` (pushed, worktree `../hephaestus-wt/handler-integration`): 198 backend tests pass and the frontend builds.
- **Before merging `handler-integration` into Crontal `main`:**
  1. The owner's uncommitted migration `p1k2l3m4n5q6_bid_currency_categories.py` and `q2m3n4o5p6r7_handler_outbox.py` share `down_revision = "n0j1k2l3m4p5"`. Re-point whichever merges second.
  2. The outbox row commits right after the notification, not in the same transaction (`create_model_instance` commits), so a crash between them can lose one event.
  3. `requests` isn't declared in Crontal's `pyproject.toml`.
- **Not yet run:** a real agent run over Crontal tools (needs Codex quota or an API key).

## 4. Rules and decisions to keep

- **Work-package pattern:** contracts first, then parallel packages in worktrees (`git worktree add ../handler-wt/<name> -b <branch> rebuild`), then review, merge `--no-ff`, run the full suite, push `rebuild`.
- **Storage is mixins.** Each domain gets `store/sqlite_<domain>.py` + `schema_<domain>.sql`, plus a Postgres equivalent in `postgres_domains.py` / `schema_pg_domains.sql`. Compose them into `SQLiteStore` (bottom of `sqlite.py`) and `PostgresStore`.
- **Tools** are `ToolSpec`s with an action id and risk; the gateway authorizes them. Write descriptions for the model: say what the tool does and when to use it. Avoid `oneOf` in schemas, because OpenAI function calling can reject it.
- **Codex as a subprocess:** stdin must be closed (`DEVNULL` / `< /dev/null`); an inherited open stdin stalled runs for an hour. On macOS, `killpg` raises EPERM after the group leader exits, so treat that as "already gone".
- **Codex sandbox:** `workspace-write` makes `.git` read-only, so sandboxed Codex jobs can't commit. Either run the driver session with permission to write `.git`, or leave files uncommitted and commit after review.
- **No vendor lock-in:** MCP plus generic OAuth, with providers swappable by config. Composio is the fallback, not the plan.
- **Cost:** the owner's ChatGPT Plus subscription is for building only. Keep live tests on cheap models (`gpt-6-luna`, low effort).
- **Ask the owner before** creating cloud resources, spending money, pushing to `main`, or anything outward-facing. Routine local work (tests, worktrees, commits and pushes to `rebuild`) needs no confirmation.

## 5. Commands

```bash
cd /Users/dannyliu/dev/repos/handler
# full test suite (don't use `uv run`; the sandbox has no network)
PYTHONPATH=src .venv/bin/python -m pytest tests/runtime -q -p no:cacheprovider
# Postgres tests create a throwaway cluster with /opt/homebrew/opt/postgresql@17/bin/initdb — never touch the Homebrew service's data dir.

# run the server locally against real Codex (dev only; uses the ChatGPT login copied into isolated homes)
HANDLER_CODEX_BIN=$(ls -d ~/.vscode/extensions/openai.chatgpt-*/bin/macos-aarch64/codex | tail -1) \
  .venv/bin/python -m handler.runtime.server serve --data-dir /tmp/handler-dev --port 8100 \
  --model gpt-6-luna --reasoning-effort low --codex-auth-json ~/.codex/auth.json --users danny
# after package L: create a user/token with handler-rt user add / token create;
# open http://localhost:8100/login; POST /api/chat uses a Bearer token,
# with {"conversation_id","text"} (user_id is derived from authentication)
# delete copied auth.json files under <data-dir>/harness_homes afterwards
```

Codex CLI on this machine: `~/.vscode/extensions/openai.chatgpt-*/bin/macos-aarch64/codex` (0.162). It isn't on PATH.
