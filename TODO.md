This file marks the list of TODOs to improve the handler agent framework.
Items are ranked by impact — highest impact first.

---

## 1. Sandbox (Impact: Critical)

**Problem:** The agent has unrestricted access to the host machine. `run_python` uses `exec()` in the main process. `run_shell` uses `shell=True` with no filesystem or network restrictions. `write_file` can write anywhere. A hallucinated `rm -rf /` or a bad `exec()` can crash the handler or destroy user data. This is the single biggest blocker for anyone running this on their personal computer.

**Design:**

Three-tier approach, implementable incrementally:

**Tier 1 — Path restrictions (do first, easy win):**
- Add an allowlist of writable directories to `write_file`: `data/`, `actions_custom/`, and an explicit user-configured workspace path. Reject writes outside.
- `read_file` should have a blocklist: no reading `~/.ssh/`, `~/.aws/`, `/etc/shadow`, etc.
- `run_shell` should reject dangerous patterns (`rm -rf /`, `dd if=`, `mkfs`, `:(){ :|:& };:`, etc.) via a command blocklist. Not bulletproof, but catches the obvious.

**Tier 2 — Subprocess isolation (medium effort):**
- Move `run_python` out of the main process. Spawn a subprocess for each execution instead of `exec()`. This prevents bad code from crashing the handler.
- Run `run_shell` and `run_python` under a restricted user or use `sandbox-exec` (macOS) / `unshare` (Linux) to limit filesystem and network access.
- Set resource limits: max memory (512MB), max CPU time (60s), max file size.

**Tier 3 — Container sandbox (bigger project, aligns with Docker):**
- Run all agent code execution inside a lightweight Docker container or podman sandbox.
- Mount only `data/` and `handler/` into the container. Network access is opt-in per tool call.
- The handler process itself stays on the host (for channel I/O and watchdog), but delegates execution to the container.
- This is where the `Dockerfile.sandbox` patterns from OpenClaw could be referenced.

**Recommendation:** Start with Tier 1 immediately — it's a few hours of work and prevents the worst outcomes. Tier 2 for `run_python` (move to subprocess) is the next highest-value change. Tier 3 is a longer-term goal that pairs well with the Runtime Process simplification.

---

## 2. ~~Runtime Process Simplification~~ ✅ Done

Merged job execution into SchedulerChannel (in-process, checks every 30s). Prompt jobs push events directly onto the queue. Shell jobs run via `run_in_executor`. External watchdog slimmed to liveness-only probe (PID check + restart + rollback). Removed `/api/cron` endpoint and `watchdog/jobs.py`.

---

## 3. ~~Token Usage Tracking~~ ✅ Done

Implemented:
- `token_usage` table in SQLite (conversation_id, model, input/output/total tokens, estimated_cost_usd, trigger)
- `Memory.record_token_usage()` — called after every `agent.run()` (chat), compaction, and end_session
- `Memory._estimate_cost()` — model pricing lookup (per 1M tokens) with prefix matching and fallback
- `Memory.get_token_summary(days=None)` — totals, daily breakdown, per-conversation breakdown
- `Memory.get_token_cost_brief()` — one-line summary injected into the system prompt each turn
- `GET /api/tokens?days=N` API endpoint for the web channel
- `MODEL_COSTS` dict with pricing for gpt-4o, gpt-4.1, gpt-5, and variants

**Still TODO:**
- Token budget tool (agent/user can set daily/monthly spending cap)
- Dashboard visualization (depends on web channel rewrite)

---

## 4. ~~Minimize Setup Friction~~ ✅ Done

Implemented:
- Replaced all PDM references with pip (`watchdog/core.py`, `actions/selfmod.py`)
- `pyproject.toml`: renamed project to "handler", removed `[tool.pdm]` sections, relaxed Python to `>=3.11`, added `[tool.setuptools]`
- Created `requirements.txt` (pip fallback for users who don't want editable install)
- Created `.env.example` with OPENAI_API_KEY and optional vars
- Created `setup.sh` — one-command bootstrap: venv + pip install + .env copy
- Cleaned up stale `mail.cli` script entries from pyproject.toml

Requirements are now just: Python 3.11+, an OpenAI API key, and git.

**Still TODO:**
- Dockerfile + docker-compose.yml for zero-friction container setup (pairs with sandbox work)

---

## 5. Versioning (Impact: High)

**Problem:** The repo is yours. When other users clone it and the agent starts committing (via `write_action`, `write_core_file`, `mark_stable`), their git history fills up with agent commits. They can't easily pull your updates. If they fork, their fork diverges immediately.

**Design:**

**Separate code from data. The agent should never commit to the main repo.**

**Option A — Git-ignored data directory (simplest):**
- `data/` is already gitignored (it contains `handler.db`, memory files, uploads, config).
- Move `actions_custom/` under `data/` (e.g., `data/actions_custom/`). Agent-created tools are user data, not repo code.
- The `mark_stable` / rollback system currently operates on the *whole repo*. Change it to operate on a user-local branch or a separate git repo for user customizations.
- User clones repo → gets clean code. Agent writes to `data/`. User can `git pull` freely.

**Option B — Overlay architecture:**
- Keep the handler source as read-only (user never modifies it, agent never modifies it).
- All user customizations (identity, persona, memory, custom actions, core overrides) live in `data/`.
- Instead of `write_core_file` modifying handler source, use a plugin/hook system: agent writes hooks into `data/hooks/` that get loaded at startup and can override behavior.
- Rollback becomes trivial: delete `data/hooks/`, restart.
- `git pull` always works because the source tree is untouched.

**Option C — Release-based distribution:**
- Publish handler as a pip package on PyPI (or a private index).
- Users install it: `pip install handler-agent`.
- No git repo to pollute. Updates via `pip install --upgrade`.
- Agent customizations live in a user-chosen directory.

**Recommendation:** Start with Option A — it's minimal disruption. Move `actions_custom/` under `data/`, adjust paths, make `write_core_file` write to a `data/overrides/` directory instead of the source tree. Long-term, Option C is the cleanest for distribution.

---

## 6. Memory Architecture ✅ (Impact: Medium)

**Problem:** The `Memory` class handles everything: messages, events, summaries, cron jobs, and serves as the gateway to agent-controlled md files. These are conceptually different: system state (messages, events) vs. agent knowledge (md files, structured facts). The agent loads all md files into the system prompt every turn, which won't scale.

**Design:**

**Split into two interfaces:**

**SystemStore** (agent cannot directly access):
- Messages, conversations, summaries, events, cron jobs, token usage
- Methods: `add_message()`, `get_messages()`, `compact()`, `record_event()`, etc.
- This is the "operating system" of the agent

**AgentMemory** (agent reads and writes):
- File-based knowledge (md files) — keep as-is, this pattern works well
- Add a structured key-value store for facts the agent wants to recall without bloating system prompts
- Add a **memory index**: instead of loading ALL md files every turn, maintain a one-line summary of each file. Load the index into the prompt, and let the agent request specific files via a `recall_memory(filename)` tool when needed
- Budget: keep the prompt-loaded memory under a configurable limit (e.g. 8k tokens). If total memory exceeds this, only load the index + most recently accessed files.

**Migration path:**
1. Rename `Memory` → `SystemStore`, extract the cron methods into the in-process scheduler
2. Create `Memory` wrapper that manages `data/memory/` with indexing
3. Add `recall_memory` and `store_memory` tools (the agent already uses `write_file` for this — these are more intentional)
4. Build memory summarization: when a memory file gets too large, offer to summarize or split it

This doesn't need to be done all at once. Start with the memory index to prevent prompt bloat, then split the class later.

---

## 7. Dashboard + Web Channel Rewrite (Impact: Medium)

**Problem:** The web UI is inline HTML/JS in a Python string. It works but can't scale to a dashboard with memory management, token tracking, tool configuration, conversation management, etc.

**Design:**

**Phase 1 — Separate the frontend ✅ Done:**
- Extracted inline HTML/CSS/JS into `channels/static/index.html`, `channels/static/style.css`, `channels/static/app.js`
- Served via FastAPI `StaticFiles` mounted at `/static`. `GET /` returns `FileResponse(index.html)`.
- No build step. Vanilla JS. UI is identical to before, now fully maintainable.

**Phase 2 — React dashboard (bigger project):**
- Use Vite + React + Tailwind (or shadcn/ui since you already use it in hephaestus).
- Build output → `static/dist/`, served by FastAPI.
- Pages:
  - **Chat** — current chat UI, but cleaner. Multi-conversation support.
  - **Memory** — browse/edit agent memory files, view the memory index.
  - **Tokens** — usage charts (daily/weekly/monthly), per-conversation costs, budget settings.
  - **Tools** — list all tools (built-in + custom), enable/disable, view custom action source.
  - **Logs** — tail `handler.log` in real-time (SSE or WebSocket).
  - **Config** — edit identity.md, persona.md, system.md. Manage env vars.
  - **Cron** — view/create/delete scheduled jobs.

**API additions needed:**
- `GET /api/memory` — list memory files with summaries
- `GET /api/memory/:name` — read a memory file
- `PUT /api/memory/:name` — edit a memory file
- `GET /api/tokens/summary`
- `GET /api/tools` — list all tools
- `GET /api/logs?lines=100` — recent logs
- `GET /api/config` — current config files
- `PUT /api/config/:name` — update config

**Authentication:**
- Add a simple password/token auth. The web server currently binds to `0.0.0.0:8000` with zero authentication. At minimum, require a `HANDLER_WEB_TOKEN` env var and check it on all API calls.
- For local-only use, bind to `127.0.0.1` instead of `0.0.0.0`.

**Recommendation:** Phase 1 is a quick win — do it alongside other work. Phase 2 is a separate project that depends on token tracking and memory improvements being in place first.

---

## 8. Self-Evolution Tooling (Impact: Medium-Low)

**Problem:** The agent modifies its own code via `write_core_file`, which requires it to read each file, understand the structure, make targeted edits, and restart. This is slow and error-prone — the agent often needs many tool calls to read, plan, and write.

**Design:**

**Option A — Better built-in tools (easiest):**
- `search_codebase(pattern, path)` — grep/ripgrep over handler source. The agent currently has `run_shell` for this but a dedicated tool would be more discoverable and safer.
- `patch_file(path, search, replace)` — find-and-replace within a file, like a programmatic sed. Safer than rewriting entire files.
- `read_source(path, start_line, end_line)` — read a specific range of a source file. Less overhead than `read_file` which converts everything through markitdown.

These 3 tools would cover 90% of the code modification workflow more efficiently.

**Option B — Delegate to a coding agent:**
- Build a `code_task(instruction)` tool that spawns a subprocess running Claude Code (or aider, or similar) with a scoped task.
- The handler agent describes what it wants ("add a rate-limit middleware to the web channel"), the coding agent does the file reads/edits, and returns a summary.
- Pros: much more capable code modifications, the coding agent has its own context window.
- Cons: additional API cost, dependency on external tool, harder to control.

**Option C — Hybrid:**
- Use built-in tools for small changes (patch a line, add an import).
- Use the coding agent for bigger refactors.
- The agent decides which approach based on task complexity.

**Recommendation:** Start with Option A — `search_codebase` and `patch_file` are easy to build and immediately useful. Option B is worth exploring once the sandbox is in place (you don't want an unsandboxed coding agent either).

---

## 9. Actions Review (Impact: Low)

**Problem:** Are some actions redundant? Specifically, `actions_custom` might not be needed if the agent can write code and run it.

**Analysis of current actions:**

**Keep as-is:**
- `read_file`, `write_file` — fundamental I/O
- `run_shell` — needed for system operations
- `web_search` — needed for information retrieval
- `cron_tools` — needed for scheduling
- `restart_self`, `stop_self` — needed for lifecycle
- `get_handler_status` — useful diagnostic
- `detect_watchdog`, `configure_watchdog` — needed for setup
- Gmail tools — needed for email access

**Consider merging:**
- `run_python` and `calculate` overlap. `calculate` is a sandboxed eval of simple expressions; `run_python` does the same thing with more power. **Keep both** — `calculate` is safer for arithmetic, and the LLM naturally reaches for it for simple math. The cost of keeping it is near zero.

**Consider removing:**
- `mark_stable` — if we move to Option A/B from versioning (agent doesn't modify source), this becomes unnecessary.
- `write_core_file` — same as above. If agent customizations go to `data/`, this tool goes away.
- `write_action` / `delete_action` — if `actions_custom/` moves under `data/`, these should update accordingly. The validation logic (syntax check, function name check) is valuable — keep the tools, just change the path.

**The `actions_custom` pattern is worth keeping.** Even though the agent can write code and restart, `actions_custom` provides a structured way to add tools with validation, git checkpointing, and clean namespacing. It's a guardrail, not redundancy.

**No urgent changes needed.** Actions will naturally evolve as sandbox and versioning are addressed.

---

## Summary: Implementation Order

| Priority | Item | Effort | Why This Order |
|----------|------|--------|----------------|
| 1 | Sandbox (Tier 1 — path restrictions) | Small | Safety-critical, blocks wider adoption |
| 2 | Runtime Process Simplification | Medium | Reduces setup friction, makes everything else easier |
| 3 | Token Usage Tracking | Small-Medium | Users need cost visibility now, foundation for budgets |
| 4 | Setup Friction (PDM → pip, setup.sh) | Small | Quick win, immediately improves onboarding |
| 5 | Versioning (move agent data out of repo) | Medium | Blocks multi-user adoption |
| 6 | Memory Architecture (index + recall) | Medium | Prevents prompt bloat as usage grows |
| 7 | Web Channel Phase 1 (extract static files) | Small | Quick win, prerequisite for dashboard |
| 8 | Dashboard + Web Channel Phase 2 | Large | Depends on 3, 6, 7 being done |
| 9 | Self-Evolution Tooling | Small-Medium | Nice to have, agent already works |
| 10 | Actions Review | Minimal | Falls out naturally from 5 (versioning) |
| 11 | Sandbox Tier 2+ (subprocess, container) | Large | Do after Tier 1 is proven, pairs with Docker |