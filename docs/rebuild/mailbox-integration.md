# Mailbox integration (IMAP/SMTP)

Built 2026-10-25 on `feat/mailbox-integration`. It connects any number of ordinary, password-authenticated mailboxes to one Handler instance. IMAP is used for ingress and SMTP for egress. The agent monitors business email and helps with it: it triages, summarises, notifies and drafts. It sends only after a human approves. Gmail keeps its OAuth connector (`gmail-connector.md`); this integration is for everything else.

The first real mailbox is `sales@yeup-tube.com`, hosted on chengmail.cn (Coremail):
- IMAP: `pop.chengmail.cn`, either 993 with TLS or 143 with STARTTLS.
- SMTP: `smtp.chengmail.cn`, either 465 with TLS or 587 with STARTTLS.
- The server advertises IMAP4rev1, UIDPLUS and AUTH=PLAIN, and has **no IDLE**.

## 1. Design

```
Crontal  ──/api/v1/handler/connections/mailboxes──▶  Handler /api/mailboxes  (admin API)
                                                       │
                     mail_accounts (+ encrypted password in `secrets`)
                                                       │
   MailboxPoller (1 task per instance) ──imaplib/TLS──▶ IMAP  (poll every N s, UIDs)
        │ new message
        ▼
   MailboxService.ingest ── parse (GB18030/RFC 2047) ── mail_messages / mail_refs / mail_conversations
        │ inbound, live, wake mode triage|full
        ▼
   Event mail.received → thread mail:<mailbox_id>:<conversation_id> → agent run (dispatcher limits)
        │
   mail_* tools (gateway: grants, audit, approvals) ──smtplib/TLS──▶ SMTP (+ APPEND to Sent/Drafts)
```

Code: `src/handler/runtime/integrations/mailbox/`.

| File | Role |
|---|---|
| `__init__.py` | `MailboxIntegration`: thread type, routers, tools, poller, UI entries, prompt hints |
| `config.py` | `Mailbox` record, API input models (`MailboxCreate`/`Update`/`Test`), provider presets |
| `transport.py` | `MailTransport` interface and `StdlibTransport` (imaplib + smtplib, TLS only), modified UTF-7 folder names, error messages without secrets |
| `message.py` | Raw message → `MailMessage`: charset fallbacks, RFC 2047/2231, 8-bit headers, attachments |
| `service.py` | Sync algorithm, ingest, threading, wake rules, send, draft copies, connection test |
| `poller.py` | Per-instance scheduler, process-wide IMAP concurrency, backoff, login-failure notice |
| `store.py` | `mail_*` tables on the instance's own store (SQLite or Postgres), created on first use |
| `tools.py` | `mail_*` agent tools |
| `api.py` | `/api/mailboxes` admin API |
| `events.py` | Turn text for `mail.received` and the `mail` thread guidance |

### 1.1 Sync

- **Polling.** The server has no IDLE, so Handler polls each enabled mailbox every `poll_seconds` (default 120, minimum 30). Each poll opens one session: login, `EXAMINE` (read-only) each folder, `UID SEARCH`, and then `UID FETCH BODY.PEEK[]` one message at a time. Handler never sets flags, so nothing is marked as read.
- **State per folder.** Each folder keeps `(UIDVALIDITY, last_uid, backfill_until_uid, backfill_since, wake_after)`.
  - The first time a folder is seen, `backfill_until_uid` is set to `UIDNEXT-1`. UIDs at or below it are backfill. Only messages with `SINCE <today − backfill_days>` are fetched, and they never wake the agent. Higher UIDs are live mail.
  - **UIDVALIDITY changes** reset the folder to the new numbering and backfill again. Messages already stored (matched by Message-ID) only get their folder, UIDVALIDITY and UID updated, so nothing is duplicated. Unknown messages whose INTERNALDATE is after the last successful sync still wake the agent, so mail that arrived during the renumbering is not missed.
- **Bounded work.** A poll handles at most 100 messages, oldest first. If more are waiting, the next poll comes 5 s later instead of after the full interval. Messages larger than `HANDLER_MAIL_MAX_MESSAGE_BYTES` (25 MB) are stored with headers only, and the agent can still fetch their parts on demand.
- **Concurrency.** All instances on the dyno share one semaphore (`HANDLER_MAIL_CONCURRENCY`, default 2), so at most two IMAP sessions run at once in the whole process. Blocking imaplib/smtplib calls run in worker threads (`asyncio.to_thread`).
- **Failures.** A failed poll records `last_error` and backs off: interval × 2^failures, capped at 1 h. A login failure sets the status to `auth_failed` and notifies the mailbox's users **once**. The notice goes through the normal notifier, so it reaches Crontal through the sink. Saving the mailbox (for example with a new password) clears the backoff.

### 1.2 Parsing

- Every GB label (`gb2312`, `gbk`, `cp936`, …) is decoded as **GB18030**. Chinese clients routinely label GBK text as gb2312, and strict gb2312 fails on it.
- If decoding still fails, the fallback chain is: declared charset → UTF-8 → GB18030 → Big5-HKSCS → replacement characters. A bad label never loses a message.
- Headers:
  - RFC 2047 encoded words are decoded, in any charset.
  - Raw 8-bit header bytes with no declared charset are decoded with the same chain.
  - Filenames are decoded whether they use RFC 2231 or (non-standard) RFC 2047.
- Bodies: `text/plain` is preferred; HTML is converted with html2text. The stored body is capped at 200k characters.
- Attachments are saved under their **Unicode filenames**, cleaned by the shared `uploads.sanitize_filename` (the same function `documents`, Gmail and Crontal downloads use). They go into the conversation thread's workspace at `mail/<mailbox_id>/<date>-<hash>/attachments/<name>`, and only for messages that wake the agent. Other messages keep only attachment metadata; `mail_get_attachment` fetches a part from the server when it is asked for. This keeps storage and the dyno's memory small.

### 1.3 Threading

1. **References.** The ids are Message-ID, In-Reply-To and References, looked up in `mail_refs`. Every message adds its own id and its parents' ids, so grouping works in any arrival order. A child whose parent was never seen still groups with its siblings through the shared root id.
2. **Subject fallback.** This step applies only to reply-looking subjects (`Re:`, `回复:`, `转发:` …) or to messages whose parents we never saw. Handler compares the normalised subject with conversations from the last 60 days that share a participant other than the mailbox itself. Two customers who both write "Re: 报价" stay in separate conversations.
3. **New conversation.** Otherwise the message starts a new conversation, `c` + sha256(mailbox, root id).

Each conversation is a Handler thread `mail:<mailbox_id>:<conversation_id>` of thread type **`mail`**. It is a built-in `ThreadType` value, so stored threads always parse. It runs in the background lane. Its harness, model and effort come from Settings → Threads → *Mailboxes*. On existing instances, that entry starts as a copy of the `email` or `crontal` entry. Runs act as the mailbox's `owner_user_id`: by default, the Crontal user who added the mailbox.

### 1.4 What wakes the agent

The **wake mode** is set per mailbox:

| Mode | Behaviour |
|---|---|
| `off` | Mail is stored and searchable (`mail_search`). There is no run. |
| `triage` (default) | Each new inbound message is one `mail.received` event on its thread. The agent decides whether to notify the mailbox's users, summarise or draft a reply. It is told **not** to call `mail_send`. |
| `full` | Same as triage, but the agent may also request sends. Each send still waits for a human's approval. |

The agent is never woken for:
- backfill;
- mail sent from the mailbox's own address;
- anything in the detected Sent folder;
- messages sent through `mail_send` (they are recorded outbound right away, so the later copy is a duplicate).

There is also a flood guard, `HANDLER_MAIL_MAX_WAKES_PER_HOUR` (default 30 per mailbox). Mail over the limit is stored without waking. Runs go through the normal dispatcher, so `HANDLER_MAX_CONCURRENT` and the background-lane capacity apply. Several emails in one conversation that arrive before the run starts are handled in that one run.

### 1.5 Tools

The tools go through the gateway, so they work on every harness. Their shapes mirror the `gmail_*` tools.

| Tool | Action | Risk → default |
|---|---|---|
| `mail_list_mailboxes` | `mail.list_mailboxes` | read → allow |
| `mail_search(mailbox_id?, folder?, from?, to?, subject?, text?, after?, before?, direction?, conversation_id?, max_results, offset)` | `mail.search` | read → allow |
| `mail_read(message_id)` | `mail.read` | read → allow |
| `mail_read_thread(conversation_id \| message_id)` | `mail.read_thread` | read → allow |
| `mail_get_attachment(message_id, attachment)` | `mail.get_attachment` | read → allow |
| `mail_draft_reply(reply_to_message_id? \| to+subject, body, cc?, bcc?, attachments?)` | `mail.draft_reply` | write_internal → allow |
| `mail_send(draft_id \| same fields)` | `mail.send` | write_external → **ask, exact rule only** |

Notes on the tools:
- `mail_read` and `mail_read_thread` return the body (cut at 20k characters per message, 60k per thread, with the full text saved to the workspace) and the attachments with their index, name, type and size.
- `mail_get_attachment` saves the file under `mail/<mailbox>/<message>/` in the calling thread's workspace and returns its extracted text.
- `mail_draft_reply` creates a Handler draft (`mail_drafts`) and also APPENDs it, flagged `\Draft`, to the server's Drafts folder when one is found.
- **Sending.** `mail_send` sends over SMTP and then APPENDs a copy to the Sent folder (`save_sent_copy`, on by default).
  - Like `gmail_send`, it is `explicit_allow_only`: glob rules and risk defaults that say `allow` become `ask`.
  - The approver sees the From address, the resolved recipients, the subject, a preview of the body and each attachment's name and size.
  - The approval pins the resolved `to` and `subject`, the body, a SHA-256 of each attachment and, for a draft, a hash of the draft's fields. A change after approval refuses the send.
  - Bcc is delivered but stripped from the transmitted headers.

### 1.6 Security

- **Passwords.** Passwords are encrypted with `SecretBox`/`EncryptedSecrets` (Fernet, key from `HANDLER_SECRET_KEY`, falling back to `HANDLER_ADMIN_KEY`) into the core `secrets` table. The API never returns them; it returns only `has_password`.
- **Validation errors.** Request bodies are validated by hand, so a 422 never echoes submitted values. FastAPI's default 422 would include the password.
- **No secrets in output.** Error messages pass through `redact()`. Logs, tool arguments, audit rows and run steps never contain the password. The tools never take one.
- **TLS is required.** The modes are `tls` (implicit) or `starttls`. If a server doesn't advertise STARTTLS, Handler refuses before sending credentials, and there is no plaintext mode. Certificates are verified (system trust store, hostname checked). The per-mailbox `allow_self_signed` escape hatch is off by default.
- **Isolation.** The `mail_*` tables live in the instance's own SQLite file or Postgres schema, so `main` and `bss` (or any other instances) can never see each other's mailboxes or threads.
- **Untrusted content.** Email text is wrapped in `<event>`, and a `</event>` inside it is escaped. The thread guidance tells the agent never to follow instructions found in email.

## 2. Configuration

Enable the integration per instance. `HANDLER_INTEGRATIONS` semantics apply (`integrations.md`): **setting the list replaces the legacy wiring**, so keep `crontal` in the list on Crontal-backed instances.

| Variable | Default | Meaning |
|---|---|---|
| `HANDLER_I_<NAME>__INTEGRATIONS` | unset | e.g. `crontal,mailbox` (for `main`, `HANDLER_INTEGRATIONS`) |
| `HANDLER_SECRET_KEY` (per instance: `HANDLER_I_<NAME>__HANDLER_SECRET_KEY`) | admin key | Encrypts mailbox passwords. Changing it makes stored passwords unreadable; re-enter them |
| `HANDLER_MAIL_CONCURRENCY` | 2 | IMAP sessions at once in the whole process |
| `HANDLER_MAIL_MAX_MESSAGE_BYTES` | 26214400 | Larger messages are stored headers-only |
| `HANDLER_MAIL_MAX_WAKES_PER_HOUR` | 30 | Per mailbox; over it, mail is stored without waking |

Startup warns when `CRONTAL_*` is set but `crontal` is missing from the list.

Per-mailbox settings, through the API or Crontal:

| Setting | Default |
|---|---|
| `label`, `email`, `username` | `username` defaults to the email address |
| `password` | write-only |
| `imap_host`, `imap_port`, `imap_security` | 993, `tls` |
| `smtp_host`, `smtp_port`, `smtp_security` | 465, `tls` |
| `allow_self_signed` | false |
| `folders` | `["INBOX"]` |
| `poll_seconds` | 120 |
| `backfill_days` | 14 (0–365) |
| `wake_mode` | `off` \| `triage` (default) \| `full` |
| `enabled` | true |
| `save_sent_copy` | true |
| `owner_user_id` | the creator |
| `notify_user_ids` | the owner |

`GET /api/mailboxes/presets?email=` returns host and port presets for known domains: chengmail.cn and yeup-tube.com, QQ/Foxmail/Tencent Exmail, 163/126/yeah.net, Sina, Aliyun, Gmail, Outlook/Hotmail, Yahoo and iCloud. `auth_code: true` marks providers that need an app-specific *client authorization code* (客户端授权码) instead of the web password.

### Enabling on an instance

The integration is not tied to any instance. For an **existing instance** `<name>` (e.g. `bss`), list it next to `crontal` so the legacy Crontal wiring is kept:

```
heroku config:set -a crontal-agent HANDLER_I_<NAME>__INTEGRATIONS=crontal,mailbox
```

For `main`, use `HANDLER_INTEGRATIONS=crontal,mailbox`. On `main`, email intake and Google stay on their legacy wiring regardless.

For a **new instance** (e.g. `yeup` for Crontal merchant 298), give it its own secrets and agent token:

```
ADMIN=$(openssl rand -hex 32); HOOK=$(openssl rand -hex 32); AGENT=$(openssl rand -hex 32)
heroku config:set -a crontal-agent \
  HANDLER_INSTANCES=main,bss,yeup \
  HANDLER_I_YEUP__HANDLER_ADMIN_KEY="$ADMIN" HANDLER_I_YEUP__HANDLER_WEBHOOK_SECRET="$HOOK" \
  HANDLER_I_YEUP__HANDLER_SECRET_KEY="$(openssl rand -base64 48)" \
  HANDLER_I_YEUP__CRONTAL_URL=<same URL as bss> HANDLER_I_YEUP__CRONTAL_AGENT_TOKEN="$AGENT" \
  HANDLER_I_YEUP__INTEGRATIONS=crontal,mailbox
# then register it in Crontal from a one-off dyno. The image's FLASK_APP does not
# import there, so pass FLASK_APP=wsgi:app. --name must equal the Handler instance
# name: Crontal matches notification signatures on X-Handler-Instance.
heroku run:detached -a hephaestus-backend-test \
  --env "FLASK_APP=wsgi:app;HANDLER_INSTANCE_ADMIN_KEY=$ADMIN;HANDLER_INSTANCE_WEBHOOK_SECRET=$HOOK;HANDLER_INSTANCE_AGENT_TOKEN=$AGENT" \
  -- flask handler-instance add --merchant-id 298 --name yeup \
  --base-url https://crontal-agent-940cc88dda66.herokuapp.com/i/yeup
```

This was done on 2026-10-27 (Handler v45, Crontal registry #34, AI user 364); yeup also got `HANDLER_DB_SCHEMA=handler_yeup`, `HANDLER_FILE_PREFIX=handler/yeup/`, `HANDLER_DB_POOL_MAX=3`, `HANDLER_PUBLIC_URL`, `HANDLER_RETURN_TO_ORIGINS` and bss's harness/model/effort/concurrency. Also copy any other per-instance settings bss has (for example `HANDLER_I_BSS__*` harness or model overrides); see `multi-instance.md`.

### Admin API

These routes are on the instance. Crontal proxies them as `/api/v1/handler/connections/mailboxes…`.

| Route | Who | Does |
|---|---|---|
| `GET /api/mailboxes` | any user | List, no secrets |
| `GET /api/mailboxes/presets?email=` | any user | Presets for an address |
| `POST /api/mailboxes` | admin | Create; returns 201 with the mailbox |
| `POST /api/mailboxes/test` | admin | Test unsaved settings; password required |
| `GET /api/mailboxes/{id}` | any user | One mailbox, with folder sync state and counts |
| `PATCH /api/mailboxes/{id}` | admin | Update. Omitted fields are kept; `password` replaces the stored one; `enabled:false` disables |
| `POST /api/mailboxes/{id}/test` | admin | IMAP login + LIST + EXAMINE INBOX; SMTP EHLO + STARTTLS + AUTH; nothing is sent |
| `POST /api/mailboxes/{id}/sync` | admin | Poll now |
| `DELETE /api/mailboxes/{id}` | admin | Delete the mailbox, its password and the ingested mail. Threads and runs stay |

A test returns this shape:

```
{ok, imap: {ok, error?, folders, sent_folder, drafts_folder, inbox_messages, idle, capabilities},
     smtp: {ok, error?, auth}}
```

Errors are specific: certificate not verified (with the self-signed hint), TLS version mismatch (with the port and security hint), host not found, timeout, connection refused, and login failed (with the authorization-code hint).

## 3. Operations

- **Status.** Handler's own UI shows each mailbox under **Connections** (status, last sync, last error, wake mode) with Test, Sync now and Enable/Disable. Adding mailboxes and changing passwords is done in Crontal.
- **Logs.** The log lines are `mailbox synced` (fetched/new/woke), `mail received`, `mailbox poll failed` (status, error, retry_in), `mailbox folder renumbered`, `mailbox wake limit reached`, `mailbox created/updated/deleted`. None of them contain secrets.
- **Login fails after a while.** The provider may have reset the authorization code, or IMAP was disabled in webmail. Enter a new code with **Edit** in Crontal. Saving clears the backoff and polls right away.
- **Mail volume.** One mailbox at the default interval makes 720 polls a day. Polls are cheap: login, then one SEARCH per folder.
- **Removing.** Deleting a mailbox keeps its Handler threads (history). Disabling stops polling but keeps the data.

## 4. Capacity on one Basic dyno (512 MB, several instances)

| Item | Estimate |
|---|---|
| Idle | One poller task per instance, at well under 1 MB. No IMAP connection is held between polls |
| Per poll | One TLS session plus one message in memory at a time. Typical business mail (50 KB–2 MB) adds a few MB at peak |
| Worst case | A 25 MB message parsed in memory is about 3× its size, so ~75 MB per concurrent poll. With `HANDLER_MAIL_CONCURRENCY=2` that is ~150 MB transient, process-wide. Lower `HANDLER_MAIL_MAX_MESSAGE_BYTES` (e.g. 10 MB) or the concurrency to 1 if memory is tight |
| CPU | A TLS handshake + login + SEARCH is about 50–200 ms of CPU per poll, so <0.2% of a core for a mailbox at 120 s. Parsing is milliseconds per message. A 14-day backfill of a few hundred messages finishes in a few minutes at 100 messages per poll |
| Storage | ~5–50 KB per message in Postgres (body text capped at 200k characters). Attachments go to S3 only for messages that woke the agent |
| LLM cost | The main cost is triage runs: one per inbound message, capped at 30 per mailbox per hour. Use `off` for noisy mailboxes |

## 5. Tests

`tests/runtime/integrations/mailbox/`:
- `fake_mail.py` holds three fakes:
  - an in-memory fake server and `FakeTransport` at the client-wrapper boundary;
  - small real IMAP and SMTP servers over TCP with implicit TLS or STARTTLS and a generated self-signed certificate, so the imaplib/smtplib code is exercised over the wire.
- What the tests cover:
  - UID sync;
  - UIDVALIDITY reset;
  - backfill without wake, and bounded polls;
  - threading by headers, by subject and participant, and out of order;
  - outbound and `off` never waking, and the wake rate limit;
  - GBK, GB18030 and RFC 2047/2231 decoding, raw 8-bit headers, and Unicode attachment names;
  - send requires approval, glob `allow` can't skip it, and changed content is refused;
  - drafts appended to the server;
  - plaintext refused, certificates verified;
  - secrets never returned (including in 422 responses) and encrypted at rest;
  - instance isolation;
  - `crontal,mailbox` on bss equals the legacy wiring plus the mailbox parts.
- `tests/runtime/store_pg/test_mailbox_store.py` runs the store on SQLite and Postgres.

## 6. Open items

- Run identity follows the `owner_user_id` captured when a thread is created. Changing a mailbox's owner affects only new conversations.
- There is no IMAP IDLE support. It would cut latency on servers that have IDLE, but chengmail doesn't.
- `mail_send` with `draft_id` sends the Handler draft. A draft edited by a human in webmail is not picked up.
- Integration prompt hints are registered process-wide but are conditional ("If you have mail_* tools"). Wiring `PromptFragment`s into `build_instructions` per instance is phase 3 work.
