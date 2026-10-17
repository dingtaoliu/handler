"""Render shared settings and memory for any harness."""

from .memory import MemoryTools
from .settings import SettingsService
from .types import ThreadType


async def build_instructions(
    settings: SettingsService, memory_tools: MemoryTools, *,
    speaker: str | None, thread_type: ThreadType,
) -> str:
    persona, _ = await settings.get("persona")
    sections = [persona[name] for name in ("system", "identity", "persona") if persona[name]]
    sections.append("# Memory\n" + await memory_tools.index_text()
                    + "\nUse memory_read for details.")
    if speaker is not None:
        sections.append(f"You are talking with {speaker}")
    sections.append({
        ThreadType.CHAT: "Reply to the user.",
        ThreadType.GMAIL: "The event is an email; draft rather than send unless told otherwise.",
        ThreadType.AUTOMATION: "This is a scheduled job; use notify to report anything worth a human's attention.",
        ThreadType.CRONTAL: ("This is a Crontal notification about one entity (an RFQ, bid, ...). "
                             "Use the crontal_* tools to look at it, act within your user's "
                             "authority, and use notify for anything they should know or decide."),
    }[thread_type])
    sections.append("Durable facts go to memory_save, not files.")
    return "\n\n".join(sections)
