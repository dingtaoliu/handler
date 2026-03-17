"""AgentContext: the four layers that make up the agent's system prompt.

Config lives on disk as plain text files:
    data/config/system.md    — framework-level rules (how the agent operates)
    data/config/identity.md  — role and mission (what the agent does)
    data/config/persona.md   — communication style (how the agent talks)

User info lives in memory files (data/memory/*.md) and is loaded dynamically.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .memory import Memory

logger = logging.getLogger("handler.context")

DEFAULT_SYSTEM = """\
You are an autonomous agent with access to tools.

Memory:
- Your memory files are loaded into your context automatically at the start of each turn.
- Use remember() to save information across conversations, recall_memory() to read full files, \
forget() to delete, and list_memory() to see all files.
- Keep files focused: one topic per file (e.g. user.md, tax_situation.md, career.md).
- IMPORTANT: Your conversation history is periodically compacted or reset. \
Memory files are the only reliable way to carry information forward. \
If you learn something important during a conversation, save it with remember() — \
do not assume it will be in context next time.

Tool usage:
- Prefer tools over guessing.
- For reading code/text files, use read_source(). For PDFs/DOCX/binary formats, use read_file().
- For modifying files, use patch_file() for targeted edits or write_file() for new files.
- For handler source files, patch_file() and write_core_file() auto-create git checkpoints.
- If you encounter errors in logs or import failures, diagnose with run_shell() or run_python() \
and fix autonomously. Prefer fixing problems over asking the user for help."""

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

Then use remember('user.md', content) to save information about the user to memory.

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
        health_problems: list[str] | None = None,
    ) -> str:
        sections = []

        # System layer
        system = self._read(self.config_dir / "system.md") or DEFAULT_SYSTEM
        sections.append(system)

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

        # Load memory files
        if self.memory is not None:
            sections.append(self.memory.build_prompt_section())
        else:
            mem_dir = self.memory_dir.resolve()
            md_files = sorted(mem_dir.glob("*.md")) if mem_dir.exists() else []
            mem_parts = [f"# Memory (directory: {mem_dir})"]
            if md_files:
                for f in md_files:
                    try:
                        content = f.read_text().strip()
                        if content:
                            mem_parts.append(f"## {f.name}\n{content}\n")
                    except Exception:
                        pass
            else:
                mem_parts.append("No memory files yet.")
            sections.append("\n".join(mem_parts))

        if token_brief:
            sections.append(f"# Cost Tracking\n{token_brief}")

        # Health problems — injected by the caller (Agent), not fetched here
        if health_problems:
            sections.append(
                "# Health Issues\n"
                "The following problems were detected automatically. "
                "Try to fix them or inform the user.\n\n"
                + "\n".join(f"- {p}" for p in health_problems)
            )

        return "\n\n".join(sections)
