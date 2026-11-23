#!/usr/bin/env bash
# Switch the tenant registry on (runbook stage 1), check it, or switch it off again.
#
#   deploy/registry-switch-on.sh check      # before: env-defined instances answer
#   deploy/registry-switch-on.sh on         # set keys, merchant ids and the switch (one restart)
#   deploy/registry-switch-on.sh verify     # after: imported tenants, keys, isolation
#   deploy/registry-switch-on.sh off        # rollback: unset HANDLER_TENANT_REGISTRY
#
# HANDLER_APP (default crontal-agent) is the Heroku app; HANDLER_URL its public origin.
# With HANDLER_APP=local, values come from this shell's environment instead (a local
# rehearsal against `handler-rt serve`), and `on`/`off` only print what they would set.
#
# Secrets never reach a file, an argument or the terminal: they are read into shell
# variables (heroku config:get), sent to curl on stdin (`-H @-`, `--data @-`), and the
# Heroku API is called with the netrc `heroku login` writes. Generated keys are made
# by Python's `secrets` and go straight into the PATCH body.
set -euo pipefail

APP="${HANDLER_APP:-crontal-agent}"
URL="${HANDLER_URL:?set HANDLER_URL, e.g. https://crontal-agent.herokuapp.com}"
URL="${URL%/}"
# Runbook stage 1: main is merchant 2, bss 1, yeup 298.
MERCHANTS="${HANDLER_MERCHANTS:-main=2,bss=1,yeup=298}"

config_get() {  # config_get NAME -> value on stdout (captured by the caller, never shown)
  if [ "$APP" = local ]; then printenv "$1" || true
  else heroku config:get "$1" -a "$APP"; fi
}

admin_key_var() {  # the variable holding an instance's admin key
  if [ "$1" = main ]; then echo HANDLER_ADMIN_KEY
  else echo "HANDLER_I_$(echo "$1" | tr '[:lower:]' '[:upper:]')__HANDLER_ADMIN_KEY"; fi
}

prefix() { if [ "$1" = main ]; then echo ""; else echo "/i/$1"; fi; }

# status KEY PATH -> HTTP status of GET PATH with "Authorization: Bearer KEY" (from stdin)
status() {
  printf 'Authorization: Bearer %s\n' "$1" |
    curl -s -o /dev/null -w '%{http_code}' -H @- "$URL$2"
}

expect() {  # expect WANT GOT WHAT
  if [ "$1" = "$2" ]; then echo "ok    $3 ($2)"
  else echo "FAIL  $3: expected $1, got $2"; FAILED=1; fi
}

instances() { config_get HANDLER_INSTANCES | tr ',' ' '; }

check_instances() {  # each instance answers with its own key only
  local names name other key
  names="$(instances)"
  for name in $names; do
    expect 200 "$(curl -s -o /dev/null -w '%{http_code}' "$URL$(prefix "$name")/healthz")" \
      "$name healthz"
    key="$(config_get "$(admin_key_var "$name")")"
    expect 200 "$(status "$key" "$(prefix "$name")/api/me")" "$name admin key on $name"
    for other in $names; do
      [ "$other" = "$name" ] && continue
      expect 401 "$(status "$key" "$(prefix "$other")/api/me")" "$name admin key on $other"
    done
  done
  expect 200 "$(status "$(config_get HANDLER_ADMIN_KEY)" /i/main/api/me)" "main at /i/main"
  expect 404 "$(curl -s -o /dev/null -w '%{http_code}' "$URL/i/nope/healthz")" "unknown instance"
}

patch_config() {  # JSON body on stdin -> PATCH the app's config vars
  if [ "$APP" = local ]; then
    python3 -c 'import json,sys; print("would set:", sorted(json.load(sys.stdin)))'
    return
  fi
  curl -sf -n -X PATCH "https://api.heroku.com/apps/$APP/config-vars" \
    -H "Accept: application/vnd.heroku+json; version=3" \
    -H "Content-Type: application/json" --data @- >/dev/null
  echo "config set on $APP (the dyno restarts)"
}

FAILED=0
case "${1:-}" in
  check)
    check_instances
    ;;
  on)
    if [ "$(config_get HANDLER_TENANT_REGISTRY)" = 1 ]; then
      echo "HANDLER_TENANT_REGISTRY is already 1"; exit 1
    fi
    # Keep existing keys (a re-run must never change the sealing key: the registry's
    # secrets would become unreadable and startup would stop).
    PLATFORM_KEY="$(config_get HANDLER_PLATFORM_KEY)"
    SEALING_KEY="$(config_get HANDLER_TENANT_SECRET_KEY)"
    MERCHANTS="$MERCHANTS" PLATFORM_KEY="$PLATFORM_KEY" SEALING_KEY="$SEALING_KEY" python3 -c '
import json, os, secrets
body = {"HANDLER_TENANT_REGISTRY": "1",
        "HANDLER_PLATFORM_KEY": os.environ["PLATFORM_KEY"] or secrets.token_urlsafe(48),
        "HANDLER_TENANT_SECRET_KEY": os.environ["SEALING_KEY"] or secrets.token_urlsafe(48)}
for pair in os.environ["MERCHANTS"].split(","):
    name, merchant = pair.split("=")
    body[f"HANDLER_I_{name.upper()}__HANDLER_MERCHANT_ID"] = merchant
print(json.dumps(body))' | patch_config
    echo "Give Crontal the same platform key (stage 2), from stdin, e.g.:"
    echo "  heroku config:get HANDLER_PLATFORM_KEY -a $APP | (read -r k; printf '{\"HANDLER_PLATFORM_KEY\":\"%s\"}' \"\$k\") | curl -sf -n -X PATCH https://api.heroku.com/apps/hephaestus-backend-test/config-vars -H 'Accept: application/vnd.heroku+json; version=3' -H 'Content-Type: application/json' --data @- >/dev/null"
    ;;
  verify)
    PLATFORM_KEY="$(config_get HANDLER_PLATFORM_KEY)"
    LISTED="$(printf 'Authorization: Bearer %s\n' "$PLATFORM_KEY" |
      curl -sf -H @- "$URL/platform/tenants")"
    MERCHANTS="$MERCHANTS" LISTED="$LISTED" python3 -c '
import json, os, sys
want = dict(pair.split("=") for pair in os.environ["MERCHANTS"].split(","))
tenants = {t["name"]: t for t in json.loads(os.environ["LISTED"])}
bad = 0
for name, merchant in want.items():
    t = tenants.get(name)
    good = (t is not None and t["merchant_id"] == int(merchant) and t["plan"] == "paid"
            and t["enabled"])
    bad |= not good
    print(("ok   " if good else "FAIL ") + f" tenant {name}: " + (json.dumps(
        {k: t[k] for k in ("merchant_id", "plan", "enabled", "schema", "s3_prefix",
                           "integrations")}) if t else "missing"))
sys.exit(bad)' || FAILED=1
    expect 401 "$(status not-the-platform-key /platform/tenants)" "platform API without its key"
    check_instances
    ;;
  off)
    printf '{"HANDLER_TENANT_REGISTRY": null}' | patch_config
    echo "Registry off: env-defined instances again (HANDLER_INSTANCES and HANDLER_I_* are"
    echo "read as before; tenants made through the platform API wait, data intact)."
    ;;
  *)
    sed -n '2,16p' "$0"; exit 2
    ;;
esac
exit "$FAILED"
