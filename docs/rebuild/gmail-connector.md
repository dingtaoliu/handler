# Gmail connector

Built on `feat/gmail-connector` (2026-10-18). It gives the Crontal admin instance (Heroku `crontal-agent`) access to the shared mailbox `crontal.contacts@gmail.com`. That mailbox receives `contact@crontal.ai` mail, which includes the business operation emails the first client forwards. The agent can **search, read, download attachments, draft and send**. Every send waits for a human's approval (§1.1). It can **never delete or relabel**.

## 1. Design

```
harness ──MCP──▶ gateway ── gmail_* tools (gateway/gmail.py) ──httpx──▶ gmail.googleapis.com
                              │ bearer token
                              ▼
                 GoogleOAuth (gateway/google_oauth.py)
                   · connections row: connector=google, owner=user:<id>, provider=local
                   · secrets row: Fernet-encrypted refresh token (secret_ref)
                   · access token cached in memory, refreshed near expiry
```

- **Native tools, no upstream MCP server.** The tools call the Gmail REST API directly, following the `crontal_api` pattern: one `ToolProvider` registered with the gateway, which gives grants, audit and timeouts. Attachments are saved to the workspace and their text is extracted.
- **One connection per Handler user.** Interactive connections have `owner = "user:<id>"`. Gmail tools select the connection for the user who owns the run, so two users on one instance can connect different Google accounts. Operator flows with a bare admin key and system-triggered runs without a user retain the `owner = "instance"` connection for backward compatibility.
- **Credentials.** The design in §6.2 named a `SecretStore`, but none existed: `Connection.secret_ref` was a dangling reference. I added a minimal one:
  - a `secrets(key, value, updated_at)` table in both SQLite and Postgres (`CREATE TABLE IF NOT EXISTS`, so no migration step is needed);
  - `put_secret`/`get_secret`/`delete_secret` and `delete_connection` on both stores;
  - `gateway/secret_box.py`: `SecretBox` (Fernet encryption plus HMAC-signed payloads) and `EncryptedSecrets` (encrypt on write, decrypt on read).

  The store only ever holds ciphertext. `GoogleOAuth` plugs into the existing `ConnectionCredentials` as its `local` resolver. That resolver does the connection lookup, raises `NeedsReauth` for `needs_reauth` rows, and updates `last_used_at`.
- **OAuth flow** (`web/google_api.py`):
  - `POST /api/connections/google/start` returns Google's consent URL for the signed-in Handler user. The UI authenticates with a bearer header, so this is a JSON call and the browser then navigates to the URL.
    - `state` is HMAC-signed and carries a nonce, the user who started the flow, the redirect URI and the owner.
    - It expires after 10 minutes and works only once.
    - It is stateless, so it survives restarts.
  - `GET /oauth/google/callback` is public: the signed state authenticates it. It exchanges the code, stores the connection, and redirects to `/app?google=connected#connections`, or to `?google_error=…` on failure.
  - **Starting from another app (Crontal).** `start` takes an optional JSON body `{"return_to": "<url>"}`. Its origin (scheme, host, port) must be in `HANDLER_RETURN_TO_ORIGINS`, otherwise `start` answers 400; URLs with userinfo, backslashes, control characters or a non-http(s) scheme are always refused. The URL is signed into the state, and the callback checks it against the allowlist again before redirecting to it with `?google=connected` or `?google=error&reason=…`. Without `return_to`, or if the state can't be verified, the callback goes to handler's own Connections tab as before.
  - `GET /api/connections/google` returns the status for the Connections tab.
  - `DELETE /api/connections/google/{id}` revokes the signed-in user's grant at Google, then deletes the connection row and its secret. A user cannot delete another user's grant.
  - The consent URL uses `access_type=offline` and `prompt=consent`, so Google always returns a refresh token.
- **Reconnecting** replaces the old connection without revoking it. Revoking would also kill the new grant, because Google revokes per client and account.
- **Token refresh.** An access token is refreshed when it has less than 2 minutes left, or once after a Gmail 401.
  - `invalid_grant` (revoked, expired, password changed, Testing-mode expiry): the connection is marked `needs_reauth` and tools tell that user to open Handler's Connections tab and click **Reconnect Google**.
  - `invalid_client`: this is a configuration error. Tools say so, and the connection stays active.
  - A refresh token that can't be decrypted (secret key changed) is treated as `needs_reauth`.
- **Scopes.** The scopes Google actually granted are stored on the connection and shown in the Connections tab; users can untick boxes on the consent screen. Each tool checks for the scope it needs:
  - read tools accept `gmail.modify`, `gmail.readonly` or `mail.google.com`;
  - draft tools accept `gmail.modify`, `gmail.compose` or `mail.google.com`.

  If the needed scope is missing, the tool asks for a reconnect with `gmail.modify`.
- **The scope does not decide what the agent can do.** The token's scope (`gmail.modify` also allows labels) is an upper bound. The agent is limited by the **tool set and grants**. No delete, trash, label or modify tool exists, so the agent can't do those things whatever the token allows. Adding such a tool later is a code change plus a grant decision. Send tools accept `gmail.modify`, `gmail.compose`, `gmail.send` or `mail.google.com`, so the existing connection needs no new scope.

### 1.1 Sending (added 2026-10-18)

- **Tools.** `gmail_send` sends a new email or a reply (same fields as `gmail_create_draft`; workspace files as attachments; large emails use the multipart upload endpoint). `gmail_send_draft(draft_id)` sends an existing draft as it is.
- **Always approved by a human.** Both are `write_external`, whose default is `ask`, and both are marked `explicit_allow_only`: a glob rule (`gmail.*`, `*`) or a risk default that says `allow` is turned into `ask`. Only an exact rule (`{"gmail.send": "allow"}` or `{"gmail.send_draft": "allow"}`) can skip approval, and nothing sets one by default. Globs and defaults can still `deny`.
- **What the approver sees.** Before the call is parked, the tool resolves the email and writes a summary into the approval's `reason`, which the Approvals tab shows above the raw arguments: the recipients (To/Cc/Bcc, with a reply's `to` resolved from Reply-To/From), the subject (with `Re:`), the first 600 characters of the body and each attachment's name and size. For `gmail_send_draft` it reads the draft itself.
- **What runs is what was shown.** The stored arguments carry the resolved `to` and `subject`, a SHA-256 of each attachment and, for a draft, the draft's current message id. After approval, a changed attachment or an edited draft (Gmail gives an updated draft a new message id) refuses the send with "… changed after the send was approved; not sent".
- **Before parking**, the same checks as the call run: missing scope, not connected, needs reconnect, missing `to`/`subject`, attachment paths outside the workspace and drafts with no recipients all fail at once, and no approval is created.
- **Results.** A sent email returns `status: "sent"`, the message and thread ids, the recipients, the subject and a Gmail link. The prompt (`instructions.py`) and the tool descriptions tell the agent to send only when asked and never to claim an email was sent before a result says `sent`.

### Tools

| Tool | Action (grants) | Risk → default | What it does |
|---|---|---|---|
| `gmail_search(query, max_results=20, page_token?)` | `gmail.search` | read → allow | Takes Gmail search syntax. Returns id, thread_id, from, to, cc, date, subject, snippet, labels, attachment names/sizes/types and `next_page_token`. `max_results` is capped at 50 |
| `gmail_read_thread(thread_id)` | `gmail.read_thread` | read → allow | Every message: headers, a plain-text body and attachments with `attachment_id`/`part_id`. The body budget is 60k characters per thread (4k–20k per message) |
| `gmail_read_message(message_id)` | `gmail.read_message` | read → allow | One message, with a 20k-character body |
| `gmail_get_attachment(message_id, attachment_id, filename?)` | `gmail.get_attachment` | read → allow | Saves to `downloads/gmail/<message_id>/<file>` in the thread workspace and returns the extracted text (markitdown, as in crontal_api). Also accepts a `part_id`, because Gmail attachment ids change between fetches |
| `gmail_create_draft(to?, subject?, body, cc?, bcc?, reply_to_message_id?, attachments?)` | `gmail.create_draft` | write_internal → allow | Builds the MIME message. For a reply it sets `threadId`, `In-Reply-To`, `References` (the original's references plus its Message-ID) and a `Re:` subject, and `to` defaults to Reply-To or From. Attachments are workspace paths. Returns `draft_id`, `message_id`, `thread_id` and a Gmail link |
| `gmail_update_draft(draft_id, …same fields)` | `gmail.update_draft` | write_internal → allow | Fields you leave out keep their current values, existing attachments included. Passing `attachments` replaces them |
| `gmail_list_drafts(max_results=20)` | `gmail.list_drafts` | read → allow | id, to, subject, snippet and link for each draft |
| `gmail_send(to?, subject?, body, cc?, bcc?, reply_to_message_id?, attachments?)` | `gmail.send` | write_external → **ask** (exact rule only) | Sends after approval; see §1.1 |
| `gmail_send_draft(draft_id)` | `gmail.send_draft` | write_external → **ask** (exact rule only) | Sends an existing draft after approval; refused if the draft changed |

Notes on the tools:
- **Body text.** `text/plain` parts are preferred; when there are none, HTML is converted with html2text. Quoted and forwarded text is kept. A body that is cut short says so (`body_truncated`), and its full text is saved to `downloads/gmail/<id>/body.txt`.
- **Draft size.** Drafts up to 4 MB go as JSON `raw`. Larger ones (up to the ~25 MB attachment limit) go through Gmail's multipart upload endpoint, because the plain JSON endpoint rejects bodies over about 5 MB.
- **Why drafts are `write_internal`.** A draft never leaves the mailbox, so the default grants allow it with no approval step.
- **Grant rules.** `gmail.*` works as a grant rule; for example, `{"gmail.*": "deny", "gmail.search": "allow"}`.
- **Drafts are for a human to send.** Every draft tool's description says drafts wait for a human to review and send, and that a draft is not sent. Sending goes through `gmail_send`/`gmail_send_draft` and an approval (§1.1).
- **When the tools exist.** The tools are registered only when `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` are set. Before a connection exists, they return "Gmail is not connected …".

### Configuration

| Variable | Required | Meaning |
|---|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | yes | The Web OAuth client from Google Cloud |
| `GOOGLE_OAUTH_CLIENT_SECRET` | yes | That client's secret |
| `HANDLER_PUBLIC_URL` | recommended | Handler's public origin, for example `https://crontal-agent-940cc88dda66.herokuapp.com`. The redirect URI is `<HANDLER_PUBLIC_URL>/oauth/google/callback`. When unset, it is derived from the request (`X-Forwarded-Proto` + `Host`) |
| `HANDLER_SECRET_KEY` | recommended | Any long random string; it encrypts stored tokens and signs OAuth state. When unset, handler falls back to `HANDLER_ADMIN_KEY` (and logs a warning). **Changing the key** makes the stored token unreadable, so the connection shows "needs reconnect" |
| `HANDLER_RETURN_TO_ORIGINS` | for Crontal | Comma-separated origins that `start`'s `return_to` may point at, for example `https://crontal.ai` (Crontal builds `return_to` from its `PUBLIC_APP_URL`). Empty means no `return_to` is accepted |
| `GOOGLE_OAUTH_SCOPES` | no | Space-separated scopes to request. The default is `openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/gmail.modify` |

`gmail.modify` covers search, read, attachments, drafts and send. It also covers labels, which no tool exposes. It does not cover permanent delete. If the agent later needs to read Google Drive links, add `https://www.googleapis.com/auth/drive.readonly` to `GOOGLE_OAUTH_SCOPES` and to the consent screen's Data Access page, then reconnect.

## 2. Owner setup (Google Cloud, Heroku, then connect)

First deploy a build that contains this branch. Then:

### 2.1 Create the Google Cloud project

1. Go to <https://console.cloud.google.com/>. Sign in with the Google account that should own the OAuth app; `crontal.contacts@gmail.com` is the simplest choice.
2. Open the project picker (top bar) → **New project**. Name it, for example, `Crontal Agent`, then click **Create** and select the new project.

### 2.2 Enable the Gmail API

3. Go to **☰ → APIs & Services → Library**, search for **Gmail API**, open it and click **Enable**.

### 2.3 Set up the consent screen (Google Auth Platform)

4. Go to **☰ → APIs & Services → OAuth consent screen**. This opens *Google Auth Platform*. Click **Get started** and fill in:
   - **App information:**
     - App name: `Crontal Agent`;
     - User support email: `crontal.contacts@gmail.com`;
     - click **Next**.
   - **Audience:** **External** → **Next**.
   - **Contact information:** your email → **Next**.
   - Agree to the policy → **Continue** → **Create**.
5. **Data Access** (left menu) → **Add or remove scopes**. Add these three; to find a scope, filter for it or paste it under *Manually add scopes*:
   - `openid`
   - `https://www.googleapis.com/auth/userinfo.email`
   - `https://www.googleapis.com/auth/gmail.modify` (listed under *Restricted scopes*)

   Optional: `https://www.googleapis.com/auth/drive.readonly`, only if the agent should read Drive links later. If you add it, also add it to `GOOGLE_OAUTH_SCOPES`.

   Click **Update** → **Save**.
6. **Branding** (optional): app logo and links. Leave **Authorized domains** empty unless Google insists. `herokuapp.com` can't be added there because it is a public suffix. If Google ever refuses the redirect URI over this, put a custom domain (for example `agent.crontal.ai`) in front of the dyno and use it in step 9 and in `HANDLER_PUBLIC_URL`.

### 2.4 Publish to production without verification

7. **Audience** → *Publishing status: Testing* → **Publish app** → **Confirm**. The status becomes **In production**. **Do not** click *Prepare for verification* / submit for verification.
   - **Why:** in *Testing* status, Google expires refresh tokens after **7 days**, which would force a weekly reconnect. An app *In production* keeps its refresh tokens until they are revoked.
   - **The cost:** the app is unverified, so the consent screen shows a one-time **"Google hasn't verified this app"** warning. Unverified apps are also capped at 100 users in total, which is irrelevant for one shared mailbox. Restricted scopes such as `gmail.modify` work for an unverified app at this scale.
   - **Who verification is for:** it is needed only before *other people's* accounts connect to this app (see design §6.1).

### 2.5 Create the OAuth client

8. Go to **Clients** (left menu) → **Create client**:
   - **Application type:** **Web application**;
   - **Name:** `handler crontal-agent`.
9. Under **Authorized redirect URIs**, click **Add URI** and enter exactly:
   ```
   https://crontal-agent-940cc88dda66.herokuapp.com/oauth/google/callback
   ```
   Leave *Authorized JavaScript origins* empty. Click **Create**.
10. Copy the **Client ID** and **Client secret** from the dialog. Google shows the secret only here and in the client's details; you can download the JSON as a backup.

### 2.6 Set the Heroku config vars

11. Run:
    ```
    heroku config:set -a crontal-agent \
      GOOGLE_OAUTH_CLIENT_ID='<client id>' \
      GOOGLE_OAUTH_CLIENT_SECRET='<client secret>' \
      HANDLER_PUBLIC_URL='https://crontal-agent-940cc88dda66.herokuapp.com' \
      HANDLER_SECRET_KEY="$(openssl rand -base64 48)"
    ```
    The dyno restarts. The logs should list the `gmail_*` tools in the `handler-rt serving` line.

### 2.7 Connect in handler

12. Open <https://crontal-agent-940cc88dda66.herokuapp.com/login> and sign in with the admin key (or an admin user's token).
13. Go to **Connections**. The *Google (Gmail and Workspace)* card shows *Not connected* and the redirect URI, which must match step 9 exactly. Click **Connect Google**. Repeat this while signed in as each Handler user who needs a Google account.
14. Sign in as **crontal.contacts@gmail.com**. On *"Google hasn't verified this app"*, click **Advanced** → **Go to Crontal Agent (unsafe)**. On the permissions screen, **tick every checkbox**, then click **Continue**.
15. You land back on **Connections** with a "Google connected" toast. The card shows `crontal.contacts@gmail.com`, a **connected** badge and the granted scopes (they should include `…/gmail.modify`).
16. Check that it works. In Chat, ask "search Gmail for newer_than:7d and list subjects". Or open **Tools**: the `gmail_*` tools should show `allow`.

## 3. Operations

- **"needs reconnect"** means Google rejected the refresh token: the password was changed, access was revoked at <https://myaccount.google.com/permissions>, or the app was back in Testing for 7 days. It can also mean `HANDLER_SECRET_KEY` changed. Fix it with **Reconnect Google** on the Connections tab.
- **Missing permission errors** mean a scope checkbox was unticked during consent. Reconnect and tick every box.
- **Disconnect** revokes the grant at Google and deletes the connection and its encrypted token. If Google can't be reached, the toast says so; you can also remove the app at <https://myaccount.google.com/permissions>.
- **Audit.** Every tool call is audited under its `gmail.*` action, like other gateway tools.
- **Sends** appear in the Approvals tab (and the pending badge). Approving runs the send and posts the result into the thread; denying sends nothing.
- **Not built:** push or new-mail triggers (the separate email-intake feature covers inbound mail), Drive/Docs/Sheets/Slides tools, and Gmail label/delete tools. Extra Workspace scopes may be present in the grant, but only Gmail tools currently consume them.

## 4. Upstream MCP evaluation (2026-10-18, not switched)

We looked at replacing these native tools with an upstream Google MCP server (design §6.1). Per the owner's timebox, the switch is left for a follow-up and the native tools stay.

**Google's official servers** (`https://gmailmcp.googleapis.com/mcp/v1`, `https://drivemcp.googleapis.com/mcp/v1`; streamable HTTP, OAuth bearer):
- They are Developer Preview only. Using them needs the Cloud project enrolled in the Workspace Developer Preview Program, and the application asks for a **Google Workspace account**. `crontal-test` is owned by a consumer account. Our agentic client is required (enforced from 2026-11-11) and is in place.
- To set them up you would enable `gmailmcp.googleapis.com` and `drivemcp.googleapis.com` as well as the REST APIs. They use the scopes `gmail.readonly` + `gmail.compose` and `drive.readonly`/`drive.file`. No quotas or pricing are published.
- Gmail has 10 tools: search_threads, get_thread, get_message, create_draft, list_drafts, list/label/unlabel. It has **no send**, no attachment download, no draft update, and drafts can't carry attachments yet. Drive is good: `read_file_content` returns text for Docs/Sheets/Slides/PDF/Office, and `download_file_content` returns the file as inline base64.
- Not usable for us now.

**Fallback `taylorwilsdon/google_workspace_mcp`** (PyPI `workspace-mcp`, v2.1.0 from 2026-10-09; MIT; about 3.4k stars):
- This can work. In `MCP_ENABLE_OAUTH21=true EXTERNAL_OAUTH21_PROVIDER=true WORKSPACE_MCP_STATELESS_MODE=true` mode it takes the caller's `ya29.` bearer on each request, validates it against userinfo (so it needs `userinfo.email`) and writes nothing to disk. It does not run its own OAuth flow and stores no tokens. A bad token gets HTTP 401. A token Google rejects mid-call comes back as a tool error, "Authentication error occurred …". Checked locally.
- To keep sends inside handler's approval flow, run it with `--permissions gmail:drafts drive:readonly`, which drops its send tool, and `--disabled-tools` for the label and sharing tools.
- The costs:
  - It needs its own venv: it installs top-level packages named `auth`, `core`, `gmail`, `main`. That is a 187 MB venv and about **127 MB RSS** idle (measured on macOS arm64).
  - It needs a sidecar process on a 512 MB dyno.
  - Tool results are formatted text rather than JSON.
  - It has no list-drafts or update-draft tools.
  - A stateless attachment download carries no filename.
  - Draft attachments take server-local `path` or `url` values, which handler would have to replace with base64 content to avoid file exfiltration or SSRF.
- Handler would still need thin native tools for drafts, attachment saving and approval-gated send. So the gain is mostly Drive.

**Recommendation:** keep the native Gmail tools. Add Drive as small native read-only tools on the same `GoogleOAuth` connection: search/list, metadata, and export/download into the workspace with text extracted. They need `drive.readonly` plus the Drive API, and the owner reconnects. Revisit the official servers when they reach GA for consumer accounts, or the sidecar when several more Workspace services are wanted.
