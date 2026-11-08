"""Validated runtime settings with append-only history."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .gateway.crontal_catalog import (
    DEFAULT_FULL_SPECS,
    DEFAULT_RESOURCES,
    resource_prefix,
    spec_key,
)
from .mail.instructions import DEFAULT_EMAIL_INSTRUCTIONS
from .store.base import SettingsStore
from .types import Decision, Risk, SettingsVersion


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersonaSettings(Section):
    system: str = ""
    identity: str = ""
    persona: str = ""


class HarnessName(StrEnum):
    """Harnesses this runtime can run; settings UIs render it as a dropdown."""

    CODEX = "codex"
    OPENAI_AGENTS = "openai_agents"


# Where a new thread's harness comes from: a fixed name, or a lookup that
# reads the current settings (so changing it needs no restart).
HarnessSource = str | Callable[[], Awaitable[str]]


async def resolve_harness(source: HarnessSource) -> str:
    return source if isinstance(source, str) else await source()


class ModelName(StrEnum):
    """Models the Codex harness lists (`codex debug models`, visibility=list)."""

    GPT_6_1_SOL = "gpt-6.1-sol"
    GPT_6_1_TERRA = "gpt-6.1-terra"
    GPT_6_ASTRA = "gpt-6-astra"
    GPT_6_SOL = "gpt-6-sol"
    GPT_6_LUNA = "gpt-6-luna"
    GPT_5_6_SOL = "gpt-5.6-sol"
    GPT_5_6_TERRA = "gpt-5.6-terra"
    GPT_5_6_LUNA = "gpt-5.6-luna"


class ReasoningEffort(StrEnum):
    """Levels every listed model supports."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ThreadSettings(Section):
    harness: HarnessName = Field(HarnessName.CODEX, title="Harness", description="Agent harness that runs this thread type.")
    model: ModelName | None = Field(None, title="Model",
                                    description="Model override; empty uses the default shown (instance, else harness).")
    reasoning_effort: ReasoningEffort | None = Field(
        None, title="Reasoning effort",
        description="Reasoning effort override; empty uses the default shown (instance, else harness).")


# Each field carries its own title: settings UIs resolve the shared
# ThreadSettings definition and would otherwise label every type the same.
class ThreadsSettings(Section):
    chat: ThreadSettings = Field(default_factory=ThreadSettings, title="Chat",
                                 description="Conversations with users.")
    gmail: ThreadSettings = Field(default_factory=ThreadSettings, title="Gmail",
                                  description="Incoming Gmail threads.")
    automation: ThreadSettings = Field(default_factory=ThreadSettings, title="Automations",
                                       description="Scheduled jobs.")
    crontal: ThreadSettings = Field(default_factory=ThreadSettings, title="Crontal",
                                    description="Notifications from Crontal (RFQs, bids).")
    email: ThreadSettings = Field(default_factory=ThreadSettings, title="Email intake",
                                  description="Emails forwarded to the intake address.")
    mail: ThreadSettings = Field(default_factory=ThreadSettings, title="Mailboxes",
                                 description="New mail in connected IMAP mailboxes.")

    @model_validator(mode="before")
    @classmethod
    def _email_like_crontal(cls, value: Any) -> Any:
        # Instances saved before the email/mail types existed: start them like
        # the closest background type (same harness), not on the bare default.
        if isinstance(value, dict):
            if "email" not in value and "crontal" in value:
                value = {**value, "email": value["crontal"]}
            if "mail" not in value and ("email" in value or "crontal" in value):
                value = {**value, "mail": value.get("email") or value["crontal"]}
        return value


class GrantsSettings(Section):
    defaults: dict[Risk, Decision] = Field(default_factory=lambda: {
        Risk.READ: Decision.ALLOW, Risk.WRITE_INTERNAL: Decision.ALLOW,
        Risk.WRITE_EXTERNAL: Decision.ASK, Risk.EXEC: Decision.ASK,
    })
    actions: dict[str, Decision] = Field(default_factory=dict)


class LimitsSettings(Section):
    run_timeout_seconds: float = Field(default=600, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    interactive_capacity: int = Field(default=2, ge=0)
    background_capacity: int = Field(default=1, ge=0)


class EmailSettings(Section):
    """Email intake (POST /ingest/email, fed by the Cloudflare Email Worker)."""

    blocked_senders: list[str] = Field(
        default_factory=list, title="Blocked senders",
        description="Addresses (name@example.com) or domains (@example.com) to ignore. "
                    "Empty: accept all senders that pass authentication.")
    trusted_senders: list[str] = Field(
        default_factory=list, title="Trusted senders",
        description="Addresses (name@example.com) or domains (@example.com) whose mail "
                    "is marked trusted for the agent, when DKIM confirms the From. "
                    "Everyone else is marked external. This doesn't gate acceptance.")

    @model_validator(mode="before")
    @classmethod
    def remove_legacy_allowlist(cls, value):
        # Old settings remain readable; allowlist entries are not block entries.
        if isinstance(value, dict):
            value = dict(value)
            value.pop("allowed_senders", None)
        return value

    require_auth: bool = Field(
        True, title="Require DKIM",
        description="Only accept mail with a valid DKIM signature aligned with the sender.")
    act_as: str | None = Field(
        None, title="Attribute to user",
        description="Optional handler user id that email threads are attributed to "
                    "(approvals, failure notices). The agent always works in Crontal as "
                    "the instance's AI user. Empty: no attribution; emails are ignored "
                    "only when there is no Crontal AI identity either.")
    instructions: str = Field(
        DEFAULT_EMAIL_INSTRUCTIONS, title="Instructions",
        description="What the agent does with each email. Empty restores the default.")


class MemoryKind(Section):
    """A kind of topic the agent keeps current (docs/rebuild/memory-kinds.md)."""

    name: str = Field(..., title="Name", pattern=r"^[a-z][a-z0-9_-]{0,39}$",
                      description="Kind id, e.g. inquiry.")
    prefix: str = Field(..., title="Topic prefix", pattern=r"^[a-z0-9][a-z0-9/_-]{0,59}/$",
                        description="Topics of this kind are named <prefix><slug>, "
                                    "e.g. inquiries/acme-304-pipe. Ends with '/'.")
    description: str = Field("", title="Description",
                             description="When to keep a topic of this kind (one per what).")
    template: str = Field("", title="Template",
                          description="Markdown outline each topic of this kind follows.")


class MemorySettings(Section):
    """Shared memory: special kinds of topics and the size of what the prompt shows."""

    kinds: list[MemoryKind] = Field(
        default_factory=list, title="Kinds",
        description="Kinds of topics the agent maintains, each with a name prefix and a "
                    "template. Empty: only general topics.")
    max_topic_chars: int = Field(
        8000, ge=500, le=200_000, title="Max topic size",
        description="A save that makes a topic longer than this still succeeds, but tells "
                    "the agent to rewrite the topic into a summary.")
    index_general_limit: int = Field(
        100, ge=0, le=1000, title="General topics in the index",
        description="How many general (kind-less) topics the prompt's index lists.")
    index_recent_limit: int = Field(
        30, ge=0, le=500, title="Recent topics in the index",
        description="How many of the most recently updated other topics the index lists.")
    index_max_chars: int = Field(
        4000, ge=0, le=50_000, title="Index size",
        description="Character cap of the memory index in the prompt.")
    relevant_max_chars: int = Field(
        6000, ge=0, le=50_000, title="Relevant memory size",
        description="Character cap of the topics linked to the current thread that a run "
                    "starts with.")

    @model_validator(mode="after")
    def _unique_kinds(self) -> "MemorySettings":
        names = [k.name for k in self.kinds]
        prefixes = [k.prefix for k in self.kinds]
        if len(set(names)) != len(names):
            raise ValueError("memory kinds must have unique names")
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("memory kinds must have unique prefixes")
        return self


class EfficiencySettings(Section):
    fresh_automation_sessions: bool = Field(
        True, description="Start each scheduled firing without prior raw report history; "
        "carry durable progress in memory. Approval continuations retain their session.")
    bounded_model_tasks: bool = Field(
        True, description="Let the main agent use a small, tool-free model for bounded "
        "classification, extraction and drafting; it remains responsible for verification.")
    classification_model: ModelName = ModelName.GPT_6_LUNA
    drafting_model: ModelName = ModelName.GPT_6_1_TERRA
    route_catalog: bool = Field(
        True, description="Put Crontal's API route catalog (and the contracts listed "
        "below) in the cached part of the instructions, so the agent can call common "
        "endpoints without a discovery round trip. Discovery tools stay available.")
    route_catalog_resources: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RESOURCES), title="Route catalog resources",
        description="Path prefixes ('/api/v1/rfq') whose routes are listed in the catalog. "
        "Other routes stay available through crontal_api_routes.")
    route_catalog_max_chars: int = Field(
        7_000, ge=1_000, le=200_000, description="Cap on the catalog's size (about 4 "
        "characters per token). All listed route lines are kept; contracts are added in "
        "list order while they fit. If the route lines alone exceed it, the catalog is "
        "left out.")
    route_catalog_full_specs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_FULL_SPECS), title="Route catalog full specs",
        description="Endpoints whose full contracts are included, most important first, "
        "as 'METHOD /api/v1/path' with path parameters as Crontal lists them "
        "(e.g. 'GET /api/v1/rfq/<int:rfq_id>').")

    @model_validator(mode="after")
    def check_route_catalog(self):
        bad = [entry for entry in self.route_catalog_full_specs if spec_key(entry) is None]
        if bad:
            raise ValueError("route_catalog_full_specs entries must look like "
                             f"'GET /api/v1/path': {bad}")
        bad = [entry for entry in self.route_catalog_resources if resource_prefix(entry) is None]
        if bad:
            raise ValueError("route_catalog_resources entries must be path prefixes like "
                             f"'/api/v1/rfq' (no spaces or parameters): {bad}")
        self.route_catalog_resources = [resource_prefix(e) for e in self.route_catalog_resources]
        return self


MODELS = {
    "persona": PersonaSettings, "threads": ThreadsSettings,
    "grants": GrantsSettings, "limits": LimitsSettings,
    "email": EmailSettings, "memory": MemorySettings, "efficiency": EfficiencySettings,
}


@dataclass(frozen=True)
class InstanceDefaults:
    """Per-instance fallbacks used when a thread type's setting is empty."""

    model: str | None = None
    reasoning_effort: str | None = "low"


# Built-in model of a harness when neither settings nor the instance set one,
# used when that harness isn't running here to ask (codex: its catalog decides).
HARNESS_DEFAULT_MODELS: dict[str, str | None] = {
    HarnessName.OPENAI_AGENTS.value: "gpt-6.1-sol",
    HarnessName.CODEX.value: None,
}

# Asks a harness what model/effort it uses when a run sets neither:
# harness name -> {"model", "reasoning_effort"} (None = only the vendor knows).
HarnessDefaults = Callable[[str], Awaitable[dict]]


class SettingsConflict(Exception):
    pass


class SettingsService:
    def __init__(self, store: SettingsStore, defaults: InstanceDefaults | None = None,
                 harness_defaults: HarnessDefaults | None = None,
                 section_defaults: dict[str, dict] | None = None) -> None:
        self.store = store
        self.defaults = defaults or InstanceDefaults()
        self.harness_defaults = harness_defaults
        # What a section holds before anything is saved, when it isn't the
        # model's own defaults (e.g. Crontal's memory kinds; see
        # Integration.settings_defaults). Saved versions always win.
        self.section_defaults: dict[str, dict] = dict(section_defaults or {})

    async def fallback(self, harness: str) -> dict:
        """What an empty model/effort setting resolves to on `harness`.

        Mirrors a run: RunExecutor._plan leaves an unset value empty, the
        harness then applies the instance default (HANDLER_RT_MODEL /
        HANDLER_RT_EFFORT) and, failing that, its own built-in default.
        """
        builtin: dict = {"model": HARNESS_DEFAULT_MODELS.get(harness)}
        if self.harness_defaults is not None:
            builtin = await self.harness_defaults(harness) or builtin
        return {
            "model": self.defaults.model or builtin.get("model"),
            "reasoning_effort": self.defaults.reasoning_effort or builtin.get("reasoning_effort"),
        }

    async def resolve_thread(self, entry: dict, *, harness: str | None = None,
                             thread_model: str | None = None) -> dict:
        """Concrete harness/model/effort a run uses with no per-message override."""
        harness = harness or entry.get("harness") or HarnessName.CODEX.value
        fallback = await self.fallback(harness)
        return {
            "harness": harness,
            "model": thread_model or entry.get("model") or fallback["model"],
            "reasoning_effort": entry.get("reasoning_effort") or fallback["reasoning_effort"],
        }

    async def harness_fallbacks(self) -> dict[str, dict]:
        """fallback() for every harness, so UIs can relabel when the harness changes."""
        return {h.value: await self.fallback(h.value) for h in HarnessName}

    async def effective(self, section: str, value: dict) -> dict:
        """What each nullable field resolves to when left empty; {} if none.

        Same shape as the section. For threads the model/effort are the values
        an empty field gives on that type's saved harness, independent of any
        override currently set (they label the "Default (...)" option).
        """
        if section != "threads":
            return {}
        fallbacks = await self.harness_fallbacks()
        out = {}
        for name, entry in value.items():
            harness = (entry or {}).get("harness") or HarnessName.CODEX.value
            out[name] = {"harness": harness,
                         **(fallbacks.get(harness) or await self.fallback(harness))}
        return out

    def harness_source(self, thread_type: str) -> Callable[[], Awaitable[str]]:
        """Live lookup of the harness configured for new threads of a type."""

        async def lookup() -> str:
            threads, _ = await self.get("threads")
            return (threads.get(thread_type) or {}).get("harness") or HarnessName.CODEX.value

        return lookup

    async def get(self, section: str) -> tuple[dict, int | None]:
        model = MODELS[section]
        stored = await self.store.get_settings(section)
        if stored:
            return model.model_validate(stored.value).model_dump(mode="json"), stored.version
        return self.default_value(section), None

    def default_value(self, section: str) -> dict:
        model = MODELS[section]
        if section in self.section_defaults:
            return model.model_validate(self.section_defaults[section]).model_dump(mode="json")
        return model().model_dump(mode="json")

    async def put(
        self, section: str, value: dict, updated_by: str | None,
        expected_version: int | None,
    ) -> SettingsVersion:
        validated = MODELS[section].model_validate(value).model_dump(mode="json")
        stored = await self.store.put_settings(
            section, validated, updated_by=updated_by, expected_version=expected_version,
        )
        if stored is None:
            raise SettingsConflict(f"Settings version conflict for {section}")
        return stored

    async def history(self, section: str, limit: int = 50) -> list[SettingsVersion]:
        MODELS[section]
        return await self.store.settings_history(section, limit=limit)

    async def rollback(self, section: str, version: int, updated_by: str | None) -> SettingsVersion:
        _, current_version = await self.get(section)
        # The protocol exposes history in pages capped by a count, so request
        # the whole known history to permit rollback beyond the default 50.
        history = await self.history(section, limit=current_version or 1)
        old = next((entry for entry in history if entry.version == version), None)
        if old is None:
            raise ValueError(f"Settings version {version} not found for {section}")
        return await self.put(section, old.value, updated_by, current_version)

    def schemas(self) -> dict[str, dict]:
        return {name: model.model_json_schema() for name, model in MODELS.items()}

    async def seed_from_files(self, handler_yaml: Path | None, agent_md: Path | None) -> None:
        values = {}
        if handler_yaml is not None and handler_yaml.exists():
            config = yaml.safe_load(handler_yaml.read_text()) or {}
            values.update({key: config[key] for key in ("threads", "grants", "limits") if key in config})
        if agent_md is not None and agent_md.exists():
            parts = re.split(r"(?m)^# (System|Identity|Persona)\s*$", agent_md.read_text())
            values["persona"] = {parts[i].lower(): parts[i + 1].strip()
                                 for i in range(1, len(parts), 2)}
        for section, value in values.items():
            if await self.store.get_settings(section) is None:
                try:
                    await self.put(section, value, None, None)
                except SettingsConflict:
                    pass  # Another starter seeded this section first.
