#!/usr/bin/env bash
# Actual Budget zero-touch bootstrap (Personal open-items A9d, 2026-07-30).
#
# Direction reversal of the old flow: the server password is MINTED on the dev
# PC (FinanceActualPassword, GeneratedPassword kind) and this script sets it on
# the un-bootstrapped server at first boot — instead of a human inventing it in
# Actual's UI and re-typing it into the store. Finance-Auditor then finds
# ACTUAL_PASSWORD already correct, and browser logins to actual.<domain> read
# it back with Show-DeploySecret.ps1.
#
# Mechanism: actual-server accepts one unauthenticated
#   POST /account/bootstrap {"password": "..."}
# ONLY while un-bootstrapped; afterwards the endpoint rejects, which makes this
# script naturally idempotent. State is checked first via
#   GET /account/needs-bootstrap  ->  { data: { bootstrapped: bool } }.
# NOTE (honest-test-state): endpoint shape is from actual-server's sync-server
# source, NOT yet exercised against the pinned ACTUAL_IMAGE_TAG in the sim —
# recorded in docs/status.md; the sim run is the gate.
#
# Transport: the actual container is bridge-only (expose, no host publish) and
# its image ships NO wget/curl (WI-10.14) — but it is a Node image, so the
# calls run INSIDE the container via `docker exec node -e` + fetch, against
# its own loopback. The password travels via exec env, never argv.
#
# Exit codes: 0 = bootstrapped now, or already bootstrapped (no-op);
#             nonzero + message = server unreachable / bootstrap rejected.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"
while [ $# -gt 0 ]; do
    case "$1" in
        --env) ENV_FILE="$2"; shift 2 ;;
        *) echo "usage: provision-actual.sh [--env PATH]" >&2; exit 2 ;;
    esac
done
log() { echo "[provision-actual] $*"; }

# shellcheck disable=SC1090
# load_env_file FILE — export every KEY=VALUE in FILE **literally**.
#
# NEVER `source` a compose .env. Its values are literal text, and every
# basic_auth hash in this project is bcrypt — `$2a$14$…`. Sourcing makes bash
# expand them: under `set -u` it aborts on the unbound `$2` (which is exactly
# how first boot died), and WITHOUT `set -u` it is worse — `$2`/`$1` expand to
# nothing, the hash is silently corrupted, and auth then fails with nothing
# anywhere explaining why.
load_env_file() {
    local __f="$1" __line __k __v
    [ -f "$__f" ] || return 0
    while IFS= read -r __line || [ -n "$__line" ]; do
        case "$__line" in ''|'#'*) continue ;; esac
        case "$__line" in *=*) ;; *) continue ;; esac
        __k=${__line%%=*}
        __v=${__line#*=}
        __k=${__k#"${__k%%[![:space:]]*}"}
        __k=${__k%"${__k##*[![:space:]]}"}
        case "$__k" in ''|*[!A-Za-z0-9_]*) continue ;; esac
        case "$__v" in
            \"*\") __v=${__v#\"}; __v=${__v%\"} ;;
            \'*\') __v=${__v#\'}; __v=${__v%\'} ;;
        esac
        printf -v "$__k" '%s' "$__v" 2>/dev/null && export "$__k"
    done < "$__f"
}
load_env_file "$ENV_FILE"
if [ -z "${FINANCE_ACTUAL_PASSWORD:-}" ]; then
    log "FINANCE_ACTUAL_PASSWORD is not set in $ENV_FILE — skipping (Actual stays un-bootstrapped; its UI will ask)"
    exit 0
fi

# Wait for the server to answer inside the container (compose healthcheck only
# proves the TCP port; give the HTTP layer the same grace).
up=""
for _ in $(seq 1 30); do
    if docker exec actual node -e \
        'fetch("http://127.0.0.1:5006/account/needs-bootstrap").then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))' \
        2>/dev/null; then up=1; break; fi
    sleep 2
done
[ -n "$up" ] || { log "FATAL: actual-server not answering on its loopback :5006"; exit 1; }

state="$(docker exec actual node -e '
fetch("http://127.0.0.1:5006/account/needs-bootstrap")
  .then(r => r.json())
  .then(j => { console.log(j?.data?.bootstrapped ? "bootstrapped" : "fresh"); })
  .catch(e => { console.log("error:" + e.message); process.exit(1); })')"

case "$state" in
    bootstrapped)
        log "already bootstrapped — no-op. (If the store password does not match, someone"
        log "set it by hand: either change it in Actual's UI to the store value, or"
        log "re-run PrepDeploySecrets.ps1 -Rotate FinanceActualPassword with the UI one.)"
        exit 0 ;;
    fresh) ;;
    *) log "FATAL: needs-bootstrap check failed ($state)"; exit 1 ;;
esac

result="$(docker exec -e BOOTSTRAP_PW="$FINANCE_ACTUAL_PASSWORD" actual node -e '
fetch("http://127.0.0.1:5006/account/bootstrap", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ password: process.env.BOOTSTRAP_PW }),
})
  .then(r => r.json())
  .then(j => { console.log(j?.status === "ok" ? "ok" : "rejected:" + JSON.stringify(j)); })
  .catch(e => { console.log("error:" + e.message); process.exit(1); })')"

if [ "$result" = "ok" ]; then
    log "bootstrapped: server password set from the store (never shown; Show-DeploySecret.ps1 to view)"
    exit 0
fi
log "FATAL: bootstrap rejected — $result"
exit 1
