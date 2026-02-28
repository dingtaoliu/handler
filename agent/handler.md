# Handler — Design Notes

## What is an agent?

An agent is just a stateless LLM call parameterized by a system prompt and a tool set:

```python
llm_call(messages, system_prompt, tools) -> response
```

The "agent loop" is the illusion created by appending messages to a list and calling the API repeatedly until no tool calls are returned. There is no persistent agent — only state you maintain yourself.

## What is a multi-agent system?

Multiple parameterized loop invocations with different system prompts and tool sets, wired together. The word "agent" in multi-agent frameworks is mostly marketing for this pattern.

Two separate agents are equivalent to one agent with a swapped system prompt, **unless** you need:
- **Context isolation** — agent B should only see agent A's final output, not its internal reasoning
- **Parallel execution** — agents A and B run simultaneously, requiring separate state
- **Different tool sets** — agent A has write tools, agent B has read-only tools

## Architecture

### The agent loop (already built)

`agent` is the base layer — a single agent loop:
- Takes `AgentConfig` (system prompt, model, tools, limits) + `AgentState` (messages)
- Drives the LLM ↔ tool call cycle until completion
- Yields SSE events for streaming
- Returns final state — caller owns persistence

### Handler (to be built)

Handler sits above `agent` and orchestrates multiple loop invocations:

```
Handler
├── maintains a task graph or queue
├── spawns agent instances with appropriate configs
├── routes outputs between agents
└── assembles the final result
```

**Key insight:** handler is not a new runtime. It's a coordinator that:
1. Decides which system prompt + tools to use for each step
2. Decides what context to pass between steps (full history vs summary vs just the result)
3. Decides whether steps run sequentially or in parallel

## Concurrency model

A single `asyncio` event loop managing multiple operative coroutines is the right primitive — not separate threads or processes. LLM calls are I/O bound; cooperative multitasking handles this perfectly with near-zero overhead per agent.

```python
# parallel agents in a single loop
results = await asyncio.gather(
    operative_a.run_async(state_a),  # researcher
    operative_b.run_async(state_b),  # analyst
)
# wire results into next step
```

Multiple loops (threads/processes) only make sense for:
- Agents on different machines
- Hard failure isolation between untrusted agents
- True CPU-bound parallelism (rare for LLM workloads)

## Packaging

`operative` lives in `personal/agent/` with its own `pyproject.toml`. Install in downstream projects via editable install during development:

```bash
pip install -e /path/to/personal/agent
```

`handler` will be a separate package that depends on `operative`.

## What handler is NOT

- Not a new agent loop — uses operative as-is
- Not a new LLM abstraction — operative's provider layer handles that
- Not LangGraph, CrewAI, or AutoGen — no graph DSL, no role abstractions, no framework lock-in

Existing multi-agent frameworks (LangGraph, CrewAI, AutoGen, OpenAI Agents SDK) mostly implement sequential agent handoffs, not a true single loop managing N agents concurrently. The single async loop pattern is the novel part.
