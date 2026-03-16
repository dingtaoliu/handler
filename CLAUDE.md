# Handler

Autonomous personal agent framework. The user owns the event loop, Handler provides composable components.

## Core Functionality
self evolution: The agent can modify its own code and tools and modify itself to meet the users needs
liveness: Watchdog process ensures the agent is always running, restarts on failure, rolls back bad code changes
multi-channel: FastAPI web UI + Telegram bot (more to come)
memory: Persistent knowledge across interactions, with index-based scaling and dedicated memory tools
easy onboarding: onboarding wizard helps users set up the agent, and has minimal requirements to install and run


## Quick Reference

```bash
uv sync                    # install deps (creates .venv automatically)
handler start              # start daemonized → http://localhost:8000
handler stop               # graceful shutdown
handler status             # check if running
handler run                # run in foreground (dev mode)
handler logs               # tail recent log output
```

Requires `.env` with `OPENAI_API_KEY`. Optional: `TELEGRAM_BOT_TOKEN`.

## Architecture

Two-loop design: Handler owns the outer event loop (channels → store → agent → response), OpenAI Agents SDK owns the inner reasoning loop (LLM → tool calls → repeat).

### Core Abstractions (RL vocabulary)

| Concept | File | Role |
|---|---|---|
| **Environment** | `environment.py` | Event loop — starts channels, consumes events, routes to agent, delivers responses |
| **Agent** | `agent.py` | Reasoning — wraps `openai-agents` SDK with compaction and logging |
| **EventStore** | `event_store.py` | SQLite persistence — unified `events` audit log + domain tables (conversations, messages, summaries, cron_jobs, token_usage) |
| **Memory** | `memory.py` | Agent-controlled knowledge — manages `data/memory/*.md` files with index-based prompt scaling |
| **Actions** | `actions/` | Tools — builtin, memory, cron, self-modification, watchdog |
| **Context** | `context.py` | System prompt assembly — layers: system → identity → persona → summary → memory |

### Channels

| Channel | File | Transport |
|---|---|---|
| Web | `channels/web.py` | FastAPI at `:8000`, single conversation (`"web"`) |
| Telegram | `channels/telegram.py` | Long-polling bot, per-chat conversations |
| Scheduler | `channels/scheduler.py` | Session expiry (4h idle) + cron job execution (every 30s) |

Channels implement `Channel` ABC: `start(queue)` to produce events, `deliver(event, response)` to return answers.

### Request Flow

1. Channel creates `Event` → pushes to `Environment.queue`
2. `Environment._process()`: loads messages from SQLite, adds user message, calls `Agent.run()`
3. `Agent.run()`: builds system prompt via `AgentContext.build()`, creates `OAIAgent`, calls `Runner.run()`
4. SDK handles tool-calling loop (up to `max_turns=20`)
5. Response stored in SQLite, delivered back through channel

### Memory

Two distinct persistence layers:

- **EventStore** (`event_store.py`) — system state in SQLite: conversations, messages, events audit log, cron jobs, token usage. Not directly controlled by the agent.
- **Memory** (`memory.py`) — agent-controlled knowledge as `data/memory/*.md` files. Managed via `remember()`, `recall_memory()`, `forget()`, `list_memory()` tools.

Memory uses index-based prompt scaling:
- Maintains `.index.json` mapping filename → one-line summary (derived from first heading or first line)
- Below 6k chars total: full file content included in prompt
- Above 6k chars: only the index is included; agent uses `recall_memory()` to load specific files
- Index is synced on each prompt build (mtime-based, skips unchanged files)

### Compaction

When input tokens exceed 100k, old messages are summarized via a separate LLM call. Summary stored in `summaries` table, original messages marked with `compacted_at`. Incremental — new compactions incorporate prior summaries. Recent 10 messages always kept fresh.

### Self-Modification

The agent can write custom tools via `write_action()`:
- Git checkpoint before changes (captures `actions_custom/`, `pyproject.toml`)
- Writes to `handler/actions_custom/{name}.py`
- Calls `restart_self()` to reload
- On import failure: watchdog rolls back to last stable git tag (`mark_stable()`)

The agent can also modify its own core files via `write_core_file()` with the same checkpoint + restart pattern.

### Watchdog (`watchdog/`)

Liveness-only probe run every minute by system scheduler (launchd/systemd/crontab). Checks if handler PID is alive, restarts if dead, rolls back on import failure. Two modules:
- `core.py` — PID checks, process restart, import testing, rollback logic
- `backends.py` — scheduler installer/remover for launchd, systemd, crontab, windows

Job execution happens in-process via `SchedulerChannel`, not the external watchdog.

## Key Conventions

- **Model**: `gpt-5.4-2026-03-05` (set in `agent.py`)
- **Config on disk**: `data/config/{system,identity,persona}.md` — plain text, not YAML
- **Memory on disk**: `data/memory/*.md` — managed by `Memory` class, indexed for prompt scaling
- **Database**: `data/handler.db` (SQLite)
- **Logs**: `data/handler.log` (rotating, 5MB, 3 backups)
- **PID file**: `data/handler.pid`
- **Custom actions**: `actions_custom/*.py` — each module exports `FunctionTool` instances via `@function_tool`
- **Runtime data dir** (`data/`) is gitignored

## Dependencies

- `openai-agents` — agent loop (LLM calls, tool cycling)
- `fastapi` + `uvicorn` — web server
- `markitdown[pdf]` — universal file reading (PDF, DOCX, XLSX, etc.)
- `python-telegram-bot` — Telegram integration
- `google-api-python-client` + auth libs — Gmail integration
- Python 3.12+

## File Structure

```
handler/
├── __main__.py          # entry point, wiring
├── agent.py             # Agent class, compaction, logging hooks
├── context.py           # AgentContext — system prompt builder
├── environment.py       # Environment + Channel ABC
├── event_store.py       # EventStore — SQLite persistence (events audit log + domain tables)
├── memory.py            # Memory — agent-controlled knowledge files with index
├── types.py             # Event dataclass
├── utils.py             # schedule parsing helpers
├── actions/
│   ├── builtin.py       # calculate, read_file, write_file, run_python, run_shell, web_search, memory_tools
│   ├── selfmod.py       # write_action, delete_action, write_core_file, mark_stable, restart_self
│   ├── watchdog.py      # get_handler_status, detect_watchdog, configure_watchdog
│   └── session.py       # compact_tool, cron_tools, stop_self
├── actions_custom/      # agent-written tools (dynamic loading)
├── channels/
│   ├── web.py           # FastAPI chat UI
│   ├── telegram.py      # Telegram bot
│   ├── scheduler.py     # session expiry + cron job execution
│   └── gmail.py         # Gmail OAuth tools
├── watchdog/
│   ├── core.py          # PID checks, restart, import test, rollback
│   └── backends.py      # launchd, systemd, crontab, windows installers
└── data/                # runtime (gitignored)
    ├── config/          # system.md, identity.md, persona.md
    ├── memory/          # agent's persistent memory files + .index.json
    ├── uploads/         # file uploads from web UI
    ├── handler.db       # SQLite database
    └── handler.log      # rotating log
```
