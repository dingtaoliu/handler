"""Handler. The legacy agent's top-level names load on first use (PEP 562).

Importing a subpackage such as handler.runtime then doesn't import the legacy
agent stack (about 90 MB), which matters for the runtime's reader processes.
"""

from importlib import import_module

_EXPORTS = {
    "Message": ".types", "Event": ".types",
    "EventStore": ".event_store",
    "AgentContext": ".context",
    "BaseAgent": ".agent", "OpenAIAgent": ".agent", "ClaudeAgent": ".agent", "Agent": ".agent",
    "Environment": ".environment", "Channel": ".environment",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
