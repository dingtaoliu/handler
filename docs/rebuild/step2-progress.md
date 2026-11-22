# Shared runtime step 2: progress

Branch `feat/shared-runtime-step2` (from `feat/shared-runtime` at 1c36947). Brief:
role-split-design.md, build order step 2. Delete this file when step 2 is merged.

## Design (decided)

- `due_work` rows: (tenant, kind, ref, due_at, version, attempts, last_error,
  lease_owner, lease_until). Postgres: `handler_control.due_work`, `tenant` is the
  tenant's schema name. SQLite: a `due_work` table in the store's own file, tenant
  `local`. Rows are written in the same transaction as the work they describe.
- One `HostTimer` per host (`runtime/timer.py`) claims due rows (Postgres:
  `FOR UPDATE SKIP LOCKED` plus a renewed lease), resolves the tenant through one
  function, runs the kind's handler, which returns the next due time or None.
  A `version` bump by the work's own transaction wins over the handler's result.
- Kinds: `schedule.fire`, `approval.expire`, `question.expire`, `inputs.release`,
  `retention.cleanup`, `mailbox.poll` (integration).

## Done (WIP commit 1)

- due_work storage: `store/due_work.py`, `schema_due_work.sql`, DDL in the hub's control
  DDL; a Postgres store without a hub keeps rows in its own schema. Rows written in the
  same transaction as schedules, approvals, questions, held inputs and mailboxes.
- `runtime/timer.py` HostTimer, TenantDirectory; registry directory in tenancy/host.py.
- Kinds: schedule.fire (Scheduler.fire_due; run_forever removed), approval.expire,
  question.expire, inputs.release, retention.cleanup (`runtime/retention.py`),
  mailbox.poll (integration `timer_kinds`, poller ported).
- housekeeping removed; grants via settings listener plus a check before each run.
- workspace sync: one WorkspaceBackstop per host. One host bus with TenantBus views.
- Backfill at instance start (`backfill_due`), also on a plan change.
- Tests: scheduler/test_timer.py, store_pg/test_due_work.py, poller tests rewritten.

## Next

1. Fix test_host_with_ten_tenants_stays_within_twelve_connections.
2. Cancel as a stored flag (no cancel API exists today: add one).
3. Bus isolation test (SSE), idle-tenant task/query tests (env and registry).
4. Docs: role-split-design.md step 2 row, integrations.md, mailbox and scheduler docs.
