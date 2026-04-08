# Context: What the LLM Sees

This documents everything that goes into a single LLM API call — the full input the model receives when processing a user message.

## The API Call

Every agent backend (OpenAI SDK, OpenAI manual, Claude) makes the same logical call:

```
LLM(
    system_prompt,      ← assembled by AgentContext.build()
    messages,           ← conversation history from SQLite
    tools,              ← tool definitions (names, descriptions, param schemas)
)
```

The three backends differ only in wire format:
- **OpenAI SDK** (`agent/openai.py`): passes `instructions` + `tools` to `OAIAgent()`, SDK handles the loop
- **OpenAI manual** (`agent/openai_manual.py`): system message prepended to messages, manual tool loop
- **Claude** (`agent/claude.py`): `system` param + `tools` param, manual tool loop with `thinking: adaptive`

## 1. System Prompt

Built by `AgentContext.build()` in `context/__init__.py`. Sections are joined with `\n\n`:

```
┌─────────────────────────────────────────────────────┐
│  System layer                                       │
│  Source: data/config/system.md (or DEFAULT_SYSTEM)  │
│  Contains: memory instructions, tool usage guide    │
├─────────────────────────────────────────────────────┤
│  # Identity                                         │
│  Source: data/config/identity.md                    │
│  Contains: agent role, mission, what it does        │
├─────────────────────────────────────────────────────┤
│  # Persona                                          │
│  Source: data/config/persona.md                     │
│  Contains: communication style, tone               │
├─────────────────────────────────────────────────────┤
│  # Earlier Conversation  (if compacted)             │
│  Source: EventStore.get_latest_summary()            │
│  Contains: summary of older messages that were      │
│  compacted to save context space                    │
├─────────────────────────────────────────────────────┤
│  # Memory                                           │
│  Source: data/memory/*.md via Memory class           │
│  If total < 6k chars: full file contents inline     │
│  If total >= 6k chars: index only (name → summary)  │
│    → agent must call recall_memory() for full text  │
├─────────────────────────────────────────────────────┤
│  # Cost Tracking                                    │
│  Source: EventStore.get_token_cost_brief()          │
│  Contains: recent token usage stats                 │
├─────────────────────────────────────────────────────┤
│  # Health Issues  (if any)                          │
│  Source: tools/watchdog.get_health_problems()     │
│  Contains: auto-detected problems for agent to fix  │
└─────────────────────────────────────────────────────┘
```

### Onboarding mode

On first run (no `identity.md`), the system prompt is just:
- System layer (DEFAULT_SYSTEM)
- ONBOARDING_IDENTITY — guides the agent to collect identity, persona, and user info

### End session mode

When a session expires (idle > 4 hours), the system prompt gets an extra section appended:
- `# SESSION ENDING` — tells the agent to review the conversation and persist anything important to memory
- max_turns reduced from 20 → 5

## 2. Messages (Conversation History)

Loaded from SQLite via `EventStore.get_messages(conversation_id)`:

```
[
    {"role": "user",      "content": "..."},
    {"role": "assistant", "content": "..."},
    {"role": "user",      "content": "..."},
    ...
]
```

### Message lifecycle

1. User sends message → `Environment._process()` stores it in SQLite
2. All messages for the conversation are loaded from SQLite
3. Passed to `Agent.run()` as the `messages` list
4. Agent response is stored back to SQLite

### Compaction

When input tokens exceed 100k (`compact_token_threshold`), auto-compaction triggers:
1. All messages except the most recent 10 (`keep_recent`) are summarized
2. The summary is stored in SQLite
3. On next turn, the summary appears in the system prompt under `# Earlier Conversation`
4. The original messages are removed from the messages list

The agent can also trigger compaction manually via the `compact_conversation` tool.

### Cron prompts

Cron job messages are injected as:
```
{"role": "system", "content": "[SCHEDULED TASK — job: job_name]\n...payload...\n(This is an automated cron prompt, not a live user message.)"}
```

## 3. Tools

Registered at startup in `__main__.py` and passed to the agent. The LLM sees each tool as:

```json
{
    "name": "tool_name",
    "description": "Full docstring from the @function_tool decorator",
    "parameters": { "JSON schema from function signature" }
}
```

### Current tool list

| Tool | Description (what the LLM sees) |
|------|------|
| `read_file` | Read any file (PDF, DOCX, code, etc.) with optional line ranges |
| `write_file` | Write a file, auto git checkpoint for handler/ |
| `edit_file` | Find-and-replace in a file |
| `shell` | Run any shell command |
| `web_search` | OpenAI SDK WebSearchTool |
| `search_codebase` | Regex search handler source |
| `cron` | Manage scheduled jobs (action: create/list/delete/help) |
| `compact_conversation` | Summarize older messages to free context |
| `memory` | Persist knowledge via save/read/rewrite/delete actions |
| `gmail` | Email: search/read/draft_reply (optional, requires credentials) |
| `google_drive` | Drive: list/read/create/update docs & sheets (optional, requires credentials) |

Tool descriptions are the full docstring — not truncated. The `Args:` section is parsed into per-parameter descriptions in the JSON schema. This is the main lever for controlling context size from tools.

### The agentic loop

Each LLM call can result in tool calls. The loop works like:

```
for turn in range(max_turns):      # max_turns=20 for chat, 5 for end_session
    response = LLM(system, messages, tools)
    if response has no tool calls:
        return response.text
    for each tool_call in response:
        result = execute_tool(tool_call)
        append result to messages
    # loop continues with updated messages
```

The model can call multiple tools in a single turn (parallel tool calls).

## Token Budget (approximate)

Understanding where tokens go helps reason about context pressure:

| Component | Typical size | Notes |
|-----------|-------------|-------|
| System prompt (system + identity + persona) | 500–1500 tokens | Relatively fixed |
| Memory (full load) | 0–2000 tokens | Grows with memory files, caps at ~6k chars then switches to index |
| Memory (index only) | 100–500 tokens | Just filenames + one-line summaries |
| Compaction summary | 500–2000 tokens | Replaces potentially 50k+ tokens of old messages |
| Tool definitions | ~2000–3000 tokens | All tool names + descriptions + param schemas |
| Conversation messages | Variable | The main consumer — grows until compacted |
| Health/cost sections | 50–200 tokens | Small, conditional |

## Key Files

| File | Role |
|------|------|
| `context/__init__.py` | `AgentContext.build()` — assembles the system prompt |
| `memory.py` | `Memory.build_prompt_section()` — builds the memory section |
| `agent/openai.py` | OpenAI SDK agent — passes system prompt + tools to SDK Runner |
| `agent/openai_manual.py` | OpenAI manual agent — prepends system message, manual loop |
| `agent/claude.py` | Claude agent — system param, manual loop with adaptive thinking |
| `agent/tools.py` | Tool abstraction — builds JSON schemas, converts between API formats |
| `event_store.py` | SQLite — stores/loads messages, summaries, token usage |
| `environment.py` | Event loop — loads messages, calls agent.run(), stores response |
| `data/config/system.md` | System layer (editable, overrides DEFAULT_SYSTEM) |
| `data/config/identity.md` | Agent identity (created during onboarding) |
| `data/config/persona.md` | Agent persona (created during onboarding) |
| `data/memory/*.md` | Agent-controlled knowledge files |
