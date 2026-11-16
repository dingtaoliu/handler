# Handoff: multi-tenant Handler and free tier

For the next session picking up this work. Written November 14, 2026, right after the
stage 1 release. Read this, then the two design documents it links. The release record
is at the top of [HANDOFF.md](HANDOFF.md).

## Where things stand

| | State |
| --- | --- |
| Design | [multi-tenant-design.md](multi-tenant-design.md): all phases built. Owner decisions are in its Decisions table |
| Next architecture step | [role-split-design.md](role-split-design.md): **proposed, not approved**, nothing built |
| Code | Merged to `main` in both repos: Handler `25a3284`, Crontal `0767312`, plus a docs commit in each |
| Production | Handler **v61**, Crontal **v126**, migration `y1hfree20261109`, backup **b041** |
| Switched on | Shared Postgres pool, `handler_control` schema, memory logs per run and per document read, process-wide cap of 2 document reads, one Crontal webhook per merchant-wide event |
| Switched off | Handler tenant registry, Crontal platform API, free plans in practice, every merchant's cutover switch |
| Who uses Handler | Only merchants 2 (`main`), 1 (`bss`), 298 (`yeup`). The other 14 merchants are on Crontal's old built-in agent |

## Owner decisions (settled; don't reopen)

1. Tenant target under about 100 in the next year: schema per tenant stays.
2. **Free plan:** chat and Crontal threads only. It gets 50 inputs per merchant per UTC calendar month, a figure that is configurable and "for now". The model is fixed at `gpt-6.1-terra`. Free has no email of any kind, no automations, no admin console and no Codex.
3. **Free over the limit:** nothing is held or processed later, for any input type. Chat gets a 429, and other inputs are recorded but never run (`at_limit: "drop"`).
4. **Email on free:** refused and not stored. An upgrade processes only mail that arrives after it. The never-lose-mail rule applies wherever email is a channel, and paid tenants keep the email spool.
5. **Merchant-wide Crontal events** (bids today) go to Handler as one webhook and one input per merchant. Per-user notifications are unchanged.
6. **Production follows `main`** in both repos. `rebuild` was merged and deleted.
7. The owner wants the role split (separate inbox, worker and outbox roles, able to run on one dyno) designed next, after this release. **Revised November 15:** the goal is one host whose processes, memory and connections are shared across tenants. The stages may share one process. See the rewritten role-split-design.md.

## What to do next, in order

1. **Watch production for a few days.** Nothing else should be switched on until then.
   - **Memory logs:** they are the evidence for the R14 cause, which is still unknown.
   - **Connections:** `heroku pg:info -a hephaestus-backend-test` showed 12/20 after the release. Check it stays there.
   - **The first bid event:** check it arrives at Handler once per merchant (Crontal `handler_outbox` row with `recipient_user_id` NULL, sent with `"scope": "merchant"`).
   - **`document read waiting for a slot` logs:** if these appear, raise `HANDLER_DOC_READ_MAX_CONCURRENT`.
2. **`HANDLER_MAX_CONCURRENT` is still 20.** The review suggested 3, but lowering it only helps if each instance's lane limits are at their defaults (2 interactive, 1 background). Check before changing it.
3. **Registry timing is decided (November 15):** build role-split-design.md through step 3, then switch on the registry and import `main`, `bss` and `yeup`. No free pilot before that.
4. **Switching on the registry, at the end of role-split step 3:**
   1. Set `HANDLER_TENANT_REGISTRY=1`, `HANDLER_PLATFORM_KEY` and `HANDLER_TENANT_SECRET_KEY` on `crontal-agent`. Verify the three env tenants import into the registry unchanged.
   2. Set `HANDLER_PLATFORM_URL` and `HANDLER_PLATFORM_KEY` on Crontal.
   3. Switch a few merchants on with `flask handler-instance cutover --on --merchant-id <id>`.

   See [tenant-registry.md](tenant-registry.md), [plan-limits.md](plan-limits.md) and Crontal's `docs/handler-instances.md`. Remove Crontal's old agent (`AIAgentPanel`, the agent loop) only after a week with all merchants moved.
5. **The role split is approved (November 15):** follow its build order. Steps 0 and 1 add the native-only guard and parked threads; step 2 replaces the per-tenant scheduler, housekeeping and poller tasks with one timer for the host. Approvals were checked on November 15: they park and resume as new events, with no in-memory wait.

## Known gaps and accepted risks

- **The R14 cause is unproven.** The leading hypothesis is in-process document reading. Role-split step 4 (branch `feat/shared-runtime-readers`, not yet deployed) moves all parsing into a reader process pool; after release, compare `run memory` (`run_peak_rss_mb`, `reader_peak_rss_mb`) and `document read` logs with the R14 times. The fork server costs about 190 MB idle on Linux, so check total dyno memory after the release.
- ~~Uncapped work: email parsing and image downscaling~~ — closed by step 4: both run in the reader pool.
- ~~The 2-read cap is shared by all tenants, so one hung PDF delays every tenant's reads~~ — closed by step 4: a read times out (120 s) and is killed, and a tenant over its share is preempted while another waits.
- **Gmail** is unavailable for any tenant Crontal creates, because there are no per-tenant Google credentials.
- **Starting guesses, not measurements:** the free plan's 200,000 tokens and 15 turns per run, 100 MB of uploads, 200 MB of workspace, and the active-tenant cap of 40. Tune them from Phase 0 data and the usage ledger.
- **Crontal Alembic can't build a fresh Postgres database from scratch.** The second migration expects a `user` table, an older problem. New migrations are tested only by the suite.

## Rollback

Code rollback is safe against the migrated schema; both old images were tested. The
flush command below follows the usual Crontal one-off form but has not been run in
production yet.

```sh
heroku rollback v59 -a crontal-agent
# Crontal: first flush merchant-level rows, then roll back
heroku run -a hephaestus-backend-test -- sh -c 'cd /app/backend && FLASK_APP=wsgi:app flask handler-outbox-flush'
heroku rollback v125 -a hephaestus-backend-test
```

- **Git tags:** `deployed/crontal-agent-v59`, `deployed/hephaestus-backend-test-v125`, and `pre-multi-tenant` in each repo.
- **Snapshots:** in `~/dev/backups/2026-11-14-pre-multi-tenant/` (mode 600).
- **Migration downgrade** (`alembic downgrade z1coverage20261112`) deletes pending merchant-level outbox rows. Use it only if required.
- **Restoring `b041`** is a last resort: it loses everything written since.

## Working notes

- **Both main checkouts carry other sessions' uncommitted work.** Don't build images from them, because `docker build` packages the working tree. Build from a clean worktree at the commit being deployed.
  - `/Users/dannyliu/dev/repos/handler-wt-multitenant` (`feat/multi-tenant`)
  - `/Users/dannyliu/dev/repos/hephaestus-wt-free-tenants` (`feat/handler-free-tenants`)

  Both are merged and safe to remove, or to reuse after `git merge main`.
- **Tests:**
  - **Handler:** `PYTHONPATH=src .venv/bin/python -m pytest tests/runtime -q -p no:cacheprovider`. 1,530 passed and 4 skipped at release. The Postgres tests start their own server from Homebrew binaries.
  - **Crontal:** in `backend/`, run with `PDM_IGNORE_ACTIVE_VENV=1 pdm run pytest -q`. 738 passed at release. `TEST_POSTGRES_URL` must point at a scratch Postgres for the concurrency test, which drops all tables, so never a real one.
  - **Crontal frontend:** `npm run build`, `npx vitest run`, `npm run lint`. Lint has 64 errors and 30 warnings that are pre-existing and none in this work's files.
- **Scratch Postgres on this Mac:** `initdb` into the scratchpad and start with `-c unix_socket_directories=''`. Long socket paths fail.
- **Production schema dumps** need `/opt/homebrew/opt/postgresql@17/bin/pg_dump`, because the server is 17.10 and the default `pg_dump` is 16. Pass credentials through `PG*` environment variables, never arguments.
- **Crontal releases that add columns the new code reads:** use `heroku maintenance:on`, release, migrate, then `maintenance:off`. `heroku run` uses the released image, so migrations can only run after the release.
- **Subagents** hit session limits several times during this work. Resume them with SendMessage instead of starting new ones; their uncommitted work survives.
