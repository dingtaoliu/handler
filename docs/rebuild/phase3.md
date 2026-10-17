# Rebuild — Phase 3a/3b work packages

Goal: make the runtime safe to expose (authentication) and deployable to Heroku as `crontal-agent` (one Basic dyno, Crontal's Postgres in its own schema, Crontal's Bucketeer under its own prefix). Rules from `docs/rebuild/phase1.md` and `phase2.md` apply (contracts fixed, mixins for storage, Codex jobs don't commit, tests via the venv python).

---

## L. Authentication and users

**Files**: `runtime/auth.py`, `runtime/web/auth_api.py`, `store/sqlite_users.py`, `store/schema_users.sql`, Postgres equivalents appended to `store/postgres_domains.py` + `store/schema_pg_domains.sql` (users section only), `tests/runtime/auth/`, and the minimal edits needed in `runtime/web/app.py`, `runtime/gateway/api.py`, `runtime/web/settings_api.py`, `runtime/web/static/timeline.html` and `runtime/server.py` to apply auth.

- **Users**: table `users(id, display_name, role ['admin'|'member'], created_at, disabled)` and `user_tokens(id, user_id, token_hash, label, created_at, last_used_at, revoked_at)`. Tokens are random (`secrets.token_urlsafe(32)`), shown once, stored as SHA-256 hashes. Mixin methods: `create_user`, `get_user`, `list_users`, `update_user`, `create_token(user_id, label) -> (token_id, token)`, `resolve_token(token) -> User | None` (updates last_used_at), `revoke_token`.
- **Principals** (`runtime/auth.py`): `Principal(user_id: str | None, role, via: "admin_key" | "user_token")`. A FastAPI dependency `require_principal` accepts `Authorization: Bearer <token>`:
  - the **instance admin key** (`HANDLER_ADMIN_KEY` env / `--admin-key`, compared in constant time) → admin principal. An admin request may act for a user with header `X-Handler-User: <user_id>` (this is how Crontal's backend calls on behalf of its users);
  - or a **user token** → that user, with their role.
  - SSE endpoints also accept `?access_token=` because `EventSource` can't send headers.
  - Missing/invalid → 401; insufficient role → 403.
- **Apply everywhere**: all `/api/*` routes and `/timeline` require a principal, except `GET /healthz` (new, returns `{"ok": true}`). `/mcp/` keeps its per-run tokens. Rules:
  - `POST /api/chat`: `user_id` comes from the principal (or `X-Handler-User` for admins), never from the body (drop the field; return 400 if an admin sends neither).
  - Chat visibility: a member only sees their own `chat:` threads and their messages/runs/stream; shared threads (gmail, automation, crontal) are visible to every user. Admins see everything.
  - Settings `PUT`/rollback, approval approve/deny, user management: any authenticated user may approve/deny (design §6.4 initial version), but settings writes and user/token management require `admin`.
- **Admin API** (`runtime/web/auth_api.py`): `POST /api/users` `{id, display_name, role}`, `GET /api/users`, `PATCH /api/users/{id}`, `POST /api/users/{id}/tokens` `{label}` → `{token_id, token}`, `DELETE /api/tokens/{token_id}`, `GET /api/me`.
- **Timeline page**: a small login box that stores the token in `sessionStorage`, sends it on every fetch, passes `?access_token=` for SSE, shows who is logged in, and logs out on 401.
- **CLI**: `handler-rt user add <id> [--admin]` and `handler-rt token create <user_id> [--label]` (printing the token once) operating on the configured store.

**Tests**: token hashing/resolve/revoke; admin key and `X-Handler-User`; 401/403 matrix across routes; members can't read others' chat threads but can read shared threads; `/healthz` open; SSE `access_token`; CLI commands; works on SQLite and Postgres (reuse the store_pg cluster fixture for the users mixin).

---

## M. Packaging and Heroku deployment

**Files**: `Dockerfile` (repo root, replacing nothing — the legacy app has none), `.dockerignore`, `heroku.yml`, `deploy/entrypoint.sh`, `deploy/README.md`, `runtime/store/postgres.py` (only: a `schema` option), `runtime/server.py` (only: `--db-schema`, `--harness-env-file`, `PORT`/env defaults, `/healthz` not included — package L adds it), `tests/runtime/deploy/`.

- **Postgres schema**: `PostgresCore(dsn, *, schema: str | None = None, ...)` — when set, `CREATE SCHEMA IF NOT EXISTS` and pin `search_path` on every pooled and LISTEN connection; NOTIFY channel name includes the schema so two instances in one database don't wake each other. Server `--db-schema` (default `$HANDLER_DB_SCHEMA`). Tests on the throwaway cluster: two schemas in one database are isolated.
- **Image**: `python:3.12-slim`; install deps with `uv sync --frozen --no-dev` (or pip from the lock); copy `src`; install the **Codex CLI Linux binary pinned to a version** from the official GitHub release assets for `openai/codex` (x86_64 musl), verify it runs (`codex --version`) at build time; non-root user; `CMD ["deploy/entrypoint.sh"]`.
- **Entrypoint**: runs `handler-rt serve --host 0.0.0.0 --port "$PORT"` with `--database-url "$DATABASE_URL"`, `--db-schema "${HANDLER_DB_SCHEMA:-handler_crontal}"`, data dir `/tmp/handler` (ephemeral by design — durable state is in Postgres and the file store), and passes `CODEX_API_KEY` through to the harness environment (Codex reads it; no `codex login` needed).
- **`heroku.yml`**: `build: docker: web: Dockerfile`; `run: web: deploy/entrypoint.sh`.
- **`deploy/README.md`**: exact steps for the driver to run later (do not run them): create the app `crontal-agent`, set the stack to container, attach Crontal's Postgres and Bucketeer add-ons from `hephaestus-backend-test` with `heroku addons:attach`, set config vars (`CODEX_API_KEY`, `HANDLER_ADMIN_KEY`, `HANDLER_DB_SCHEMA`, `HANDLER_RT_MODEL`, Bucketeer prefix), deploy, check `/healthz`, create the first admin user. Include the memory caveat (Basic dyno 512 MB; one concurrent run) and how to read logs.
- **Tests**: entrypoint argument construction (pure Python helper or shell test with env), the schema option, and a Docker build smoke test **only if `docker` is available** (skip otherwise; mark slow).

---

## N. Durable workspaces and session recovery

**Files**: `runtime/workspaces.py`, `tests/runtime/workspaces/`, and the minimal edits needed in `runtime/run.py` and `runtime/server.py` to use it.

- **`WorkspaceSync(file_store: FileStore | None, local_root: Path)`**: before a run, `restore(thread_slug)` pulls `workspaces/<slug>/` and `homes/<slug>/` from the file store into the local data dir if they're missing locally; after a run (success or failure), `persist(thread_slug)` pushes them back (excluding `auth.json` and other credential files, which must never be uploaded). With no file store it's a no-op. Uses `FileStore.sync_down` / `sync_up` from `runtime/files.py`.
- **Session recovery** in the executor: if the harness fails on a resumed session because the session can't be found (Codex reports this in its error/stderr — detect it conservatively, e.g. by message match, and also when the session's rollout files are absent from the restored home), clear `thread.session_id` (fenced `set_session` with an empty value, or a new store method if needed — propose it rather than changing contracts silently), and retry once in the same run as a fresh session whose prompt is prefixed with a compact recap built from the thread's last 20 transcript messages (handler's official record). Record a `RETRY` step.
- **Server**: `--file-store local|s3` (s3 uses `S3FileStore.from_env()` with an optional `HANDLER_FILE_PREFIX`), wiring `WorkspaceSync` into the executor.
- **Tests**: restore/persist round-trips with `LocalFileStore`, credential exclusion, no-op without a store; executor recovery path with a fake harness that fails resume once (the second request has no session id and carries the recap), and that a non-session error does not trigger recovery.
