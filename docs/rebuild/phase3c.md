# Rebuild — Phase 3c: Crontal integration

Goal: Crontal's site admins (the premium pilot) use handler as their agent from inside Crontal. Handler can read and act on their Crontal data through MCP tools, Crontal notifications trigger handler runs, and admins see handler's settings and approvals in a Crontal settings tab. Design: `docs/agent-runtime-design.md` §12 (applications) and §6.2 (credentials). Rules from phases 1–3 apply.

Repos:
- **handler**: `/Users/dannyliu/dev/repos/handler`, branch `rebuild`.
- **Crontal**: `/Users/dannyliu/dev/repos/hephaestus` (Flask backend + React frontend). **Work only in the worktree `/Users/dannyliu/dev/repos/hephaestus-wt/handler-integration`** (branch `handler-integration`). The main hephaestus checkout has the owner's uncommitted work and must not be touched.

## Pilot decisions

- **Identity**: a Crontal user maps to the handler user with id `str(user.id)`. Crontal provisions the handler user lazily on first use, with role `admin` for site admins.
- **Authority**: the agent acts **as the Crontal user it works for**, with that user's own permissions (their merchant). It gets no instance-wide Crontal powers.
- **Entitlement**: `handler_enabled(user) = is_site_admin_user(user)` for now (`services/auth.py`), behind `HANDLER_ENABLED=true`. Everyone else keeps Crontal's built-in agent, unchanged.
- **Shared secrets** (env on both sides):
  - `HANDLER_URL` + `HANDLER_ADMIN_KEY`: Crontal → handler API, acting for a user via `X-Handler-User`.
  - `CRONTAL_AGENT_TOKEN`: handler → Crontal MCP, with `X-Crontal-User: <id>`.
  - `HANDLER_WEBHOOK_SECRET`: HMAC for Crontal → handler events.

  The broker/short-lived tokens of design §6.2 come later.
- **Crontal thread owner**: the notification's recipient. A notification fanned out to several users of a merchant becomes several events on the same entity thread, which the dispatcher coalesces into one run (the owner is the first recipient). Good enough for the pilot.

---

## P. Handler side (driver)

**Files** (handler repo):
- `runtime/types.py`: add `ThreadType.CRONTAL = "crontal"`. This is a deliberate contract change; update `settings.py` `ThreadsSettings` and the per-type guidance in `instructions.py`.
- `runtime/web/ingest.py`: the webhook route.
- `runtime/gateway/crontal.py`: credentials and tool curation.
- `runtime/server.py`: wiring.
- `tests/runtime/crontal/`.

- `POST /ingest/crontal` (not under `/api`, no bearer auth):
  - **Signature**: verify `X-Crontal-Signature: t=<unix>,v1=<hex hmac-sha256(secret, f"{t}.{raw_body}")>` in constant time, rejecting timestamps older than 5 minutes.
  - **Body**: `{id, type, recipient_user_id, entity_type, entity_id, data, created_at}`.
  - **Event**: `type="crontal.notification"`, `idempotency_key=f"crontal:{id}"`, `user_id=str(recipient_user_id)`, `thread_key=f"crontal:{entity_type}:{entity_id}"` (or `crontal:notification:{id}` when there's no entity).
  - **Thread**: `ThreadSpec(CRONTAL, BACKGROUND, owner_user_id=str(recipient), harness from threads.crontal settings)`.
  - **Response**: 202 when new, 200 for a duplicate, 401 for a bad signature.
- `CrontalCredentials(url, agent_token)` (a `CredentialProvider`): for owner `user:<id>`, return the MCP url with headers `Authorization: Bearer <agent_token>` and `X-Crontal-User: <id>`. Instance-level (no user) → `None`, because the agent has no Crontal identity of its own.
- **Curated tools** (from Crontal's MCP `list_tools`):
  - reads → `read`;
  - creating or changing RFQs, demand listings, bids → `write_external` (ask by default);
  - file read/OCR → `read`.

  Only tools that exist upstream are exposed. Startup fails clearly if Crontal is unreachable while configured.
- **Server**: `--crontal-url` / `CRONTAL_URL`, `CRONTAL_AGENT_TOKEN`, `HANDLER_WEBHOOK_SECRET`. The Crontal connector is registered only when configured. The Codex env allowlist already keeps these secrets out of the agent's commands.
- **Tests**: signature valid/invalid/stale, idempotency, thread routing and coalescing, credentials per owner, and a fake Crontal MCP server (reuse `tests/runtime/upstream` patterns) exercised through the gateway.

## Q. Crontal backend (hephaestus worktree)

**Files**: `backend/src/api_server/services/handler_service.py`, `controllers/handler_controller.py` (blueprint `handler_blueprint`, prefix `/api/v1/handler`), `controllers/agent_mcp_controller.py` (blueprint at `/api/v1/agent-mcp`), a `HandlerOutbox` model + Alembic migration, the hook in `services/notification_service.py`, config keys in `config/*.yaml` (non-secret only) and `.env.local` docs, and tests under `backend/tests/`. Follow the repo's conventions (`CLAUDE.md`, controller → service → model, ruff).

1. **MCP endpoint** `POST /api/v1/agent-mcp`: MCP over streamable HTTP in **stateless JSON-response mode**, implemented directly in Flask with no new dependency.
   - **JSON-RPC methods**:
     - `initialize` → `protocolVersion`, `capabilities: {tools: {}}`, `serverInfo`;
     - `notifications/initialized` → 202 with an empty body;
     - `tools/list`;
     - `tools/call`;
     - `ping`.
   - **Errors**: unknown methods get a JSON-RPC error.
   - **Auth**: `Authorization: Bearer $CRONTAL_AGENT_TOKEN` compared in constant time, plus `X-Crontal-User` naming an active user who is `handler_enabled`. Otherwise 401/403 before any tool runs.
   - **Tools**: the existing agent `ToolRegistry` (`agent/tools/tool_registry.py`), each `AgentTool` exposed with its name, description and JSON schema. Executed with an `AgentSession` built for that user, matching how `agent_controller` builds sessions; mint the user's auth token server-side the same way login does.
   - **Results**: `CallToolResult` with text content (JSON-encoded `ToolResult`) and `isError`.
   - Include `annotations: {"requiresApproval": tool.requires_approval}` as a hint.
   - Verify with handler's MCP client: handler's `.venv` has the `mcp` SDK, and a test can drive the endpoint via Flask's test client with raw JSON-RPC.
2. **Webhook outbox**:
   - **Table**: `handler_outbox(id, recipient_user_id, type, entity_type, entity_id, data json, status pending|sent|failed, attempts, next_attempt_at, last_error, created_at, sent_at)`.
   - **Hook**: in `create_notification`, after the notification is created, if `HANDLER_ENABLED` and the recipient is `handler_enabled`, insert an outbox row in the same session/transaction.
   - **Sender**: `deliver_pending(limit)` posts signed JSON to `$HANDLER_URL/ingest/crontal` with the signature format above. Success → `sent`; failure → backoff (1m, 5m, 30m, 2h) and `failed` after 6 attempts. Delivery must never break the request that created the notification.
   - **Triggering**: call `deliver_pending` from a background thread started in `create_app` when enabled (lock rows with `FOR UPDATE SKIP LOCKED` on Postgres so gunicorn workers don't double-send), and also expose a `flask handler-outbox-flush` CLI command.
3. **Handler proxy** (`/api/v1/handler/*`, JWT-protected, entitled users only, 403 otherwise). Calls handler with `HANDLER_ADMIN_KEY` + `X-Handler-User: str(user.id)`, provisioning the handler user on a 404/401 from handler (`POST /api/users` `{id, display_name, role}` as admin).
   - `GET /status` → `{enabled}`; for non-entitled users `{enabled: false}`.
   - `POST /chat` `{conversation_id?, text}`; `GET /threads`; `GET /threads/<path:key>/messages`; `GET /threads/<path:key>/runs`.
   - `GET /threads/<path:key>/stream`: an **SSE passthrough**, a streaming Flask response that relays handler's SSE (`requests` with `stream=True`).
   - `GET /settings`, `GET|PUT /settings/<section>`, forwarding `If-Match` and returning handler's 409/422.
   - `GET /approvals`, `POST /approvals/<id>/approve|deny`.
   - `GET /notifications`: handler's notifications for the user.
4. **Config**: `HANDLER_ENABLED`, `HANDLER_URL` (non-secret, in yaml with env override), plus `HANDLER_ADMIN_KEY`, `CRONTAL_AGENT_TOKEN` and `HANDLER_WEBHOOK_SECRET` from env only.
5. **Tests**: MCP auth matrix (no token, wrong token, unknown or non-entitled user), `initialize` → `tools/list` → `tools/call` round-trip, the outbox hook (entitled vs not), the sender's signature/backoff/failed path (mock HTTP), and proxy endpoints with handler mocked (`requests` mocked or a fake server), including entitlement 403 and lazy provisioning.

## R. Crontal frontend (hephaestus worktree)

**Files**: `frontend/src/api/services/handler-service.ts`, a handler chat view or a mode of the existing AI chat in `frontend/src/views/chat/`, `frontend/src/views/settings/HandlerSettings.tsx` (or wherever settings tabs live), routes, and en + zh-CN locale strings. Follow the repo's conventions: typed API wrappers, Radix + Tailwind, no raw `fetch` in views.

- `GET /api/v1/handler/status` decides the mode. When `enabled`, the AI chat sends messages through `/api/v1/handler/chat` and shows the thread's messages, updating live from the SSE proxy (use the same auth approach the app uses for its existing SSE, e.g. a token query param if `EventSource` can't send headers; if the backend proxy needs it, accept the JWT as `?access_token=` on the stream route only). When disabled, today's AI chat is unchanged.
- **Settings tab "AI Assistant (Handler)"**, shown only when enabled:
  - Sections from `GET /settings`, rendered as forms from each section's JSON Schema. Support string (textarea for long text), number, boolean, enum and nested objects.
  - Save with `If-Match`, and show 409 ("changed elsewhere, reload") and 422 field errors.
  - A **Pending approvals** list (tool, action, arguments preview, requested by, age) with Approve / Deny.
  - A **Notifications** list.
- `npm run lint` and `npm run build` must pass.
