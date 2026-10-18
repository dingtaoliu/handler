# Cloudflare Email Worker: contact@crontal.ai → Gmail + handler

```
sender → contact@crontal.ai (Cloudflare Email Routing MX)
       → this Worker ─┬─ message.forward(FORWARD_TO)          (Gmail copy, as today)
                      └─ POST raw RFC 822 + HMAC → crontal-agent /ingest/email
```

The Worker forwards every message to the Gmail destination first, then posts the
raw bytes to handler. It never rejects or bounces mail. If the POST fails, the
failure is logged and the Gmail copy remains. Design and runbook:
`docs/rebuild/email-intake.md`.

## What it sends

`POST $HANDLER_INGEST_URL`, body = `message.raw`, unchanged. Headers:

| Header | Value |
|---|---|
| `X-Handler-Timestamp` | Unix seconds. Handler rejects requests more than 5 minutes off |
| `X-Handler-Signature` | `v1=` + hex HMAC-SHA256(`HANDLER_EMAIL_SECRET`, `"<timestamp>." + body`) |
| `X-Handler-Envelope-From` / `-To` | Cloudflare's SMTP envelope (`message.from` / `message.to`), URI-encoded |
| `X-Handler-Auth-Results` | Base64 JSON of the `Authentication-Results`, `ARC-Authentication-Results` and `Received-SPF` headers the Worker sees. Informational only (see below) |
| `X-Handler-Forwarded` | `ok`, or `failed: …` if the Gmail forward failed |

## SPF/DKIM: what Cloudflare gives us

- **Cloudflare filters at its MX before the Worker runs.** Since 2025-07-03, Email
  Routing only accepts mail that passes SPF or DKIM. It also rejects mail that fails
  the sender domain's DMARC policy (`p=reject`/`quarantine`).
- **The Worker gets no trustworthy verdict.** The runtime API (`from`, `to`,
  `headers`, `raw`, `rawSize`, `forward`, `reply`, `setReject`) has no auth result.
  In reports, Email Routing → Worker deliveries carry no `Authentication-Results`
  from Cloudflare (workerd issue #6740, open since May 2026), and the only ARC header
  present is the sender's own. Any such header could also have been written by the
  sender.
- **So handler verifies DKIM itself, on the raw bytes.** It requires a valid
  signature whose `d=` aligns with the allowlisted sender. The Worker passes the
  bytes through unchanged, so the forwarder's signature still verifies. Cloudflare's
  own acceptance is a second, weaker layer: some SPF or DKIM passed, but not
  necessarily for the From domain.

`forward()` can only add `X-` headers, and only to the Gmail copy. This Worker adds
none.

## Deploy

Prerequisites: a Cloudflare account with access to the `crontal.ai` zone, and Node 20+.

```bash
cd deploy/cloudflare-email-worker
npm test                                  # local checks (node --test)
npx wrangler login

# 1. Point HANDLER_INGEST_URL in wrangler.toml at the Heroku app:
#    heroku apps:info -a crontal-agent  → Web URL + "ingest/email"
#    FORWARD_TO defaults to crontal.contacts@gmail.com (the current destination).

# 2. Shared secret (the same value goes to Heroku as HANDLER_EMAIL_SECRET):
openssl rand -hex 32                      # generate once
npx wrangler secret put HANDLER_EMAIL_SECRET

# 3. Deploy the Worker:
npx wrangler deploy
```

`FORWARD_TO` must be a **verified destination address** in the account (Email
Routing → Destination addresses). `crontal.contacts@gmail.com` probably already is,
because routing to it works today. If it isn't, `forward()` fails, the failure is
logged, and the Worker still posts to handler.

Then bind the address to the Worker in the dashboard:

1. Cloudflare dashboard → `crontal.ai` → **Email** → **Email Routing** → **Routing rules**.
2. Edit the custom address `contact@crontal.ai`. Change the action from
   *Send to an email* (Gmail) to **Send to a Worker**, then pick `crontal-email-intake`
   and save. The Worker now does the Gmail forward itself.
3. Leave the catch-all rule and the MX/SPF records as they are.

Set the same secret on handler (`heroku config:set HANDLER_EMAIL_SECRET=… -a crontal-agent`)
**before** switching the rule. Otherwise handler answers 404, and only the Gmail copy
is made (logged, nothing lost).

## Check and roll back

- `npx wrangler tail crontal-email-intake` shows each message: `handler accepted the email`
  with status 202 (new) or 200 (duplicate/ignored, with the reason in the body), or the
  error.
- Roll back by switching the routing rule's action back to *Send to an email →
  crontal.contacts@gmail.com*. The Worker can stay deployed.
