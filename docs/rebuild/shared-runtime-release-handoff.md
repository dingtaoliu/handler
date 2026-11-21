# Handoff: merging and releasing the shared runtime (steps 0, 1 and 4, plus review fixes)

For the agent taking over the merge and deployment. Written November 15, 2026; brought up
to date November 17, 2026 once all the work was built and merged. Read this first, then
[role-split-design.md](role-split-design.md) (the design and owner decisions) and the
release record at the top of [HANDOFF.md](HANDOFF.md).

The owner has asked for the merge and deployment to be handed over. **Ask the owner before
each production release.** This handoff authorizes preparing releases, not shipping
them. Never print secret config values or pass them as command arguments.

## Where things stand

All build work is finished and committed. Nothing is pushed or deployed.

| | State |
| --- | --- |
| Production | Handler **v62** on `crontal-agent` (`7c7fde2`); Handler `main` is `6bfb180` (a docs commit on top). Tenants `main` (schema `handler_crontal`), `bss` (`handler_bss`), `yeup` (`handler_yeup`). Crontal on `hephaestus-backend-test` |
| Handler integration branch | `feat/shared-runtime` in worktree `/Users/dannyliu/dev/repos/handler-wt-shared-runtime`. Release 2's code ends at **`db00880`**; commits after it change only this handoff, so merging the branch head is equivalent |
| Crontal branch | `feat/chat-client-message-id`, **`e927a3f`** (two commits on Crontal `main` at `08d2d1f`), in worktree `/Users/dannyliu/dev/repos/hephaestus-wt-client-msg-id`. Release 3 |
| Merged, safe to remove | `feat/shared-runtime-readers` (`/Users/dannyliu/dev/repos/handler-wt-shared-readers`) and `feat/shared-runtime-gating` (`/Users/dannyliu/dev/repos/handler-wt-shared-gating`): both merged into `feat/shared-runtime` |
| Main checkouts | `/Users/dannyliu/dev/repos/handler` and `/Users/dannyliu/dev/repos/hephaestus` hold other sessions' uncommitted work. Don't edit them, and never build images from them |

Test results at handover:

| Commit | Suite |
| --- | --- |
| Handler `e0140e9` (Release 1, branch `feat/shared-runtime-r1`) | 1,631 passed / 4 skipped; 1,587 / 4 three runs in a row earlier on this line |
| Handler `feat/shared-runtime` at `1d9ddf6` (Release 2, includes the Release 1 line) | 1,676 passed / 5 skipped. The skip is a Linux-only address-space test. The Linux pool tests passed in an arm64 container |
| Crontal `e927a3f` (Release 3) | Backend 759 passed / 1 skipped. Frontend `npm run build` passes; `npx vitest run` 87/87; lint has 64 errors and 30 warnings, all already there, none in changed files |

## Plan: three releases

| Release | App | What | Commit |
| --- | --- | --- | --- |
| 1 | `crontal-agent` | Steps 0 and 1: native-only harness guard, workspace upload at run end, accepted failure with Failed runs and Replay, a user retry endpoint for failed chat runs, dead-delivery redeliver; plus two production bug fixes (uppercase `.PDF` uploads, retry classification) | Handler `e0140e9` on `feat/shared-runtime-r1` |
| 2 | `crontal-agent` | Step 4 (reader pool, PyMuPDF for PDF text) and the run-gating fixes from Codex's review | Head of `feat/shared-runtime` (it already contains the Release 1 line) |
| 3 | `hephaestus-backend-test` | Chat sends `client_message_id` and a client-made `conversation_id`; a red failure icon with a single Retry link on failed messages | Crontal `e927a3f` |

Releases 1 and 2 are separate because the reader pool has no on/off switch: undoing it
means rolling back the release. **Ship Release 3 after Release 1**: the run-failure Retry
needs Release 1's endpoint and `retryable` field. Against v62 it is still safe (the icon
shows without Retry, and send-failure Retry works), so the order only affects whether Retry
appears on failed runs.

### Release 1: steps 0 and 1 (Handler `e0140e9`, branch `feat/shared-runtime-r1`)

**Contents:**

| Commit | Change |
| --- | --- |
| `eafdcc0` | Design doc and owner decisions |
| `a9437e8` | A thread's workspace uploads in the background when its run ends; a failed upload falls back to the 300 s sync |
| `625e38b` | `HANDLER_ALLOWED_HARNESSES`: only listed harnesses can be built, set as default, saved in threads settings or set through the platform API. A stored thread on a disallowed harness parks with a clear reason |
| `9b14857` | Parked threads: an alert, an admin notice, one notice for new work while parked, and a list and Retry in the API, CLI and UI. Dead deliveries: an alert, an admin notice at most hourly per sink, and redeliver in the API and CLI. New column `threads.park_noticed` |
| `98358db` | No park notice for chat threads (superseded by the next rows) |
| `0241bbe` | Cherry-pick of `6cedced`: the held-email test no longer depends on timing |
| `cd5ae17`, `69ebc3a` | **Accepted failure replaces parking** (owner decision, November 21). A run that fails for good is `FAILED`, its events are consumed by it, and the thread stays usable. No user or tenant-admin failure notices, including the pre-existing background one. Operator signals: ERROR `run failed` (tenant, thread, type, run, attempts, error, event ids), a `run_failures` table, `GET /api/runs/failed`, `POST /api/runs/{run_id}/replay` (once), CLI `handler-rt run failed` / `run replay`, and a Failed runs table with Replay on Activity. A startup sweep clears old `dead` threads, marks their queued events `failed-before-upgrade` and records them for replay. Parking (`park_noticed`, `/api/threads/parked`, `/retry`, badges) is removed |
| `70e48a5` | PDFs are uploaded under a lowercase `.pdf` name. OpenAI refused `X.PDF` as input with a 400, which failed 9 BSS mail threads |
| `e0140e9` | `POST /api/runs/{run_id}/retry` for the person chatting: their own chat thread, the thread's latest run, a final failure, once (shared with Replay). It reruns the same events, charges no input and keeps one copy of the message. `run_finished` and the runs listing carry `retryable`; `run_finished` carries `event_ids` |
| `ead189b` | Transient provider errors retry: overload, 429/408/409/5xx, and "another process is operating on this conversation" (after at least 30 s). "Model did not produce a final response" retries once. Other 400s stay final |

**Checks before release:**

0. **Already done, November 21 (read-only):**
   - Threads settings name `openai_agents` everywhere. `handler_crontal` has 3 chat threads on `codex` (user 1, last used October 18); writing in them fails the run, and nothing else is affected.
   - Dead threads: none in `handler_crontal` or `handler_yeup`. `handler_bss` has 13: 12 mail threads with 40 queued events (November 9 to 20) and 1 chat thread (November 11).
   - The causes were the uppercase `.PDF` upload (9 threads), "overloaded" classified as final (2), "another process…" (1) and "no final response" (1). All are fixed in this release.
   - Re-run both queries just before releasing, since new dead threads may appear.
1. **No Codex left in production.** Setting `HANDLER_ALLOWED_HARNESSES=openai_agents` parks
   any stored thread whose harness is `codex`, and logs an ERROR at startup if saved threads
   settings name it. The owner says production uses only `openai_agents`; confirm it with
   read-only queries in each schema (`handler_crontal`, `handler_bss`, `handler_yeup`):

   ```sql
   SELECT harness, type, count(*) FROM <schema>.threads GROUP BY 1, 2;
   SELECT value FROM <schema>.settings_versions
   WHERE section = 'threads' ORDER BY version DESC LIMIT 1;
   ```

   Run them with `heroku pg:psql -a hephaestus-backend-test` (Handler and Crontal share the
   database). If anything names `codex`, stop and ask the owner.
2. **The intermittent test (done: the fix is cherry-picked, and the suite passed 3 runs in a row).** One full-suite run in about 10 failed once. The likely cause
   is the held-email test in `tests/runtime/store_pg/test_review_fixes.py`, which depended
   on parse timing. It also failed when run alone at `c9cae4e`. It is fixed in `6cedced`,
   which is only on the Release 2 side. Run the suite on the Release 1 merge at least 3
   times with `-rf`. A failure in that one test is the known timing flake (rerun it, or
   cherry-pick `6cedced`, which is test-only). Investigate any other failure.
3. **The UI in a browser (done, November 21).** Handler at `e0140e9` with SQLite and a
   fake OpenAI server that answers 400, driven by headless Chromium: the thread shows "Run
   failed (send your message again to retry): …"; Tasks shows the Failed runs table with
   Replay; Replay re-queued the run and the row changed to "replayed"; no JavaScript
   errors. Against the same server: `POST /api/runs/{id}/retry` gave 202, then 409 "already
   replayed or retried"; the message appeared once; `run failed` logged with all fields. Never connect a local run to
   production stores.

**Merge:** merge `feat/shared-runtime-r1` (`e0140e9`) into `main`, not `feat/shared-runtime`, which includes Release 2.
Production follows `main`.

```sh
git -C /Users/dannyliu/dev/repos/handler worktree add ../handler-wt-release1 main
cd /Users/dannyliu/dev/repos/handler-wt-release1
git merge --no-ff e0140e9 -m "Merge shared runtime steps 0 and 1"
```

Then run the suite on the merge result. How `main` gets updated (direct merge, or a pull
request and push) is the owner's call; ask.

**Release** (commands from Crontal's `.claude/skills/deploy/SKILL.md`). Build from a clean
worktree at the merged commit, never from the main checkout:

```sh
heroku pg:backups:capture -a hephaestus-backend-test
heroku pg:backups -a hephaestus-backend-test        # confirm it completed
heroku releases -a crontal-agent | head -3          # note the rollback release (v62)
git -C <clean worktree> status --short             # must be empty
docker build --platform linux/amd64 -t registry.heroku.com/crontal-agent/web <clean worktree>
heroku container:login
docker push registry.heroku.com/crontal-agent/web
heroku config:set HANDLER_ALLOWED_HARNESSES=openai_agents -a crontal-agent
heroku container:release web -a crontal-agent
```

`config:set` restarts the dyno on the old image. Setting the variable first is safe:
v62 ignores it. Alternatively set it immediately after the release.

**Verify:**

- `/healthz`, `/i/main/healthz`, `/i/bss/healthz` and `/i/yeup/healthz` return 200; an
  unknown instance returns 404; cross-instance admin keys and no key return 401.
- Startup logs: no harness ERROR, and one `run failed` line with `failed_before_upgrade: true` per BSS dead thread swept (13 expected). Check `SELECT table_schema FROM information_schema.tables WHERE table_name = 'run_failures';` returns each schema after its first use, and that no thread has `dead = TRUE`.
- A chat through Crontal's proxy works for one user each on `bss` and `yeup`.
- `GET /i/bss/api/runs/failed` lists the swept threads (admin key).
- **The BSS backlog: ask the owner** before replaying. The recommendation is to replay the 12 mail failures (`POST /i/bss/api/runs/{run_id}/replay`) and skip the November 11 chat. The agent will act on mail up to two weeks old, and replay can repeat tool side effects.
- Watch logs for `run failed` and `delivery dead` for a day. The old message
  `delivery dead-lettered` is gone, so update any log filter that matched it.

**Rollback:** `heroku rollback v62 -a crontal-agent`. The schema change only adds a table
(`run_failures`), which v62 ignores. v62 would park threads again, and the next upgrade
sweeps them. Unset `HANDLER_ALLOWED_HARNESSES` if desired; v62 ignores it.

**Record** the release at the top of `HANDOFF.md`, in the style of the v62 entry: release,
commit, image id and size, backup id, rollback target, verification evidence and suite
count. Also mark steps 0 and 1 as released in `role-split-design.md`.

### Release 2: reader pool, PyMuPDF and run gating (Handler `db00880`)

**Contents** (everything on `feat/shared-runtime` after `98358db`):

| Area | Commits | Change |
| --- | --- | --- |
| Reader pool | `7f1b90b`, `9d13c08`, `07fd29b`, `bdfdaef`, `90cf0d6` | Document, email and image parsing in child processes. Fairness applies only when a read starts. An RSS watchdog per reader (`HANDLER_READER_MAX_RSS_MB`, default 220). The fork server preloads nothing heavy and stops when idle. Tool result and file size caps in the main process. Peak memory logged per run |
| PDF text | `26c5bca`, `88ac6c0`, `ec43185` | Whole-document PDF text comes from PyMuPDF instead of MarkItDown (owner approved) |
| One library per reader | `8cdfaaa`, `db00880` | A reader holds PyMuPDF or MarkItDown, never both. A call needing the other library gets a fresh reader. A reader left above 75 % of the cap after a call is recycled |
| Run gating | `6a489cb`, `77c6bc5`, `acead1e`, `518756b`, `b93bcfc` | Each run's tool view comes from the live plan, so a plan change applies on the next run without a restart. One host `RunGate` for the env tenants. Interactive before background across tenants. A duplicate chat message returns its original thread |
| Test fix | `6cedced` | The held-email test no longer depends on parse timing |
| Merges | `c9cae4e`, `8c987bb`, `f9756e6` | Integration merges. Conflicts were only in `server.py`'s `instance_runtime` signature and in the design doc |

**Behaviour changes to expect in production:**

- **Concurrency.** The dyno runs at most **6 runs at once, 3 per tenant**
  (`HANDLER_GLOBAL_MAX_RUNS` and `HANDLER_PLAN_RUN_CAPS` defaults). Each tenant's own lane
  limits (2 chat and 1 background by default) still apply beneath that. Before, the cap
  was each instance's lane capacity times 3. Measured peak concurrency is 2. **Not
  checked:** whether production sets any of these gate variables, or stores lane limits in
  each tenant's settings. Check `heroku config -a crontal-agent` for variable names only
  (`heroku config -a crontal-agent | cut -d: -f1`), never values.
- **Document reads.** `HANDLER_DOC_READ_MAX_CONCURRENT` now defaults to **1** (was 2), with
  a 220 MB RSS cap per reader. A document too large for the cap fails with "… is too large
  to read" instead of threatening the dyno.
- **PDF output format.** PDF text changed from MarkItDown's text (lines separated by blank
  lines, no page markers) to PyMuPDF page text: one `--- page N of M ---` block per page,
  the format `pages=` reads already used. Paging (`offset`, `max_chars`, "Call read_file
  with offset=…"), scan detection and page images are unchanged. Chat `read_file` on a PDF
  with the native harness uploads the file and extracts nothing, so it is unaffected.
- **Plan changes apply live.** This doesn't matter in production yet: the platform API
  is off.

**Measurements** (amd64 container, the v62 image's dependencies with the branch's `src`
mounted, SQLite, fake model key; whole process tree; details in
[multi-instance.md](multi-instance.md#memory-budget-measured-november-17-amd64-linux-python-312-release-image)):

| | Before (first reader pool build) | Now (defaults) |
| --- | --- | --- |
| Idle, before any read | 154 MB | 154 MB |
| 60 s after the last read | 395 MB | 157 MB (the main process only) |
| Reader busy on a large file | 779 MB, and the PDF reader crashed | about 440 MB at most (main 157 + fork server 45 + tracker 26 + one reader at its 220 cap) |
| PDF of 2, 30, 100 or 400 pages | MarkItDown: 244 MB to 6 GB | PyMuPDF: 164 MB peak, 0.5–1.0 s in a fresh reader; none refused |
| Switching library (PDF ↔ docx) | — | about 0.5 s (PDF) or 0.9–1.3 s (docx) extra, on a switch only |

**MarkItDown's PDF conversion is the likely cause of the R14 errors.** It used 1.57 GB
for a 100-page PDF, and before this pool it ran inside the main process.

**Memory budget:** keep the defaults on the 512 MB Basic dyno (owner decision: stay on
Basic until measurements show otherwise). On a 1 GB dyno, 2 readers at 300 MB or 3 at
220 MB fit (measured busy tree 584 MB RSS for 2 at 300 MB).

**Before release 2:**

1. **Release 1 is live and has been watched for at least a day.**
2. **Repeat the measurement on the real release build.** The numbers above came from the
   branch's `src` mounted over v62's dependencies, not from a built image. Build the
   release image (`--platform linux/amd64`) and run it with disposable storage, SQLite
   and a fake model key, never production stores. Check:
   - whole-tree idle RSS before any read and 60 s after the last one;
   - a 100-page PDF read, a large xlsx and a docx in turn;
   - that a file over the cap fails only its own call.

   Stop and report to the owner if idle RSS is materially above about 160 MB, or if a
   reader peak exceeds 220 MB for an ordinary file.
3. **Check production's gate settings** (see Concurrency above) and report the effective
   limits to the owner.
4. **Merge** `feat/shared-runtime` at `db00880` into `main` (after Release 1's merge), and
   run the suite on the result.
5. **Release** as in Release 1: backup, clean worktree, amd64 build, push, release. No new
   variables are required; the new optional ones and their defaults are listed in
   [multi-instance.md](multi-instance.md).

**Verify:**

- Release 1's checks again.
- Read a real PDF in each tenant through a preview path: an email attachment, a Crontal
  file preview, or a bounded task. Look for `document read` logs with `family`, and
  `run memory` logs with `run_peak_rss_mb` and `reader_peak_rss_mb`.
- Watch `heroku logs` for R14 over the following days, and compare with the days before.
  Look for "too large to read" refusals, and tell the owner if ordinary files hit them.

**Rollback:** to Release 1's release number. No schema change in Release 2.

**Record** the release in `HANDOFF.md` like Release 1, and mark step 4 released in
`role-split-design.md`.

### Release 3: Crontal chat message ids and failure Retry (Crontal `e927a3f`)

**Change:** the chat panel gives each message a `client_message_id` once, and reuses it on
resend after a timeout, network error or 5xx, so Handler stores and runs it once. A new
conversation gets its `conversation_id` on the client when it starts, so a resent first
message lands in the same thread. The backend forwards page context for such a
conversation (`new_conversation` flag). A 4xx, including a 429 at the free monthly limit,
counts as a clear rejection, so a later send gets a new id. That is intended: a dropped
over-limit message is recorded under its id, so reusing it would never run.

Also (`e927a3f`): a failed message shows a red `CircleAlert` icon and a "Retry" link
beside the user's bubble. A send failure (network, timeout, 5xx) keeps the message and
Retry resends with the same ids; a 4xx gives a new message id; the free-plan 429 keeps its
limit notice and offers no Retry. A run failure is found from `run_finished` or, after a
reload, the runs listing (matched by event id), and Retry calls the new proxy
`POST /api/v1/handler/threads/<key>/runs/<run_id>/retry` (own chat threads only, run
checked against the thread), which forwards to Handler's user retry. A missing
`retryable` (older Handler) shows the icon without Retry.

**Checks:** the suites listed above. The panel was not rendered in a browser; its states
are covered by component tests (`HandlerChatPanel.test.tsx`). After release, look at one
failed message in the panel to confirm the icon and Retry look right.

**Merge and release:** merge `18a3018` into Crontal `main`, in a clean worktree, never the
main checkout. Then follow Crontal's `.claude/skills/deploy/SKILL.md` for
`hephaestus-backend-test`. This change has no Alembic migration; still confirm
`alembic current` is at head afterwards. Take a backup first, as the skill requires.

**Verify:** a chat through the agent panel works for one user each on `bss` and `yeup`.
The `POST /api/v1/handler/chat` request body carries `client_message_id`, and a new
conversation also carries `conversation_id` and `new_conversation`. If a failed run is
available (for example one from the BSS sweep is not a chat; use any chat failure), its
message shows the icon and Retry.

**Rollback:** `heroku rollback` to the previous Crontal release; nothing else changes.

## Codex review, November 16 (findings and dispositions)

| # | Finding | Disposition |
| --- | --- | --- |
| 1 | **Upgrade leaves paid tools missing.** Automation tools were registered at runtime start (`server.py` ~806). A plan change updates live limits (`tenancy/host.py` ~672), but an open SSE connection prevented the restart that would add the tools. Reproduced | **Fixed** (`6a489cb`, Release 2): every tool is registered on every plan, each run's tool view comes from the live plan, and the gateway refuses calls and approvals the current plan excludes. Tested with an upgrade and a downgrade under an open SSE stream. Changing a tenant's integrations or secrets still needs a runtime restart; step 3's integration contract removes that |
| 2 | **No shared run cap for env tenants** | **Fixed** (`77c6bc5`, Release 2): one host `RunGate` for every env instance, each configured `paid` as the registry imports it. The SQLite store gained `has_claimable`, without which a gated SQLite dispatcher would have crashed silently |
| 3 | **Reader pool is a release blocker**; a per-child cap needs a check across the whole tree | **Fixed** (Release 2): the three pool fixes, whole-tree measurements, PyMuPDF for PDF text and one library family per reader |
| 4 | **No interactive-before-background priority across tenants** | **Fixed** (`acead1e`, Release 2): interactive before background across tenants, then paid before free. Background work waiting past `HANDLER_RUN_MAX_WAIT_SECONDS` (60 s) goes first once, so it can't starve |
| 5 | **The chat client doesn't send `client_message_id`** | **Fixed** (Crontal `18a3018`, Release 3) |
| — | **Call position is too weak an idempotency key** for nondeterministic retries | Design changed: thread, input events, tool name and argument hash, with an intent row written before the call. Build order step 5 |
| — | **90-day retention cleanup isn't implemented** | Added to build order step 2 |
| — | **`multi-tenant-design.md` status was stale** | Fixed |
| — | **A duplicate chat message reported a new thread** when the resend had no `conversation_id` (found while fixing 5) | **Fixed** (`518756b`, Release 2): a duplicate returns the stored original's `event_id`, `thread_key` and `conversation_id` |

## Not part of these releases

- **Steps 2, 3, 5, 6 and 7 of the build order.** Steps 2 and 3 (one timer, one claim
  loop, the context cache, the integration contract) come next, on top of
  `feat/shared-runtime` after Release 2.
- **The tenant registry stays off** until the end of step 3 (owner decision). The
  three env tenants are then imported into it.
- **Automatic retries of parked threads and deliveries**, with exponential backoff, are
  future work (owner decision).

## Working notes

- **Handler tests:**
  `cd <worktree> && PYTHONPATH=src /Users/dannyliu/dev/repos/handler/.venv/bin/python -m pytest tests/runtime -q -p no:cacheprovider`
  takes about 2 minutes. The Postgres tests start their own server from Homebrew binaries.
- **Crontal tests:** in `backend/`, `PDM_IGNORE_ACTIVE_VENV=1 pdm run pytest -q`. Confirm
  `api_server.__file__` resolves inside the worktree, and repair a worktree venv with a
  plain `pdm sync`, never `--clean`. In `frontend/`: `npm run build`, `npx vitest run`,
  `npm run lint`.
- **Scratch Postgres on this Mac:** `initdb` into a temp directory and start with
  `-c unix_socket_directories=''`, because long socket paths fail.
- **Production schema dumps** need `/opt/homebrew/opt/postgresql@17/bin/pg_dump`, because
  the server is 17.10. Pass credentials through `PG*` environment variables.
- **Heroku apps:** `crontal-agent` is Handler. `hephaestus-backend-test` is Crontal and
  owns the shared database. Ignore `crontal-agent-bss` and `hephaestus-web-app`.
- **Measurement scripts** from the reader work (`measure.py`, `combos.py`) are in
  `/private/tmp/claude-501/-Users-dannyliu-dev-repos-hephaestus/fa33381b-e25f-4d11-817c-618eb99f2408/scratchpad/pdfwork/`.
  This is a temporary directory and may be gone.
