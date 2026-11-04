"""Crontal's default memory kinds (the `memory` settings section).

Applied to an instance with Crontal until its owner saves their own `memory`
settings. Handler core knows nothing about these: they are plain settings
values (docs/rebuild/memory-kinds.md).
"""

from __future__ import annotations

INQUIRY_TEMPLATE = """\
**Status:** new / quoting / quoted / negotiating / won / lost / closed (date)
**Buyer:** company (topic:companies/<slug>), contact person
**Lines:** one line per item: grade, shape, size, quantity, delivery terms
**Quote:** what we quoted or plan to quote, and when; open questions about price
**Open follow-ups:** who owes what, by when
**Records:** Crontal RFQ/bid ids, mail conversations (also in links)
**Log:** dated one-line entries of what changed (newest last)"""

COMPANY_TEMPLATE = """\
**Company:** name, country, website/domain; buyer, supplier or both
**Contacts:** name, role, email/phone (only as given by the company or our users)
**Preferences:** grades, standards, packaging, documents, communication language
**Terms:** payment, delivery (Incoterms), currency, agreed discounts
**Inquiries:** topic:inquiries/<slug> per inquiry, with status
**Notes:** relationship history worth knowing (dated)"""

CONVERSATION_TEMPLATE = """\
**With:** company (topic:companies/<slug>), people in the conversation
**About:** inquiry (topic:inquiries/<slug>) or subject
**Summary:** what was asked, what we answered, what was agreed (not the emails themselves)
**Open:** what we still owe them, or they owe us
**Last update:** date and one line"""

CRONTAL_MEMORY_DEFAULTS: dict = {
    "kinds": [
        {"name": "inquiry", "prefix": "inquiries/",
         "description": "One per buyer inquiry or RFQ (in Crontal or by email): its status, "
                        "buyer, lines, quote state and open follow-ups. Link the Crontal "
                        "records (crontal:rfq:<id>, crontal:bid:<id>), mail conversations "
                        "and the buyer's domain.",
         "template": INQUIRY_TEMPLATE},
        {"name": "company", "prefix": "companies/",
         "description": "One per external buyer or supplier: contacts, preferences, terms "
                        "and the list of their inquiries. Link domain:<their domain>.",
         "template": COMPANY_TEMPLATE},
        {"name": "conversation", "prefix": "conversations/",
         "description": "One per customer email conversation: a summary of what was asked "
                        "and agreed, never a copy of the emails. Link "
                        "mail:<mailbox_id>:<conversation_id> (or thread:<key> for intake mail).",
         "template": CONVERSATION_TEMPLATE},
    ],
}
