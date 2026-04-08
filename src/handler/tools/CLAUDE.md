# Tools

Tools exposed to the agent via the OpenAI Agents SDK `@function_tool` decorator.

## How tools work

Each tool is a Python function decorated with `@function_tool`. The full docstring becomes the tool description sent to the LLM (no truncation). The `Args:` section is parsed for per-parameter descriptions. The SDK's strict mode makes all parameters required.

## Modules

| File | Tools | Purpose |
|---|---|---|
| `builtin.py` | `read_file`, `write_file`, `shell`, `web_search` | General-purpose file I/O, code execution, web access |
| `builtin.py` | `memory_tool()` factory | Single `memory` tool with actions: save, read, rewrite, delete, help |
| `selfmod.py` | `_git_checkpoint` (internal helper) | Git checkpoint helper used by guarded file-edit tools |
| `coding.py` | `search_codebase`, `edit_file` | Code navigation and editing |
| `session.py` | `compact_tool()`, `cron_tool()` factories, `compact_messages()` | Conversation compaction and cron job CRUD |
| `gmail.py` | `gmail_tool()` factory | Single `gmail` tool with actions: search, read, draft_reply, help |
| `gdrive.py` | `gdrive_tool()` factory | Single `google_drive` tool with actions: list, read, create_doc, create_sheet, update_doc, update_sheet, help |
| `watchdog.py` | `get_health_problems()` (not a tool) | Health check function called by Agent before building the system prompt |

## Consolidated tool design

Tools are designed to minimize the number of entries in the LLM's tool list while keeping each tool focused:

- **`read_file`** — single reader for all file types (PDF, DOCX, XLSX, code, text, etc.) via markitdown, with optional line ranges
- **`write_file`** — single writer, auto-detects handler source files and creates git checkpoints
- **`edit_file`** — find-and-replace for targeted edits, also auto-checkpoints handler files
- **`shell`** — run any shell command with configurable timeout
- **`memory`** — single tool with `action` param (save/read/rewrite/delete/help)
- **`cron`** — single tool with `action` param (create/list/delete/help) instead of 3 separate tools
- **`google_drive`** / **`gmail`** — single entry point per service with `action='help'` for detailed docs, keeping tool descriptions compact

## Tool factory pattern

Some tools need runtime references (Memory instance, EventStore, conversation ID). These use factory functions that return `@function_tool`-decorated closures:

- `memory_tool(memory)` — captures a `Memory` instance, returns 1 tool
- `compact_tool(run_ctx, agent_getter)` — captures shared `RunContext` and delegates to the active backend, returns 1 tool
- `cron_tool(store, run_ctx)` — captures `EventStore` + shared `RunContext`, returns 1 tool
- `gmail_tool()` — authenticates OAuth, returns 1 tool
- `gdrive_tool()` — authenticates OAuth, returns 1 tool

`RunContext` (from `types.py`) is a shared mutable container holding the current `conversation_id`. Agent sets it at the start of each `run()`, and tool closures read from it.

Factories are called once in `__main__.py` during startup.

## Self-modification safety

`write_file` and `edit_file` (when targeting `handler/` files) create a git checkpoint before making changes. Files in `handler/watchdog/` are blocked from modification. On import failure after restart, the watchdog rolls back to the last `handler-stable` tag.

## Adding new tools

1. Create function with `@function_tool` decorator
2. Write a clear docstring with `Args:` section
3. Export from `__init__.py`
4. Add to the tools list in `__main__.py`
