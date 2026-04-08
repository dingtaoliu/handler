# Memory

Agent-controlled persistent knowledge. Survives conversation compaction and session resets.

## Design

Memory is a set of topic files (`data/memory/*.md`) with a central index (`data/memory/index.md`). The index maps topic filenames to short descriptions and is always included in the system prompt. Full topic content is never inlined — the agent loads it on demand.

```
data/memory/
├── index.md              ← always baked into system prompt
├── user.md               ← topic file (full content loaded on demand)
├── tax_situation.md
└── career.md
```

### index.md format

```markdown
- user.md: User profile — Danny Liu, ML engineer, prefers concise responses
- tax_situation.md: 2024 tax prep notes, estimated quarterly payments
- career.md: Career goals and job search status
```

The agent writes descriptions when saving. No auto-derivation.

## Operations

All via a single `memory` tool with an `action` param:

| Action | What it does | Required params |
|--------|-------------|-----------------|
| `save` | Upsert — creates topic if new, appends if existing. Updates index description. | topic, content, description |
| `read` | Load full content of a topic. | topic |
| `rewrite` | Full replace of content + description. Optionally rename via `new_topic`. | topic, content, description |
| `delete` | Remove topic file and index entry. | topic |
| `help` | Return usage docs. | — |

### save (upsert)

The common path. If the topic doesn't exist, creates it. If it does, appends the new content below existing content. Always updates the index description.

### rewrite

For restructuring knowledge — merging topics, correcting outdated info, renaming. Replaces the entire file content and description. If `new_topic` is provided, renames the file (old file deleted, new file created, index updated).

### delete

Removes the topic file and its index entry. Needed for cleanup after merging two topics via rewrite.

## How memory enters the prompt

`Memory.build_prompt_section()` reads `index.md` and returns:

```
# Memory
You have 3 memory topic(s). Use memory(action='read', topic='...') to load full content.

- user.md: User profile — Danny Liu, ML engineer, prefers concise responses
- tax_situation.md: 2024 tax prep notes, estimated quarterly payments
- career.md: Career goals and job search status
```

This is appended to the system prompt on every LLM call. Full content is never included — the agent calls `memory(action='read')` when it needs details.

## Key files

| File | Role |
|------|------|
| `memory/memory.py` | `Memory` class — file operations, index management, prompt builder |
| `tools/builtin.py` | `memory_tool()` factory — creates the `memory` tool closure over a Memory instance |
| `context/context.py` | `AgentContext.build()` — calls `memory.build_prompt_section()` to include index in prompt |
| `channels/admin.py` | Admin API — memory CRUD endpoints for the web dashboard |

## Admin API

The web dashboard has REST endpoints for direct memory access (bypasses the agent):

- `GET /admin/memory` — list all topics
- `GET /admin/memory/{name}` — read topic content
- `PUT /admin/memory/{name}` — write topic content
- `DELETE /admin/memory/{name}` — delete topic

These use `Memory.read()`, `Memory.save()`, `Memory.delete()` directly — they don't go through the tool layer.
