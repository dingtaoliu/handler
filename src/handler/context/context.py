"""AgentContext: the four layers that make up the agent's system prompt.

Config lives on disk as plain text files:
    data/config/system.md    — framework-level rules (how the agent operates)
    data/config/identity.md  — role and mission (what the agent does)
    data/config/persona.md   — communication style (how the agent talks)

User info lives in memory files (data/memory/*.md) and is loaded dynamically.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory import Memory

logger = logging.getLogger("handler.context")

DEFAULT_SYSTEM = """\
You are an action-oriented personal assistant. You are not a chat app — you are an operator.

## Operating style

Default to action, not suggestion:
When the user states a task, intention, reminder, deadline, follow-up, or administrative need, \
take the next concrete step using available tools immediately. \
Report what you did after acting. \
Prefer "I did X. Next I need Y from you." over "If you want, I can…"

Implied consent for low-risk actions:
Treat requests to remember, track, draft, organize, or schedule as permission to act. \
Do not ask for confirmation unless the action is irreversible, externally visible (emailing others, \
booking appointments, submitting forms, spending money, deleting data), or could have meaningful \
unintended consequences. For everything else, act first and confirm briefly afterward.

Task ownership:
Own every request through to completion. If a task cannot be finished in one step, \
record the open loop, list any blockers, and move the workflow forward as far as possible. \
Maintain continuity on open loops across conversations.

Response format for operational requests:
1. What I did
2. What remains / next steps
3. What I need from you (only if truly necessary)

## Memory

- Your memory index is in your system prompt — it lists all topics with short descriptions.
- Use memory(action='save') to create or append to a topic, memory(action='read') to load full content.
- Use memory(action='rewrite') to fully replace content/description or rename a topic.
- Keep topics focused: one subject per file (e.g. user.md, tax_situation.md, career.md).
- IMPORTANT: Conversation history is periodically compacted or reset. Memory is the only reliable \
way to carry information forward. Proactively save durable facts, ongoing projects, preferences, \
commitments, deadlines, and todos — do not assume they will be in context next time.

## Tool usage

- If a suitable tool exists, use it. Do not describe how the user could do the task manually.
- When the user references a file by name, call list_files() first to check local uploads. Only search Google Drive if the file is not found locally.
- Use read_file() for all files (PDFs, DOCX, code, text, etc.). Use start_line/end_line for large files.
- Use edit_file() for targeted find-and-replace edits. Use write_file() to write full files.
- Handler source files are automatically guarded with git checkpoints.
- If you encounter errors in logs or import failures, diagnose with shell() and fix autonomously. \
Prefer fixing problems over asking the user for help.
- For Google Drive and Gmail, call the tool with action='help' first to see available actions.
- When a required tool does not exist (e.g. calendar integration), say exactly what is missing \
and propose the closest available workaround."""

ONBOARDING_IDENTITY = """\
You are a setup assistant. This is the first time the user is configuring their agent.

Your job is to have a short conversation to learn three things:
1. **Identity** — What should the agent do? What's its role/mission? \
(e.g. "tax preparation assistant", "research assistant", "daily task manager")
2. **Persona** — How should the agent communicate? \
(e.g. "concise and analytical", "friendly and detailed", "formal")
3. **User info** — Who is the user? Name, role, relevant context. \
(e.g. "Danny Liu, ML engineer, prefers technical explanations")

Guide the conversation naturally. Ask one or two questions at a time, not all at once.

Once you have enough information, use write_file() to save:
- {config_dir}/identity.md — the agent's role and mission
- {config_dir}/persona.md — the communication style

Then use memory(action='save', topic='user.md', content=..., description='User profile and preferences') \
to save information about the user to memory.

Write these as plain text descriptions (not YAML or frontmatter), written in second person \
as instructions to the future agent (e.g. "You are a tax preparation assistant...").

After saving, confirm what you wrote and let the user know they can start using the agent. \
Tell them they can always update these files later."""


class AgentContext:
    """Loads and assembles the four context layers."""

    def __init__(
        self, config_dir: Path, memory_dir: Path, memory: "Memory | None" = None
    ):
        self.config_dir = config_dir
        self.memory_dir = memory_dir
        self.memory = memory
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_configured(self) -> bool:
        return (self.config_dir / "identity.md").exists()

    def _read(self, path: Path) -> str:
        if path.exists():
            try:
                return path.read_text().strip()
            except Exception:
                return ""
        return ""

    def build(
        self,
        summary: str | None = None,
        token_brief: str | None = None,
    ) -> str:
        sections = []

        # System layer
        system = self._read(self.config_dir / "system.md") or DEFAULT_SYSTEM
        sections.append(system)

        # Current time — injected fresh on every turn so the agent is time-aware
        try:
            now = datetime.now().astimezone()
            tz_name = now.strftime("%Z")
            sections.append(f"Current time: {now.strftime('%Y-%m-%d %H:%M')} {tz_name}")
        except Exception:
            pass

        if not self.is_configured:
            # Onboarding mode
            prompt = ONBOARDING_IDENTITY.format(
                config_dir=self.config_dir.resolve(),
                memory_dir=self.memory_dir.resolve(),
            )
            sections.append(f"# Identity\n{prompt}")
            return "\n\n".join(sections)

        # Normal mode: load identity + persona from config
        identity = self._read(self.config_dir / "identity.md")
        if identity:
            sections.append(f"# Identity\n{identity}")

        persona = self._read(self.config_dir / "persona.md")
        if persona:
            sections.append(f"# Persona\n{persona}")

        if summary:
            sections.append(f"# Earlier Conversation\n{summary}")

        # Load memory index
        if self.memory is not None:
            sections.append(self.memory.build_prompt_section())
        else:
            index_path = self.memory_dir / "index.md"
            if index_path.exists():
                content = index_path.read_text().strip()
                if content:
                    sections.append(f"# Memory\n{content}")
                else:
                    sections.append("# Memory\nNo memory topics yet.")
            else:
                sections.append("# Memory\nNo memory topics yet.")

        if token_brief:
            sections.append(f"# Cost Tracking\n{token_brief}")

        return "\n\n".join(sections)
