# Deployment Setup

## Overview

There are two handler instances:

- **Local** (`localhost:8000`) — dev machine, used for development and testing
- **Server** (`macbook-server:8000`) — centralized instance for family use

Both machines are on the same Tailscale network, so `macbook-server` resolves over Tailscale.

## Server details

- Hostname: `macbook-server`
- User: `dannyliu`
- Handler binary: `/Users/dannyliu/.local/share/uv/tools/handler/bin/handler`
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

To do a full migration (config, memory, credentials, etc.):

```bash
ssh dannyliu@macbook-server '/Users/dannyliu/.local/share/uv/tools/handler/bin/handler stop'
scp -r ~/.handler/ dannyliu@macbook-server:~/.handler/
ssh dannyliu@macbook-server '/Users/dannyliu/.local/share/uv/tools/handler/bin/handler start'
```

## Testing a new handler build on the server

1. Deploy the new build to the server (e.g. via `uv tool install` or `pip install`)
2. Stop handler, copy updated data if needed, restart
3. Hit `http://macbook-server:8000` from any Tailscale-connected device to verify
