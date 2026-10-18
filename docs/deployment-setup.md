# Deployment Setup

## Overview

There are two handler instances:

- **Local** (`localhost:8000`) — dev machine, used for development and testing
- **Server** (`macbook-server:8000`) — centralized instance for family use

Both machines are on the same Tailscale network, so `macbook-server` resolves over Tailscale.

## Server details

- Hostname: `macbook-server`
- User: `dannyliu`
- Repo: `~/repos/handler` (note: different from local `~/dev/repos/handler`)
- Handler binary: `/Users/dannyliu/.local/share/uv/tools/handler/bin/handler`
- uv binary: `~/.local/bin/uv` (not on PATH over SSH — use full path)
- Data dir: `~/.handler/` (same as local default)
- SSH key auth is set up from the dev machine (`~/.ssh/id_ed25519`)

## Managing the server instance

```bash
# SSH in
ssh dannyliu@macbook-server

# Start/stop/status (must use full path, handler not on PATH over SSH)
ssh dannyliu@macbook-server '/Users/dannyliu/.local/share/uv/tools/handler/bin/handler start'
ssh dannyliu@macbook-server '/Users/dannyliu/.local/share/uv/tools/handler/bin/handler stop'
ssh dannyliu@macbook-server '/Users/dannyliu/.local/share/uv/tools/handler/bin/handler status'
```

## Syncing data from local to server

**Always stop handler on the server before copying the database** — SQLite file locking will cause an incomplete copy otherwise.

```bash
# Stop server, copy DB, restart
ssh dannyliu@macbook-server '/Users/dannyliu/.local/share/uv/tools/handler/bin/handler stop'
scp ~/.handler/handler.db dannyliu@macbook-server:~/.handler/handler.db
ssh dannyliu@macbook-server '/Users/dannyliu/.local/share/uv/tools/handler/bin/handler start'
```

To do a full migration (config, memory, credentials, uploads, etc.):

```bash
# Stop first — handler locks handler.db and scp -r on a running instance misses files
ssh dannyliu@macbook-server '/Users/dannyliu/.local/share/uv/tools/handler/bin/handler stop'

# Copy each subdirectory explicitly (avoid scp -r on the parent dir — nesting issues)
scp ~/.handler/handler.db dannyliu@macbook-server:~/.handler/handler.db
ssh dannyliu@macbook-server 'mkdir -p ~/.handler/memory ~/.handler/uploads ~/.handler/credentials ~/.handler/config'
scp ~/.handler/memory/* dannyliu@macbook-server:~/.handler/memory/
scp ~/.handler/uploads/* dannyliu@macbook-server:~/.handler/uploads/
scp ~/.handler/credentials/* dannyliu@macbook-server:~/.handler/credentials/
scp ~/.handler/config/identity.md ~/.handler/config/persona.md dannyliu@macbook-server:~/.handler/config/

ssh dannyliu@macbook-server '/Users/dannyliu/.local/share/uv/tools/handler/bin/handler start'
```

## Deploying a new build to the server

```bash
# Commit and push locally first, then:
ssh dannyliu@macbook-server '
  /Users/dannyliu/.local/share/uv/tools/handler/bin/handler stop &&
  cd ~/repos/handler &&
  git pull &&
  ~/.local/bin/uv tool install --reinstall --editable ~/repos/handler &&
  /Users/dannyliu/.local/share/uv/tools/handler/bin/handler start
'
```

Notes:

- Use `--reinstall` when updating the server tool. `--force` only refreshes metadata and can leave stale code in the existing tool environment.
- Use `--editable` so the installed tool imports directly from `~/repos/handler/src`. That keeps the running code aligned with the repo checkout and preserves watchdog git-based update checks.

Verify: `curl http://macbook-server:8000/api/chat -X POST -H "Content-Type: application/json" -d '{"message":"hi","conversation_id":null}'`

## Rebuild runtime (handler-rt) on macbook-server

Runs **alongside** the legacy instance, which is untouched on port 8000 and auto-updated by its watchdog from `~/repos/handler`.

| What | Where |
|---|---|
| Checkout | `~/repos/handler-rt` (branch `rebuild`, HTTPS remote) |
| Data | `~/.handler-rt/` (SQLite `handler.db`, workspaces, logs) |
| Secrets/config | `~/.handler-rt/env` (mode 600): admin key, Codex settings, users |
| Admin user token | `~/.handler-rt/danny.token` (mode 600) |
| Codex CLI | `~/.local/bin/codex` (0.162.0, x86_64-apple-darwin, sha256-verified) |
| Codex tool host | `~/.local/bin/codex-code-mode-host` (matching 0.162.0, x86_64-apple-darwin) |
| Codex login | `~/.codex/auth.json` from `codex login --device-auth`, symlinked into each run (`HANDLER_RT_CODEX_AUTH_LINK=1`) |
| Service | launchd `com.handler.rt` (`~/Library/LaunchAgents/com.handler.rt.plist`), runs `~/.handler-rt/run.sh` |
| URL | `http://macbook-server:8100` (`/login`, then paste the token) |

```bash
# status / restart / logs
ssh dannyliu@macbook-server 'launchctl print gui/$(id -u)/com.handler.rt | grep -E "state|pid ="'
ssh dannyliu@macbook-server 'launchctl kickstart -k gui/$(id -u)/com.handler.rt'
ssh dannyliu@macbook-server 'tail -50 ~/.handler-rt/logs/handler-rt.log'

# update to the latest rebuild
ssh dannyliu@macbook-server 'cd ~/repos/handler-rt && git pull --ff-only origin rebuild && ~/.local/bin/uv sync --frozen && launchctl kickstart -k gui/$(id -u)/com.handler.rt'

# users and tokens
ssh dannyliu@macbook-server 'cd ~/repos/handler-rt && .venv/bin/python -m handler.runtime.server user add <id> --data-dir ~/.handler-rt'
ssh dannyliu@macbook-server 'cd ~/repos/handler-rt && .venv/bin/python -m handler.runtime.server token create <id> --data-dir ~/.handler-rt'
```

The launchd job lives in the GUI session domain. After a reboot it starts when you log in; it doesn't start before login.

### Missing Codex tool host

The standalone Codex CLI installation also needs `codex-code-mode-host` next to
`codex`. If it is missing, agent tool calls fail before commands execute; changing
sandbox permissions does not repair the installation. Install both binaries from
the same release and for the server's architecture when upgrading.

On 2026-10-18, the missing helper on macbook-server was installed from the official
`rust-v0.162.0` release asset `codex-code-mode-host-x86_64-apple-darwin.tar.gz`.
The archive's SHA-256 was verified against the release asset digest:
`a7e206e3177745467ac7cf2d43b8ff93134dfc955c1cb46529600e1a34213349`.

Verify the helper without making a model call:

```bash
ssh dannyliu@macbook-server 'cd ~/repos/handler-rt && ~/.local/bin/codex sandbox -c '\''sandbox_mode="workspace-write"'\'' -- ~/.local/bin/codex-code-mode-host --help'
```

The helper launch and a source-file read both passed under `workspace-write` after
the repair; `/healthz` returned `{"ok":true}`. An agent turn still needs to be
retried to verify the full model/tool round trip.
