# Actions TODO

## Tool chaining / write_and_run
The SDK already supports parallel tool calls per turn. Evaluate whether `write_and_run` is worth keeping as a dedicated tool or if it should be removed in favor of the LLM calling `write_file` + `run_shell` sequentially. Main motivation: save tokens by reducing tool count.

## Memory tools consolidation
Currently 4 tools: `remember`, `recall_memory`, `forget`, `list_memory`. Rethink around a compact index pattern — the agent should see a lightweight index of all memory files (e.g. tax memory, user memory, project memory) in context, but only load full memory content on demand. Goal: agent knows what exists without paying the token cost of loading everything.

## Cron CLI
Add CLI commands for cron management: `handler cron list`, `handler cron create`, `handler cron delete`. Mirrors the agent's `cron` tool but for direct user access from the terminal.

## Tool sets / lazy loading
Explore grouping tools into sets that can be loaded on demand rather than all at once. The `google_drive` and `gmail` tools already use the `action='help'` pattern to keep descriptions compact. Consider whether more tools should adopt this pattern, or whether tool descriptions should be dynamically trimmed based on conversation context.
