# Channels

Event sources and sinks that connect the outside world to the handler event loop.

## Channel ABC

Defined in `environment.py`. Every channel implements two methods:

- `start(queue)` — begin producing `Event` objects onto the shared asyncio queue
- `deliver(event, response)` — return the agent's response to the original caller

Channels run as concurrent asyncio tasks started by `Environment.run()`.

## Channels

| Channel | File | Transport | Conversation model |
|---|---|---|---|
| **Web** | `web.py` | FastAPI HTTP at `:8000` | Single conversation (`"web"`) |
| **Telegram** | `telegram.py` | Long-polling bot | Per-chat (`"telegram:{chat_id}"`) |
| **Scheduler** | `scheduler.py` | In-process background loops | Varies (uses existing conversation IDs) |
| **Gmail** | `gmail.py` | Google API (tools only, no channel) | N/A — tools, not a channel |
| **Google Drive** | `gdrive.py` | Google API (tools only, no channel) | N/A — tools, not a channel |

## Web channel (`web.py` + `admin.py`)

The web channel is split into two files:

- **`web.py`** — `WebChannel` class (chat UI only): `GET /`, `POST /api/chat`, `GET /api/history`
- **`admin.py`** — `create_admin_router()` returns a FastAPI `APIRouter` with all admin/dashboard endpoints: memory CRUD, config editing, cron management, log tailing, file uploads, token usage, tools list, recovery endpoint

`WebChannel._build_app()` includes the admin router via `app.include_router()`.

The static web UI lives in `channels/static/` (index.html, style.css, app.js, logo.svg).

## Telegram channel

Per-chat conversation isolation. Handles text, photos, documents, and voice messages. Sends typing indicators while the agent is processing. Renders markdown responses with plain-text fallback.

## Scheduler channel

One background loop:
1. **Cron executor** — every 30 seconds, runs due jobs (shell commands or prompt injection)

## Gmail

Not a channel — provides a single `gmail` tool via `gmail_tool()` factory. The tool uses an `action` param (search, read, draft_reply, help) to dispatch. Requires OAuth credentials at `data/credentials/desktop.json`.

## Google Drive

Not a channel — provides a single `google_drive` tool via `gdrive_tool()` factory. The tool uses an `action` param (list, read, create_doc, create_sheet, update_doc, update_sheet, help) to dispatch. Uses the same OAuth client (`data/credentials/desktop.json`) but a separate token (`data/credentials/drive_token.json`). Requires Drive and Sheets APIs enabled in Google Cloud Console.
