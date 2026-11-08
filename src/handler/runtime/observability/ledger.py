"""Cost ledger contracts. Provider costs and local context estimates never overlap."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..types import TokenUsage, utcnow


@dataclass
class RunAttribution:
    run_id: str
    thread_key: str
    instance_name: str = "main"
    initiating_user_id: str | None = None
    attributed_user_id: str | None = None
    thread_owner_user_id: str | None = None
    attribution_reason: str = "shared_system"
    trigger_type: str = "unknown"
    trigger_event_ids: list[str] = field(default_factory=list)
    captured_at: datetime = field(default_factory=utcnow)


@dataclass
class UsageRecord:
    id: str
    run_id: str
    thread_key: str
    request_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    record_kind: str = "model_request"
    model: str | None = None
    requested_model: str | None = None
    service_tier: str | None = None
    harness: str | None = None
    metering_quality: str = "provider_reported"
    status: str = "completed"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    occurred_at: datetime = field(default_factory=utcnow)
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float | None = None
    pricing: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallUsage:
    id: str
    run_id: str
    thread_key: str
    tool_call_id: str
    tool_name: str
    request_id: str | None = None  # request that produced this call, not its own direct spend
    started_at: datetime = field(default_factory=utcnow)
    ended_at: datetime | None = None
    status: str = "running"
    input_tokens_estimate: int | None = None
    output_tokens_estimate: int | None = None
    output_bytes: int = 0
    output_images: int = 0
    single_read_context_cost_estimate_usd: float | None = None
    estimate_method: str = "o200k_base_text_only_one_uncached_read"
    metadata: dict[str, Any] = field(default_factory=dict)
