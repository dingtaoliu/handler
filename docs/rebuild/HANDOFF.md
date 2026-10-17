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

### In flight when the previous driver stopped (phase 3a/3b)

Three packages, specified in `docs/rebuild/phase3.md`, were being built in git worktrees under `/Users/dannyliu/dev/repos/handler-wt/`. **None were committed or reviewed.** Their builders may have stopped mid-way.

| Package | Worktree / branch | Snapshot of what existed |
|---|---|---|
| L. Auth + users | `handler-wt/p3-l` / `rb3-l` | `sqlite_users.py`, `schema_users.sql`, edits to `sqlite.py`, `postgres*.py`. No `auth.py` or API yet; looked early-stage |
| M. Docker + Heroku | `handler-wt/p3-m` / `rb3-m` | `Dockerfile`, `.dockerignore`, `heroku.yml`, `deploy/`, edits to `server.py` and `postgres.py` |
| N. Durable workspaces + session recovery | `handler-wt/p3-n` / `rb3-n` | `workspaces.py`, edits to `run.py` |

**What to do with them:**
1. For each worktree, run `git -C <wt> status`, `git -C <wt> log rebuild..HEAD` and `git -C <wt> diff`, then compare what exists against its section in `phase3.md`.
2. Run the full suite in that worktree (command in §5).
3. Finish what's missing, review it critically (security matters for L), commit, and merge into `rebuild` with `git merge --no-ff`.
4. **L, M and N all edit `runtime/server.py`.** Merge one at a time and resolve carefully.
5. If a worktree is a mess, it's fine to `git worktree remove --force` it and redo the package from the brief.

## 3. What's next, in order

1. **Finish and merge L, M, N** (above).
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
# then: POST /api/chat {"user_id","conversation_id","text"}; open http://localhost:8100/timeline
# delete copied auth.json files under <data-dir>/harness_homes afterwards
```

Codex CLI on this machine: `~/.vscode/extensions/openai.chatgpt-*/bin/macos-aarch64/codex` (0.162). It isn't on PATH.
