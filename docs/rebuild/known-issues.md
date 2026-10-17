# Known issues

Open problems found during the rebuild, with the evidence and the options considered. Remove an entry when it's fixed and reference the commit.

---

## 1. Workspace sync never deletes remote files

**Where**: `runtime/workspaces.py` (`WorkspaceSync.persist` / `restore`) on top of `FileStore.sync_up` / `sync_down` in `runtime/files.py`.

**What happens**: `persist(slug)` copies a sanitized snapshot of the thread's workspace and harness home and calls `sync_up(snapshot, prefix)`. `sync_up` uploads every file in the snapshot, and nothing else. It never lists the remote prefix or deletes keys whose local file is gone. So a file the agent deletes locally stays in the file store indefinitely.

**Why it matters**:
- **Deleted files come back.** `restore(slug)` downloads everything under the prefix when the local directory is missing, for example after a Heroku dyno restart. Files the agent deliberately deleted reappear in its workspace, and their stale contents can mislead later runs. Leftover harness session files are the risky case, since they can confuse session resume.
- **Storage only grows.** Bucketeer usage grows with every file a thread ever produced, including temporary files, build outputs and downloaded documents. Nothing is ever reclaimed, even when a thread is finished.
- **Sensitive files linger.** A file that held sensitive data and was deleted on purpose (an exported report, a downloaded attachment) remains in the bucket. The same goes for a file that slipped past the credential filename exclusions before they were tightened: it persists until someone notices.

**Options**:
1. **Manifest-based sync.** After uploading, list the remote prefix and delete keys not present in the snapshot, scoped strictly to `workspaces/<slug>/` and `homes/<slug>/`. This is the smallest change, but a half-finished persist (crash between upload and delete) can leave a mixed state, and the S3 list plus deletes cost extra requests on every run.
2. **Snapshot archives (recommended).** Persist each location as one compressed archive per run, e.g. `workspaces/<slug>/snapshot-<run_id>.tar.zst`, and record the latest snapshot key on the thread (or in a small `latest` pointer object written last). Restore reads only the latest snapshot.
   - Each upload is atomic, so there's no mixed state.
   - Deletes are naturally honored.
   - Old snapshots can be pruned by keeping the last N per thread.
   - It also gives point-in-time rollback of a workspace for free.
   - Costs one object per run instead of one per file, which is cheaper on S3.
   - Workspaces are small, so archiving each run is cheap.
3. **Retention policy only** (an S3 lifecycle rule or a periodic sweep of threads with no recent runs). This complements 1 or 2 but doesn't fix resurrection.

**Also**: decide what happens to a thread's files when the thread is deleted or archived (nothing deletes them today), and add a test showing that a file deleted locally does not come back after `persist` → wipe local → `restore`.

---

## 2. Codex's Linux sandbox does not run in unprivileged containers (likely including Heroku)

**Tested 2026-10-17** with the package M image (`codex-cli 0.162.0`, `linux/amd64`, Docker Desktop kernel `6.12.76-linuxkit`), running `codex sandbox -c 'sandbox_mode="workspace-write"' -- <probe>`. This invokes the sandbox directly with no model call. The probe writes inside the workspace, writes to a separate mount outside it, and makes an HTTPS request.

| Image / container profile | Result |
|---|---|
| Original image, default Docker (uid 1000) | Panic: `bubblewrap is unavailable: no system bwrap was found on PATH and no bundled codex-resources/bwrap binary was found next to the Codex executable`. **Every sandboxed command would fail.** |
| Image + `apt-get install bubblewrap` (0.12.0), default Docker | `bwrap: No permissions to create a new namespace … the kernel does not allow non-privileged user namespaces` |
| Same, Heroku-like (`--cap-drop ALL --security-opt no-new-privileges`) | Same error |

Without the sandbox, the same probe could write anywhere the user can and reach the network. That's the baseline the sandbox would have restricted.

**Conclusions**:
- Codex 0.162's Linux sandbox is bubblewrap-based and needs unprivileged user namespaces. Docker's default security profile already denies them, and Heroku dynos are unprivileged containers, so `workspace-write` will almost certainly fail on Heroku too. **Confirm with a one-off dyno** (`heroku run`) on the first deployment before relying on this.
- The Dockerfile now installs `bubblewrap`, so the image works on hosts that do allow user namespaces (VMs, Fly.io-style microVMs, Linux hosts with the setting enabled). It does not fix unprivileged containers.

**Options for the hosted profile**:
1. **Container as the boundary**: run Codex with `-s danger-full-access` inside the dyno and harden around it. Acceptable for the first, **admin-only** instance if made explicit:
   - **Scrub what the agent's commands can see.** Use Codex's `shell_environment_policy` (e.g. `inherit="none"` plus an allowlist) so spawned commands never see `CODEX_API_KEY`. Never pass `DATABASE_URL`, `HANDLER_ADMIN_KEY` or Bucketeer credentials to the Codex process at all; the harness env must be an explicit allowlist.
   - **Accept the residual risks:**
     - commands can read other threads' workspaces in the same dyno, a privacy issue between members;
     - commands have open network access;
     - a destructive command only damages the ephemeral dyno.
   - A hosted-profile setting (e.g. `HANDLER_CODEX_SANDBOX=danger-full-access`) should select this explicitly. **Don't silently fall back** when `workspace-write` fails.
2. **Remote sandbox** (design §8 "remote" backend): run the harness (or its commands) in a managed sandbox provider. Proper isolation, but more work and another vendor.
3. **A host that allows user namespaces**: a VM or microVM platform instead of Heroku containers. `workspace-write` then works as-is.
4. **Tools-only threads**: for thread types that don't need shell or file work (e.g. Crontal notifications handled purely through handler and Crontal MCP tools), disable Codex's shell, if Codex exposes a reliable switch for that (needs checking).

Decide before onboarding non-admin users. Option 1 is reasonable for the admin-only pilot, as long as the environment allowlist is implemented and tested.

**Status (2026-10-17):** option 1 is implemented: `--codex-sandbox` / `HANDLER_CODEX_SANDBOX`, an environment allowlist in `harness/codex.py`, and a Codex `shell_environment_policy` stripping key/token/secret variables. **Still to verify live:** that a real Codex run's shell commands don't see variables matching the exclude patterns (the check needs Codex quota), and the `heroku run` sandbox check on the first deploy.
