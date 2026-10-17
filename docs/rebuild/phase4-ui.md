# Rebuild — Phase 4: port the legacy web UI to the runtime

Goal: the runtime gets the legacy handler's full web UI (chat, memory, usage, automations, activity, logs, settings, tools, files, connections) plus approvals, notifications and users, so the personal instance no longer needs the legacy UI.

- **Reference UI**: `src/handler/channels/static/{index.html,app.js,style.css}` (vanilla HTML/JS, about 2,000 lines) and its backend `src/handler/channels/admin.py` + `web.py`. Keep its look, layout and interaction style.
- **Runtime APIs**: see `src/handler/runtime/web/`, `gateway/api.py`, `web/settings_api.py`, `web/auth_api.py`, and `runtime/auth.py`. Every `/api/*` route needs `Authorization: Bearer <token>` (user token, or the admin key with `X-Handler-User`); SSE routes also accept `?access_token=`.
- Rules from `docs/rebuild/phase1.md` and `phase2.md` apply. Tests via `PYTHONPATH=src /Users/dannyliu/dev/repos/handler/.venv/bin/python -m pytest tests/runtime -q -p no:cacheprovider` (whole suite green). Don't modify `types.py`, `store/base.py` or `gateway/base.py`. `harness/base.py` `RunRequest` already has `images: list[Path]`.

## W. Backend APIs (package W)

**Files**:
- `runtime/web/admin_api.py` (new router; mount it in `server.py`)
- `runtime/uploads.py`
- small edits to `runtime/web/app.py` (chat attachments), `runtime/run.py` (attachments into the run), `harness/codex.py` (`-i <image>` per image)
- `observability/logging.py` (optional log file)
- `server.py` (mounting, `--log-file`)
- tests under `tests/runtime/admin_api/`

All routes require a principal. Member-vs-admin as noted.

- **Memory** (any user):
  - `GET /api/memory` → `[{name, description, version, updated_by, updated_at, size}]`
  - `GET /api/memory/{name:path}` → the topic with content
  - `PUT /api/memory/{name:path}` `{content, description}` with optional `If-Match: <version>`; create when absent; 409 on conflict
  - `DELETE /api/memory/{name:path}`

  Use the store's `MemoryStore` and the name validation in `runtime/memory.py`.
- **Automations** (any user; a member may only edit or delete their own, admins any):
  - `GET /api/automations` → schedules with `id, name, prompt, spec, timezone, enabled, one_shot, next_run_at, last_run_at, created_by, notify, thread_key` plus the latest run status of the automation thread (`last_run: {status, finished_at, error}`)
  - `POST /api/automations` `{name, prompt, schedule, timezone?, notify?}` → validated with `scheduler.parse_spec`; sets `next_run_at` and `one_shot`; calls `scheduler.wake()`
  - `PATCH /api/automations/{id}` (any of name, prompt, schedule, timezone, notify, enabled; recompute `next_run_at` when the schedule or enabled changes)
  - `DELETE /api/automations/{id}`
  - `POST /api/automations/{id}/run` → fire once now: an `automation.fired` event with idempotency key `automation:{id}:manual:{uuid}` routed to its thread, without changing the schedule

  The router needs the `Scheduler` instance; pass it in.
- **Usage**: `GET /api/usage?days=30` → totals (input, cached, output, reasoning tokens, `cost_usd` or null if unpriced, runs) plus breakdowns by day, by model and by thread (top 50), aggregated from the runs table. Members see their own chat threads plus shared threads; admins see all. Add a store-agnostic helper that pages through `list_threads` / `list_runs`, or an efficient SQL query on `SQLiteStore` with a fallback.
- **Tools**: `GET /api/tools` → each gateway spec `{name, action, risk, description, decision}`, where `decision` is the current policy decision for a context acting as the caller.
- **Uploads** (`runtime/uploads.py`):
  - `POST /api/uploads` (multipart, max 20 MB per file, max 10 files) → stored under `<data_dir>/uploads/<user>/<file_id>/<sanitized filename>` → `[{id, filename, size, content_type}]`
  - `GET /api/uploads` (own files; admins all)
  - `GET /api/uploads/{id}` (download)
  - `DELETE /api/uploads/{id}`

  The file id is random, and paths must never escape the uploads dir.
- **Chat attachments**: `POST /api/chat` accepts optional `attachments: [file_id]` (the caller's own uploads) and stores them in the event payload. In `run.py`, attachments of the pending events are resolved to paths:
  - images (`image/*`) → `RunRequest.images` (the Codex harness adds `-i <path>` for each);
  - other files → copied into the thread workspace under `attachments/`, with a line appended to the turn ("Attached files: attachments/<name>, …").

  Messages keep `{type: "file", file_id, filename, content_type}` blocks so the UI can show them.
- **Logs** (admin):
  - `configure_json_logging(..., file=Path | None)` also writes to a rotating file (`RotatingFileHandler`, 10 MB × 5);
  - `--log-file` / `HANDLER_RT_LOG_FILE` (default `<data_dir>/logs/handler-rt.jsonl`);
  - `GET /api/logs?lines=200&level=&q=` tails it, filtered, as parsed JSON objects.
- **Connections**: `GET /api/connections` → `[{id, connector, owner, provider, status, scopes, last_used_at}]` for the caller (admins all). No secrets ever.
- **Tests**: every route, including authorization (member vs admin, other users' data), validation (bad schedule 422, memory 409), upload path safety and size limit, attachments reaching `RunRequest.images` and the workspace, `-i` in the Codex argv, usage aggregation, log tail.

## X. Frontend (package X)

**Files**: `src/handler/runtime/web/static/app/index.html`, `app.js`, `style.css` (port of the legacy UI), plus routing in `runtime/web/app.py`. `GET /` and `GET /app` serve the new UI (unauthenticated shell; data calls carry the token). Keep `/login` (redirect to `/` after login) and `/timeline`. Don't change `pyproject.toml` package data (`static/*` already ships; if subdirectories aren't included, add `static/app/*`).

- **Auth**: reuse the token storage that `login.html` uses. Redirect to `/login` when there's no token or on a 401. Show the signed-in user and a Log out button (as on the timeline). Hide admin-only tabs for members (`GET /api/me` gives the role).
- **Tabs** (legacy layout and style):
  - **Chat**: the thread list (own chats plus shared threads, with type badges), messages with tool calls collapsible, live updates via `/api/threads/{key}/stream?access_token=`, new conversation, and file and image attachments (upload first, then send `attachments`). Show `file` blocks as chips or thumbnails.
  - **Memory**: list with descriptions, a view/edit pane with a textarea (save with `If-Match`, conflict message), create, delete with confirm.
  - **Automations** (legacy "Cron"): a table (name, schedule, next run, last result, enabled toggle), create/edit form with help text for `every 30m` / `cron: …` / `at: …`, run now, delete, and a link to the automation's thread.
  - **Activity** (legacy "Sessions"): recent runs across visible threads (status, thread, duration, tokens, cost) and the step waterfall for a selected run. Reuse the logic of `web/static/timeline.html`.
  - **Usage** (legacy "Tokens"): totals plus by-day bars (simple CSS bars, no chart library), by model, and top threads; the days selector is 1/7/30.
  - **Approvals**: pending (tool, action, args preview, requested by, age) with Approve/Deny and an optional reason; a history toggle.
  - **Notifications**: inbox with mark-read, plus an unread badge on the tab.
  - **Settings** (legacy "Config"): sections from `GET /api/settings`, rendered as forms from their JSON Schema (strings, with a textarea for long text; numbers; booleans; enums; nested objects; string→enum maps for grants). Save with `If-Match`, show 409/422 errors, and offer history with rollback.
  - **Tools**: a table of tools with risk and the current decision.
  - **Files**: uploads list, upload, download, delete.
  - **Logs** (admin): tail with level filter and search, auto-refresh toggle.
  - **Connections** (legacy "Google"): the connections list with an explanatory placeholder ("Gmail connector coming").
  - **Users** (admin): list, create, enable/disable, issue a token (shown once), revoke tokens.
- No build step and no external CDN dependencies. Accessible markup (labels, buttons, focus states) and dark/light via `prefers-color-scheme`, as in the legacy style.
- **Verify**: start the server locally against a temporary data dir with an admin token and click through every tab. Use a headless check if available; at minimum `curl` each page and API the UI calls, and run `node --check app.js`. Add a small test that `/` and `/app` serve the shell.
