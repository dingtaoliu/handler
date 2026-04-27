from .base import BaseAgent
from .openai import OpenAIAgent
from .openai_manual import OpenAIManualAgent
from .claude import ClaudeAgent
from .loop import ManualAgent

# Backward-compat alias — existing code imports `from .agent import Agent`
Agent = OpenAIAgent

__all__ = [
    "BaseAgent",
    "OpenAIAgent",
    "OpenAIManualAgent",
    "ClaudeAgent",
    "ManualAgent",
    "Agent",
]
