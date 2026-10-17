#!/bin/sh
# Container entrypoint: build the `handler-rt serve` command from the environment.
#
#   PORT                 listen port (Heroku sets it; default 8100)
#   DATABASE_URL         Postgres URL; unset falls back to SQLite under the data dir
#   HANDLER_DB_SCHEMA    Postgres schema (default handler_crontal)
#   HANDLER_RT_DATA_DIR  scratch directory (default /tmp/handler, ephemeral by design)
#   HANDLER_RT_MODEL     default model (read by handler-rt itself)
#   CODEX_API_KEY        read by Codex from the inherited environment
#   HANDLER_ENTRYPOINT_DRY_RUN=1  print the command, one argument per line, and exit
set -eu

: "${PORT:=8100}"
: "${HANDLER_DB_SCHEMA:=handler_crontal}"
: "${HANDLER_RT_DATA_DIR:=/tmp/handler}"
: "${HANDLER_MAX_CONCURRENT:=1}"
export HANDLER_RT_DATA_DIR HANDLER_MAX_CONCURRENT

set -- handler-rt serve --host 0.0.0.0 --port "$PORT" --data-dir "$HANDLER_RT_DATA_DIR"
if [ -n "${DATABASE_URL:-}" ]; then
    set -- "$@" --database-url "$DATABASE_URL" --db-schema "$HANDLER_DB_SCHEMA"
fi

if [ -z "${CODEX_API_KEY:-}" ]; then
    echo "entrypoint: warning: CODEX_API_KEY is not set; Codex runs will fail to authenticate" >&2
fi

if [ "${HANDLER_ENTRYPOINT_DRY_RUN:-}" = "1" ]; then
    printf '%s\n' "$@"
    exit 0
fi

mkdir -p "$HANDLER_RT_DATA_DIR"
exec "$@"
