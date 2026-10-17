# Handler — Design

Status: draft, 2026-10-17. Describes the north-star end state. Migrating the existing macbook-server install is out of scope for this document.

## What handler is

Handler is an independent system that turns vendor agent loops (Anthropic's Tool Runner and Claude Agent SDK, the OpenAI Agents SDK, and Codex) into an **always-on coworker**. **Handler doesn't build agent loops from raw model APIs.** Every loop comes from a vendor SDK, which owns reasoning, tool calling and context management. Handler may configure a lightweight in-process agent on top of an SDK, but it never reimplements the loop. Handler is the layer around the harnesses:

| The harness owns | Handler owns |
|---|---|
| The agent loop, planning, tool calling | Events and triggers: chat, Gmail, automations, Crontal |
| Context window, compaction, prompt caching | Threads ↔ harness sessions, ordering, retries, durability |
| Built-in tools (bash, file edit, search) | Users, identities, chat visibility |
| Synchronous sub-agents | Durable asynchronous delegation |
| Its own session transcript | The official record of each thread; delivery and notifications |
| | Authorization and approvals (MCP gateway + permission hooks) |
| | Shared memory and skills, provided to every harness |
| | Connectors exposed as MCP servers |
| | Workspaces, sandboxing, secrets, usage and cost |

**One handler instance is one agent**: a single persona with its own configuration, memory, skills, automations, connectors and data store. Instances are not aware of each other. Running many agents (for example one per Crontal merchant) means running many instances. Routing users or merchants to the right instance is the job of the embedding application (such as Crontal), not of handler.

```
┌──────────────────────────────── handler instance ────────────────────────────────┐
│                                                                                  │
│  writers                      event queue              readers                   │
│  chat · gmail poller ──put──▶ events ──▶ threads ──claim──▶ run                  │
│  scheduler · crontal webhook  (store)    (leases)            │                   │
│                                                              ▼                   │
│                                         workspace (AGENT.md, skills, files)      │
│                                                              │                   │
│                                     harness session: Claude Agent SDK | Codex    │
│                                        │ built-in tools          │ MCP tools     │
│                                        ▼ (permission hooks)      ▼               │
│                                     ┌────────── tool gateway (authorize, audit) ─┐│
│                                     │ memory · skills · notify · automation ·    ││
│                                     │ delegate · gmail · drive · crontal         ││
│                                     └────────────────────────────────────────────┘│
│  deliveries ──▶ web · telegram · email                                           │
│                                                                                  │
│  Store (SQLite | Postgres)   FileStore (local | object)   SecretStore            │
└──────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Current capabilities vs. goal state

| Goal | What handler has today | Gap |
|---|---|---|
| **Async, event-driven orchestrator** | `Environment` (`environment.py`) runs an asyncio queue fed by `Channel`s (web, telegram, scheduler). Cron prompts and task notifications go to a cheaper `background_agent`. | Events are handled **one at a time**. The queue is in memory, so events are lost on restart. There are no external event sources (webhooks, new mail). The scheduler polls every 30s. |
| **Harnesses** | `ClaudeAgent` (Claude Agent SDK, tools in an in-process MCP server), `OpenAIAgent` (OpenAI Agents SDK `Runner`), and `ManualAgent` + providers (a hand-written loop over raw APIs). | `ManualAgent` and the providers are removed: no hand-written loops. `ClaudeAgent` flattens history instead of resuming sessions, and uses `bypassPermissions`. `OpenAIAgent` replays stored text instead of using an SDK session. There's no Codex adapter. |
| **Sub-agents** | The `task` tool spawns a `task_runner` subprocess with its own conversation and workspace; completion becomes an event. | It uses a custom loop with a hard-coded tool list, inherits no user or permission scope, and has no limits. |
| **Memory** | Markdown topic files plus `index.md` in the prompt, `end_session` consolidation, Gmail fact extraction (`kb/`). | Memory is split per user, which the end state doesn't need. It's tied to the local filesystem. There are no skills. |
| **Isolation** | Instances exist (`instance.py`, `paths.py`). Users live in `users.json` with per-user dirs. | `RunContext` is one shared mutable object, so concurrent runs would mix up users. Instance creation is interactive. |
| **Connectors** | Gmail (no send), Drive, Telegram, web UI. Tools are `agents.function_tool` functions. | Gmail can't trigger the agent. Tools aren't served over MCP to external harnesses. Tokens are plaintext files. |
| **Authorization** | A Telegram allowlist, a watchdog-edit block, git checkpoints, and a prompt-only "confirm first" rule. | No authorization model or approval flow. The admin API has no auth. |
| **Sandbox** | `shell()` runs `subprocess.run(shell=True)` on the host. | No workspaces or isolation. |
| **Config** | Spread over `agent.json`, `agent_background.json`, `models.json`, `instance.json`, `scheduler.json`, `users.json`, `config/*.md`. | Should be consolidated. |

**Worth keeping:** the outer event loop / inner harness split, the in-process MCP approach in `ClaudeAgent`, the action-dispatch tool style with `action='help'`, the memory index, token/cost accounting, the foreground/background model split, and the task-completion-as-event pattern.

---

## 2. Instance and users

- **Instance** = one agent. Its whole state lives behind its `Store`, `FileStore` and `SecretStore` (§10). Creating an instance must work without prompts (`handler init --non-interactive …`), so an outside system can provision it.
- **One persona per instance.** Identity, persona and system prompt layers are instance-wide. Specialized behavior comes from skills and sub-agents, not from multiple personas.
- **Users**: an instance can have several users, each with linked identities (web login, Telegram id, email addresses, external app user id).
  - **Chat threads are scoped per user**: a user sees only their own chats.
  - **Everything else is shared**: memory, skills, automations, files, and non-chat threads. If something must be private, it belongs in a separate instance.
- **Run context** is per run and immutable, carried in a `contextvars.ContextVar`. The MCP gateway gets it from a per-run token. Concurrent runs can't see each other's context.

```python
@dataclass(frozen=True)
class RunContext:
    run_id: str
    thread_key: str
    user_id: str | None         # None for system-triggered runs
    acting_as: str | None       # user whose authority the agent is exercising (§6)
    parent_run_id: str | None   # set for sub-agents
    budget: Budget
```

---

## 3. Event loop

The loop has three parts connected only through the store: **writers** put events in, the **event queue** holds them durably, and **readers** do the work. The default runs all three in one process. Nothing in the design requires that.

### 3.1 Thread types

A thread is a unit of ongoing work. It maps to one harness session (§5), and it's the unit of ordering: at most one active run per thread. **Thread types map one-to-one to the kinds of events that can trigger the agent.** The initial set:

| | **chat** | **gmail** | **automation** | **crontal** |
|---|---|---|---|---|
| Triggered by | A user's message (web, Telegram) | A new email in a monitored mailbox | A schedule firing | A Crontal notification webhook (RFQ, bid, …) |
| Event type | `chat.message` | `gmail.received` | `automation.fired` | `crontal.notification` |
| Thread key | `chat:<user>:<conversation>` | `gmail:<mailbox>:<gmail_thread_id>` | `automation:<job_id>` | `crontal:<entity_type>:<entity_id>` |
| Owner / acts as | The user | The mailbox owner | The job's creator | The agent itself |
| Visible to | Owner only | All users | All users | All users |
| Lane | interactive | background | background | background |
| Which events start a run | All | Monitoring rules (labels, senders); never mail sent by the owner or the agent | Every firing | Notification types enabled in config |
| Turn content | The user's message | `<event type="gmail.received">` with sender, subject, body | The job's prompt, marked as scheduled | `<event type="crontal.notification">` with the payload |
| Results go to | A reply on the user's channel | `notify()` and/or a draft reply | `notify(creator)` | `notify()` |

- **Continuity**: every message in an email conversation lands in the same thread and session. Each automation keeps one thread across its firings.
- **Internal events** (`task.completed`, `approval.resolved`) aren't thread types. They're delivered into the thread that started the work.
- **Crontal notifications** carry `entity_type` and `entity_id`, so all events about one RFQ share a thread.
- Each thread type is a small class in core: key derivation, owner and acting-as, visibility, lane, trigger rule, turn rendering, default delivery, default harness and model.

### 3.2 Writers (ingress)

- **Producers**: HTTP routes (chat, Crontal webhook), the Gmail poller, the scheduler, and internal events. All call `event_queue.put()`.
- **Durable before acknowledged**: an HTTP sender gets 2xx only after the event is committed. A poller or the scheduler saves its own progress (Gmail `historyId`, next run time) in the same transaction as the event.
- **Idempotency**: every event has an `idempotency_key` (webhook delivery id, Gmail message id, `automation:<id>:<fire_time>`). Duplicates are no-ops.
- **Routing at write time**: the thread type derives the thread key and applies its trigger rule. In the same transaction, the writer marks the thread as having pending work. Events that fail the trigger rule are stored as `ignored`.

### 3.3 Event queue: a database table

The event queue lives behind an `EventQueue` interface on the instance's `Store` (SQLite by default, Postgres in production). A database is used because handler needs per-thread ordering, coalescing, and atomic writes alongside the rest of its state, at low volume.

```
events   id, idempotency_key UNIQUE, type, source, payload, user_id, thread_key,
         status (routed|ignored), consumed_by_run, received_at
threads  key PK, type (chat|gmail|automation|crontal), owner_user_id, lane, priority,
         harness, model, session_id, pending_since, available_at, attempts,
         last_error, dead, lease_owner, lease_until, lease_version, summary
runs     id, thread_key, lease_version, trigger_event_ids, status, parent_run_id,
         harness, model, usage, cost, started_at, finished_at, error
messages id, thread_key, run_id, role (user|assistant|tool|event), blocks (normalized),
         raw (harness-native), created_at
```

### 3.4 Readers (dispatcher)

- **Claim threads, not events.** A reader leases a thread with pending events and no live lease (`FOR UPDATE SKIP LOCKED` on Postgres; an equivalent single-process claim on SQLite). The claim commits immediately.
- **Coalescing**: a run takes all of the thread's pending events as one turn. Events that arrive during the run leave the thread pending, and it's claimed again right after (unless the harness accepts them mid-run, §4).
- **Ordering**: one lease per thread. Different threads run in parallel.
- **Lanes**: `interactive` is prioritized over `background`.
- **Failures**: a heartbeat extends the lease. An expired lease makes the thread claimable again. A failed run backs off, and after N attempts the thread is marked `dead` and an admin is notified. Every write from a run is conditional on `lease_version`.
- **At-least-once**: a retried run can repeat tool calls, so tools with side effects are deduplicated by the gateway (§6).
- **Waking up**: readers wait on a signal (an in-process event, or `LISTEN/NOTIFY`), and the scheduler sleeps until the next due time. All state is in the store, so stopping or restarting is always safe.

### 3.5 Delivery

A run's outputs become rows in a `deliveries` table, and a deliverer for each channel (web, Telegram, email) sends them with retries.

- **Chat**: the reply goes back to the channel the user wrote from, and is streamed live in the web UI.
- **Other threads**: the result is recorded in the thread (visible in an Activity view). People are told through the `notify(to, message)` tool. Approval requests always notify approvers.

### 3.6 Scaling considerations (deferred)

Recorded so the core doesn't rule them out.

- **Load target worth testing against**: 100 users at 10 messages/minute ≈ 17 events/s sustained, ~50/s peak. A Postgres event queue handles this easily. The real limits are LLM throughput and cost, and the weight of each harness run (a CLI process per active run).
- **Horizontal scale**: stateless web writers; many reader workers on Postgres (`SKIP LOCKED`, leases, fencing); pollers and schedules as leased sources; `--role web|worker|all`.
- **LLM economics**: a shared per-provider rate limiter with backpressure; cheap models for triage threads; per-thread budgets.
- **Idle instances**: workers could exit when idle and report `next_wake_at`, with a host supervisor or platform scale-to-zero restarting them.
- **Retention**: archive or time-partition `events`.

---

## 4. The run

What happens after a reader claims a thread:

1. **Prepare the context.** For library loops: build the system prompt (`AGENT.md` layers, memory index, skills index, speaker profile, thread-type guidance) and load the session from the `Store`. For full harnesses, prepare the workspace instead: the thread's working directory, restored from `FileStore`, containing:
   - **instructions**: `AGENT.md` layers, the memory index, the speaker's profile, and thread-type guidance. Passed as the harness system prompt and/or written as `CLAUDE.md` / `AGENTS.md`.
   - **skills**: synced into the harness's skill location (for example `.claude/skills/`).
   - **files** the thread works on.
   - **the harness session state** (§5.1).
2. **Decide whose authority applies** (`acting_as`), from the thread type. Issue a per-run token for the MCP gateway carrying the `RunContext`.
3. **Start or resume the session** with the thread's harness and model, handler's MCP servers, permission hooks pointing at the gateway, and limits (turns, time, budget).
4. **Send one turn**: the pending events rendered by the thread type. Handler never replays history; the session has it.
5. **Stream**: harness output goes to the live UI and is converted into handler `messages` (normalized blocks plus the raw harness payload).
6. **New events mid-run**: if the harness supports adding input to a live session, the new events are passed in. Otherwise they wait for the next run, which coalesces them.
7. **Commit in one transaction**, conditional on `lease_version`: messages, deliveries, consumed events, usage and cost. Save the session state and workspace back to `FileStore`.
8. **After the run**: a `thread.idle` timer later triggers a short consolidation run that saves durable facts to memory. Compaction is the harness's job.

---

## 5. Harness adapters

```python
class Harness(Protocol):
    async def run(self, req: RunRequest) -> AsyncIterator[RunEvent]: ...
    async def send(self, run_id: str, turn: Turn) -> bool: ...   # mid-run input, if supported
    async def interrupt(self, run_id: str) -> None: ...
    # RunRequest: workspace, session_id?, instructions, turn, model, mcp_servers,
    #             permission_hook, limits
    # RunEvent:   assistant_text | tool_call | tool_result | usage | session_id | final
```

Vendors offer their loops at two levels. **Handler starts with the full harnesses** and keeps library loops as the fallback if a harness does too much or integrates poorly. Observability (§11) exists to make that call with data.

| Adapter | Level | Built on | Notes |
|---|---|---|---|
| `tool-runner` | Library loop | Anthropic SDK Tool Runner (`client.beta.messages.tool_runner`) | Fallback for Claude models. In-process. Loops only over handler's tools, with per-turn hooks. Handler stores the message history in its `Store`. Context management uses Anthropic's server-side compaction and context editing. |
| `agents-sdk` | Library loop | OpenAI Agents SDK (`Runner`) | Fallback for OpenAI models. In-process. Its `Session` is implemented on handler's `Store`. |
| `claude-code` | Full harness | Claude Agent SDK (Claude Code as a library) | Opt-in, for threads that need Claude models. Built-in coding, file and shell tools, sub-agents, hooks. Session resume, no `bypassPermissions`. |
| `codex` | Full harness | Codex (`codex exec --json` / its SDK or app-server protocol) | **Default harness for all thread types.** Native Rust binary, lighter than Claude Code (roughly half the memory per process in local observation, 2026-10), which matters on small dynos. Built-in shell and file actions are bounded by Codex's own sandbox and approval settings, not by per-call gateway hooks. Uses its own sandbox modes inside the workspace. |

Switching a thread type to a library loop is a config change. Library loops avoid host-config isolation, generated instruction files and session files, at the cost of the harnesses' built-in tools. Handler never writes its own loop or summarizer.

The hand-written loop (`ManualAgent` + providers) is removed. Every adapter uses a vendor SDK's loop.

**Harness and model are chosen per thread type** (in `handler.yaml`), and can be overridden per automation or delegation. For example: a fast, cheap model for Gmail and Crontal triage, a strong model for chat, Codex for data-heavy automations. A thread's harness is fixed when the thread is created.

### 5.1 Threads and sessions

- A thread maps **1:1 to a harness session**. `threads.session_id` holds it.
- **The session is the working context**: history, compaction, caching. **The handler thread is the official record** used by the UI, audit, memory consolidation and delivery.
- **Durability** (library loops): the session is stored directly in handler's `Store` (the Agents SDK `Session` interface; message history for the Tool Runner).
- **Durability** (full harnesses): session files live in the harness's home directory (Claude's config dir, `CODEX_HOME`), which points into the thread workspace. They're saved to `FileStore` after every run and restored before the next.
- **Fallback**: if a session can't be restored, start a new one seeded with the thread's summary and recent messages from handler's record. A full rebuild of the harness's session file from the stored `raw` messages is possible in principle, since the file is just the harness's own messages chained in order. But the format isn't a stable API, so it stays optional.
- Pin harness versions in the runtime image, and test session save/restore on each upgrade.

---

## 6. Tools, gateway and authorization

### 6.1 Tools and connectors

Handler doesn't build connectors. **A connector is an upstream MCP server plus an auth strategy, configured in `handler.yaml`.** Handler builds only what's specific to it: choosing credentials per user, authorization, auditing, and event triggers.

```
harness ──MCP──▶ handler gateway (MCP proxy) ──MCP──▶ upstream connector
                  · selects credentials for the acting_as user
                  · authorize / ask / deny, dedupe
                  · audit + observability (§11)
```

| Server | Source | Tools |
|---|---|---|
| `handler` | Built into handler | `memory`, `skill`, `automation` (create/list/delete, incl. one-shot), `delegate`, `notify`, `files` (FileStore access outside the workspace) |
| `google` (Gmail, Drive) | Upstream: a managed connector platform (Composio / Pipedream Connect / Arcade class) or a self-hosted open-source Workspace MCP server | Whatever the upstream exposes, filtered by grants |
| `crontal` | Upstream: an MCP endpoint **hosted by Crontal's backend**, authenticated as the agent principal | RFQs, bids, inventory, files, notifications |
| others | Any MCP server added in config | Proxied through the gateway |

Shell, file editing and web search come from the harness's own built-in tools.

- **Connected accounts**: handler stores `user → connector → upstream account id`. OAuth runs through the upstream's hosted flow (managed platform) or handler's own OAuth app (self-hosted), with tokens in `SecretStore`. Handler's UI only shows "Connect …" and records the result.
- **Event triggers**: MCP is request/response, so triggers come separately: the platform's webhooks or triggers, provider push notifications (Gmail push), or a small poller using the same connected account. All become events through the normal writer path (§3.2).
- **Google verification**: reading other people's Gmail through your own OAuth app needs Google's restricted-scope verification. Managed platforms allow starting on their already-verified app.
- **Scope of an upstream**: only that connector's calls pass through it. LLM calls, built-in harness tools, handler's own tools and other connectors never touch it. The gateway exposes a **curated subset** of each upstream's tools (for example Gmail: search, read, draft, send, label) under handler's grants. The upstream's full catalog and meta-tools are not exposed.
- **Lock-in rules**: (1) handler depends only on MCP and standard OAuth, with no vendor SDKs in core, and each connector's upstream is configuration; (2) harnesses only ever see the gateway's MCP endpoint; (3) no consumer-account connectors (claude.ai / ChatGPT) and no harness-managed auth, since both require users to have their own Claude or ChatGPT accounts. Worst-case cost of leaving a vendor: a config change plus users reconnecting once.
- **Google path**:
  - **Now**: **Composio's free plan** (20k tool calls/month, unlimited connected accounts, as of 2026-10) on the platform's own OAuth app. Pipedream Connect is free only in development mode (≤10 end users; production from $99/month). Self-hosting with handler's own unverified Google app means weekly re-consent while the app is in Testing status.
  - **Before onboarding external users**: switch to handler's own Google OAuth app (Google verification), either through the same platform or with a self-hosted open-source Workspace MCP server.
  - **Later**: Google's official Workspace MCP servers (`gmailmcp.googleapis.com`, `drivemcp.googleapis.com`, …) once they leave Developer Preview. As of 2026-10 they're limited to Workspace accounts, and Gmail can draft but not send.
- **Platform trial checklist**: hosted connect link per user; new-mail trigger or webhook; send, not just draft; bring-your-own OAuth app; pricing at expected volume; self-hostable MCP server.
- **Removed**: `tools/gmail.py`, `tools/gdrive.py`, `google_oauth.py` and the copy-paste OAuth flow. `kb/` becomes optional and can run on top of the same connected account.

### 6.2 Credentials and connections

At call time the gateway answers one question: **for this connector, acting as this user (or the instance), what credential is attached?** The answer doesn't depend on who manages the credential.

```
gateway call (connector, acting_as) → connection lookup → credential provider → inject into upstream request

connections  id, connector, owner (user:<id> | instance), provider (local | platform | broker),
             external_ref, secret_ref, scopes, status (active | needs_reauth | revoked),
             created_by, last_used_at
```

| Provider | Who holds the secret | Used when |
|---|---|---|
| `local` | Handler's `SecretStore` (tokens from handler's own OAuth flow, or a static API key) | Standalone handler, self-hosted MCP servers |
| `platform` | A managed connector platform; handler stores only the account id | Pipedream / Composio connections |
| `broker` | An external app; handler requests a short-lived token from the app's token endpoint at call time, with a signed request | The embedding app owns the account and the OAuth flow |

- Secrets never enter the harness's environment or workspace. The gateway attaches them only to upstream requests.
- On a 401 or a failed refresh, the connection becomes `needs_reauth`, the call fails cleanly, and the owner is sent a reconnect link.
- Every use is audited per connection.

**Embedding apps (for example Crontal)**: no long-lived secrets are shared.

- **Provisioning**: the app calls handler's admin API with an instance admin credential to create users (`external_id`) and connections. Inbound webhooks are signed with a shared secret per instance.
- **The app's own connector**: either a static agent token pushed at provisioning and rotated through the admin API (`local`), or preferably `broker`, where handler exchanges its instance credential for a short-lived agent token on demand.
- **Users' third-party accounts**, in order of preference:
  - (a) A shared platform project: users connect in the app's UI, and handler stores only the account id.
  - (b) `broker`: the app runs the OAuth flow and serves short-lived tokens.
  - (c) The app links to handler's own connect flow.

  Pushing refresh tokens from the app into handler is avoided.
- **Build order**: `local` and `platform` first, since the standalone instance needs them. Add `broker` with the first embedding integration.

### 6.3 Gateway

Two entry points, one policy:
- **MCP calls** go through the gateway inside the MCP server.
- **Built-in tool calls** (bash, edit, web fetch) go through the harness's permission hook or callback, which asks the gateway.

```
validate → authorize (→ allow | ask | deny) → dedupe by (run_id, call_id)
→ execute with timeout → move oversized output to FileStore → audit
```

### 6.4 Authorization model

- **Actions**: each tool action, and each built-in tool, has an action id and risk (`read` | `write_internal` | `write_external` | `exec`), for example `gmail.send` external write, `builtin.bash` exec.
- **User grants**: the actions each user is authorized for, optionally with constraints (recipient allowlist, amount ceiling, path prefix).
- **Agent grants**: what the agent may do on its own authority.
- **Acting as a user**: effective authority = **user grants ∩ agent grants**. Using a user's credentials (their Gmail) always means acting as that user.

**Initial version**: every user holds every grant, and any user can approve anything, including on shared threads. Agent defaults: `read` allow, `write_internal` allow, `write_external` ask, `exec` allow only inside the workspace sandbox. The full data model is in place from the start, so tightening it is configuration.

### 6.5 Approvals don't pause the harness

The gateway stores the exact call, notifies approvers, and returns "pending approval" to the harness. The run ends normally. When a user approves (web button, Telegram inline button), **the gateway executes the stored call** and emits `approval.resolved` into the originating thread, which resumes the session. Approvals expire if not answered.

Every call is audited. Credentials are encrypted at rest (`SecretStore`) and injected into MCP servers, never into the harness environment. The web UI and API require authentication.

---

## 7. Sub-agents

- **Sync**: the harness's own sub-agents (for example Claude's built-in sub-agents) for parallel fan-out within a run. Handler only supplies the definitions, as files next to skills.
- **Async**: `delegate(instructions, harness?, model?)` over MCP creates a **one-shot automation**, a new thread with its own session that fires once. On completion, `task.completed` is emitted into the parent thread with the result and artifact keys. This replaces today's `task` tool and `task_runner` subprocess.
- **Rules**: an async child inherits the parent's `user_id`/`acting_as` and can only narrow its authority. Limits: depth ≤ 2, a cap on concurrent children, and a token budget deducted from the parent. Cancelling a parent cancels its children.

---

## 8. Workspaces and sandbox

Each thread has a persistent **workspace** in `FileStore`, materialized as a local directory for the run. The harness process runs inside it.

- **local-process (default)**: the harness runs as a subprocess with the workspace as its working directory, an environment scrubbed of secrets, and resource limits. It also uses the harness's own sandbox features (Codex sandbox modes, Claude Code sandbox and permission settings). This guards against accidents, **not** against malicious code.
- **container**: the harness runs inside Docker/podman with only the workspace mounted. Recommended for any instance shared by several users.
- **remote**: a managed sandbox provider, for hosts that can't run containers.

---

## 9. Memory and skills

One shared memory per instance. Handler owns memory and skills so that **every harness sees the same knowledge**, and switching harness or model loses nothing.

| Layer | How it reaches the harness |
|---|---|
| Thread history | The harness session |
| Memory topics | Index in the instructions; full topics through the `memory` MCP tool. Per-person facts are ordinary topics (`people/<name>.md`). |
| Skills | Synced into the workspace in the harness's skill format (`SKILL.md`) |
| System of record (Crontal, Gmail, …) | Through MCP tools only, never copied into memory |

**Handler is the source of truth; each harness's native formats are only how things are delivered.** For library loops, handler passes these directly (system prompt, tools, stored history). For full harnesses, a per-harness renderer generates the workspace and the harness home directory from handler's store before each run:

| Item | Source of truth | Delivered as | Harness-native version used? |
|---|---|---|---|
| Instructions | `AGENT.md` + memory index + speaker profile | Generated `CLAUDE.md` / `AGENTS.md` and system prompt | Format only |
| Memory | Handler `Store` | Index in instructions; full topics through the `memory` MCP tool | No: harness auto-memory is off |
| Skills | Handler `FileStore` | Library loops: index in the system prompt plus the `skill` tool. Full harnesses: synced into the harness's skill directory | Format only (full harnesses) |
| Sub-agent definitions | Handler `FileStore` | Synced into the harness's agent directory, where supported | Format and runtime |
| MCP servers | `handler.yaml` | Only the gateway's servers | No |
| Permissions and hooks | Handler grants | Generated settings whose hooks call the gateway | Mechanism only |
| Conversation history, compaction, built-in tools | The harness | Native | Yes |

Rule: anything that should outlast a thread (knowledge, procedures, identity, permissions) belongs to handler. The mechanics of a single conversation belong to the harness.

- **Isolation from host config**: the harness home directory (Claude's config dir, `CODEX_HOME`) always points at a generated directory, so the host user's own `~/.claude` / `~/.codex` settings, memory, plugins and MCP servers never load into handler runs.
- **Edits the harness makes directly**: new or changed skill files in the workspace are synced back to `FileStore` after each run, with an audit record. Generated instruction files (`CLAUDE.md` / `AGENTS.md`) are treated as read-only and regenerated each run. Instructions tell the agent to save durable facts through the `memory` tool.
- **Memory topics** live in the `Store` as markdown, editable in the web UI. Appends are atomic; `rewrite` uses optimistic versioning, because concurrent runs write memory.
- **The agent can create and edit skills** through the `skill` tool. Changes are audited, and an admin can revert them.
- **Consolidation**: a `thread.idle` timer starts a short run that saves durable facts from the thread.
- **Later**: entity notes, full-text search, user-private scopes.

---

## 10. Configuration and pluggable infrastructure

```
<instance>/
  handler.yaml   # backends, harness + model per thread type, users and identities, grants,
                 # gmail monitoring rules, crontal notification types, MCP servers, limits, channels
  AGENT.md       # prompt layers as sections: System, Identity, Persona
  data/          # SQLite + local FileStore (when using local backends); not edited by hand
```

Configuration has two layers, because settings edited through the UI must survive restarts on hosts with ephemeral disks:

| Layer | Contents | Lives in | Edited by |
|---|---|---|---|
| **Bootstrap config** | Store, file store and secret store backends; enabled apps; admin API credentials; secret references | `handler.yaml` + environment variables | Deployment |
| **Runtime settings** | Persona (`AGENT.md`), harness and model per thread type, users, connections, grants, thread-type options (Gmail rules, notification types), limits | **The `Store`**, versioned with history and rollback, audited with the actor | Settings UI and admin API |

On first start, runtime settings are seeded from `handler.yaml` and `AGENT.md`, so an instance can still be set up from files. After that, the store is authoritative. Secrets never go in `handler.yaml`; they come from environment variables or the `SecretStore`. Automations, memory and skills are runtime data in the store.

| Interface | Default | Production options |
|---|---|---|
| `Store`: events, threads, runs, messages, memory, automations, approvals, grants, audit | SQLite | Postgres |
| `FileStore`: workspaces, session state, skills, uploads, artifacts | Local directory | S3-compatible object storage |
| `SecretStore`: OAuth tokens, API keys | Encrypted file | Encrypted DB column or platform secret manager |
| `Sandbox` (where harness processes run) | local-process | container, remote |

The core uses only these interfaces.

### 10.1 Initial deployment for Crontal (proposed, cost-minimized)

Crontal today (`hephaestus-backend-test`): Basic web dyno ($7), Postgres essential-1 ($9; 10 GB, ~20 connections, no extra credentials), Bucketeer hobbyist ($5).

- **Premium, opt-in**: handler is a paid add-on, not the default assistant. Crontal's existing built-in agent stays for all users. Crontal decides per user or merchant whether chat and notifications go to handler or to its built-in agent, using an entitlement flag. Webhooks are sent only for entitled users.
- **First instance**: a single handler instance whose users are Crontal's hardcoded admin users (the `SITE_ADMIN_EMAILS` list). Paying merchants get their own instances later, through the provisioning API, with Crontal storing `merchant → instance URL, credentials`.
- **A separate Heroku app** (for example `crontal-agent`) on **one Basic dyno (+$7/month)**, running one handler instance. Not a process inside Crontal's app: Heroku only routes HTTP to the `web` process, and handler needs its own endpoints.
- **Shared Postgres**: attach Crontal's add-on to the handler app. Handler uses its own schema (`handler_<instance>`) and migrations. Neither side reads the other's tables; they integrate only through MCP, webhooks and the API. Handler's connection pool stays at ~5.
- **Shared Bucketeer**: everything under `handler/<instance>/`. Run payloads and session files go here, not into Postgres.
- **Memory**: Codex is the default harness partly because it's lighter. 512 MB may still be tight for handler plus a Codex run. Start with one concurrent run and measure. If it doesn't fit, use a library-loop harness for Crontal threads or move to Standard-2X.
- **Development**: the Crontal-facing instance can run on macbook-server behind a free tunnel until a real merchant depends on it.
- **Moving out later**: dump the schema into its own database, copy the bucket prefix, change config vars.

---

## 11. Observability and cost

A primary requirement: see exactly what each harness does, find problems and integration gaps, and estimate cost accurately.

### 11.1 Sources

| Source | Shows | Claude Code | Codex |
|---|---|---|---|
| Harness event stream | Turns, assistant text, thinking summaries, tool calls and results, sub-agents, final usage | SDK messages (init: model, tools, session; content blocks; result: `total_cost_usd`, per-model usage, turns, duration) | `codex exec --json` events (turns, commands, file changes, MCP calls, reasoning, usage) |
| Hooks | Every built-in tool use with its permission decision; compaction; sub-agent start and stop | PreToolUse / PostToolUse / sub-agent / compaction / session hooks → gateway | Approval and sandbox events |
| Gateway audit | Every handler tool call: arguments, decision, result size, duration, errors | Same | Same |
| Session transcript archive | The harness's own full record, per run | Session JSONL | Rollout JSONL |
| LLM traffic capture (debug mode) | Exactly what is sent to the model: hidden system prompt, tool definitions, compaction and side-task calls, cache hits, per-call usage | API base URL → local recording proxy | Model provider base URL → same proxy |

Harness OpenTelemetry exports can be ingested as an extra cross-check.

### 11.2 Trace model

`event → thread → run → steps`. Steps are append-only and ordered:

```
run_steps  run_id, seq, parent_step, kind (llm_call|tool_call|permission|subagent|compaction|error|retry),
           name, started_at, ended_at, status, input_digest, output_size, decision, model,
           tokens (in, out, cache_read, cache_write), cost_usd, source (stream|hook|gateway|proxy),
           raw_ref (FileStore pointer)
```

- Large payloads live in `FileStore`, with a pointer from the row.
- Logs are structured JSON, with `run_id`, `thread_key` and `step` on every line, including harness stderr.
- Steps are labeled with OpenTelemetry GenAI conventions so they can be exported (Langfuse, Phoenix, Honeycomb). Handler's store remains the source of truth.

### 11.3 Views

- **Run timeline**: a waterfall of steps with timing, tokens and cost, drilling down to raw payloads (the prompt as the model saw it, tool input and output).
- **Thread view**: runs over time and session history.
- **Gap report**, per harness and thread type:
  - denied or approval-required attempts
  - calls to tools that don't exist
  - edits to generated instruction files or skill folders
  - files touched outside the workspace
  - harness-native memory use
  - turn or budget limits hit
  - retries and crashes
  - compactions
  - tool-dominated runs and fast context growth
- **Usage**: tool frequency, error rates, latency percentiles, context fill.

### 11.4 Cost

- **Actual cost**: per LLM call, tokens × a versioned price table (effective dates, in config), split into input, output (including thinking), cache write, cache read, and server-tool fees. Attributed to the model that served the call, including harness side calls.
- **Cross-checks**: Claude's `total_cost_usd`; Codex token usage priced by handler; proxy capture when enabled. Mismatches appear in the gap report.
- **Roll-ups**: by run, thread, thread type, automation, user, model, harness and day. Sandbox and compute time are recorded per run at a configured rate.
- **Estimates**: Σ over thread types (events/day × fraction that start a run × cost per run), with p50 and p95, plus what-if comparisons by model or harness.
- **Budgets**: per-run budget (passed to the harness where supported), a daily instance cap, alert thresholds.

### 11.5 Improvement loop: finding missing tools

The traces exist to drive integration work. For example: if the agent keeps writing its own script to download and read files from Crontal, a `crontal.get_file` tool that returns the content directly would save the tokens and time spent writing and debugging that script on every run.

- **Fingerprint what the agent does with general-purpose tools.** Normalize bash commands and scripts into templates, and record which network destinations they contact (from hooks or the proxy), which files they write, and which tools follow them. For example, `python fetch_*.py` → `GET crontal/api/files/*` → `read_file`.
- **Group recurring patterns** across runs and thread types, using deterministic fingerprints first.
- **Attribute cost to each pattern**: the output tokens spent writing the script, the turns spent running and fixing it, and the context added by its output. The attribution is approximate, but good enough to rank.
- **Opportunity report**, ranked by frequency × cost per occurrence, with example runs linked. The fix is either:
  - **a new tool**, when credentials, large payloads or authorization are involved (for example the Crontal file fetch), or
  - **a skill with a bundled script**, which is cheaper to add and keeps the procedure in one place, or
  - **an instruction change**, when the agent is simply choosing badly.
- **Periodic review automation**: a scheduled handler run that reads the opportunity report and recent gap reports and drafts proposals. A human decides what to build. Gap report entries such as "calls to tools that don't exist" and "repeated denials" feed the same report.
- **Measure the fix**: after a tool or skill ships, the same fingerprint should drop in frequency and the thread type's cost per run should fall. Both are visible in the cost views.

### 11.6 Defaults (proposed)

- Full payload capture with a retention period, switchable to metadata-only per instance.
- Traffic capture available as a debug mode per instance or thread type, on during early testing.
- A basic run timeline and cost views in the web UI, with optional export to an external tracing tool.

---

## 12. Applications

An **application** extends handler for a domain (for example Crontal) with its own thread types, event sources, connectors, skills, grants and hooks. **Applications integrate through protocols first and code second.** Most of an application's logic stays in its own backend, connected through MCP (tools), signed webhooks (events), the `broker` credential provider, handler's admin API (provisioning) and remote hooks. Python code inside handler is only for logic that must run in-process.

### 12.1 Extension points

| Extension point | Declarative (manifest) | Code (optional Python) |
|---|---|---|
| Thread types | Key template, owner rule, lane, default harness/model, trigger filter | Custom key derivation, rendering, trigger logic |
| Event sources | Webhook route, signature scheme, payload → event type mapping | Pollers, custom parsing |
| Connectors | MCP upstream, credential provider, curated tools with risk per action | — |
| Skills and sub-agent templates | Bundled files | — |
| Instructions | `AGENT.md` fragments | — |
| Default grants | Agent and user grants for the app's actions | — |
| Channels | — | Ingress + delivery (e.g. an in-app chat) |
| Hooks | Remote hook URLs | In-process hook functions |
| Config | The app's section of `handler.yaml` | Validation |

Names are namespaced by app (`crontal.*` events and tools, `crontal` thread type, `crontal/…` skills).

### 12.2 Hooks (designed, not built yet)

**The first version has no hooks.** Every need below is covered by a simpler mechanism. A hook is added only when observability (§11) shows it's needed. For example: if every Crontal run starts by calling `get_rfq` and fetching buyer history, that's the evidence for `before_run`. When hooks are added, they're **remote** by default. The first likely one is `on_approval`, once approvals need to appear in the app's own UI.

| Hook | Covered for now by |
|---|---|
| `on_event` | The app only sends wanted events; the thread type's trigger filter drops the rest |
| `before_run` | The agent calls the app's tools itself, guided by skills |
| `on_tool_call` | The app's MCP server enforces its own business rules; handler grants cover allow/ask/deny |
| `after_tool_call` | The app's MCP server shapes its own outputs |
| `on_approval` | Handler's UI plus `notify()` |
| `after_run` | The agent calls an app tool to record outcomes (e.g. `mark_reviewed`) |
| `on_delivery` | An app tool (e.g. `crontal.notify_user`) |

Design for when hooks are built:


| Hook | When | Kind | On failure or timeout |
|---|---|---|---|
| `on_event` | After ingress, before routing: filter, enrich, reroute | decision | Fail open |
| `before_run` | Before a run: add context | decision | Fail open |
| `on_tool_call` | In the gateway, after grants: domain checks | decision | **Fail closed** |
| `after_tool_call` | After a tool returns: post-process, redact | decision | Fail open |
| `on_approval` | Approval created: route it to the app's UI | notification | Outbox retries; still visible in handler |
| `after_run` | Run committed: sync results back to the app | notification | Outbox retries |
| `on_delivery` | Delivering a message through the app | notification | Retries, then the default channel |

- Decision hooks are synchronous with a short timeout. Notification hooks go through an outbox with retries.
- **Remote hooks**: handler makes a signed HTTP call to the app's backend and applies the JSON answer. The app's logic stays in its own codebase, next to its data. Example:
  ```
  POST {app}/agent/hooks/tool-call   {"hook":"on_tool_call","run_id":"r_123","acting_as":"user:jane",
                                      "tool":"crontal.submit_bid","args":{"rfq_id":981,"unit_price":2.10}}
  ← {"decision":"ask","reason":"price is below this merchant's cost floor"}
  ```
- **In-process hooks**: Python functions in the app package, loaded into handler. Faster, but the logic is handler-side code.
- Every hook is optional. Hooks can't bypass the gateway, and every execution is recorded as a run step (§11).

### 12.3 Packaging

```
handler-<app>/
  app.yaml        # manifest: api_version, thread types, event sources, connectors, hooks, grants
  agent.md        # instruction fragment
  skills/ agents/ # bundled skills and sub-agent templates
  <app>_app/      # optional in-process code
```

- Registered through a Python entry point (`handler.apps`) and enabled per instance in `handler.yaml` (`apps: [{name, config}]`).
- `api_version` is checked at startup. Hooks run in the order apps are listed.
- In-process app code is fully trusted. Remote hooks are authenticated in both directions.

### 12.4 Settings API and native settings pages

Handler's settings are exposed through a **typed settings API**. Handler's own web UI and an embedding app's native pages both use it. There is one source of truth: handler's `Store`.

```
App settings page (native) ──▶ App backend (proxy: app auth + entitlement check)
                                 ──▶ handler settings API (instance admin credential + acting user)
                                       ──▶ handler Store (versioned settings)
```

- **API**: `GET/PUT /api/settings/{section}`, with a JSON Schema per section (persona and instructions, harness and model per thread type, grants and approvals, thread-type options, limits, users), plus `GET /api/settings/{section}/history` and rollback. Writes use optimistic versioning (`If-Match`), so concurrent edits from the app, handler's UI or the agent don't overwrite each other.
- **Schema-driven forms**: the app can render forms from the JSON Schema, so new settings appear without hand-building each one. The app chooses which sections to show.
- **Live views from the same API**: connections (with connect links), automations, memory and skills, approvals queue, runs, usage and cost (§11). They're read live, never copied.
- **The app stores only what it owns**: entitlement, instance URL and credentials, and optional templates (for example a default persona) applied once at provisioning. It doesn't keep a copy of handler's settings.
- **Browser never talks to handler directly**: the app backend proxies every call with its own auth. OAuth connect flows (for example Composio) open in a popup.

### 12.4.1 Settings persistence

Handler's settings must survive restarts and redeploys on hosts with ephemeral disks:

- All runtime settings, users, grants, connections, automations and memory live in the `Store` (Postgres on Heroku). Skills, session state and artifacts live in the `FileStore` (Bucketeer on Heroku). In hosted mode nothing is persisted to the local disk.
- `settings` rows: `section, version, value (json), schema_version, updated_by, updated_at`. Each write appends a new version; history is kept for rollback and audit.
- `handler.yaml` and `AGENT.md` only **seed** settings the first time (or on an explicit `handler settings import`). They never overwrite stored settings on restart.
- On startup handler loads settings from the `Store`; there's no state held only in memory. Settings changes take effect on the next run without a restart.
- Backups come from the store's own backups (Heroku Postgres backups) plus the bucket. `handler settings export` produces a portable snapshot.

### 12.5 Example: Crontal

```yaml
api_version: 1
thread_types:
  crontal:
    key: "crontal:{payload.entity_type}:{payload.entity_id}"
    owner: agent
    lane: background
    trigger: { types_from_config: notification_types }
event_sources:
  - { route: /ingest/crontal, signature: hmac-sha256, event_type: crontal.notification }
connectors:
  crontal:
    upstream: "{config.api_url}/mcp"
    credentials: { provider: broker, token_url: "{config.api_url}/agent/token" }
    tools:
      - { name: get_rfq, risk: read }
      - { name: get_file, risk: read }
      - { name: draft_bid, risk: write_internal }
      - { name: submit_bid, risk: write_external }
      - { name: adjust_inventory, risk: write_external }
grants:
  agent: { crontal.submit_bid: ask, crontal.adjust_inventory: ask }
```

All Crontal integration code lives in the **Crontal repo**: the `handler-crontal` package (manifest, skills, instruction fragments, optional Python) plus its MCP server, token endpoint and webhook outbox. Crontal's deployment installs the package into the handler instance and enables it in `handler.yaml`. Crontal has a native settings tab for entitled users, backed by handler's settings API through Crontal's backend (§12.4). Crontal doesn't store handler's settings. Handler contains no Crontal-specific code. The `crontal` thread type (§3.1) moves from core into this app. Gmail, Drive and Telegram could later be repackaged the same way.

---

## 13. Open items

- **Implementation language**: the Claude Agent SDK has Python and TypeScript versions. Codex's SDK is TypeScript-first (Python would drive it over the CLI's JSON protocol). Document processing now happens inside harness workspaces. Python is still viable; TypeScript gives first-class support for both harnesses.
- **Mid-run input**: confirm which harnesses accept new input into a live session.
- **Gmail thread granularity**: one thread per Gmail conversation (assumed) vs. per mailbox.
- **Crontal notification payload**: confirm it carries `entity_type` and `entity_id`.
- **Notifications**: assumed agent-decided via `notify()`; optionally a per-automation "always notify" setting.
- **Full authorization model**: grant format, roles as grant bundles, delegation of grants.
- **Applications** (§12): which hook to build first, when evidence shows a need (likely `on_approval`).
- **Sandbox**: container and remote backends, and when container becomes the default.
- **Connectors**: managed platform vs. self-hosted for Google; which platform; gateway proxy (assumed) vs. direct harness connections.
- **Scaling**: see §3.6.
- **Observability defaults**: payload capture level, traffic capture, external viewer (§11.6).
