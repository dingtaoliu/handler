# Integrations refactor — progress

Design: `integrations.md`. Branch `feat/integrations-p1` (worktree `../handler-wt-integrations`).

## Phase 1 (interface + registry, no code moves)

- [x] `integrations/base.py` interface + contribution dataclasses
- [x] `integrations/registry.py` (loading, deps, dup detection, one identity provider)
- [x] wire registry into `instance_runtime` in `runtime/server.py`
- [x] `GET /api/ui/manifest` (`web/ui_manifest_api.py`)
- [x] tests in `tests/runtime/integrations/` (`fakes.py`, `test_registry.py`, `test_wiring.py`)

**Phase 1 done.** Suite: 761 passed, 2 skipped (was 749 + 12 new).

## Decisions

- Package location: the tree already has `src/handler/runtime/integrations/` (with `crontal/sink.py`).
  The interface lives there (`handler.runtime.integrations`), not `src/handler/integrations/`,
  so the existing crontal sink stays put.

- Phase 1 keeps ALL legacy wiring (crontal sink/tools/ingest, email intake, google) in every case;
  integration contributions are added on top, after core wiring, so duplicate routes/tools against
  core are reported with the integration's name. Phase 2 skips each legacy block when its
  integration is enabled (and, once all three exist, when `HANDLER_INTEGRATIONS` is set at all).
- `HANDLER_INTEGRATIONS` is in `_PRIVATE_ENV`: non-main instances do not inherit main's list.
  Per-instance override: `HANDLER_I_<NAME>__INTEGRATIONS` (becomes `INTEGRATIONS` in the scoped env)
  or `HANDLER_I_<NAME>__HANDLER_INTEGRATIONS`.
- Core thread types `chat`, `automation` are reserved. Thread-type specs are only collected
  (`app.state.integrations.thread_types`); `ThreadType` is still an enum — phase 2 relaxes it.
- Identity provider is collected into `app.state.identity_provider`; nothing consumes it until phase 4.
- `instance_runtime(..., registry=...)` lets tests inject a registry.

## Open questions

## Next: phase 2 plan (move code behind the interface, no behaviour change)

Order: crontal, then email_intake, then google. For each: create
`src/handler/runtime/integrations/<name>/__init__.py` with an `Integration` subclass,
register it in `default_registry`, and in `server.instance_runtime` skip the matching
legacy block when the integration is enabled. Keep routes, URLs, settings keys and stored
data unchanged. When `HANDLER_INTEGRATIONS` is unset, keep the legacy block (until phase 5
sets it in prod), so each move is safe to ship alone.

1. **crontal** (`integrations/crontal/`), legacy blocks in `server.py`:
   - config: `CRONTAL_URL` (args.crontal_url), `CRONTAL_AGENT_TOKEN`, `HANDLER_WEBHOOK_SECRET`;
   - thread type `crontal` (`types.ThreadType.CRONTAL`; instructions/rendering in
     `settings.py` thread defaults and `web/ingest.py`);
   - router: `web/ingest.py::crontal_ingest_router` (`/ingest/crontal`);
   - tools: `gateway/crontal_api.py::CrontalApiTools` (`crontal_api_*`);
   - background task: `start_crontal_connector` (curated `crontal.*` MCP tools via
     `gateway/crontal.py::crontal_connector`, registered into the gateway once reachable);
   - sink: `integrations/crontal/sink.py::crontal_sink_from_config` (already here);
   - later: identity provider (phase 4), persona fragment, `HANDLER_RETURN_TO_ORIGINS`.
   - Needs: `ThreadType` enum accepting integration-defined strings (or keep the enum members
     and just validate against `loaded.thread_types`).
2. **email_intake** (`integrations/email_intake/`): config `HANDLER_EMAIL_SECRET`;
   router `web/email_ingest.py::email_ingest_router`; thread type `email` and its
   instructions; sender gate (blocklist + DKIM) in `runtime/mail/`; settings section
   for the blocklist; Worker docs (`docs/rebuild/email-intake.md`).
3. **google** (`integrations/google/`): config `GOOGLE_OAUTH_*`, `HANDLER_SECRET_KEY`,
   `HANDLER_PUBLIC_URL`; router `web/google_api.py::google_router`; OAuth +
   secrets `gateway/google_oauth.py`, `gateway/secret_box.py`; tools
   `gateway/gmail.py::GmailTools`; UI Connections card entry; `FULL_GOOGLE_SCOPES`
   and `_configure_google` CLI in `server.py`.
4. Then core-only tests: an instance with `HANDLER_INTEGRATIONS=` has no crontal/email/google
   routes, tools or sinks.
