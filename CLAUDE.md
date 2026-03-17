# Handler

Autonomous personal agent framework. The user owns the event loop, Handler provides composable components.

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

### Core modules

| Module | File | Responsibility |
|---|---|---|
| **Environment** | `environment.py` | Event loop — starts channels, consumes events from queue, routes to agent, delivers responses. Also defines the `Channel` ABC. |
| **Paths** | `paths.py` | Centralized path constants — every module imports from here instead of recomputing paths locally. |
| **Agent** | `agent.py` | Inner reasoning loop — wraps OpenAI Agents SDK, manages compaction (auto-summarize when >100k input tokens), tracks token usage per turn. |
| **EventStore** | `event_store.py` | SQLite persistence — conversations, messages, summaries, cron jobs, token usage, audit log. Self-contained, no handler imports. |
| **Memory** | `memory.py` | Agent-controlled knowledge — markdown files in `data/memory/` with an index for prompt scaling. Self-contained, no handler imports. |
| **AgentContext** | `context.py` | System prompt assembly — layers: system config → identity → persona → compaction summary → memory → health checks. |
| **Types** | `types.py` | `Event`, `Message`, and `RunContext` — shared data types across all modules. |

### Component docs

Subfolders have their own CLAUDE.md with detailed documentation:

- `actions/CLAUDE.md` — tool system, factory pattern, self-modification safety
- `channels/CLAUDE.md` — channel ABC, web/telegram/scheduler/gmail details
- `watchdog/CLAUDE.md` — liveness probe, rollback strategy, auto-configuration

### Request flow

1. Channel creates `Event` → pushes to `Environment.queue`
2. `Environment._process()`: loads messages from SQLite, adds user message, calls `Agent.run()`
3. `Agent.run()`: builds system prompt via `AgentContext.build()`, creates SDK agent, calls `Runner.run()`
4. SDK handles tool-calling loop (up to `max_turns=20`)
5. Response stored in SQLite, delivered back through channel

### Dependency direction

```
__main__.py (wiring)
    ├── Environment ← Agent, EventStore, Channels
    ├── Agent ← AgentContext, EventStore, Actions
    ├── AgentContext ← Memory
    └── Channels ← EventStore, types.Event
```

EventStore, Memory, and Types have zero handler imports — they are leaf dependencies.

## Key Conventions

- **Model**: `gpt-5.4-2026-03-05` (set in `__main__.py`)
- **Config on disk**: `data/config/{system,identity,persona}.md` — plain text, not YAML
- **Memory on disk**: `data/memory/*.md` — managed by `Memory` class, indexed for prompt scaling
- **Database**: `data/handler.db` (SQLite)
- **Logs**: `data/logs/handler-YYYY-MM-DD.log` (daily files)
- **PID file**: `data/handler.pid`
- **Paths**: all defined in `paths.py` — do not recompute `Path(__file__)` in other modules
- **Runtime data dir** (`data/`) is gitignored

## File Structure

```
handler/
├── __main__.py          # entry point, wiring
├── cli.py               # CLI: start, stop, restart, status, run, logs
├── paths.py             # centralized path constants (DATA_DIR, PID_PATH, etc.)
├── agent.py             # Agent — LLM reasoning + compaction
├── context.py           # AgentContext — system prompt builder
├── environment.py       # Environment + Channel ABC
├── event_store.py       # EventStore — SQLite persistence
├── memory.py            # Memory — agent knowledge files + index
├── types.py             # Event, Message, RunContext dataclasses
├── utils.py             # schedule parsing helpers
├── actions/             # tools (see actions/CLAUDE.md)
│   ├── builtin.py       # read_file, write_file, write_and_run, run_python, run_shell, web_search, memory_tools
│   ├── selfmod.py       # mark_stable, restart_self, write_core_file
│   ├── coding.py        # search_codebase, patch_file, read_source
│   ├── session.py       # compact_messages, compact_tool, cron_tools, stop_self
│   └── watchdog.py      # get_health_problems (non-tool health check)
├── channels/            # event sources/sinks (see channels/CLAUDE.md)
│   ├── web.py           # FastAPI chat UI (chat endpoints only)
│   ├── admin.py         # Admin API router (memory, config, cron, logs, files, tools)
│   ├── telegram.py      # Telegram bot (per-chat conversations)
│   ├── scheduler.py     # session expiry + cron job execution
│   ├── gmail.py         # Gmail tools (search, read, draft reply)
│   └── static/          # web UI assets
├── watchdog/            # liveness probe (see watchdog/CLAUDE.md)
│   ├── core.py          # PID checks, restart, import test, rollback
│   └── backends.py      # launchd, systemd, crontab installers
└── data/                # runtime (gitignored)
    ├── config/          # system.md, identity.md, persona.md
    ├── memory/          # agent's persistent memory files + .index.json
    ├── credentials/     # Gmail OAuth (desktop.json, token.json)
    ├── uploads/         # file uploads from web UI
    ├── logs/            # daily log files (handler-YYYY-MM-DD.log)
    └── handler.db       # SQLite database
```
