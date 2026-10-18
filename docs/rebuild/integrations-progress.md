# Integrations refactor — progress

Design: `integrations.md`. Branch `feat/integrations-p1` (worktree `../handler-wt-integrations`).

## Phase 1 (interface + registry, no code moves)

- [x] `integrations/base.py` interface + contribution dataclasses
- [x] `integrations/registry.py` (loading, deps, dup detection, one identity provider)
- [x] wire registry into `instance_runtime` in `runtime/server.py`
- [x] `GET /api/ui/manifest` (`web/ui_manifest_api.py`)
- [ ] tests in `tests/runtime/integrations/`

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

## Next
