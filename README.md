# Handler

Autonomous personal agent framework. You own the event loop, Handler provides composable components.

- **Self-evolving** — the agent can modify its own code and tools
- **Always on** — watchdog process ensures liveness, auto-restarts on failure, rolls back bad changes
- **Multi-channel** — web UI + Telegram bot (more to come)
- **Persistent memory** — knowledge that scales across interactions

## Setup

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Clone and configure

```bash
git clone <repo-url> handler
cd handler
cp .env.example .env
```

Edit `.env` and add your API key:

```
OPENAI_API_KEY=sk-...
```

Optional: add `TELEGRAM_BOT_TOKEN` for Telegram integration.

### 3. Install dependencies

```bash
uv sync
```

This creates a `.venv`, downloads the right Python version if needed, and installs all dependencies.

### 4. Start

```bash
handler start
```

Open http://localhost:8000. On first run, the onboarding wizard will help you configure the agent.

## CLI

```bash
handler start          # start in the background
handler stop           # graceful shutdown
handler status         # check if running
handler run            # run in foreground (dev mode)
handler logs           # show recent log output
handler logs -n 100    # show last 100 lines
```

If the venv isn't activated, prefix with `uv run`:

```bash
uv run handler start
```

## Requirements

- Python 3.12+
- `OPENAI_API_KEY` in `.env`
