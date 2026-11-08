# Mail intake: keep everything, wake less (plan, not built)

Written 2026-11-08 from the owner review. Status: **planned, not started.**

## Why

Connected mailboxes are Crontal's main view of what is happening in a business
(besides users chatting with the agent). The owner's rule: **never drop an email
or a source that may hold useful information.** At the same time, every
incoming email currently starts a full agent run, which is expensive.

Measured on `bss` (14 days to 2026-11-08):
- Total spend $42 over 574 runs. Mail was about $17 over 160 runs, roughly
  $0.10 a run. The usage report only itemizes the top 50 threads, so mail's
  real share is probably higher.
- A run sends about 100k input tokens (persona, full tool list, memory), even
  for junk.
- 229 stored emails woke the agent, across 68 distinct conversations. By sender:
  - 124 (54%) were from the merchant's own staff (`@yeup-tube.com`): CCs,
    staff replies to customers, insurance notices.
  - About 35 (15%) were junk: marketing, scams, the mail server's spam notices.
  - About 70 were real external business mail.
- The same conversation runs once per mailbox. For example, the Stirlings
  booking S2605484513 ran 14 + 4 + 4 times across `sales@`, `lisa.lee@` and
  `ReginaJ@`. All three mailboxes are in `full` mode.

## How it works today (integrations/mailbox)

- **Polling:** IMAP every 120 s, **INBOX only**, at most 100 messages per poll.
  On connect, the last 14 days are backfilled; backfilled mail never wakes the
  agent.
- **Stored for every message** (`mail_messages`): headers, sender and
  recipients, threading ids, the plain-text body (up to 200k characters), the
  attachment list (metadata only) and the size. Over 25 MB, only the headers
  are stored.
- **Wake:** an email wakes the agent if it is live (not backfill), incoming,
  the mailbox's `wake_mode` is triage or full, and the mailbox is under 30
  wakes an hour. Only then are the body and **attachment files** copied into
  the thread workspace (S3).
- Attachments of emails that did not wake the agent are fetched from IMAP on
  demand (`fetch_raw`).

## Gaps

1. Attachments of emails that did not wake the agent depend on the mail server
   keeping them. If staff delete the mail, or the server's retention removes
   it, the attachment is lost.
2. Sent folders are not read. Staff's outgoing mail is only seen when it is
   addressed or CC'd to another connected mailbox.
3. No raw `.eml` is kept, so HTML, inline images and full headers can't be
   recovered.
4. Each mailbox keeps its own copy and wakes separately, so the same email in
   three mailboxes costs three runs.
5. The spam folder is never read. That is fine, except for mail the server
   misfiles there.

## Plan, in order

A false positive must only ever **delay** handling, never lose data. Steps 1
and 2 come first, before anything stops mail from waking the agent.

1. **Store everything at ingest, whether or not it wakes the agent:** the raw
   `.eml` plus attachment files, in the instance's file store (S3), referenced
   from `mail_messages`. Nothing then depends on the mail server.
2. **Read Sent folders, stored only.** The agent can read and search them, but
   they never wake it, so staff's outgoing quotes and commitments become
   visible.
3. **One run per conversation per instance.** Group the same Message-ID, or
   the same conversation, across an instance's mailboxes into one thread. Add a
   short debounce (a few minutes) so a burst of replies is handled in one run.
   The run still sees every message from every mailbox. No false positives;
   only a few minutes of delay.
4. **Digest tier.** Mail that a filter (step 6) would not wake goes into a
   periodic digest run: every few hours or daily, over a compact list of
   sender, subject and snippet. The agent opens what matters, updates memory
   and notifies. One digest run costs about one normal run instead of N.
5. **A slimmer prompt for mail runs:** a smaller tool list, and memory loaded
   by relevance (see `memory-kinds.md`). This makes every run cheaper,
   including the useful ones.
6. **Filters, in shadow mode first (1–2 weeks).** Each filter records what it
   *would* route to the digest, while mail still wakes the agent as today. The
   results are reviewed with the owner, the filters are tuned, and only then
   do they route mail to the digest. They never discard mail.
   - **Block or allow senders and domains, per instance,** with a "Block
     sender" action on Inbox threads in Crontal. Very low risk, because people
     set it.
   - **Header rules:** `List-Unsubscribe`/`List-Id`, `Precedence: bulk/list/junk`,
     `Auto-Submitted`, the server's spam flags, mailer-daemon and
     spam-notification senders. **Medium false-positive risk:** carrier
     bookings (DHL, Rohlig), ERP/PO notifications, portals and CRM-sent sales
     mail carry these headers.
   - **A cheap screening model** for unknown senders: sender, subject and the
     start of the body, about 1–2k tokens, biased toward waking. **Medium
     risk:** a first contact from a real buyer looks like cold mail (e.g.
     amardeepsteel.com "RFQ FOR SS 410 SMLS PIPE").
7. **Optional: a daily spend cap for background runs.** Only together with the
   digest, so mail over the cap is delayed, not lost.

**Rejected:** skipping mail from the merchant's own domain. Staff mail carries
the real business state (replies, prices, commitments, shipping
instructions). Steps 3 and 5 cut its cost instead.

**Open decisions for the owner:**
- Whether mailboxes other than the main sales one should be `triage` rather
  than `full`.
- The digest frequency.
- Which screening model to use.
