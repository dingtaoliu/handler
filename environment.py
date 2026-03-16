"""Environment: the event loop that connects channels to the agent."""

import asyncio
import logging
from abc import ABC, abstractmethod

from .types import Event
from .agent import Agent
from .event_store import EventStore

logger = logging.getLogger("handler.environment")


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

    def __init__(self, agent: Agent, store: EventStore):
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
            if event.type == "session_expiry":
                await self._handle_session_expiry(event)
                return

            response = await self._process(event)
            channel = self.channels.get(event.source)
            if channel:
                await channel.deliver(event, response)
        except Exception as e:
            logger.error(f"event processing failed: {e}", exc_info=True)
            channel = self.channels.get(event.source)
            if channel:
                try:
                    await channel.deliver(event, f"Error: {e}")
                except Exception:
                    pass

    async def _handle_session_expiry(self, event: Event) -> None:
        """End an expired session: let the agent persist to memory, then compact."""
        cid = event.conversation_id
        if not cid:
            return
        active = self.store.get_messages(cid)
        if not active:
            return
        logger.info(f"[session] ending expired session {cid}")
        await self.agent.end_session(cid)

    async def _process(self, event: Event) -> str:
        cid = event.conversation_id or f"{event.source}:default"
        content = event.data.get("content", "")

        if event.type == "cron_prompt":
            job_name = event.data.get("cron_job_name", "unknown")
            content = (
                f"[SCHEDULED TASK — job: {job_name}]\n"
                f"{content}\n"
                f"(This is an automated cron prompt, not a live user message.)"
            )

        self.store.ensure_conversation(cid, channel=event.source)
        role = "system" if event.type == "cron_prompt" else "user"
        self.store.add_message(cid, role, content)
        self.store.log_event(event.type, event.source, event.data)

        messages = self.store.get_messages(cid)
        response = await self.agent.run(cid, messages)

        self.store.add_message(cid, "assistant", response)
        self.store.log_event("agent_response", event.source, {"content": response[:500]})

        return response
