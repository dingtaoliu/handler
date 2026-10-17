# Rebuild — Phase 1 work packages

Goal: a vertical slice of the new runtime.

```
chat message → event queue (SQLite) → dispatcher → Codex harness (exec --json, resume)
            → transcript + run steps + cost → reply via the web API
```

Design reference: `docs/agent-runtime-design.md`. New code lives in `src/handler/runtime/`; the legacy modules elsewhere in `src/handler/` are not touched.

## Rules for every package

- **Contracts are fixed.** Do not modify `runtime/types.py`, `runtime/context.py`, `runtime/store/base.py`, `runtime/harness/base.py` or `runtime/bus.py`. If a contract is wrong or missing something, stop and explain the needed change in your final message instead of working around it.
- **Only create or edit the files listed for your package**, plus tests under `tests/runtime/<package>/`.
- **No new dependencies** beyond those already in `pyproject.toml` (stdlib, fastapi, uvicorn, httpx via fastapi's test client, pyyaml).
- **Tests**: pytest with `asyncio_mode = auto`. Run `uv run --extra dev pytest tests/runtime/<package> -q` until green. Tests must not call the network or a real `codex` binary.
- **Commit at milestones** on your branch (`git add` your files only, clear messages), so partial progress survives if you stop early. Don't push.
- Python 3.12+, type hints, small functions, docstrings only where behavior isn't obvious. Match the style of the contract files.
- Final message: what you built, test results, anything unfinished, and any contract change you think is needed.

---

## A. SQLite store and event queue

**Files**: `src/handler/runtime/store/sqlite.py`, `src/handler/runtime/store/schema.sql`, `tests/runtime/store/`.

Implement `SQLiteStore(path: str | Path, *, clock: Callable[[], datetime] = utcnow)` satisfying the `Store` protocol in `store/base.py` (read every docstring there; they are the spec).

- stdlib `sqlite3`, WAL mode, `foreign_keys=ON`, `busy_timeout`. All DB work runs off the event loop through **one dedicated worker thread** (single connection, e.g. a 1-thread executor), so writes are serialized and claims are atomic. Use `BEGIN IMMEDIATE` for multi-statement writes.
- Timestamps stored as ISO-8601 UTC text; JSON fields (payload, blocks, raw, data, usage, trigger_event_ids) as TEXT. Enums stored as their string values.
- Tables: `events`, `threads`, `runs`, `messages`, `run_steps` (columns follow the dataclasses in `types.py`; `run_steps` primary key `(run_id, seq)`). Index `threads(lane, priority, pending_since)` where pending and `events(thread_key, consumed_by_run)`.
- Fencing: every fenced write checks `threads.lease_owner = ? AND lease_version = ?` in the same transaction and returns False otherwise.
- `claim`: select eligible threads per the protocol docstring, set `lease_owner`, `lease_until = now + lease_seconds`, `lease_version = lease_version + 1`, return `Lease`s.
- `complete`: consume exactly the given `event_ids`; keep the thread pending if any routed, unconsumed events remain.
- `wait_for_work`: an `asyncio.Event` set by `put`, `complete` and `fail` (plus the timeout). Same-process only for now.
- `clock` is injectable so tests can move time forward for lease expiry and backoff.

**Tests must cover**: idempotent put; ignored events (no thread); thread created from `ThreadSpec` once; claim ordering (priority, then pending_since) and lane filtering; two owners never hold the same thread; expired lease is reclaimable and bumps the version; every fenced write rejects a stale lease; `complete` leaves the thread pending when a new event arrived after the run's snapshot; `fail` backoff (`available_at`) and dead-lettering; 50 concurrent puts across 10 threads with 3 concurrent claimers never double-claim; message ordering; step add/update/list; run create/finish round-trip including `TokenUsage`.

---

## B. Codex harness adapter

**Files**: `src/handler/runtime/harness/codex.py`, `tests/runtime/harness/`. Fixtures: `tests/runtime/fixtures/codex/exec_new_session.jsonl` and `exec_resume.jsonl` are **real** `codex exec --json` outputs (codex-cli 0.162) — study them.

Implement `CodexHarness` satisfying the `Harness` protocol in `harness/base.py`:

```python
CodexHarness(
    binary: str | Path,               # resolve with codex_binary() helper below
    default_model: str | None = None,
    default_reasoning_effort: str | None = None,
    sandbox: str = "workspace-write",
    auth_json: Path | None = None,    # copied into req.home/auth.json (mode 0600) if missing
    extra_args: list[str] | None = None,
)
def codex_binary() -> str  # $HANDLER_CODEX_BIN, else shutil.which("codex"), else raise
```

- **Command**: `[binary, "exec", "--json", "--ignore-user-config", "--skip-git-repo-check", "-s", sandbox, "-C", workspace, ("-m", model)?, ("-c", f"model_reasoning_effort={effort}")?, *mcp_overrides, *extra_args, ("resume", session_id)?, prompt]`. Flags go before the `resume` subcommand (verified working).
- **Environment**: inherit the parent env, set `CODEX_HOME=req.home` (create it), merge `req.env`.
- **Instructions**: if `req.instructions` is non-empty, write it to `<workspace>/AGENTS.md` before starting (Codex reads it natively).
- **MCP servers**: map each `McpServerSpec` to `-c` TOML overrides: `mcp_servers.<name>.url="..."` for URL servers; `mcp_servers.<name>.command="..."`, `mcp_servers.<name>.args=[...]`, `mcp_servers.<name>.env={...}` for stdio. Quote values as TOML. Unit-test the generated argv only.
- **Event mapping** (stdout, one JSON object per line):
  - `thread.started` → `SESSION {session_id: thread_id}`
  - `turn.started` → `TURN_STARTED`
  - `item.started` → `TOOL_STARTED` for tool-like items (`command_execution`: tool `"shell"`, input `command`; `mcp_tool_call`, `file_change`, `web_search`: tool = item type or server/tool name, input = the item minus bookkeeping fields)
  - `item.completed` → `agent_message` → `ASSISTANT_TEXT {text}`; `reasoning` → `REASONING {text}`; tool-like items → `TOOL_FINISHED {id, tool, kind, output (aggregated_output or result), status, exit_code}`
  - `turn.completed` → `USAGE` with `TokenUsage(input_tokens, cached_input_tokens, cache_write_tokens=cache_write_input_tokens, output_tokens, reasoning_tokens=reasoning_output_tokens)` and `data.model`
  - `turn.failed` / `error` → `ERROR {message, retryable}` then raise `HarnessError`
  - unknown types: skip (debug log), never crash. Malformed lines: debug log and skip.
  - Always set `raw` to the original object.
  - After a clean exit with a completed turn: `FINAL {text: last agent_message text, session_id}`.
- **Failure handling**: non-zero exit or missing `turn.completed` → `HarnessError` with the last ~4 KB of stderr. `limits.timeout_seconds` exceeded → terminate the process group (SIGTERM, then SIGKILL after 5 s) and raise `HarnessError(retryable=True)`. On task cancellation, terminate the process and re-raise. `interrupt(run_id)` terminates the tracked process for that run.
- Read stdout line by line asynchronously (`asyncio.create_subprocess_exec`, `start_new_session=True`); stderr drained concurrently into a bounded buffer.

**Tests**: use a fake `codex` executable (a small Python script written in `tmp_path`) that records its argv and env to a file and replays a fixture, with switches for non-zero exit, a hang (for timeout), malformed lines and `turn.failed`. Cover: argv for new vs resumed sessions; `CODEX_HOME` and `auth.json` copy; `AGENTS.md` written; full event mapping from both fixtures including usage numbers; FINAL text and session id; timeout kill; cancellation kill; error paths. Add one live test marked `@pytest.mark.skipif(os.environ.get("HANDLER_CODEX_LIVE") != "1", ...)`.

---

## C. Observability and cost

**Files**: `src/handler/runtime/observability/pricing.py`, `prices.yaml`, `recorder.py`, `logging.py`, `tests/runtime/observability/`.

- **`PriceTable`** loaded from YAML: entries `{model, effective_from (date), input_per_mtok, cached_input_per_mtok, cache_write_per_mtok, output_per_mtok}`; `model` may be exact or a glob (`gpt-6.1-*`). Exact beats glob; latest `effective_from <= at` wins. `cost(model, usage, at) -> float | None` (None when unpriced or a needed rate is null). Math: uncached input = input − cached; cost = uncached·in + cached·cached_in + cache_write·cw + output·out (reasoning is already inside output — never add it twice). `prices.yaml` ships entries for the models we use with **null rates and a `# TODO: verify` comment** — never invent prices; tests use their own YAML.
- **`RawSink(base_dir)`**: appends each event's `raw` to `<base_dir>/<run_id>.jsonl` and returns a ref `"<run_id>.jsonl#L<n>"`.
- **`StepRecorder(store, run_id, *, model, prices, raw_sink, preview_chars=500)`** with `async on_event(ev: RunEvent)` and `totals() -> (TokenUsage, cost_usd | None)`. It needs only `store.add_step` / `store.update_step`; use a small in-memory fake in tests (don't depend on package A). Mapping:
  - `TOOL_STARTED` → new `TOOL_CALL` step, status `running`, name = tool, `data` = truncated input preview
  - `TOOL_FINISHED` → update the matching step (by `data.id`) with `ended_at`, status (`ok`/`error`), output preview, output size, exit code
  - `ASSISTANT_TEXT` / `REASONING` → completed step with a text preview
  - `USAGE` → `LLM_CALL` step with usage and cost, and add to the running totals
  - `ERROR` → `ERROR` step
  - `SESSION`, `TURN_STARTED`, `FINAL` → no step
  - every step gets `raw_ref` from the sink; `seq` increments from 1
- **`configure_json_logging(level="INFO", stream=None)`**: a JSON-lines formatter with `ts, level, logger, msg`, exception info, and `run_id` / `thread_key` taken automatically from `context.current_run()`.

**Tests**: pricing math (cached split, cache writes, exact vs glob, effective dates, unpriced); the recorder fed the events produced from the codex fixtures (construct `RunEvent`s directly in the test); tool step start → finish update; totals; raw refs; JSON log lines include `run_id` when a context is set.

---

## D. Web API and run timeline

**Files**: `src/handler/runtime/web/app.py`, `src/handler/runtime/web/static/timeline.html`, `tests/runtime/web/`.

`create_app(store: Store, bus: RunEventBus, *, default_harness: str = "codex") -> FastAPI`:

- `POST /api/chat` `{user_id, text, conversation_id?, client_message_id?}` → `conversation_id` defaults to a new uuid; `thread_key = f"chat:{user_id}:{conversation_id}"`; `Event(type="chat.message", source="web", payload={"text": text}, idempotency_key=client_message_id or new uuid, user_id, thread_key)`; `store.put(event, ThreadSpec(key, ThreadType.CHAT, Lane.INTERACTIVE, owner_user_id=user_id, harness=default_harness))`; publish `BusMessage(type="event_queued")`. Returns `{event_id, thread_key, conversation_id, duplicate}`.
- `GET /api/threads?user_id=` · `GET /api/threads/{key}` (thread + pending events) · `GET /api/threads/{key}/messages` · `GET /api/threads/{key}/runs` · `GET /api/runs/{run_id}` (run + steps) — dataclasses serialized to JSON (datetimes ISO, enums as values). Thread keys contain `:`; use a `{key:path}` parameter.
- `GET /api/threads/{key}/stream` and `GET /api/stream` (all threads): Server-Sent Events from `bus.subscribe(...)`, one `data:` JSON line per `BusMessage`, plus a comment heartbeat every 15 s.
- `GET /timeline` serves `timeline.html`: dependency-free HTML/JS. Left: threads list. Right: the thread's messages, and below them its runs; clicking a run shows a step waterfall (kind, name, status, duration bar, tokens, cost, expandable data). A chat box posts to `/api/chat`. Live updates via the SSE stream. Readable, plain styling; dark mode via `prefers-color-scheme`.
- No auth in phase 1 (local only); leave a `# TODO(auth)` at the router.

**Tests**: with an in-memory fake store implementing just the methods you call (don't depend on package A) and a real `RunEventBus`: chat creates the right event and thread spec; duplicate `client_message_id` reports `duplicate: true`; listing endpoints serialize correctly; SSE delivers a published message; `/timeline` returns HTML.

---

## E. Dispatcher and run lifecycle (driver)

Done by the driver after A–D merge: claim loop with lanes, run lifecycle (render pending events → harness → recorder → commit messages, run, consumption, session id), heartbeat, retries, bus publishing, a `handler-rt` entry point, and an end-to-end test with the fake codex.
