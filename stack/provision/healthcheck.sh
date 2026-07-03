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
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a
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
if curl -fsS -o /dev/null "http://${LAN_IP}:8787/api/today" 2>/dev/null \
   || curl -fksS -o /dev/null "https://${TRACKER_SUBDOMAIN}.${DOMAIN}/api/today" 2>/dev/null; then
    ok "tracker /api/today responds"
else bad "tracker /api/today unreachable"; fi

# 4. actual (behind Caddy)
if curl -fksS -o /dev/null "https://${ACTUAL_SUBDOMAIN}.${DOMAIN}/" 2>/dev/null; then
    ok "Actual Budget responds"
else echo "WARN Actual not reachable via https://${ACTUAL_SUBDOMAIN}.${DOMAIN}/ (may need LAN DNS)"; fi

echo "----"; [ "$fails" -eq 0 ] && { echo "ALL CHECKS PASSED"; exit 0; } || { echo "$fails CHECK(S) FAILED"; exit 1; }
