"""Backward-compatibility shim.

OpenAIManualAgent is now ManualAgent pre-wired with OpenAIProvider.
New code should use ManualAgent + OpenAIProvider directly.
"""

from .loop import ManualAgent
from .providers.openai import OpenAIProvider


class OpenAIManualAgent(ManualAgent):
    """ManualAgent pre-wired with OpenAIProvider."""

    def __init__(self, model: str = "gpt-5.4-mini", **kwargs):
        super().__init__(provider=OpenAIProvider(model), model=model, **kwargs)
