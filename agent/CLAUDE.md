# Agent Framework — CLAUDE.md

General-purpose, hand-rolled LLM agent loop. No LangChain, no LangGraph at runtime. The framework drives an LLM ↔ tool-call cycle backed by any `ModelProvider` implementation (OpenAI included out of the box).

---

## File Map

```
agent/
├── agent_loop.py       # Main event loop — drives LLM ↔ tool call cycle
├── agent_state.py      # AgentState + AgentStep dataclasses (mutable per-request state)
├── agent_session.py    # AgentSession — config + history context for one request
├── agent_config.py     # AgentConfig dataclass — per-request settings
├── agent_logger.py     # AgentLogger — appends AgentStep records to a JSON-lines log file
├── workspace.py        # Workspace — temp local file cache (/tmp/agent_workspaces/)
├── providers/
│   ├── model_provider.py    # Abstract base: ModelProvider, ProviderResponse, ToolCall
│   ├── openai_provider.py   # OpenAIProvider (production)
│   └── dummy_provider.py    # DummyProvider (dev/test — no API key needed)
└── tools/
    ├── base_tool.py         # AgentTool ABC + ToolResult dataclass
    └── tool_registry.py     # ToolRegistry + build_tool_registry()
```

---

## Minimal Usage

```python
from agent.agent_config import AgentConfig
from agent.agent_session import AgentSession
from agent.agent_state import AgentState
from agent.agent_loop import AgentLoop
from agent.providers.openai_provider import OpenAIProvider
from agent.tools.tool_registry import build_tool_registry

config = AgentConfig(model="gpt-4o-mini", system_prompt="You are a helpful assistant.")
session = AgentSession(config=config)

# Build initial messages
messages = session.build_messages()
messages.append({"role": "user", "content": "Hello!"})

state = AgentState(
    user_message="Hello!",
    system_prompt=config.system_prompt,
    messages=messages,
    max_iterations=config.max_iterations,
)

provider = OpenAIProvider(model=config.model, api_key="sk-...")
tools = build_tool_registry()  # empty by default; register your tools

state = AgentLoop(provider, tools, session).run(state)
print(state.final_response)
```

For streaming:
```python
for event in AgentLoop(provider, tools, session).run_stream(state):
    print(event)  # {"type": "text_delta"|"tool_calls"|"chunk"|"error", ...}
```

---

## How It Wires Together

1. Build `AgentConfig` (model, temperature, system prompt, limits)
2. Create `AgentSession(config=config, history=[...prior messages...])` — subclass to attach auth context, DB sessions, etc.
3. Call `session.build_messages()` to get `[system_msg, ...history...]`, then append the new user message
4. Construct `AgentState` with those messages
5. Instantiate a `ModelProvider` and a `ToolRegistry`
6. Run `AgentLoop(provider, tools, session).run(state)` or `.run_stream(state)`
7. Inspect `state.final_response`, `state.steps`, `state.tokens_used` — persist however you like

---

## Component Details

### AgentLoop (`agent_loop.py`)

Two public methods:
- `run(state) → AgentState` — blocking
- `run_stream(state) → Iterator[dict]` — yields SSE event dicts

**Loop logic:**
```
while iteration < max_iterations:
    call LLM with state.messages + tool schemas
    append assistant message to state.messages
    if no tool calls → set final_response, phase="complete", break
    if enable_approval and any tool requires_approval → phase="approval", return early
    execute each tool call → append tool-result messages to state.messages
    record AgentStep, continue
if max_iterations reached → phase="error"
```

**SSE event types** (streaming only):
| Event | When |
|---|---|
| `step_start` | Before each LLM call |
| `text_delta` | Partial LLM text chunk |
| `tool_calls` | When LLM requests tools |
| `approval_required` | Approval gate triggered |
| `tool_result` | After each tool executes |
| `chunk` | Final text response |
| `error` | LLM failure or max iterations |

### AgentState / AgentStep (`agent_state.py`)

`AgentState` — mutable, one per request:
- `messages: list[dict]` — the OpenAI-format wire list, grows throughout the loop
- `steps: list[AgentStep]` — one per loop iteration
- `phase: str` — `"planning"` | `"executing"` | `"approval"` | `"complete"` | `"error"`
- `final_response: str | None`
- `tokens_used: int`, timing fields

`AgentStep` — one per LLM call:
- `llm_content`, `llm_model`, `tokens_in`, `tokens_out`
- `tool_calls: list[dict]` — `{"id", "name", "arguments"}`
- `tool_results: list[dict]` — `{"id", "name", "result", "error"}`

### AgentSession (`agent_session.py`)

```python
@dataclass
class AgentSession:
    config: AgentConfig
    history: list[dict] = field(default_factory=list)   # pre-built wire messages
    workspace: Optional[Workspace] = None

    def build_messages(self) -> list[dict]: ...
```

Subclass to add app-specific context (auth tokens, DB sessions, user objects) that your tools need via `session=`:

```python
@dataclass
class MyAppSession(AgentSession):
    user_id: int = 0
    auth_token: str = ""
```

### AgentConfig (`agent_config.py`)

| Field | Default | Notes |
|---|---|---|
| `model` | `"gpt-4o-mini"` | Any OpenAI model ID |
| `temperature` | `1` | Ignored for o1/o3 reasoning models |
| `max_tokens` | `None` | No limit |
| `max_iterations` | `10` | Hard limit on loop iterations |
| `llm_timeout` | `90.0` | Seconds per LLM call; 0 = no timeout |
| `enable_tools` | `True` | Set False to get plain LLM responses |
| `enable_approval` | `False` | Set True to pause before write tools |
| `system_prompt` | `"You are a helpful AI assistant."` | Full override |

Build from a dict: `AgentConfig.from_dict(data)` — ignores unknown keys.

### ModelProvider (`providers/`)

Abstract base in `model_provider.py`:
```python
class ModelProvider(ABC):
    def generate(messages, tools, temperature, max_tokens, timeout) -> ProviderResponse
    def generate_stream(messages, tools, temperature, max_tokens, timeout) -> Iterator[dict]
    def get_model_name() -> str
    def supports_streaming() -> bool
    def supports_tools() -> bool
```

`ProviderResponse`:
```python
@dataclass
class ProviderResponse:
    content: Optional[str]
    tool_calls: list[ToolCall]   # ToolCall: id, name, arguments (dict)
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    model: Optional[str]
    raw: Any
```

**`OpenAIProvider`**: wraps `openai.OpenAI`. Automatically omits `temperature` for `o1`/`o3` reasoning models. Supports streaming with usage tracking.

**`DummyProvider`**: no API call. Cycles through preset `responses`. If `tool_calls` is set at init, the first call returns those; subsequent calls return text. Useful for testing the loop without an API key.

### AgentTool / ToolResult (`tools/base_tool.py`)

```python
class AgentTool(ABC):
    name: str
    description: str
    parameters: dict      # JSON Schema object
    requires_approval: bool = False

    def execute(self, session: Any, **kwargs) -> ToolResult: ...
    def to_openai_schema(self) -> dict: ...
```

```python
@dataclass
class ToolResult:
    tool_name: str
    success: bool
    result: Any
    error: Optional[str]

    def to_message(self) -> dict:   # {"role": "tool", "content": ...}
```

### ToolRegistry (`tools/tool_registry.py`)

```python
class ToolRegistry:
    def register(tool: AgentTool)
    def get(name: str) -> Optional[AgentTool]
    def all() -> list[AgentTool]
    def get_openai_schemas() -> list[dict]
```

`build_tool_registry()` returns an empty registry. Register your tools there.

### AgentLogger (`agent_logger.py`)

```python
AgentLogger(session_id="conv_42", log_file="/tmp/agent.log").flush(state)
```

Appends one JSON-lines entry per `AgentStep` to the log file after the loop completes.

### Workspace (`workspace.py`)

Temp directory at `/tmp/agent_workspaces/<session_id>/`. Optional per-session file cache.

```python
Workspace.create(session_id="conv_42")
workspace.get_file(file_id, file_storage_service, file_record) -> Path
workspace.put_file(filename, content) -> Path
workspace.list_files() -> list[Path]
workspace.cleanup()
```

---

## Adding a New Tool

1. **Create the tool file:**

```python
# myapp/tools/my_tool.py
from agent.tools.base_tool import AgentTool, ToolResult

class MyTool(AgentTool):
    name = "my_tool"
    description = "Does something useful."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look up."},
        },
        "required": ["query"],
    }
    requires_approval = False

    def execute(self, session, **kwargs):
        try:
            result = do_the_thing(kwargs["query"])
            return ToolResult(tool_name=self.name, success=True, result=result)
        except Exception as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))
```

2. **Register it:**

```python
registry = build_tool_registry()
registry.register(MyTool())
```

---

## Important Notes

- **Providers never read env vars.** Pass API keys explicitly to the provider constructor.
- **`o1`/`o3` reasoning models** reject `temperature` — the OpenAI provider detects the prefix and omits it.
- **`AgentState.messages`** grows with every iteration — each assistant message and every tool result is appended, giving the LLM full context. This list is not persisted by the loop; the caller owns persistence.
- **`requires_approval`** only triggers when `config.enable_approval=True`. Disabled by default.
- **Tool arguments** arrive as a parsed `dict`. Tools receive them as `**kwargs` — use `.get()` for optional params.
- **Retry logic**: the loop retries on `openai.APITimeoutError` / `openai.APIConnectionError` (up to 2 attempts). The `openai` import is guarded — if the package is absent (custom provider only), retries are simply disabled.

---

## Not Yet Implemented

- **Conversation summarization** — long conversations will eventually exceed the context window.
- **Long-term memory** — no cross-session facts.
- **Per-step timeouts** — `max_iterations` is the only guard; no wall-clock timeout.
- **File-reading tools** — `Workspace.get_file()` exists but no built-in tools use it.
