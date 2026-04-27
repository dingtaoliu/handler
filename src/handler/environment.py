"""Environment: the event loop that connects channels to the agent."""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Protocol, cast

from .types import Event
from .event_store import EventStore

logger = logging.getLogger("handler.environment")


class _PushMessageChannel(Protocol):
    async def push_message(
        self, conversation_id: str, role: str, content: str
    ) -> None: ...


class Channel(ABC):
    """Event source + response destination. Pushes events into the shared queue."""

    name: str

    @abstractmethod
    async def start(self, queue: asyncio.Queue) -> None:
        """Start producing events. Push Event objects into the queue."""
        ...

    async def deliver(self, event: Event, response: str) -> None:
        """Deliver the agent's response. Default: resolve the event's future."""
        if event._response_future and not event._response_future.done():
            event._response_future.set_result(response)


class Environment:
    """The outer loop. Starts channels, consumes events, runs the agent, delivers responses."""

    def __init__(self, agent, store: EventStore):
        self.agent = agent
        self.store = store
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self.channels: dict[str, Channel] = {}

    def add_channel(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

    async def run(self) -> None:
        for channel in self.channels.values():
            asyncio.create_task(channel.start(self.queue))
            logger.info(f"started channel: {channel.name}")

        logger.info(f"environment running ({len(self.channels)} channel(s))")

        while True:
            event = await self.queue.get()
            logger.info(
                f"event: type={event.type} source={event.source} "
                f"conversation={event.conversation_id}"
            )
            await self._handle(event)

    async def _handle(self, event: Event) -> None:
        try:
            response = await self._process(event)
            channel = self.channels.get(event.source)
            if channel:
                await channel.deliver(event, response)
            await self._broadcast_response(event, response)
            await self._notify_channel(event, response)
        except Exception as e:
            logger.error(f"event processing failed: {e}", exc_info=True)
            channel = self.channels.get(event.source)
            if channel:
                try:
                    await channel.deliver(event, f"Error: {e}")
                except Exception:
                    pass

    async def _notify_channel(self, event: Event, response: str) -> None:
        """Push cron responses to the specified notification channel (e.g. telegram)."""
        notify = event.data.get("notify_channel", "")
        if not notify or event.type != "cron_prompt":
            return
        cid = event.conversation_id
        if not cid:
            return
        channel = self.channels.get(notify)
        if channel is not None and hasattr(channel, "push_message"):
            push_channel = cast(_PushMessageChannel, channel)
            try:
                await push_channel.push_message(cid, "assistant", response)
                logger.info(
                    f"cron notification pushed to {notify} (conversation={cid})"
                )
            except Exception:
                logger.warning(f"cron notification to {notify} failed", exc_info=True)
        else:
            logger.warning(f"channel '{notify}' has no push_message method")

    async def _broadcast_response(self, event: Event, response: str) -> None:
        cid = event.conversation_id
        if not cid:
            return
        web = self.channels.get("web")
        if web is not None and hasattr(web, "push_message"):
            push_channel = cast(_PushMessageChannel, web)
            try:
                await push_channel.push_message(cid, "assistant", response)
            except Exception:
                logger.warning("web push failed", exc_info=True)

    async def _process(self, event: Event) -> str:
        cid = event.conversation_id or f"{event.source}:default"
        content = event.data.get("content", "")
        images = event.data.get("images")  # list of {"path": ..., "media_type": ...}

        if event.type == "cron_prompt":
            job_name = event.data.get("cron_job_name", "unknown")
            content = (
                f"[SCHEDULED TASK — job: {job_name}]\n"
                f"{content}\n"
                f"(This is an automated cron prompt, not a live user message.)"
            )

        # Build multi-modal content blocks if images are present
        if images:
            blocks = []
            if content:
                blocks.append({"type": "text", "text": content})
            for img in images:
                blocks.append(
                    {
                        "type": "image",
                        "path": img["path"],
                        "media_type": img.get("media_type", "image/jpeg"),
                    }
                )
            store_content = blocks
        else:
            store_content = content

        self.store.ensure_conversation(cid, channel=event.source)
        role = "system" if event.type == "cron_prompt" else "user"
        self.store.add_message(cid, role, store_content)
        self.store.log_event(event.type, event.source, event.data)

        messages = self.store.get_messages(cid)
        response = await self.agent.run(cid, messages)

        self.store.add_message(cid, "assistant", response)
        self.store.log_event(
            "agent_response", event.source, {"content": response[:500]}
        )

        return response
