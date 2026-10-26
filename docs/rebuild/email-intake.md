# Email intake (contact@crontal.ai → Crontal objects)

Status: deployed to main. Updated 2026-10-25: lockdown after the security audit (external
senders, least privilege on email/crontal threads, sender-bound thread keys).

## Brief

Crontal's first client forwards its business email to `contact@crontal.ai`. That
domain uses Cloudflare Email Routing (MX `route*.mx.cloudflare.net`), which today
forwards to `crontal.contacts@gmail.com`. The Crontal admin Handler instance (Heroku
`crontal-agent`) should react to each email:
- parse it;
- create the matching objects in Crontal (RFQs, demand listings, bids, supply
  listings, …) under the instance's merchant, acting as its AI user.

The owner then checks the results in Crontal as admin. This is a test setup:
per-sender user mapping comes later. Neither the Gmail API nor the Crontal backend
changes.

```
sender → contact@crontal.ai → Cloudflare Email Worker ─┬─ message.forward(crontal.contacts@gmail.com)
                                                        └─ POST raw RFC 822 + HMAC → crontal-agent /ingest/email
                                                              → email.received event → email thread → agent
                                                              → crontal_api_* as the instance's AI user
```

## Decisions

| Topic | Decision |
|---|---|
| Transport | Cloudflare Email Worker (`deploy/cloudflare-email-worker/`). It forwards to Gmail first, then POSTs `message.raw`. It never bounces mail: a failed POST is logged, and the Gmail copy remains the record |
| Request auth | `X-Handler-Signature: v1=<hex HMAC-SHA256(secret, "<ts>." + body)>` and `X-Handler-Timestamp`. Handler rejects a skew over 5 minutes, and a bad or missing signature, with 401. Because the timestamp is in the signed string, it can't be swapped |
| Durability | Durable before acknowledged. The `.eml` and attachments are written straight to the thread's durable workspace copy (`FileStore`, i.e. Bucketeer on Heroku). Then the event is committed. Then 2xx. Paths are derived from the Message-ID, so a retry rewrites identical files |
| Idempotency | `email:<Message-ID>`. Without a Message-ID, a sha256 of the raw bytes is used. A duplicate gets 200 `duplicate: true` |
| Size | 413 above 30 MiB (Cloudflare accepts up to 25 MiB). At most 100 attachments and 5 nested `message/rfc822` levels; anything skipped is listed in the event |
| Parsing | Stdlib `email`, `policy=default` (`mail/parse.py`). See below |
| Sender gate | Anyone may write in (email is core functionality). Settings `email.blocked_senders` and `email.require_auth`: an empty blocklist accepts every DKIM-authenticated sender. Failing mail becomes an `ignored` event with `ignored_reason`, shown in Activity → *Ignored email*. It never creates a thread or files, and never reaches the agent |
| Sender trust | Every accepted email is from an **external, untrusted party** unless its DKIM-confirmed From matches `email.trusted_senders` (marked `trusted`; it doesn't gate acceptance and grants nothing extra yet). The turn starts with a handler notice outside the `<event>` element saying so, and the default instructions tell the agent it works for the merchant, not the sender |
| Least privilege | On `email` and `crontal` threads (and `gmail`), a call that leaves the merchant, runs code, or persists beyond the thread needs approval, whatever the instance grants say (`crontal_api.*: allow` included). See *Grants on untrusted threads* below |
| Auth verdict | Handler verifies DKIM itself on the raw bytes (`mail/dkim.py`), because Cloudflare doesn't hand the Worker a trustworthy verdict (findings below). A pass needs a valid signature whose `d=` aligns with the sender |
| Thread type | `email`: event `email.received`, key `email:<scope>:<root id>`. The root is the first id of `References`, else `In-Reply-To`, else the message's own Message-ID. Those headers are written by the sender, so the key also carries the sender's scope: its DKIM-confirmed address (or the `trusted_senders` entry it matched, so a trusted `@domain` shares threads); `via:<domain>` when only the envelope sender's domain passed; `unverified:<address>` with `require_auth` off. A stranger who knows a root Message-ID gets a thread of their own. Background lane, visible to all users (it isn't a `chat:` key) |
| Acting user | In Crontal, always the instance's **AI user** ("<merchant> AI", discovered via `GET /api/v1/agent-access/identity`; `gateway/crontal_identity.py`), never a person. The optional setting `email.act_as` (an enabled handler user id) only attributes the thread on the handler side (owner, approvals' `requested_by`, failure notices); the run's `acting_as` reads it live. Mail is ignored with `act_as_not_configured` only when `act_as` is empty **and** Crontal definitively has no AI identity for the instance (or Crontal isn't configured); an unreachable Crontal still accepts the mail and the run retries |
| Harness/model | `threads.email` in Settings → Threads, like the other types. An instance whose saved threads settings predate `email` starts the type with its `crontal` entry (same harness), not the bare default |
| Instructions | Setting `email.instructions` (default in `mail/instructions.py`) replaces the one-line per-type guidance in the prompt. Saving it empty restores the default. Shown in the prompt preview as type `email` |

### Parsing details

- **Forwarder**: the top-level From, To, Cc, Date and Subject. Also recorded: the
  envelope from/to from Cloudflare.
- **Inline forwards**: a marker line followed by a `From:` + `Date:`/`Subject:`
  header block. Markers covered:
  - Gmail's `---------- Forwarded message ---------`;
  - Outlook's `-----Original Message-----`, an underscore rule, or a bare
    `From:/Sent:` block;
  - Apple Mail's `Begin forwarded message:`;
  - Chinese and Japanese variants (`轉寄的郵件`, `寄件者:`, `主旨:`, `日期:`, …).

  The text above the marker is the forwarder's note. HTML-only bodies are converted
  with `html2text` first.
- **Forward as attachment**: each `message/rfc822` part becomes an original
  (sender, date, subject, Message-ID, body) and is saved as `forwarded-N.eml`. Its
  own attachments are collected with a `forwarded-N-` prefix, recursively.
- **Files**: everything goes to `inbox/<YYYY-MM-DD>-<subject-slug>-<hash8>/` in the
  thread workspace:
  - `message.eml` (the raw bytes);
  - `body.txt`;
  - `attachments/<name>`;
  - `extracted/<name>.md`, a markitdown text version of PDFs and Office files (the
    converter `crontal_api` uses for downloads). Extraction is capped at 15 MB per
    file, 20 s each and 60 s per email.

  Filenames go through `uploads.sanitize_filename`: no paths, `[A-Za-z0-9._ -]`
  only, at most 120 characters, de-duplicated.
- **The turn** (`render_email_event`) is readable text, not JSON:
  `<event type="email.received" source="email">`, then the forwarder, received-at,
  date and subject; each original (from, date, subject, inline or attached); the
  workspace paths with type, size and text version; the sender check; and the body
  (40k characters max, with the full text in `body.txt`).

### Sender gate in detail

Identities checked, in order:
1. the header From (manual forward, or forward as attachment);
2. the envelope sender (auto-forward rules keep the original From but send from the
   client's mailbox).

A message is blocked if either its header or envelope identity matches
`blocked_senders`:
- an exact address, ignoring `+tags`;
- `@domain`, or a bare `domain`, for that exact domain.

An empty list blocks nobody. Legacy saved `allowed_senders` values are discarded
when read; they are never converted into blocked or trusted addresses. Other saved
settings and instructions are preserved.

The message must have exactly one From header with exactly one address
(`invalid_sender` otherwise): DKIM signs the bottom-most From while parsers read the
top one, so a second From could show a forged address next to someone else's valid
signature.

`trusted_senders` (same entry syntax) marks mail `trusted` only when DKIM confirms the
header From itself and it matches. Never trusted: an identity confirmed only through
the envelope sender (its mailbox isn't signed), forwarded originals (plain text in the
body), and anything accepted with `require_auth` off. The payload records
`sender_trust` (`trusted`/`external`) and `accepted_sender`.

With `require_auth`, a DKIM signature must also verify:
- algorithm `rsa-sha256` (key ≥1024 bits) or `ed25519-sha256`;
- From must be signed;
- `x=` not expired;
- an `l=` body length, if present, covering the whole body (text after `l=` bytes
  is unsigned, so anyone could append to a signed message).

Its `d=` must align with the identity's domain: the same domain, or a parent with at
least two labels that isn't a shared suffix like `com.tw`/`co.uk`. Keys are fetched
over DNS-over-HTTPS (Cloudflare, then Google); a resolver failure counts as a fail.

The verifier is tested against fixtures signed by dkimpy, an independent
implementation: relaxed, simple and ed25519 (`tests/runtime/fixtures/email/make_fixtures.py`).

Ignore reasons:
- `sender_blocked`;
- `invalid_sender` (no usable From, or more than one From header or address);
- `auth_failed` (the detail says why: no signature, body hash mismatch, not aligned,
  key lookup failed, …);
- `act_as_not_configured`.

## Cloudflare SPF/DKIM findings

- **The MX enforces authentication.** Since 2025-07-03 Email Routing only accepts
  mail that passes SPF or DKIM, and it rejects mail that fails the sender domain's
  DMARC policy. Forwarded mail gets SRS for SPF plus DKIM signatures for
  `email.cloudflare.net` and the zone; the From header isn't changed.
  ([changelog](https://developers.cloudflare.com/changelog/2025-06-30-mail-authentication),
  [postmaster](https://developers.cloudflare.com/email-routing/postmaster/))
- **The Worker gets no verdict.** The Email Worker API (`from`, `to`, `headers`,
  `raw`, `rawSize`, `canBeForwarded`, `forward(rcpt, headers?)`, `reply`,
  `setReject`) exposes no auth result, and `forward()` can only add `X-` headers.
  ([runtime API](https://developers.cloudflare.com/email-routing/email-workers/runtime-api/))
- **No usable headers either.** Reports say Worker deliveries carry no Cloudflare
  `Authentication-Results`, `Received-SPF` or `Received` headers; the only ARC
  header is the sender's own (`i=1; mx.google.com; arc=none`). See
  [workerd #6740](https://github.com/cloudflare/workerd/issues/6740), open with no
  Cloudflare response as of 2026-10. A community thread reports the opposite for
  `raw` versus `headers`. In both cases a header in the message can be written by
  the sender, unless Cloudflare is known to strip same-authserv-id headers, which
  isn't documented.
- **Consequence.** The Worker forwards those headers as `X-Handler-Auth-Results` for
  the record only. Handler trusts its own DKIM check. Cloudflare's MX acceptance
  stays a weaker outer layer, because SPF on the envelope domain may not align with
  From, and DMARC is enforced only where the domain publishes a policy.
- **What it means for the client.** Gmail and Microsoft 365 sign outgoing mail with
  the sender's domain, so manual forwards pass. A Google Workspace domain *without*
  custom DKIM is signed as `<domain>.<n>.gappssmtp.com`, which doesn't align. In
  that case, set up DKIM for the domain, or (test only) turn `email.require_auth`
  off and rely on the blocklist.

## Configuration

| Where | Name | Value |
|---|---|---|
| Heroku `crontal-agent` | `HANDLER_EMAIL_SECRET` | Shared HMAC secret (`openssl rand -hex 32`). Unset means `/ingest/email` isn't mounted (404) |
| Worker secret | `HANDLER_EMAIL_SECRET` | The same value (`wrangler secret put`) |
| Worker var | `FORWARD_TO` | `crontal.contacts@gmail.com` (verified destination) |
| Worker var | `HANDLER_INGEST_URL` | `https://<crontal-agent web URL>/ingest/email` |
| Worker var | `HANDLER_TIMEOUT_MS` | 60000 |
| Handler settings `email` | `blocked_senders` | Addresses or `@domain` to ignore; empty accepts authenticated senders |
| | `trusted_senders` | Addresses or `@domain` marked trusted when DKIM confirms the From; empty: everyone is external |
| | `require_auth` | `true` (default) |
| | `act_as` | Optional handler user id the threads are attributed to (must exist and be enabled). Crontal work always runs as the AI user |
| | `instructions` | Default prompt; editable |
| Handler settings `threads.email` | `harness`/`model`/`reasoning_effort` | As for other types (inherits `crontal`'s on existing instances) |

### Grants on untrusted threads

Email and Crontal-notification threads carry text written outside the merchant, so
`GrantPolicy` (`gateway/policy.py`) puts a floor on them: an `allow` becomes `ask` for
- a call that sends the merchant's data outside it. `crontal_api_call` is classified
  per call (`outward_call` in `gateway/crontal_api.py`): writes count as inside only
  under `/rfq`, `/demand_listing`, `/bid`, `/contact`, `/files`,
  `/operations`, `/notification`, `/inventory`, `/products`, and even there these are
  outward: RFQ `status` other than draft (publish, close), `/rfq/<id>/share-email`,
  `/bid/<id>/lock` (submit), `/bid/token/<t>/revisions`, `/bid/<id>/respond`, and any
  path with share/invite/publish/submit/send/email/message/public/token. Everything
  else (supply listings, which go live on the marketplace when created; chat, message,
  merchant incl. invites, card, auth, agent, new areas) is
  outward. The curated `crontal_*` tools: `publish_rfq`, a non-draft status, and
  `define_shape` are outward;
- `gmail_send`/`gmail_send_draft` and any other `write_external` tool without a
  classifier, and every `exec` tool;
- `write_internal` tools that persist beyond the thread (memory, automations, and any
  future one), except `thread_set_title`, `notify` and Gmail drafts.

Reads, and writes that stay inside the merchant (draft RFQs and their listings, draft
bids, contacts, uploads), keep the instance rules, so with
`crontal_api.*: allow` they run unasked. `deny` stays `deny`. Chat threads are
unchanged.

Hidden Crontal routes (`_HIDDEN`, refused by every `crontal_api_*` tool and left out of
`crontal_api_routes`): `/api/v1/agent-access`, `/agent-mcp`, `/handler`, `/admin`,
`/marketplace/query`, `/files/stats`. Paths must be plain (no `%`-escapes, `//`, `.`
or `..` segments, or backslashes), so no spelling of them reaches the server.

## Runbook

1. **Handler.** Deploy this branch to `crontal-agent`, then
   `heroku config:set HANDLER_EMAIL_SECRET=<secret> -a crontal-agent`.
2. **Settings** (Handler UI → Settings → `email`, or
   `PUT /api/settings/email` with the admin key):
   - `blocked_senders`: unwanted addresses or domains (empty initially);
   - `trusted_senders`: optional (empty: every sender is external);
   - `act_as`: optional, the handler user email threads are attributed to;
   - `require_auth`: true;
   - `instructions`: `""` to use the current default (a saved copy of an older
     default stays in force otherwise).

   Check Settings → Threads → *Email intake* for the harness (`openai_agents` on
   Heroku).
3. **Worker.** Follow `deploy/cloudflare-email-worker/README.md`: set
   `HANDLER_INGEST_URL`, `wrangler secret put HANDLER_EMAIL_SECRET`,
   `wrangler deploy`, then switch the `contact@crontal.ai` routing rule to *Send to
   a Worker*.
4. **Smoke test.** Send a test RFQ from a non-blocked, DKIM-authenticated address. Expect:
   - `wrangler tail` shows `handler accepted the email` 202;
   - Handler Activity shows an `email:<id>` run;
   - the Gmail copy arrives as before.

   From a blocked address, expect a 200 `ignored` row in Activity → *Ignored
   email*. Ask the email to publish the RFQ: the publish call must show up in
   Approvals instead of running.
5. **Rollback.** Switch the routing rule back to *Send to an email →
   crontal.contacts@gmail.com*.

Operational notes:
- An ignored message keeps its Message-ID. After fixing settings, forward it again
  (a new forward has a new Message-ID).
- Raw bodies of ignored mail aren't stored in handler; the Gmail copy has them.

## Code

| File | What |
|---|---|
| `src/handler/runtime/web/email_ingest.py` | `/ingest/email`, signature, gate, durable write, event |
| `src/handler/runtime/mail/parse.py` | MIME walk, inline and attachment forwards, folder name |
| `src/handler/runtime/mail/dkim.py` | DKIM verifier and DNS-over-HTTPS TXT resolver |
| `src/handler/runtime/mail/intake.py` | Sender gate and trust, alignment, thread scope, act_as, file saving, payload, turn rendering |
| `src/handler/runtime/gateway/policy.py`, `gateway/crontal_api.py` | Floor on untrusted threads; outward classification and hidden routes |
| `src/handler/runtime/mail/instructions.py` | Default email instructions |
| `settings.py`, `instructions.py`, `types.py`, `run.py` | `email` settings section, thread type, prompt, live act_as, turn |
| `workspaces.py` | `WorkspaceSync.put_file` (durable write without a local copy) |
| `store/*` | `list_events(source, status, limit)`; `GET /api/events` in `web/app.py` |
| `deploy/cloudflare-email-worker/` | Worker, `wrangler.toml`, Node tests, README |
| `tests/runtime/email/`, `tests/runtime/fixtures/email/` | Tests and dkimpy-signed fixtures |

## Later

- Per-sender attribution (on-behalf-of) for email threads.
- Reprocessing an ignored email from Activity. That needs its raw bytes kept
  (bucket, with retention).
- An SRS decode for auto-forwarded envelopes. An ARC chain check if Cloudflare ever
  exposes its own seal to Workers.
