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
2. When the auto-update interval elapses, watchdog checks the git remote for newer release tags
3. If a newer release tag exists and the repo worktree is clean, watchdog fetches tags, checks out the latest release, runs `pip install -e .`, verifies `import handler.__main__`, and restarts the handler onto that release
4. If release validation fails, watchdog restores the previous checkout and falls back to rollback behavior if needed
5. Watchdog reads `data/handler.pid` and checks if the process is alive
6. If dead: tests `python -c "import handler"` to verify code health
7. If import fails: rolls back to `handler-stable` git tag (or HEAD)
8. Restarts handler via `python -m handler` in background

## Auto-update mode

The first auto-update implementation tracks published git release tags only.

- Config lives in `data/scheduler.json` under `auto_update`
- Default remote: `origin`
- Default interval: every 6 hours
- Default mode: `release-tag`
- If the repo has local tracked or untracked changes, auto-update skips the check rather than overwriting work
- If the checkout is on an untagged commit, release mode still moves the machine to the latest published release tag once one exists

## Rollback strategy

- Guarded edit tools create git checkpoints before modifying handler source files
- Rollback still prefers an existing `handler-stable` git tag if one is present
- On import failure, watchdog checks out that tag, cleaning up `actions_custom/` and `pyproject.toml`
- If no stable tag exists, falls back to `git checkout HEAD -- handler/`

## Auto-configuration

At boot, `__main__.py` detects available scheduler backends and auto-installs the watchdog. Config is saved to `data/scheduler.json`.

## Related

`tools/watchdog.py` provides `get_health_problems()` — a plain function (not a tool) that checks process liveness, watchdog status, and recent error logs. Called by `AgentContext.build()` to inject health warnings into the system prompt when problems are detected.
