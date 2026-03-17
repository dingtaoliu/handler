# Actions

Tools exposed to the agent via the OpenAI Agents SDK `@function_tool` decorator.

## How tools work

Each tool is a Python function decorated with `@function_tool`. The full docstring becomes the tool description sent to the LLM (no truncation). The `Args:` section is parsed for per-parameter descriptions. The SDK's strict mode makes all parameters required.

## Modules

| File | Tools | Purpose |
|---|---|---|
| `builtin.py` | `read_file`, `write_file`, `write_and_run`, `run_python`, `run_shell`, `web_search` | General-purpose file I/O, code execution, web access |
| `builtin.py` | `memory_tools()` factory | `remember`, `recall_memory`, `forget`, `list_memory` — agent-controlled knowledge |
| `selfmod.py` | `mark_stable`, `restart_self`, `write_core_file` | Self-modification with git checkpoints and rollback safety |
| `coding.py` | `search_codebase`, `patch_file`, `read_source` | Code navigation and editing of handler source |
| `session.py` | `compact_tool()`, `cron_tools()` factories, `compact_messages()`, `stop_self` | Conversation compaction, cron job CRUD, graceful shutdown |
| `watchdog.py` | `get_health_problems()` (not a tool) | Health check function called by Agent before building the system prompt |

## Tool factory pattern

Some tools need runtime references (Memory instance, EventStore, conversation ID). These use factory functions that return `@function_tool`-decorated closures:

- `memory_tools(memory)` — captures a `Memory` instance
- `compact_tool(store, run_ctx, model, keep_recent)` — captures `EventStore` + shared `RunContext`
- `cron_tools(store, run_ctx)` — captures `EventStore` + shared `RunContext`

`RunContext` (from `types.py`) is a shared mutable container holding the current `conversation_id`. Agent sets it at the start of each `run()`, and tool closures read from it. This replaces the old `_current_conversation_id` instance state on Agent and the `_agent_ref` hack in `__main__.py`.

Factories are called once in `__main__.py` during startup.

## Self-modification safety

`write_core_file` and `patch_file` (when targeting `handler/` files) create a git checkpoint before making changes. On import failure after restart, the watchdog rolls back to the last `handler-stable` tag.

## Adding new tools

1. Create function with `@function_tool` decorator
2. Write a clear docstring with `Args:` section
3. Export from `__init__.py`
4. Add to the tools list in `__main__.py`
