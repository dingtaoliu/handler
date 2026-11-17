# Handoff: merging and releasing the shared runtime, steps 0, 1 and 4

For the agent taking over the merge and deployment. Written November 15, 2026. Read
this first, then [role-split-design.md](role-split-design.md) (the design and owner
decisions) and the release record at the top of [HANDOFF.md](HANDOFF.md).

The owner has asked for the merge and deployment to be handed over. **Ask the owner before
each production release.** This handoff authorizes preparing releases, not shipping
them. Never print secret config values or pass them as command arguments.

## Where things stand

| | State |
| --- | --- |
| Production | Handler **v62** on `crontal-agent` (`7c7fde2`); `main` is `6bfb180` (a docs commit on top). Tenants `main` (schema `handler_crontal`), `bss` (`handler_bss`), `yeup` (`handler_yeup`) |
| Integration branch | `feat/shared-runtime` in worktree `/Users/dannyliu/dev/repos/handler-wt-shared-runtime` |
| Reader-pool branch | `feat/shared-runtime-readers` in `/Users/dannyliu/dev/repos/handler-wt-shared-readers`. It has merged `feat/shared-runtime` (at `c9cae4e`). **The follow-up fixes are unfinished:** a Claude agent stopped at a usage limit with uncommitted edits to `documents/pool.py` only. Ask the owner whether that agent resumes or you finish it; never both (see Release 2) |
| Main checkout | `/Users/dannyliu/dev/repos/handler` holds other sessions' uncommitted work. Don't edit it, and never build images from it |

Commits on `feat/shared-runtime`, oldest first:

| Commit | Content | Release |
| --- | --- | --- |
| `eafdcc0` | Design doc and owner decisions | 1 |
| `a9437e8` | Upload a thread's workspace when its run ends (in the background; failure falls back to the 300 s sync) | 1 |
| `625e38b` | `HANDLER_ALLOWED_HARNESSES`: only listed harnesses can be built, set as default, saved in threads settings or set through the platform API. A stored thread on a disallowed harness parks with a clear reason | 1 |
| `9b14857` | Parked threads (alert, admin notice, one notice for new work while parked, list and Retry in API, CLI and UI). Dead deliveries (alert, admin notice at most hourly per sink, redeliver in API and CLI). New column `threads.park_noticed` | 1 |
| `98358db` | No park notice for chat threads (owner decision); the operator log line stays | 1 |
| `7f1b90b`, `9d13c08`, `07fd29b` | Reader pool: document, email and image parsing in child processes; tool result and file size caps; peak memory logged per run | 2 |
| `c9cae4e` | Merge of the reader pool. One conflict in `server.py` `instance_runtime`, resolved by keeping both `allowed_harnesses` and the `set_current_tenant` line | 2 |

Suite results: `98358db` 1,578 passed / 4 skipped; `c9cae4e` 1,595 passed / 6 skipped. The
2 extra skips are Linux-only memory tests.

## Plan: two releases

Ship steps 0 and 1 first. Ship the reader pool alone afterwards, because it has no on/off
switch: undoing it means rolling back the whole release.

### Release 1: steps 0 and 1 (`98358db`)

**Checks before release:**

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
2. **The flaky test.** One full-suite run out of about 10 had a single failure; its name
   was not captured. The tests this work changed passed 8 times in a loop. Run the full
   suite at least 5 times with `-rf`, and name any failure. Fix it if the new code caused
   it (the background workspace upload is the first suspect). Otherwise record it as
   pre-existing.
3. **The UI in a browser.** The parked badge, the error with Retry, and the Parked threads
   table on Activity (`web/static/app/`) were only checked with `node --check`. Run
   Handler locally against a scratch database, park a thread (a run that fails with no
   retries left), and check the badge, Retry and the table. Never connect a local run to
   production stores.

**Merge:** merge `98358db` into `main`, not the branch head, which includes the reader
pool. Production follows `main`.

```sh
cd /Users/dannyliu/dev/repos/handler-wt-shared-runtime
git fetch . 98358db
git checkout --detach main   # or work in a fresh worktree on main
git merge --no-ff 98358db -m "Merge shared runtime steps 0 and 1"
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
`main` ignores it. Alternatively set it immediately after the release.

**Verify:**

- `/healthz`, `/i/main/healthz`, `/i/bss/healthz` and `/i/yeup/healthz` return 200; an
  unknown instance returns 404; cross-instance admin keys and no key return 401.
- Startup logs: no harness ERROR. Check the column exists:
  `SELECT column_name FROM information_schema.columns WHERE table_name = 'threads' AND column_name = 'park_noticed';`
  This should return one row per schema, after each tenant's first use.
- A chat through Crontal's proxy works for one user each on `bss` and `yeup`.
- `GET /i/<name>/api/threads/parked` returns a list (admin key).
- Watch logs for `thread parked` and `delivery dead` for a day. The old message
  `delivery dead-lettered` is gone, so update any log filter that matched it.

**Rollback:** `heroku rollback v62 -a crontal-agent`. The schema change only adds a column,
so v62 runs on it unchanged. Unset `HANDLER_ALLOWED_HARNESSES` if desired; v62 ignores it.

**Record** the release at the top of `HANDOFF.md`, in the style of the v62 entry: release,
commit, image id and size, backup id, rollback target, verification evidence and suite
count. Also mark steps 0 and 1 as released in `role-split-design.md`.

### Release 2: the reader pool (step 4)

**Follow-up done (November 17) on `feat/shared-runtime-readers`; release needs the
owner's call on the memory budget below.** The three review problems:

1. **Idle memory: fixed.** The fork server preloads only the reader modules, and it and
   the resource tracker stop with the last idle reader. Measured 60 s after the last
   read: 157 MB for the whole tree (the main process alone), against 395 MB before.
2. **Fair sharing no longer kills reads.** Fairness applies at admission only: fewest
   slots held, oldest call first; while another tenant waits, a tenant at
   ceil(slots / active tenants) can't start another read.
3. **Memory cap: an RSS watchdog.** The thread waiting on each reader samples its RSS
   every 200 ms and kills it over `HANDLER_READER_MAX_RSS_MB`; the call fails with
   "… is too large to read" and the reader is replaced. `RLIMIT_AS` is now **off** by
   default (`HANDLER_READER_MEMORY_MB=0`): at 2,048 MB it already crashed an ordinary
   large-PDF read, because PyMuPDF and MarkItDown reserve so much address space.

**Measured whole-tree memory** (amd64 container from the release image, Python 3.12,
SQLite, fake model key; RSS in MB; details in
[multi-instance.md](multi-instance.md#memory-budget-measured-november-17-amd64-linux-python-312-release-image)):

| | Before (preload, 2 readers, no RSS cap) | Now, defaults (1 reader, cap 220) |
| --- | --- | --- |
| Idle, before any read | 154 | 154 |
| 60 s after the last read | 395 | 157 |
| Every reader busy (400-page PDF + 60,000-row xlsx) | 779 RSS / 503 PSS; PDF reader crashed | 440 RSS / 400 PSS; PDF refused "too large to read", xlsx read |
| First read, cold: PDF / xlsx | 2.7 s / about 2.3 s | about 1.2 s / 1.1 s |

**The main finding:** PDF text conversion through MarkItDown (pdfminer) is what uses
memory. With no cap, one reader reached 244 MB for a 2-page PDF, 597 MB for 30 pages,
1.57 GB for 100 and **6 GB** for 400 pages; before this pool that ran inside the main
process, which fits the R14 errors. PyMuPDF's text extraction of the same files added
nothing measurable (0.3 s for 100 pages). xlsx peaked at 73 MB, HTML at 152 MB.

**Recommendation and defaults:** on the 512 MB Basic dyno, 1 reader with a 220 MB cap
(`HANDLER_DOC_READ_MAX_CONCURRENT=1`, `HANDLER_READER_MAX_RSS_MB=220`, now the
defaults): main (about 155 idle) + fork server 45 + tracker 26 + reader 220 is about
450 MB. With MarkItDown, PDF **text** previews (downloads, email attachments, bounded
tasks) failed "too large to read" at that cap even for small PDFs; chat `read_file` on
a PDF uploads the file and parses nothing, so it was unaffected. Fixed: PDF text now
comes from PyMuPDF (see below). On a
1 GB dyno, 2 readers at 300 MB (busy tree measured 584 RSS / 516 PSS) or 3 at 220 MB
fit.

**PDF text moved to PyMuPDF (owner approved; done November 17 on
`feat/shared-runtime`).** `documents/pdf.py` extracts whole-document PDF text
(`read_file`/`handler-read` without `pages`, download and attachment previews, bounded
tasks) with PyMuPDF, page by page; MarkItDown is no longer used or imported for PDFs
(other formats keep it). **The output changed:** from MarkItDown's text (lines
separated by blank lines, no page markers) to PyMuPDF page text, one
`--- page N of M ---` block per page, the format `pages=` reads already used. Paging is
unchanged (`offset`, `max_chars`, "Call read_file with offset=…"), from the cached page
text. Scan detection and page images are unchanged. Measured through the pool at the
220 MB cap (amd64, v62 image's dependencies, branch `src` mounted): 2-, 30-, 100- and
400-page PDFs all read at a 164 MB reader peak, 0.5–1.0 s in a fresh reader; none
refused (the old path refused even 2 pages at about 223 MB). A reader holding both
PyMuPDF and MarkItDown would be over 220 MB on the imports alone, so the pool keeps one
heavy library family per reader: a call needing the other family gets a fresh reader,
the old one stopped first (about 0.5 s extra for a PDF, 0.9–1.3 s for a docx, on a
switch only), and a reader left above 75 % of the cap after a call is recycled.
Mixed sequences (PDF then docx, docx then PDF, PDF then xlsx then PDF) read with no
refusals. Tables in
[multi-instance.md](multi-instance.md#memory-budget-measured-november-17-amd64-linux-python-312-release-image).

**Before release 2:**

1. **Follow-up: done** (see above). The owner decides the memory budget (defaults: 1
   reader, 220 MB). PDF text moved to PyMuPDF, and readers keep one library family
   (both done, above).
2. **Merge into the integration branch.** Merge `feat/shared-runtime-readers` into
   `feat/shared-runtime`. Then run the full suite, and the pool tests in a Linux container.
3. **Measure on amd64 Linux.** Run in a Docker container built from the release image
   (`--platform linux/amd64`), with disposable storage and fake model credentials.
   Measure:
   - idle RSS of the whole process tree, before any read and 60 s after the last one;
   - peak RSS during a large PDF read and an xlsx read;
   - whole-tree RSS with every reader busy on a large file at once (the budget above);
   - first-read latency;
   - that a PDF exceeding the RSS cap kills only its child, and the tool call reports
     "document too large".

   Compare idle RSS with v62's (about 140 MB at idle locally). Stop and report to the
   owner if the idle cost is materially higher.
   *Done November 17* (numbers above) with the branch's `src` mounted over the v62
   image's dependencies, and an in-process read rather than a chat run; repeat the idle
   and busy checks on the real release build.
4. **Merge to `main`, then release** as in Release 1 (backup, clean worktree, amd64
   build). All new env vars are optional, with defaults listed in
   [multi-instance.md](multi-instance.md). `HANDLER_DOC_READ_MAX_CONCURRENT` now
   defaults to **1** (was 2), and `HANDLER_READER_MAX_RSS_MB` to 220.
5. **Verify.** Run Release 1's checks again. Then read a real PDF through one tenant's
   chat. Look for `run memory` logs with `run_peak_rss_mb` and `reader_peak_rss_mb`.
   Watch `heroku logs` for R14 over the following days, and compare with the period
   before.
6. **Rollback** to Release 1's version number. Record it in `HANDOFF.md` like Release 1.

## Codex review, November 16 (findings and dispositions)

| # | Finding | Disposition |
| --- | --- | --- |
| 1 | **Upgrade leaves paid tools missing.** Automation tools are registered at runtime start (`server.py` ~806). A plan change updates live limits (`tenancy/host.py` ~672), but an open SSE connection prevents the restart that would add the tools. Reproduced | **Fixed** on `feat/shared-runtime-gating` (step 3): every tool is registered on every plan, each run's tool view is built from the live plan, and the gateway refuses calls (and approvals) the current plan excludes. Tested with an upgrade and a downgrade under an open SSE stream |
| 2 | **No shared run cap for env tenants.** With the registry off, `create_host` gives tenant runtimes no `RunGate`, so each instance's own limit (`HANDLER_MAX_CONCURRENT=20`) adds up across the dyno | **Fixed** on `feat/shared-runtime-gating`: `create_host` builds one host `RunGate` and passes it to every env instance, each configured `paid` as the registry imports it. With production's settings the dyno runs at most 6 at once, 3 per instance (was 3 instances × their lane capacities, `HANDLER_MAX_CONCURRENT=20` never binding). The SQLite store gained `has_claimable`, which a gated dispatcher needs |
| 3 | **Reader pool is a release blocker**; a per-child cap needs a check across the whole tree | Agreed; Release 2 now has a memory budget for the whole tree |
| 4 | **No interactive-before-background priority across tenants.** `RunGate` has no lane | **Fixed** on `feat/shared-runtime-gating`: dispatchers pass the lane to the gate, which ranks interactive before background across tenants, then paid before free; background work waiting past `HANDLER_RUN_MAX_WAIT_SECONDS` goes first once, so it can't starve |
| 5 | **The chat client doesn't send `client_message_id`** (`hephaestus/frontend/src/api/services/handler-service.ts` ~108) | Already listed below; still open |
| — | **Call position is too weak an idempotency key** for nondeterministic retries | Agreed. The design now keys on thread, input events, tool name and argument hash, with an intent row written before the call |
| — | **90-day retention cleanup isn't implemented** | Agreed; added to step 2 |
| — | **`multi-tenant-design.md` status was stale** | Fixed |
| — | **A duplicate chat message reported a new thread** when the resend had no `conversation_id` | **Fixed** on `feat/shared-runtime-gating`: a duplicate returns the stored original's `event_id`, `thread_key` and `conversation_id` |

## Not part of these releases

- **Crontal frontend `client_message_id`** (step 0). This lives in the `hephaestus` repo,
  where another agent may be working. Not started.
- **Steps 2, 3, 5, 6 and 7 of the build order.** Steps 2 and 3 (one timer, one claim
  loop, the integration contract) come next, on top of `feat/shared-runtime` after both
  releases.
- **The tenant registry stays off** until the end of step 3 (owner decision).

## Working notes

- **Tests:**
  `cd <worktree> && PYTHONPATH=src /Users/dannyliu/dev/repos/handler/.venv/bin/python -m pytest tests/runtime -q -p no:cacheprovider`
  takes about 2 minutes. The Postgres tests start their own server from Homebrew binaries.
- **Scratch Postgres on this Mac:** `initdb` into a temp directory and start with
  `-c unix_socket_directories=''`, because long socket paths fail.
- **Production schema dumps** need `/opt/homebrew/opt/postgresql@17/bin/pg_dump`, because
  the server is 17.10. Pass credentials through `PG*` environment variables.
- **Heroku apps:** `crontal-agent` is Handler. `hephaestus-backend-test` is Crontal and
  owns the shared database. Ignore `crontal-agent-bss` and `hephaestus-web-app`.
- **Crontal needs no change** for either release.
