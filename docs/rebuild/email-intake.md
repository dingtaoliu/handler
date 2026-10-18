# Email intake (contact@crontal.ai → Crontal objects)

Status: deployed to main. Updated 2026-10-18: blocklist-based sender policy.

## Brief

Crontal's first client forwards its business email to `contact@crontal.ai`. That
domain uses Cloudflare Email Routing (MX `route*.mx.cloudflare.net`), which today
forwards to `crontal.contacts@gmail.com`. The Crontal admin Handler instance (Heroku
`crontal-agent`) should react to each email:
- parse it;
- create the matching objects in Crontal (RFQs, demand listings, bids, supply
  listings, …) under the admin merchant, acting as the admin user.

The owner then checks the results in Crontal as admin. This is a test setup:
per-sender user mapping comes later. Neither the Gmail API nor the Crontal backend
changes.

```
sender → contact@crontal.ai → Cloudflare Email Worker ─┬─ message.forward(crontal.contacts@gmail.com)
                                                        └─ POST raw RFC 822 + HMAC → crontal-agent /ingest/email
                                                              → email.received event → email thread → agent
                                                              → crontal_api_* as email.act_as
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
| Sender gate | Settings `email.blocked_senders` and `email.require_auth`. An empty blocklist accepts every authenticated sender. Failing mail becomes an `ignored` event with `ignored_reason`, shown in Activity → *Ignored email*. It never creates a thread or files, and never reaches the agent |
| Auth verdict | Handler verifies DKIM itself on the raw bytes (`mail/dkim.py`), because Cloudflare doesn't hand the Worker a trustworthy verdict (findings below). A pass needs a valid signature whose `d=` aligns with the sender |
| Thread type | `email`: event `email.received`, key `email:<root id>`. The root is the first id of `References`, else `In-Reply-To`, else the message's own Message-ID. Background lane, visible to all users (it isn't a `chat:` key) |
| Acting user | Setting `email.act_as` (a handler user id; on the Crontal instance that's the Crontal user id). The run's `acting_as` reads the setting live, falling back to the thread owner set at ingest. `crontal_api_*` then mint that user's JWT (`POST /api/v1/agent-access/token` with `X-Crontal-User`), the same path chat uses with its owner. The user must exist and be enabled, the same rule as `X-Handler-User`. If not, mail is ignored with `act_as_not_configured` |
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
when read; they are never converted into blocked addresses. Other saved settings
and instructions are preserved.

With `require_auth`, a DKIM signature must also verify:
- algorithm `rsa-sha256` (key ≥1024 bits) or `ed25519-sha256`;
- From must be signed;
- `x=` not expired.

Its `d=` must align with the identity's domain: the same domain, or a parent with at
least two labels that isn't a shared suffix like `com.tw`/`co.uk`. Keys are fetched
over DNS-over-HTTPS (Cloudflare, then Google); a resolver failure counts as a fail.

The verifier is tested against fixtures signed by dkimpy, an independent
implementation: relaxed, simple and ed25519 (`tests/runtime/fixtures/email/make_fixtures.py`).

Ignore reasons:
- `sender_blocked`;
- `invalid_sender`;
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
| | `require_auth` | `true` (default) |
| | `act_as` | Crontal admin user id (must exist as a handler user, as for chat) |
| | `instructions` | Default prompt; editable |
| Handler settings `threads.email` | `harness`/`model`/`reasoning_effort` | As for other types (inherits `crontal`'s on existing instances) |

Grants: the agent's writes go through `crontal_api_call` (`write_external`), which
is already `allow` on the Heroku instance.

## Runbook

1. **Handler.** Deploy this branch to `crontal-agent`, then
   `heroku config:set HANDLER_EMAIL_SECRET=<secret> -a crontal-agent`.
2. **Settings** (Handler UI → Settings → `email`, or
   `PUT /api/settings/email` with the admin key):
   - `blocked_senders`: unwanted addresses or domains (empty initially);
   - `act_as`: the admin's user id;
   - `require_auth`: true.

   Check Settings → Threads → *Email intake* for the harness (`openai_agents` on
   Heroku).
3. **Worker.** Follow `deploy/cloudflare-email-worker/README.md`: set
   `HANDLER_INGEST_URL`, `wrangler secret put HANDLER_EMAIL_SECRET`,
   `wrangler deploy`, then switch the `contact@crontal.ai` routing rule to *Send to
   a Worker*.
4. **Smoke test.** Forward a test RFQ from a non-blocked, DKIM-authenticated address. Expect:
   - `wrangler tail` shows `handler accepted the email` 202;
   - Handler Activity shows an `email:<id>` run;
   - the Gmail copy arrives as before.

   From a blocked address, expect a 200 `ignored` row in Activity → *Ignored
   email*.
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
| `src/handler/runtime/mail/intake.py` | Allowlist, alignment, act_as, file saving, payload, turn rendering |
| `src/handler/runtime/mail/instructions.py` | Default email instructions |
| `settings.py`, `instructions.py`, `types.py`, `run.py` | `email` settings section, thread type, prompt, live act_as, turn |
| `workspaces.py` | `WorkspaceSync.put_file` (durable write without a local copy) |
| `store/*` | `list_events(source, status, limit)`; `GET /api/events` in `web/app.py` |
| `deploy/cloudflare-email-worker/` | Worker, `wrangler.toml`, Node tests, README |
| `tests/runtime/email/`, `tests/runtime/fixtures/email/` | Tests and dkimpy-signed fixtures |

## Later

- Per-sender user mapping, replacing the single `act_as`.
- Reprocessing an ignored email from Activity. That needs its raw bytes kept
  (bucket, with retention).
- An SRS decode for auto-forwarded envelopes. An ARC chain check if Cloudflare ever
  exposes its own seal to Workers.
