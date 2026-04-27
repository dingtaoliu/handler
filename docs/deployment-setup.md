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
  ~/.local/bin/uv tool install . --force &&
  /Users/dannyliu/.local/share/uv/tools/handler/bin/handler start
'
```

Verify: `curl http://macbook-server:8000/api/chat -X POST -H "Content-Type: application/json" -d '{"message":"hi","conversation_id":null}'`
