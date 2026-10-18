# Deploying handler-rt to Heroku (`crontal-agent`)

One Basic dyno, running the container built from the repo-root `Dockerfile`
(`heroku.yml` selects it). Crontal's Postgres is shared, with handler confined
to its own schema (`handler_crontal`); Crontal's Bucketeer is shared, with
handler under its own key prefix. Design: `docs/agent-runtime-design.md` §10.1.

These are steps for a human (or the driver) to run later. Nothing here has been run.
Get the owner's approval before creating resources or deploying, and use a dedicated
OpenAI API key chosen by the owner. Never copy a personal ChatGPT login into the image.

## 1. Create the app on the container stack

```bash
heroku create crontal-agent
heroku stack:set container -a crontal-agent
```

## 2. Attach Crontal's add-ons (shared, no new cost)

The add-ons belong to `hephaestus-backend-test`; attach them, don't create new ones.

```bash
heroku addons -a hephaestus-backend-test          # note the add-on names
heroku addons:attach <postgres-addon-name>  -a crontal-agent --as DATABASE
heroku addons:attach <bucketeer-addon-name> -a crontal-agent --as BUCKETEER
```

`--as DATABASE` makes `DATABASE_URL` available, which the entrypoint reads. Essential-1
Postgres allows ~20 connections shared with Crontal; handler's pool is capped at 5.

## 3. Config vars

```bash
heroku config:set -a crontal-agent \
  CODEX_API_KEY=<openai key for Codex> \
  HANDLER_ADMIN_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') \
  HANDLER_DB_SCHEMA=handler_crontal \
  HANDLER_RT_MODEL=<model name> \
  HANDLER_FILE_STORE=s3 \
  HANDLER_FILE_PREFIX=handler/crontal/ \
  HANDLER_CODEX_SANDBOX=danger-full-access
```

- `CODEX_API_KEY`: Codex reads it from the environment; no `codex login` is needed.
  The native `openai_agents` harness uses the same key (or `OPENAI_API_KEY`).
- `HANDLER_DEFAULT_HARNESS=openai_agents` (recommended here): every thread type uses the
  native Agents SDK harness, which runs in-process (no Codex subprocess per run, far less
  memory) and has no shell, only handler's tools and workspace file tools. Applied once,
  on a fresh instance; afterwards change it per thread type in Settings → Threads, which
  takes effect for new threads without a restart. Codex stays installed as an option.
- `HANDLER_ADMIN_KEY`: the instance admin key (package L). Crontal's backend uses it with
  `X-Handler-User` to act for users. Keep it in Crontal's own config as well.
- `HANDLER_DB_SCHEMA`: defaults to `handler_crontal` if unset. Created on first start.
- `HANDLER_FILE_STORE=s3` enables durable workspace and session storage (package N).
  Bucketeer provides `BUCKETEER_*` vars itself; `HANDLER_FILE_PREFIX` keeps handler's
  objects apart from Crontal's.
- `HANDLER_CODEX_SANDBOX=danger-full-access`: Codex's own sandbox (bubblewrap) needs
  unprivileged user namespaces, which unprivileged containers like Heroku dynos don't
  allow (see `docs/rebuild/known-issues.md` §2). On the first deploy, confirm with
  `heroku run -a crontal-agent -- codex sandbox -c 'sandbox_mode="workspace-write"' -- true`:
  if it fails with a namespace error, keep `danger-full-access`. The dyno is then the
  isolation boundary. Secrets stay protected because the harness passes Codex only an
  allowlisted environment, and Codex's shell policy strips key/token/secret variables
  from the commands the agent runs. Acceptable for the admin-only pilot; revisit before
  non-admin users.
- Optional: `HANDLER_RT_EFFORT` (default `low`), `HANDLER_RT_USERS`.

`PORT` is set by Heroku; the entrypoint binds `0.0.0.0:$PORT`.

## 4. Deploy

Either build locally and push the image:

```bash
heroku container:login
heroku container:push web -a crontal-agent
heroku container:release web -a crontal-agent
```

or let Heroku build from `heroku.yml` (needs the `container` stack):

```bash
git push heroku <branch>:main        # after: heroku git:remote -a crontal-agent
```

Build locally for amd64 on Apple Silicon: `docker build --platform linux/amd64 -t handler-rt .`

## 5. Verify

```bash
curl https://crontal-agent-<id>.herokuapp.com/healthz     # {"ok": true} (added by package L)
heroku logs --tail -a crontal-agent                       # JSON log lines, one per event
heroku ps -a crontal-agent
heroku run -a crontal-agent codex --version               # expect codex-cli 0.162.0
```

## 6. Create the first admin user

```bash
heroku run -a crontal-agent handler-rt user add <user_id> --admin
heroku run -a crontal-agent handler-rt token create <user_id> --label first
```

The token is printed once. (CLI from package L; these use the same `DATABASE_URL` and
schema as the dyno via the config vars.)

## Operating notes

- **Memory**: a Basic dyno has 512 MB. Handler plus a running Codex process can approach it.
  The container defaults `HANDLER_MAX_CONCURRENT=1`, a global cap across both
  interactive and background lanes. Lane capacities remain configurable in settings;
  restart after changing limits. Watch `heroku logs` for `R14`/`R15` errors, and move to Standard-2X if needed.
- **Ephemeral disk**: the data dir is `/tmp/handler` and is wiped on every restart and at
  least daily. Durable state is in Postgres and Bucketeer only.
- **Sandbox**: Codex's `workspace-write` sandbox may not work inside a Heroku dyno
  (no user namespaces). If runs fail with sandbox errors, that needs a harness decision.
- **Logs**: `heroku logs --tail -a crontal-agent`, filter with `--source app`.
- **Rollback**: `heroku releases -a crontal-agent` then `heroku rollback v<N> -a crontal-agent`.
  Tables use `CREATE TABLE IF NOT EXISTS`, so schema is applied automatically at start.
- **Isolation**: handler only touches the `handler_crontal` schema. Do not point it at an
  empty `search_path` or the `public` schema.
- **Moving out later**: `pg_dump -n handler_crontal`, copy the bucket prefix, change config vars.
