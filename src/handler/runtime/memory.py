"""Tools for the shared memory index and topic contents."""

from __future__ import annotations

import re

from .context import RunContext
from .gateway.base import ToolResult, ToolSpec
from .store.base import MemoryStore
from .types import MemoryTopic, Risk


class MemoryTools:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    @staticmethod
    def _name(name: str) -> str:
        if (not isinstance(name, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9/_-]{0,99}", name) or ".." in name):
            raise ValueError("Invalid topic name; use 1–100 lowercase letters, digits, /, _ or -")
        return name

    async def index_text(self, max_chars: int = 4000) -> str:
        index = "\n".join(f"{t.name}: {t.description}" for t in await self.store.list_topics())
        return index[:max(0, max_chars)]

    async def _list(self, ctx: RunContext, args: dict) -> ToolResult:
        return ToolResult("\n".join(f"{t.name}: {t.description}" for t in await self.store.list_topics()))

    async def _read(self, ctx: RunContext, args: dict) -> ToolResult:
        topic = await self.store.get_topic(self._name(args["name"]))
        return ToolResult(topic.content if topic else "Topic not found", is_error=topic is None)

    async def _save(self, ctx: RunContext, args: dict) -> ToolResult:
        name = self._name(args["name"])
        for _ in range(3):
            old = await self.store.get_topic(name)
            topic = MemoryTopic(name, args["description"],
                                old.content + "\n\n" + args["content"] if old else args["content"],
                                updated_by=ctx.acting_as or ctx.user_id)
            stored = await self.store.put_topic(topic, old.version if old else None)
            if stored:
                return ToolResult({"name": stored.name, "version": stored.version})
        return ToolResult("Memory version conflict; retry the save", is_error=True)

    async def _rewrite(self, ctx: RunContext, args: dict) -> ToolResult:
        name = self._name(args["name"])
        new_name = self._name(args.get("new_name", name))
        old = await self.store.get_topic(name)
        if old is None:
            return ToolResult("Topic not found", is_error=True)
        topic = MemoryTopic(new_name, args["description"], args["content"],
                            updated_by=ctx.acting_as or ctx.user_id)
        stored = await self.store.put_topic(topic, old.version if new_name == name else None)
        if stored is None:
            return ToolResult("Memory version conflict", is_error=True)
        if new_name != name:
            await self.store.delete_topic(name)
        return ToolResult({"name": stored.name, "version": stored.version})

    async def _delete(self, ctx: RunContext, args: dict) -> ToolResult:
        deleted = await self.store.delete_topic(self._name(args["name"]))
        return ToolResult({"deleted": deleted})

    def tools(self) -> list[ToolSpec]:
        specs = []
        for action, fields, handler in [
            ("list", [], self._list), ("read", ["name"], self._read),
            ("save", ["name", "content", "description"], self._save),
            ("rewrite", ["name", "content", "description", "new_name"], self._rewrite),
            ("delete", ["name"], self._delete),
        ]:
            specs.append(ToolSpec(
                f"memory_{action}", f"memory.{action}",
                Risk.READ if action in {"list", "read"} else Risk.WRITE_INTERNAL,
                f"{action.capitalize()} shared memory topics",
                {"type": "object", "properties": {f: {"type": "string"} for f in fields},
                 "required": [f for f in fields if f != "new_name"], "additionalProperties": False},
                handler,
            ))
        return specs
