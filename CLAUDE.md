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

Requires `OPENAI_API_KEY` — set it in `~/.handler/.env` or as an environment variable. Optional: `TELEGRAM_BOT_TOKEN`. Override workspace with `HANDLER_DATA_DIR`.

## Architecture

Two-loop design: Handler owns the outer event loop (channels → store → agent → response), the agent backend owns the inner reasoning loop (LLM → tool calls → repeat). The Claude backend uses the Claude Agent SDK (`claude_agent_sdk`) with tools exposed via an in-process MCP server. The OpenAI backend uses the OpenAI Agents SDK Runner.

### Core modules

| Module | File | Responsibility |
|---|---|---|
| **Environment** | `environment.py` | Event loop — starts channels, consumes events from queue, routes to agent, delivers responses. Also defines the `Channel` ABC. |
| **Paths** | `paths.py` | Centralized path constants — every module imports from here instead of recomputing paths locally. |
| **Agent** | `agent.py` | Inner reasoning loop — wraps OpenAI Agents SDK, manages compaction (auto-summarize when >100k input tokens), tracks token usage per turn. |
| **EventStore** | `event_store.py` | SQLite persistence — conversations, messages, summaries, cron jobs, token usage, audit log. Self-contained, no handler imports. |
| **Memory** | `memory/` | Agent-controlled knowledge — markdown files in `data/memory/` with an index.md for prompt inclusion. Self-contained, no handler imports. |
| **AgentContext** | `context/` | System prompt assembly — layers: system config → identity → persona → compaction summary → memory → health checks. |
| **Types** | `types.py` | `Event`, `Message`, and `RunContext` — shared data types across all modules. |

### Component docs

Subfolders have their own CLAUDE.md with detailed documentation:

- `context/CLAUDE.md` — **full breakdown of what the LLM sees** (system prompt, messages, tools, token budget)
- `tools/CLAUDE.md` — tool system, factory pattern, self-modification safety
- `channels/CLAUDE.md` — channel ABC, web/telegram/scheduler/gmail details
- `watchdog/CLAUDE.md` — liveness probe, release auto-update, rollback strategy, auto-configuration

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
- **Workspace**: `~/.handler/` by default; override with `HANDLER_DATA_DIR` env var
- **Config on disk**: `~/.handler/config/{system,identity,persona}.md` — plain text, not YAML
- **Memory on disk**: `~/.handler/memory/*.md` — managed by `Memory` class, indexed via `~/.handler/memory/index.md`
- **Database**: `~/.handler/handler.db` (SQLite)
- **Logs**: `~/.handler/logs/handler-YYYY-MM-DD.log` (daily files)
- **PID file**: `~/.handler/handler.pid`
- **Watchdog config**: `~/.handler/scheduler.json` — backend choice plus release auto-update state
- **Paths**: all defined in `paths.py` — do not recompute `Path(__file__)` in other modules

## File Structure

```
handler/
├── __main__.py          # entry point, wiring
├── cli.py               # CLI: start, stop, restart, status, run, logs
├── paths.py             # centralized path constants (DATA_DIR, PID_PATH, etc.)
├── agent.py             # Agent — LLM reasoning + compaction
├── context/             # AgentContext — system prompt builder (see context/CLAUDE.md)
├── environment.py       # Environment + Channel ABC
├── event_store.py       # EventStore — SQLite persistence
├── memory/              # Memory — agent knowledge files + index (see memory/CLAUDE.md)
├── types.py             # Event, Message, RunContext dataclasses
├── utils.py             # schedule parsing helpers
├── tools/               # tools (see tools/CLAUDE.md)
│   ├── builtin.py       # read_file, write_file, shell, web_search, memory_tool
│   ├── selfmod.py       # _git_checkpoint helper for guarded edits
│   ├── coding.py        # search_codebase, edit_file
│   ├── session.py       # compact_messages, compact_tool, cron_tool
│   ├── gmail.py         # gmail tool (search, read, draft_reply via action param)
│   ├── gdrive.py        # google_drive tool (list, read, create/update docs & sheets)
│   └── watchdog.py      # get_health_problems (non-tool health check)
├── channels/            # event sources/sinks (see channels/CLAUDE.md)
│   ├── web.py           # FastAPI chat UI (chat endpoints only)
│   ├── admin.py         # Admin API router (memory, config, cron, logs, files, tools)
│   ├── telegram.py      # Telegram bot (per-chat conversations)
│   ├── scheduler.py     # session expiry + cron job execution
│   └── static/          # web UI assets
├── watchdog/            # liveness probe (see watchdog/CLAUDE.md)
│   ├── core.py          # PID checks, release updates, restart, import test, rollback
│   └── backends.py      # launchd, systemd, crontab installers + scheduler config
~/.handler/              # workspace (outside repo, works with pip install)
    ├── .env             # OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, etc.
    ├── config/          # system.md, identity.md, persona.md
    ├── memory/          # agent's persistent memory files + index.md
    ├── credentials/     # Gmail OAuth (desktop.json, token.json)
    ├── uploads/         # file uploads from web UI
    ├── logs/            # daily log files (handler-YYYY-MM-DD.log)
    └── handler.db       # SQLite database
```
