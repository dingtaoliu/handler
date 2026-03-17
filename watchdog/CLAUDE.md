# Watchdog

External liveness probe that ensures the handler process stays running. Runs outside the handler process via the system scheduler (launchd, systemd, or crontab).

## Design

The watchdog is intentionally decoupled from the handler runtime. It must work even when the handler is crashed or has broken imports. It does NOT import handler modules — it tests importability as a separate subprocess.

## Modules

| File | Purpose |
|---|---|
| `core.py` | PID checks, process restart, import testing, git rollback |
| `backends.py` | Platform-specific scheduler installation (launchd, systemd, crontab, windows) |

## How it works

1. System scheduler runs `python -m handler.watchdog` every 60 seconds
2. Watchdog reads `data/handler.pid` and checks if the process is alive
3. If dead: tests `python -c "import handler"` to verify code health
4. If import fails: rolls back to `handler-stable` git tag (or HEAD)
5. Restarts handler via `python -m handler` in background

## Rollback strategy

- `mark_stable()` (in `actions/selfmod.py`) creates a `handler-stable` git tag after verified changes
- On import failure, watchdog checks out that tag, cleaning up `actions_custom/` and `pyproject.toml`
- If no stable tag exists, falls back to `git checkout HEAD -- handler/`

## Auto-configuration

At boot, `__main__.py` detects available scheduler backends and auto-installs the watchdog. Config is saved to `data/scheduler.json`.

## Related

`actions/watchdog.py` provides `get_health_problems()` — a plain function (not a tool) that checks process liveness, watchdog status, and recent error logs. Called by `AgentContext.build()` to inject health warnings into the system prompt when problems are detected.
