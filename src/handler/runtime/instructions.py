"""Render shared settings and memory for any harness."""

from .mail.instructions import DEFAULT_EMAIL_INSTRUCTIONS
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
    if thread_type == ThreadType.EMAIL:
        sections.append(await email_instructions(settings))
    else:
        sections.append(_GUIDANCE[thread_type])
    sections.append("Durable facts go to memory_save, not files.")
    sections.append("If you have crontal_api_* tools: find endpoints with crontal_api_routes, "
                    "and call crontal_api_describe before a write you haven't made in this "
                    "thread, so the body matches its schema. A 400/422 result includes the "
                    "spec as `expected`: fix the call from it once, then report.")
    sections.append("If you have gmail_* tools: you can search, read and draft email. Drafts "
                    "wait in the mailbox for a human to review and send. Send (gmail_send, "
                    "gmail_send_draft) only when the user asked for it: every send waits for "
                    "a human's approval, so never say an email was sent until a result says "
                    "status 'sent'.")
    return "\n\n".join(sections)


async def email_instructions(settings: SettingsService) -> str:
    email, _ = await settings.get("email")
    return (email.get("instructions") or "").strip() or DEFAULT_EMAIL_INSTRUCTIONS


_GUIDANCE = {
    ThreadType.CHAT: ("Reply to the user. On the first turn of a conversation, call thread_set_title "
                      "to create a concise, descriptive name (about 3–7 words) based on their request. "
                      "This tool preserves existing names and user renames."),
    ThreadType.GMAIL: "The event is an email; draft rather than send unless told otherwise.",
    ThreadType.AUTOMATION: "This is a scheduled job; use notify to report anything worth a human's attention.",
    ThreadType.CRONTAL: ("This is a Crontal notification about one entity (an RFQ, bid, ...). "
                         "Look it up through Crontal's API (crontal_api_routes to find the "
                         "endpoint, crontal_api_get to read), act within your user's "
                         "authority, and use notify for anything they should know or decide."),
}
