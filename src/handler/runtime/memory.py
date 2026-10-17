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
        name = {"type": "string", "description": "Topic name, e.g. 'people/danny' or "
                "'suppliers/acme'. Lowercase letters, digits, '/', '_', '-'."}
        content = {"type": "string", "description": "Markdown text."}
        description = {"type": "string", "description": "One-line summary shown in the "
                       "memory index, so future runs know what the topic holds."}

        def obj(props: dict, required: list[str]) -> dict:
            return {"type": "object", "properties": props, "required": required,
                    "additionalProperties": False}

        return [
            ToolSpec("memory_list", "memory.list", Risk.READ,
                     "List every shared memory topic as 'name: description'.",
                     obj({}, []), self._list),
            ToolSpec("memory_read", "memory.read", Risk.READ,
                     "Read the full content of one memory topic.",
                     obj({"name": name}, ["name"]), self._read),
            ToolSpec("memory_save", "memory.save", Risk.WRITE_INTERNAL,
                     "Save durable knowledge for future runs and other users: facts, "
                     "preferences, decisions, commitments. Appends to the topic if it "
                     "exists, otherwise creates it. Use this instead of writing files.",
                     obj({"name": name, "content": content, "description": description},
                         ["name", "content", "description"]), self._save),
            ToolSpec("memory_rewrite", "memory.rewrite", Risk.WRITE_INTERNAL,
                     "Replace a topic's whole content and description, e.g. to correct "
                     "or reorganize it. Optionally rename it with new_name.",
                     obj({"name": name, "content": content, "description": description,
                          "new_name": name}, ["name", "content", "description"]),
                     self._rewrite),
            ToolSpec("memory_delete", "memory.delete", Risk.WRITE_INTERNAL,
                     "Delete a memory topic that is obsolete or merged into another.",
                     obj({"name": name}, ["name"]), self._delete),
        ]
