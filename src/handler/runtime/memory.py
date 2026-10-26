"""Tools for the shared memory index and topic contents."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace

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

    @staticmethod
    def _user_prefix(user_id: str) -> str:
        digest = hashlib.sha256(user_id.encode()).hexdigest()[:24]
        return f"_users/{digest}/"

    @classmethod
    def _physical_name(cls, user_id: str | None, name: str) -> str:
        name = cls._name(name)
        if name.startswith("shared/"):
            shared = name.removeprefix("shared/")
            if not shared:
                raise ValueError("Shared memory names must be shared/<topic>")
            return cls._name(shared)
        if user_id is None:
            raise ValueError("A user identity is required for private memory")
        return cls._user_prefix(user_id) + name

    @classmethod
    def _logical_topic(cls, user_id: str | None, topic: MemoryTopic) -> MemoryTopic | None:
        if topic.name.startswith("_users/"):
            prefix = cls._user_prefix(user_id) if user_id is not None else ""
            return replace(topic, name=topic.name.removeprefix(prefix)) \
                if prefix and topic.name.startswith(prefix) else None
        # Topics from before private spaces are household memory.  Showing the
        # prefix makes that sharing explicit without a destructive migration.
        return replace(topic, name=f"shared/{topic.name}")

    async def list_visible(self, user_id: str | None) -> list[MemoryTopic]:
        topics = [self._logical_topic(user_id, topic) for topic in await self.store.list_topics()]
        return sorted((topic for topic in topics if topic is not None), key=lambda t: t.name)

    async def get_visible(self, user_id: str | None, name: str) -> MemoryTopic | None:
        physical = self._physical_name(user_id, name)
        topic = await self.store.get_topic(physical)
        return self._logical_topic(user_id, topic) if topic is not None else None

    async def put_visible(
        self, user_id: str | None, topic: MemoryTopic, expected_version: int | None,
    ) -> MemoryTopic | None:
        physical = self._physical_name(user_id, topic.name)
        stored = await self.store.put_topic(replace(topic, name=physical), expected_version)
        return self._logical_topic(user_id, stored) if stored is not None else None

    async def delete_visible(self, user_id: str | None, name: str) -> bool:
        return await self.store.delete_topic(self._physical_name(user_id, name))

    async def index_text(self, max_chars: int = 4000, *, user_id: str | None = None) -> str:
        index = "\n".join(f"{t.name}: {t.description}"
                          for t in await self.list_visible(user_id))
        return index[:max(0, max_chars)]

    @staticmethod
    def _user(ctx: RunContext) -> str | None:
        return ctx.acting_as or ctx.user_id

    async def _list(self, ctx: RunContext, args: dict) -> ToolResult:
        return ToolResult("\n".join(
            f"{t.name}: {t.description}" for t in await self.list_visible(self._user(ctx))))

    async def _read(self, ctx: RunContext, args: dict) -> ToolResult:
        topic = await self.get_visible(self._user(ctx), args["name"])
        return ToolResult(topic.content if topic else "Topic not found", is_error=topic is None)

    async def _save(self, ctx: RunContext, args: dict) -> ToolResult:
        name = self._name(args["name"])
        user_id = self._user(ctx)
        for _ in range(3):
            try:
                old = await self.get_visible(user_id, name)
            except ValueError as exc:
                return ToolResult(str(exc), is_error=True)
            topic = MemoryTopic(name, args["description"],
                                old.content + "\n\n" + args["content"] if old else args["content"],
                                updated_by=user_id)
            stored = await self.put_visible(user_id, topic, old.version if old else None)
            if stored:
                return ToolResult({"name": stored.name, "version": stored.version})
        return ToolResult("Memory version conflict; retry the save", is_error=True)

    async def _rewrite(self, ctx: RunContext, args: dict) -> ToolResult:
        name = self._name(args["name"])
        new_name = self._name(args.get("new_name", name))
        user_id = self._user(ctx)
        try:
            old = await self.get_visible(user_id, name)
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        if old is None:
            return ToolResult("Topic not found", is_error=True)
        topic = MemoryTopic(new_name, args["description"], args["content"],
                            updated_by=ctx.acting_as or ctx.user_id)
        stored = await self.put_visible(user_id, topic, old.version if new_name == name else None)
        if stored is None:
            return ToolResult("Memory version conflict", is_error=True)
        if new_name != name:
            await self.delete_visible(user_id, name)
        return ToolResult({"name": stored.name, "version": stored.version})

    async def _delete(self, ctx: RunContext, args: dict) -> ToolResult:
        deleted = await self.delete_visible(self._user(ctx), args["name"])
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
                     "List your private memory and explicit shared/ household topics.",
                     obj({}, []), self._list),
            ToolSpec("memory_read", "memory.read", Risk.READ,
                     "Read the full content of one memory topic.",
                     obj({"name": name}, ["name"]), self._read),
            ToolSpec("memory_save", "memory.save", Risk.WRITE_INTERNAL,
                     "Save durable private knowledge for this user. Appends to the topic "
                     "if it exists. Use a shared/<topic> name only when the user explicitly "
                     "asks to share the knowledge with everyone in this Handler instance.",
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
