"""
AgentSession: context object for one agent request-response cycle.

Holds the config and conversation history (pre-built wire messages).
Pass this to AgentLoop; tools receive it via session= so they can
access any app-specific context you attach in a subclass.

Usage:
    session = AgentSession(config=AgentConfig(), history=[...])
    state = AgentState(user_message="Hello", system_prompt=session.config.system_prompt)
    state.messages = session.build_messages()
    state.messages.append({"role": "user", "content": "Hello"})
"""

from dataclasses import dataclass, field
from typing import Optional

from .agent_config import AgentConfig
from .workspace import Workspace


@dataclass
class AgentSession:
    """
    Context for one agent request-response cycle.

    Subclass this to attach app-specific context (auth tokens, DB sessions,
    user objects, etc.) that your tools need via the session= argument.

    Fields:
        config:    AgentConfig controlling model, temperature, limits, etc.
        history:   Pre-built wire-format messages representing prior conversation
                   turns (i.e. [{"role": "user", "content": "..."}, ...]).
                   Does NOT include the system message — that is prepended by
                   build_messages().
        workspace: Optional per-session temp directory for file caching.
    """

    config: AgentConfig
    history: list[dict] = field(default_factory=list)
    workspace: Optional[Workspace] = None

    def build_messages(self) -> list[dict]:
        """
        Return the wire-format message list to pass to the LLM.

        Format: [system_message, ...history...]

        The caller is responsible for appending the new user message
        after calling this.
        """
        msgs: list[dict] = [
            {"role": "system", "content": self.config.system_prompt}
        ]
        msgs.extend(self.history)
        return msgs
