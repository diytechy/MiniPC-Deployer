#!/usr/bin/env bash
# Headless health check for the whole box, runnable from ANOTHER machine or on
# the box itself (WI-4.7 step 6 "how to verify"). Uses only curl + dig.
#
# Checks, each printed PASS/FAIL, nonzero exit if any fails:
#   - Technitium API is up (uses the saved automation token if present).
#   - Technitium resolves the split-horizon record for tracker.$DOMAIN → $LAN_IP.
#   - tracker /api/today returns JSON.
#   - Actual Budget answers.
#
# Usage: healthcheck.sh [--env PATH] [--dns HOST] [--api-host URL]
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"; DNS_HOST="127.0.0.1"; API="http://127.0.0.1:5380"
while [ $# -gt 0 ]; do case "$1" in
    --env) ENV_FILE="$2"; shift 2;; --dns) DNS_HOST="$2"; shift 2;;
    --api-host) API="$2"; shift 2;; *) echo "unknown arg $1" >&2; exit 2;; esac; done
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
            # UNQUOTED: strip a trailing ` # comment`, exactly as shell
            # sourcing and docker compose's own .env parser both do. Without
            # this, LAN_IP=0.0.0.0 followed by an explanatory comment reached
            # Technitium's API as part of the address and curl rejected the
            # URL. A `#` with no space before it is kept — it may be part of a
            # password.
            *) __v=${__v%%[[:space:]]#*}
               __v=${__v%"${__v##*[![:space:]]}"} ;;
        esac
        # Compose stores a literal '$' as '$$' (see compose_escape) because it
        # interpolates .env values. Collapse it back, so a script reading this
        # file sees exactly what the containers receive.
        __v=${__v//\$\$/\$}
        printf -v "$__k" '%s' "$__v" 2>/dev/null && export "$__k"
    done < "$__f"
}
load_env_file "$ENV_FILE"
: "${DOMAIN:?DOMAIN not set}"; : "${LAN_IP:?LAN_IP not set}"
TRACKER_SUBDOMAIN="${TRACKER_SUBDOMAIN:-tracker}"; ACTUAL_SUBDOMAIN="${ACTUAL_SUBDOMAIN:-actual}"
fails=0
ok()   { echo "PASS $1"; }
bad()  { echo "FAIL $1"; fails=$((fails+1)); }

# 1. Technitium API
TOKEN=""; [ -f "${SCRIPT_DIR}/.token" ] && TOKEN="$(cat "${SCRIPT_DIR}/.token")"
if curl -fsS -o /dev/null "${API}/api/dashboard/stats/get?token=${TOKEN}" 2>/dev/null \
   || curl -fsS -o /dev/null "${API}/" 2>/dev/null; then
    ok "Technitium API reachable (${API})"
else bad "Technitium API unreachable (${API})"; fi

# 2. split-horizon resolution
if command -v dig >/dev/null 2>&1; then
    got="$(dig +short @"${DNS_HOST}" "${TRACKER_SUBDOMAIN}.${DOMAIN}" A | head -n1)"
    if [ "$got" = "$LAN_IP" ]; then ok "dig ${TRACKER_SUBDOMAIN}.${DOMAIN} @${DNS_HOST} → ${got}"
    else bad "dig ${TRACKER_SUBDOMAIN}.${DOMAIN} @${DNS_HOST} → '${got}' (expected ${LAN_IP})"; fi
else echo "SKIP dig not installed"; fi

# 3. tracker
#
# THIS CHECK COULD NOT PASS ON A CORRECTLY BUILT HUB, and it spent every run
# saying the tracker was broken while the tracker was fine. Both probes it used
# are refused BY DESIGN, and each refusal is something another check asserts as
# a PASS:
#
#   http://LAN_IP:8787/api/today
#       :8787 is bridge-only and deliberately NOT published on the host (D2).
#       verify-hub.sh asserts exactly that: "tracker :8787 is not published on
#       the host (bridge-only, as D2 requires)". Connection refused is the
#       CORRECT result, so this probe can never succeed.
#
#   https://tracker.DOMAIN/api/today
#       goes through Caddy to oauth2-proxy, which answers an unauthenticated
#       request with 403 and its own sign-in page. `curl -f` treats 403 as a
#       failure. verify-hub.sh asserts that 403 as a PASS too — it is the
#       authentication gate doing its job.
#
# So the composite was `impossible OR impossible`, and hardening either layer
# made it worse. Measured 2026-08-09: run 20260809-011324 and 20260809-111037
# both reported "tracker /api/today unreachable" while `docker ps` showed
# `tracker Up (healthy)` and the full verify suite reported every container
# green. It was read as a firstboot-with-no-network symptom for two runs; the
# second run had a network throughout and failed identically, which is what
# ruled that out.
#
# ASK THE QUESTION THAT IS ANSWERABLE. Docker already probes this container's
# own /api/today on the bridge network — that is what its healthcheck does, from
# inside, where the port is reachable and no auth gate stands in front. Read that
# verdict rather than inventing a second probe from a vantage point the design
# forbids. Falls back to `running` for an image that declares no healthcheck, so
# this reports "up but unprobed" instead of silently passing.
_tracker_state="$(docker inspect -f '{{.State.Health.Status}}' tracker 2>/dev/null || true)"
if [ "$_tracker_state" = "healthy" ]; then
    ok "tracker is healthy (docker's own /api/today probe, on the bridge network)"
elif [ -z "$_tracker_state" ] || [ "$_tracker_state" = "<no value>" ]; then
    if [ "$(docker inspect -f '{{.State.Running}}' tracker 2>/dev/null || echo false)" = "true" ]; then
        echo "WARN tracker is running but declares no healthcheck — not probed"
    else bad "tracker is not running"; fi
else bad "tracker healthcheck is '${_tracker_state}' (docker probes /api/today from inside the bridge network)"; fi

# 4. actual (behind Caddy)
if curl -fksS -o /dev/null "https://${ACTUAL_SUBDOMAIN}.${DOMAIN}/" 2>/dev/null; then
    ok "Actual Budget responds"
else echo "WARN Actual not reachable via https://${ACTUAL_SUBDOMAIN}.${DOMAIN}/ (may need LAN DNS)"; fi

echo "----"; [ "$fails" -eq 0 ] && { echo "ALL CHECKS PASSED"; exit 0; } || { echo "$fails CHECK(S) FAILED"; exit 1; }
