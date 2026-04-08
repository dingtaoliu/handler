# Handler

Autonomous personal agent framework. You own the event loop, Handler provides composable components.

- **Self-evolving** — the agent can modify its own code and tools
- **Always on** — watchdog process ensures liveness, auto-restarts on failure, and rolls back bad changes
- **Multi-channel** — web UI + Telegram bot (more to come)
- **Persistent memory** — knowledge that scales across interactions

## Install

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv tool install git+https://github.com/dannyliu/handler.git
```

## Setup

```bash
handler init
```

Prompts for your model provider and API key, saves to `~/.handler/.env`. Everything else (database, memory, config) is created automatically on first start.

## Start

```bash
handler start
```

Open http://localhost:8000. On first run, the agent will guide you through configuring its identity and persona.

## CLI

```bash
handler init           # configure API key and provider
handler start          # start in the background
handler stop           # graceful shutdown
handler restart        # stop and start
handler status         # check if running
handler run            # run in foreground (dev mode)
handler logs           # show recent log output
handler logs -n 100    # show last 100 lines
```

## Configuration

All data lives in `~/.handler/`:

```
~/.handler/
├── .env             # API keys (OPENAI_API_KEY or ANTHROPIC_API_KEY)
├── config/          # identity.md, persona.md, system.md
├── memory/          # agent's persistent knowledge files
├── logs/            # daily log files
└── handler.db       # conversation history
```

Override the workspace location with `HANDLER_DATA_DIR`.

Optional: add `TELEGRAM_BOT_TOKEN` to `~/.handler/.env` for Telegram integration.

## Requirements

- Python 3.12+
- API key for OpenAI or Anthropic
