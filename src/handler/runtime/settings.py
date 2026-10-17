"""Validated runtime settings with append-only history."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .store.base import SettingsStore
from .types import Decision, Risk, SettingsVersion


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersonaSettings(Section):
    system: str = ""
    identity: str = ""
    persona: str = ""


class ThreadSettings(Section):
    harness: str = "codex"
    model: str | None = None
    reasoning_effort: str | None = None


class ThreadsSettings(Section):
    chat: ThreadSettings = Field(default_factory=ThreadSettings)
    gmail: ThreadSettings = Field(default_factory=ThreadSettings)
    automation: ThreadSettings = Field(default_factory=ThreadSettings)
    crontal: ThreadSettings = Field(default_factory=ThreadSettings)


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


MODELS = {
    "persona": PersonaSettings, "threads": ThreadsSettings,
    "grants": GrantsSettings, "limits": LimitsSettings,
}


class SettingsConflict(Exception):
    pass


class SettingsService:
    def __init__(self, store: SettingsStore) -> None:
        self.store = store

    async def get(self, section: str) -> tuple[dict, int | None]:
        model = MODELS[section]
        stored = await self.store.get_settings(section)
        if stored:
            return model.model_validate(stored.value).model_dump(mode="json"), stored.version
        return model().model_dump(mode="json"), None

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
