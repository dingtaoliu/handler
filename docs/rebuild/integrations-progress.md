# Integrations refactor — progress

Design: `integrations.md`. Branch `feat/integrations-p1` (worktree `../handler-wt-integrations`).

## Phase 1 (interface + registry, no code moves)

- [ ] `integrations/base.py` interface + contribution dataclasses
- [ ] `integrations/registry.py` (loading, deps, dup detection, one identity provider)
- [ ] wire registry into `instance_runtime` in `runtime/server.py`
- [ ] `GET /api/ui/manifest`
- [ ] tests in `tests/runtime/integrations/`

## Decisions

- Package location: the tree already has `src/handler/runtime/integrations/` (with `crontal/sink.py`).
  The interface lives there (`handler.runtime.integrations`), not `src/handler/integrations/`,
  so the existing crontal sink stays put.

## Open questions

## Next
