"""Core data types for the handler runtime.

These are the shared contracts between the event queue, dispatcher, harness
adapters, observability and the web API. See docs/agent-runtime-design.md.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Lane(StrEnum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"


_THREAD_TYPE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_EXTRA_THREAD_TYPES: dict[str, ThreadType] = {}


class ThreadType(StrEnum):
    CHAT = "chat"
    GMAIL = "gmail"
    AUTOMATION = "automation"
    CRONTAL = "crontal"  # Crontal notifications (RFQs, bids, ...), per entity
    EMAIL = "email"  # mail forwarded to the intake address, per email conversation

    # Integration-defined thread types. `ThreadType(name)` also accepts any name
    # registered here (integrations register theirs when an instance loads them),
    # returning a pseudo-member that behaves like the built-ins (a `str`).
    # Parsing only: which types an instance actually serves is up to its
    # integrations. Built-in values and stored threads are unaffected.

    @classmethod
    def register(cls, name: str) -> ThreadType:
        if name in cls._value2member_map_:
            return cls._value2member_map_[name]
        if not _THREAD_TYPE_NAME.fullmatch(name):
            raise ValueError(f"invalid thread type name {name!r}")
        member = str.__new__(cls, name)
        member._name_ = name.upper()
        member._value_ = name
        _EXTRA_THREAD_TYPES[name] = member
        return member

    @classmethod
    def _missing_(cls, value: object) -> ThreadType | None:
        return _EXTRA_THREAD_TYPES.get(value) if isinstance(value, str) else None


class EventStatus(StrEnum):
    ROUTED = "routed"
    IGNORED = "ignored"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    EVENT = "event"


@dataclass(frozen=True)
class Event:
    """Something that happened. Immutable once written to the event queue."""

    type: str  # e.g. "chat.message", "automation.fired"
    source: str  # producer name, e.g. "web", "scheduler"
    payload: dict[str, Any]
    idempotency_key: str
    thread_key: str | None = None
    user_id: str | None = None
    id: str = field(default_factory=lambda: new_id("evt"))
    received_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class ThreadSpec:
    """How a routed event's thread should be created if it doesn't exist yet."""

    key: str
    type: ThreadType
    lane: Lane
    owner_user_id: str | None = None
    harness: str = "codex"
    model: str | None = None
    priority: int = 0  # lower runs first


@dataclass
class Thread:
    key: str
    type: ThreadType
    lane: Lane
    owner_user_id: str | None
    harness: str
    model: str | None
    priority: int
    session_id: str | None
    pending_since: datetime | None
    available_at: datetime | None
    attempts: int
    last_error: str | None
    dead: bool
    lease_owner: str | None
    lease_until: datetime | None
    lease_version: int
    summary: str | None
    created_at: datetime


@dataclass(frozen=True)
class Lease:
    """Proof that a worker holds a thread. Writes are fenced on `version`."""

    thread_key: str
    owner: str
    version: int
    until: datetime


@dataclass
class TokenUsage:
    input_tokens: int = 0  # total input, including cache reads and writes
    cached_input_tokens: int = 0  # subset of input served from cache
    cache_write_tokens: int = 0  # subset of input written to cache
    output_tokens: int = 0  # total output, including reasoning
    reasoning_tokens: int = 0  # subset of output

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )


@dataclass
class Run:
    id: str
    thread_key: str
    lease_version: int
    trigger_event_ids: list[str]
    status: RunStatus
    harness: str
    model: str | None
    parent_run_id: str | None = None
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    error: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float | None = None  # None = unpriced


@dataclass
class Message:
    """A turn in a thread's official record (harness-independent).

    `blocks` is a list of normalized content blocks, e.g.
    {"type": "text", "text": ...}, {"type": "tool_call", "id", "name", "input"},
    {"type": "tool_result", "id", "output", "is_error"}.
    `raw` keeps the harness-native payload for fidelity.
    """

    thread_key: str
    role: Role
    blocks: list[dict[str, Any]]
    run_id: str | None = None
    raw: dict[str, Any] | None = None
    id: str = field(default_factory=lambda: new_id("msg"))
    created_at: datetime = field(default_factory=utcnow)


class StepKind(StrEnum):
    LLM_CALL = "llm_call"
    REASONING = "reasoning"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_CALL = "tool_call"  # built-in (shell, file edit) or MCP
    PERMISSION = "permission"
    SUBAGENT = "subagent"
    COMPACTION = "compaction"
    ERROR = "error"
    RETRY = "retry"


@dataclass
class RunStep:
    """One observable step of a run (§11.2)."""

    run_id: str
    seq: int
    kind: StepKind
    name: str
    source: str  # "stream" | "hook" | "gateway" | "proxy"
    started_at: datetime = field(default_factory=utcnow)
    ended_at: datetime | None = None
    status: str = "ok"  # "ok" | "error" | "denied" | "running"
    model: str | None = None
    usage: TokenUsage | None = None
    cost_usd: float | None = None
    data: dict[str, Any] = field(default_factory=dict)  # small summary fields only
    raw_ref: str | None = None  # pointer to the full payload in file storage
    parent_seq: int | None = None


# --------------------------------------------------------------------------
# Phase 2: schedules, gateway (grants, approvals, audit), memory, settings,
# connections. See docs/rebuild/phase2.md.
# --------------------------------------------------------------------------


class Risk(StrEnum):
    READ = "read"
    WRITE_INTERNAL = "write_internal"
    WRITE_EXTERNAL = "write_external"
    EXEC = "exec"


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"  # decided, execution in progress
    DENIED = "denied"
    EXPIRED = "expired"
    EXECUTED = "executed"  # approved and the stored call succeeded
    FAILED = "failed"  # approved but the stored call failed


@dataclass
class Schedule:
    """An automation. `spec` forms: "every 30m" | "cron: 0 7 * * 1-5" | "at: <ISO-8601>"."""

    name: str
    prompt: str
    spec: str
    created_by: str | None = None
    timezone: str = "UTC"
    notify: list[str] = field(default_factory=list)  # user ids to notify with results
    harness: str | None = None  # None = thread-type default
    model: str | None = None
    enabled: bool = True
    one_shot: bool = False  # derived from an "at:" spec; disabled after firing
    parent_thread_key: str | None = None  # set for async delegation
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    id: str = field(default_factory=lambda: new_id("sch"))
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class Approval:
    """A gated tool call waiting for (or decided by) a human."""

    run_id: str
    thread_key: str
    tool: str  # MCP tool name, e.g. "gmail_send"
    action: str  # action id, e.g. "gmail.send"
    args: dict[str, Any]
    requested_by: str | None  # acting_as user of the run, None = the agent itself
    reason: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    result: dict[str, Any] | None = None  # {"content": ..., "is_error": bool} after execution
    expires_at: datetime | None = None
    id: str = field(default_factory=lambda: new_id("apr"))
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class AuditEntry:
    run_id: str | None
    thread_key: str | None
    actor: str | None  # acting_as user, or None for the agent
    tool: str
    action: str
    decision: Decision
    status: str  # "ok" | "error" | "pending" | "denied"
    args_digest: str = ""  # sha256 of canonical JSON args
    args_preview: str = ""  # truncated
    result_size: int = 0
    duration_ms: int = 0
    error: str | None = None
    approval_id: str | None = None
    id: str = field(default_factory=lambda: new_id("aud"))
    at: datetime = field(default_factory=utcnow)


@dataclass
class MemoryTopic:
    name: str  # e.g. "people/danny" (no extension)
    description: str
    content: str
    version: int = 1
    updated_by: str | None = None
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class SettingsVersion:
    section: str  # e.g. "persona", "threads", "grants", "limits"
    version: int
    value: dict[str, Any]
    schema_version: int = 1
    updated_by: str | None = None
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class Connection:
    connector: str  # e.g. "google"
    owner: str  # "user:<id>" | "instance"
    provider: str  # "local" | "platform" | "broker"
    external_ref: str | None = None  # platform account id, broker subject, ...
    secret_ref: str | None = None  # SecretStore key
    scopes: list[str] = field(default_factory=list)
    status: str = "active"  # "active" | "needs_reauth" | "revoked"
    created_by: str | None = None
    last_used_at: datetime | None = None
    id: str = field(default_factory=lambda: new_id("con"))
    created_at: datetime = field(default_factory=utcnow)
