# Rebuild — Phase 2 work packages

Goal: give the agent real capabilities on top of the phase 1 slice: scheduled automations, handler tools served over MCP with permissions and approvals, shared memory and settings, and a production storage backend.

Design reference: `docs/agent-runtime-design.md` (§3 thread types, §6 gateway and authorization, §9 memory, §10 configuration). Phase 1 brief: `docs/rebuild/phase1.md` — **its rules apply here too**, with these additions:

- **Contracts for phase 2** (fixed, don't modify): the phase 2 section at the end of `runtime/types.py`, the phase 2 protocols at the end of `runtime/store/base.py`, and `runtime/gateway/base.py`.
- **SQLite storage for a domain is a mixin**: write `store/sqlite_<domain>.py` with a mixin class whose methods use `self._call(...)`, `self._clock()`, `self._wake` and the helpers in `store/sqlite.py` (`_transaction`, `_values`, `_record`, `_insert`, `_timestamp`), plus `store/schema_<domain>.sql` (loaded automatically; use `CREATE TABLE IF NOT EXISTS`). In tests, compose `class _Store(YourMixin, SQLiteCore): pass`. The driver adds your mixin to `SQLiteStore` at merge.
- **Tools** are `ToolSpec`s (`gateway/base.py`): one MCP tool per action, snake_case `name`, dotted `action`, a `Risk`, a JSON Schema, and an async `handler(ctx, args) -> ToolResult`. Group them in a class with `tools() -> list[ToolSpec]`.
- **Codex jobs: do not run `git commit`** (the sandbox makes `.git` read-only). Leave your files in the working tree; the driver commits after review.
- Run tests with `PYTHONPATH=src /Users/dannyliu/dev/repos/handler/.venv/bin/python -m pytest tests/runtime/<package> -q`. Dependencies already installed: `mcp`, `psycopg[binary,pool]`, `boto3`, `croniter`, `pyyaml`, `pydantic`, `fastapi`, `httpx`.

---

## F. Automations and scheduler

**Files**: `store/sqlite_schedules.py`, `store/schema_schedules.sql`, `runtime/scheduler.py`, `runtime/tools/__init__.py`, `runtime/tools/automation.py`, `tests/runtime/scheduler/`.

- **`ScheduleMixin`** implementing `ScheduleStore` (read every docstring). `fire_schedule` must reuse the same event-insert + thread-routing logic as `SQLiteCore.put` inside one transaction, guarded by a compare-and-set on `next_run_at`.
- **`parse_spec(spec: str, tz: str, after: datetime) -> tuple[datetime | None, bool]`** returns `(next_run_at_utc, one_shot)`:
  - `every <n><s|m|h|d>` → `after + interval`
  - `cron: <5-field expr>` → next fire after `after`, evaluated in `tz` via `croniter`, returned in UTC
  - `at: <ISO-8601>` → that instant (one-shot); naive times are interpreted in `tz`
  - anything else → `ValueError` with a helpful message.
- **`Scheduler(store, *, clock=utcnow, default_harness="codex", max_sleep_seconds=300)`** with `run_forever(stop: asyncio.Event)`, `wake()` (call after creating, editing or deleting a schedule) and `tick() -> int` (fire everything due now; returns the count; used by tests):
  - For each due schedule, build `Event(type="automation.fired", source="scheduler", payload={"schedule_id", "name", "prompt", "fired_at", "notify", "parent_thread_key"}, idempotency_key=f"automation:{id}:{expected.isoformat()}", thread_key=f"automation:{id}", user_id=created_by)` and `ThreadSpec(key, ThreadType.AUTOMATION, Lane.BACKGROUND, owner_user_id=created_by, harness=schedule.harness or default_harness, model=schedule.model)`.
  - Next run: `parse_spec(spec, tz, after=expected)`; if that is still `<= now` (the process was down), compute from `now` instead, so missed runs collapse into the one firing. One-shot → `None`.
  - Sleep until `next_due_at()` (capped at `max_sleep_seconds`), or until `wake()`/`stop`. Never poll on a fixed short interval.
- **`AutomationTools(store, scheduler)`** (`tools()`):
  - `automation_create` `{name, prompt, schedule, timezone?, notify?}` — `write_internal`; validates the spec, `created_by = ctx.acting_as or ctx.user_id`, sets `one_shot` and `next_run_at`, calls `scheduler.wake()`; returns the schedule id and next run.
  - `automation_list` — `read`; id, name, spec, next run, enabled.
  - `automation_delete` `{schedule_id}` — `write_internal`; wakes the scheduler.

**Tests**: spec parsing (each form, timezone handling for cron and naive `at:`, invalid specs); mixin CRUD, `due_schedules` ordering, `next_due_at`; `fire_schedule` compare-and-set (two concurrent fires → exactly one wins), event idempotency, routing creates the automation thread, one-shot disables; scheduler `tick` fires due schedules and advances them, collapses missed runs, `run_forever` wakes early on `wake()` and stops on `stop`; the three tools.

---

## G. Gateway: policy, approvals, audit, MCP server

**Files**: `gateway/__init__.py`, `gateway/policy.py`, `gateway/tokens.py`, `gateway/gateway.py`, `gateway/mcp_server.py`, `gateway/api.py`, `store/sqlite_gateway.py`, `store/schema_gateway.sql`, `tests/runtime/gateway/`.

- **`ApprovalMixin` + `AuditMixin`** implementing `ApprovalStore` and `AuditStore`.
- **`GrantPolicy(config: dict)`** implementing `Policy`. Config: `{"defaults": {"read": "allow", "write_internal": "allow", "write_external": "ask", "exec": "ask"}, "actions": {"gmail.send": "ask", "memory.*": "allow"}}`. Precedence: exact action → longest matching glob → risk default → `ask`. (User grants come later; for now every user holds every grant.)
- **`RunTokens`**: in-memory `issue(ctx: RunContext, ttl_seconds) -> str` (`secrets.token_urlsafe(32)`), `resolve(token) -> RunContext | None` (expired → None), `revoke(token)`.
- **`Gateway(store, policy, *, emit_event, approval_ttl_seconds=86400, call_timeout_seconds=120, clock=utcnow)`** where `emit_event: Callable[[Event], Awaitable[None]]` is provided by the server (it routes the event to its existing thread):
  - `register(provider_or_spec)`, `specs() -> list[ToolSpec]`.
  - `async call(ctx, tool_name, args) -> ToolResult`: unknown tool → error result; validate `args` against the spec's JSON Schema (use the `jsonschema` package if importable, otherwise check required keys and basic types); decide via policy; then
    - `deny` → error result `"Denied: <reason>"`, audit `denied`;
    - `ask` → store an `Approval` (expires in `approval_ttl_seconds`), audit `pending`, return `{"status": "pending_approval", "approval_id": ..., "message": "This action needs a user's approval. Do not retry it; the decision will arrive in this thread."}`;
    - `allow` → run the handler with `call_timeout_seconds`, audit `ok`/`error` with duration and result size; handler exceptions become error results (never raise to MCP).
  - `async approve(approval_id, decided_by) -> Approval | None`: CAS `pending → approved` (else None); execute the stored call once with `RunContext(run_id=approval.run_id, thread_key=approval.thread_key, user_id=approval.requested_by, acting_as=approval.requested_by)`; CAS to `executed` or `failed` with `result`; audit; `emit_event(Event(type="approval.resolved", source="gateway", payload={approval_id, tool, action, status, decided_by, result_preview}, idempotency_key=f"approval:{id}:resolved", thread_key=approval.thread_key))`.
  - `async deny(approval_id, decided_by, reason="")` and `async expire_stale() -> int` likewise CAS, audit and emit `approval.resolved`.
  - Audit `args_digest` = sha256 of canonical JSON; `args_preview` truncated to 500 chars.
- **`create_mcp_app(gateway, tokens)`**: an ASGI app serving MCP over **streamable HTTP** (stateless) using the installed `mcp` SDK (v1.26; see `mcp.server.lowlevel.Server` and `mcp.server.streamable_http_manager.StreamableHTTPSessionManager`). `list_tools` → every spec (name, description, inputSchema). `call_tool` → resolve `Authorization: Bearer <token>` to a `RunContext` (no or invalid token → the request is rejected with 401), `gateway.call`, return a `CallToolResult` with the JSON-encoded content and `isError`. The server mounts it at `/mcp`; it must manage the session manager's lifespan correctly when mounted under FastAPI.
- **`approvals_router(gateway, store)`**: `GET /api/approvals?status=`, `GET /api/approvals/{id}`, `POST /api/approvals/{id}/approve` `{decided_by}`, `POST /api/approvals/{id}/deny` `{decided_by, reason?}` (409 if not pending), `GET /api/audit?run_id=`.

**Tests**: policy precedence; `call` allow/deny/ask with audit rows; schema validation errors; handler exception and timeout; approve executes exactly once (concurrent approve → one execution), deny, expiry, resolved events emitted; **a real MCP round-trip** — list tools and call one through the streamable HTTP transport with a valid token (e.g. run the app with uvicorn on a free localhost port inside the test, and use `mcp.client.streamable_http.streamablehttp_client` + `ClientSession`), plus rejection without a token; approvals API.

---

## H. Memory, settings, notifications

**Files**: `store/sqlite_memory.py`, `store/schema_memory.sql`, `runtime/memory.py`, `runtime/settings.py`, `runtime/instructions.py`, `runtime/tools/notify.py`, `runtime/web/settings_api.py`, `tests/runtime/memory/`. (`runtime/tools/__init__.py` may already exist from package F; if not, create it empty.)

- **`MemoryMixin` + `SettingsMixin` + `NotificationMixin`** implementing `MemoryStore`, `SettingsStore`, and a small notifications store you define in `sqlite_memory.py`: table `notifications(id, user_id, thread_key, run_id, message, created_at, read_at)` with `add_notification`, `list_notifications(user_id, unread_only)`, `mark_read(id)`.
- **`MemoryTools(store)`** (`runtime/memory.py`): topic names match `^[a-z0-9][a-z0-9/_-]{0,99}$` with no `..`.
  - `memory_list` (read) → one line per topic: `name: description`.
  - `memory_read` `{name}` (read).
  - `memory_save` `{name, content, description}` (write_internal) → create, or append (`existing + "\n\n" + content`) and update the description; retry up to 3 times on a version conflict.
  - `memory_rewrite` `{name, content, description, new_name?}` (write_internal).
  - `memory_delete` `{name}` (write_internal).
  - Plus `index_text(max_chars=4000) -> str` for instructions.
- **Settings** (`runtime/settings.py`): pydantic models per section, defaults included, JSON Schema via `model_json_schema()`:
  - `persona`: `{system, identity, persona}` (strings)
  - `threads`: per thread type (`chat`, `gmail`, `automation`) `{harness, model, reasoning_effort}`
  - `grants`: `{defaults: {risk: decision}, actions: {pattern: decision}}` (same shape as `GrantPolicy`)
  - `limits`: `{run_timeout_seconds, max_attempts, interactive_capacity, background_capacity}`

  `SettingsService(store)`:
  - `get(section) -> (value_dict, version | None)`, defaults when unset
  - `put(section, value, updated_by, expected_version)` → validated through the model; raises `SettingsConflict` / `ValidationError`
  - `history`
  - `rollback(section, version, updated_by)` → appends a new version with the old value
  - `schemas()`
  - `seed_from_files(handler_yaml: Path | None, agent_md: Path | None)` → only fills sections that have never been set. `AGENT.md` is split on `# System` / `# Identity` / `# Persona` headings; `handler.yaml` may contain `threads`, `grants` and `limits` keys.
- **`build_instructions(settings, memory_tools, *, speaker: str | None, thread_type: ThreadType) -> str`** (`runtime/instructions.py`). It concatenates:
  - the persona sections;
  - a "Memory" section with the index and a note to use `memory_read` for details;
  - the speaker ("You are talking with <id>"), when set;
  - short guidance per thread type (chat: reply to the user; gmail: the event is an email, draft rather than send unless told otherwise; automation: a scheduled job, use `notify` to report anything worth a human's attention);
  - a tool note: "Durable facts go to memory_save, not files."
- **`NotifyTools(store, bus)`** (`runtime/tools/notify.py`): `notify` `{message, to}` (write_internal; `to` is a list of user ids or `"all"` — `"all"` needs a `users_provider: Callable[[], list[str]]` constructor arg) → one notification row per recipient plus a `BusMessage(type="notification")` per recipient.
- **`settings_router(service)`** (`runtime/web/settings_api.py`):
  - `GET /api/settings` (all sections with value, version and schema)
  - `GET /api/settings/{section}`
  - `PUT /api/settings/{section}` (body = value; `If-Match: <version>` → expected version; 409 on conflict, 422 on validation)
  - `GET /api/settings/{section}/history`
  - `POST /api/settings/{section}/rollback` `{version, updated_by}`
  - plus `GET /api/notifications?user_id=&unread_only=` and `POST /api/notifications/{id}/read`.

**Tests**: memory CAS (concurrent saves don't lose content), name validation, all memory tools, index truncation; settings defaults, put/validate/conflict, history and rollback, seeding (doesn't overwrite existing), `AGENT.md` parsing; `build_instructions` content per thread type; notify fan-out; the router endpoints (including 409 and 422).

---

## I. Postgres store and file storage

**Files**: `store/postgres.py`, `store/schema_pg_core.sql`, `runtime/files.py`, `tests/runtime/store_pg/`, `tests/runtime/files/`.

- **`PostgresCore(dsn: str, *, clock=utcnow, min_size=1, max_size=5)`** implementing the **phase 1 `Store` protocol** with the same semantics as `SQLiteCore` (read `store/sqlite.py` and its tests in `tests/runtime/store/`):
  - `psycopg` 3 async with `AsyncConnectionPool`; schema created at first use from `schema_pg_core.sql`, with `jsonb` columns and `timestamptz`.
  - `claim` uses `SELECT … FOR UPDATE SKIP LOCKED`; fenced writes check owner + version in the same transaction.
  - `wait_for_work` uses `LISTEN handler_work` on a dedicated connection; `put` / `complete` / `fail` `NOTIFY handler_work`. Waking works across processes; there's a timeout fallback.
  - Phase 2 domain tables come later; don't implement them now.
- **Test fixture**: a session-scoped temporary cluster. Locate `initdb`/`pg_ctl` (`/opt/homebrew/opt/postgresql@17/bin`, then `@16`, then `PATH`); `initdb` into `tmp_path_factory`, start on a free port with a Unix socket in the temp dir, create the database, and stop it at teardown. Skip the tests when the binaries are missing.
- **Shared behavioral tests**: refactor nothing in `tests/runtime/store/`. Instead, write a parametrized contract suite under `tests/runtime/store_pg/` that runs the key queue and fencing scenarios against both `SQLiteCore` and `PostgresCore`: idempotent put, claim ordering, exclusivity, lease expiry, fencing, complete-keeps-pending, fail/backoff/dead, concurrent claimers, messages, steps and runs round-trip. Add a Postgres-only test where two `PostgresCore` instances (as two "processes") claim concurrently without double-claims, and `NOTIFY` wakes the other instance's `wait_for_work`.
- **File storage** (`runtime/files.py`): `FileStore` protocol (`async put(key, data: bytes, content_type=None)`, `get(key) -> bytes | None`, `delete(key) -> bool`, `list(prefix) -> list[str]`, `async sync_down(prefix, local_dir)` / `sync_up(local_dir, prefix)` for workspaces). Keys are `/`-separated and must not escape their prefix (`..` rejected).
  - `LocalFileStore(root)`: files under `root`.
  - `S3FileStore(bucket, prefix="", client=None)`: boto3, run off the event loop; reads Bucketeer-style env vars in a `from_env()` helper (`BUCKETEER_BUCKET_NAME`, `BUCKETEER_AWS_ACCESS_KEY_ID`, `BUCKETEER_AWS_SECRET_ACCESS_KEY`, `BUCKETEER_AWS_REGION`).
  - Tests: full `LocalFileStore` coverage including path-escape rejection and sync round-trips; `S3FileStore` against `botocore.stub.Stubber` or a tiny in-memory fake client.

---

## E2. Integration (driver, after F–I merge)

- Compose the mixins into `SQLiteStore`.
- Executor: issue a run token and pass the gateway MCP server to the harness. Build instructions from settings + memory. Use per-thread-type harness and model from settings.
- Server: mount `/mcp`, the approvals, settings and notifications routers; run the scheduler; route `approval.resolved` and `task.completed` events into their threads; wire `GrantPolicy` from the `grants` settings.
- End-to-end test, plus a live check that Codex calls a handler tool over MCP.
