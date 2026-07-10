#!/usr/bin/env bash
# Technitium DNS zero-touch provisioning (WI-4.7 step 2).
#
# Drives the Technitium HTTP API (DESIGN.md §14: "every console action is an API
# call") to bring a fresh Technitium container to the fully-configured state the
# always-on box needs — with ZERO clicks in the web console:
#
#   1. Log in with the admin password from .env (Technitium sets this password on
#      first container start via DNS_SERVER_ADMIN_PASSWORD; we log in with it).
#   2. Create a non-expiring API token for headless health/config, saved to
#      provision/.token (gitignored) so later `curl` health checks need no login.
#   3. Create the local (authoritative) zone for $DOMAIN and add split-horizon
#      A records for each subdomain → $LAN_IP (DESIGN.md §10.1: home devices
#      resolve to the LAN IP and connect directly; away, public DNS answers).
#   4. Set upstream forwarders (Quad9 DoH by default) for recursion fallback.
#   5. Enable/refresh ad-blocking blocklists (same public lists as Pi-hole).
#
# IDEMPOTENT (AGENTS.md "automation-safe"): re-running is a no-op where state
# already matches — records are deleted-then-added, settings are declaratively
# set, and the token is reused if a saved one still authenticates. Safe on every
# boot; the first-boot systemd unit calls it after `docker compose up -d`.
#
# Usage:
#   provision-technitium.sh [--env PATH] [--host URL]
# Env/.env keys consumed: DOMAIN, LAN_IP, TECHNITIUM_ADMIN_PASSWORD,
#   TECHNITIUM_API_TOKEN_NAME, TECHNITIUM_FORWARDERS, TECHNITIUM_BLOCKLISTS,
#   TRACKER_SUBDOMAIN, ACTUAL_SUBDOMAIN, MAIN_BOX_IP, EXTRA_SUBDOMAINS (SR-012).
#
# Exit codes: 0 success; nonzero (with a message) on any API failure — so the
# first-boot unit is marked failed and the operator can see it, rather than the
# box coming up with silently-broken DNS.
set -euo pipefail

# ── args / config ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"
API="http://127.0.0.1:5380"
TOKEN_FILE="${SCRIPT_DIR}/.token"

while [ $# -gt 0 ]; do
    case "$1" in
        --env)  ENV_FILE="$2"; shift 2 ;;
        --host) API="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

: "${DOMAIN:?DOMAIN not set (from .env)}"
: "${LAN_IP:?LAN_IP not set (from .env)}"
: "${TECHNITIUM_ADMIN_PASSWORD:?TECHNITIUM_ADMIN_PASSWORD not set}"
TOKEN_NAME="${TECHNITIUM_API_TOKEN_NAME:-automation}"
TRACKER_SUBDOMAIN="${TRACKER_SUBDOMAIN:-tracker}"
ACTUAL_SUBDOMAIN="${ACTUAL_SUBDOMAIN:-actual}"

# ── helpers ──────────────────────────────────────────────────────────────────
# jval KEY < json  → extract a top-level string value without needing jq.
# Technitium responses are shallow JSON; this covers "status","token","errorMessage".
jval() { sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" | head -n1; }

# url VALUE → percent-encode for a query string (RFC 3986 unreserved kept as-is).
# Pure bash so it works on the minimal autoinstall host with no jq/python needed.
url() {
    local s="$1" out="" c i
    for (( i=0; i<${#s}; i++ )); do
        c="${s:$i:1}"
        case "$c" in
            [a-zA-Z0-9.~_-]) out+="$c" ;;
            *) out+="$(printf '%%%02X' "'$c")" ;;
        esac
    done
    printf '%s' "$out"
}

# api ENDPOINT [curl-args...] → echoes body; aborts if response status != "ok".
api() {
    local ep="$1"; shift
    local resp
    resp="$(curl -fsS "${API}${ep}" "$@")" || { echo "API call failed: $ep" >&2; exit 1; }
    local status; status="$(printf '%s' "$resp" | jval status)"
    if [ "$status" != "ok" ]; then
        echo "Technitium API error on $ep: $(printf '%s' "$resp" | jval errorMessage)" >&2
        echo "$resp" >&2
        exit 1
    fi
    printf '%s' "$resp"
}

# Wait for the Technitium web service to answer before doing anything.
wait_for_api() {
    local n=0
    until curl -fsS -o /dev/null "${API}/api/user/session/list" 2>/dev/null \
          || curl -fsS -o /dev/null "${API}/" 2>/dev/null; do
        n=$((n+1)); [ "$n" -ge 60 ] && { echo "Technitium API never came up on ${API}" >&2; exit 1; }
        sleep 2
    done
}

# ── 1. authenticate → get a token ────────────────────────────────────────────
# Reuse a saved token if it still authenticates (idempotent, avoids churn).
get_token() {
    if [ -f "$TOKEN_FILE" ]; then
        local saved; saved="$(cat "$TOKEN_FILE")"
        if curl -fsS "${API}/api/user/session/get?token=${saved}" 2>/dev/null | grep -q '"status":"ok"'; then
            printf '%s' "$saved"; return 0
        fi
    fi
    # Log in with the admin password, then mint a NON-expiring named API token.
    local login; login="$(api "/api/user/login?user=admin&pass=$(url "$TECHNITIUM_ADMIN_PASSWORD")&includeInfo=false")"
    local session; session="$(printf '%s' "$login" | jval token)"
    [ -n "$session" ] || { echo "login returned no session token" >&2; exit 1; }

    local mk; mk="$(api "/api/user/createToken?token=${session}&tokenName=$(url "$TOKEN_NAME")")"
    local apitoken; apitoken="$(printf '%s' "$mk" | jval token)"
    [ -n "$apitoken" ] || { echo "createToken returned no token" >&2; exit 1; }
    printf '%s' "$apitoken" > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"
    printf '%s' "$apitoken"
}

# ── 2. zone + split-horizon records ──────────────────────────────────────────
ensure_zone() {
    local tok="$1"
    # Create as a Primary (authoritative) zone. If it already exists Technitium
    # returns an error; treat "already exists" as success (idempotent).
    local resp
    resp="$(curl -fsS "${API}/api/zones/create?token=${tok}&zone=$(url "$DOMAIN")&type=Primary")" || true
    if printf '%s' "$resp" | grep -q '"status":"ok"'; then
        echo "  zone $DOMAIN created"
    elif printf '%s' "$resp" | grep -qi 'already exists'; then
        echo "  zone $DOMAIN already exists (ok)"
    else
        echo "zone create failed: $resp" >&2; exit 1
    fi
}

# add_a NAME → ensure an A record NAME.$DOMAIN → $LAN_IP exists exactly once.
# Delete-then-add makes it declarative and idempotent.
add_a() {
    local tok="$1" fqdn="$2" ip="$3"
    curl -fsS "${API}/api/zones/records/delete?token=${tok}&domain=$(url "$fqdn")&zone=$(url "$DOMAIN")&type=A&value=${ip}" >/dev/null 2>&1 || true
    api "/api/zones/records/add?token=${tok}&domain=$(url "$fqdn")&zone=$(url "$DOMAIN")&type=A&ipAddress=${ip}&ttl=300&overwrite=true" >/dev/null
    echo "  A  ${fqdn} → ${ip}"
}

# ── 3. forwarders ────────────────────────────────────────────────────────────
set_forwarders() {
    local tok="$1"
    [ -n "${TECHNITIUM_FORWARDERS:-}" ] || { echo "  forwarders: none set, leaving recursion as-is"; return 0; }
    # Space-separated → comma-separated for the API; DoH URLs imply protocol Https.
    local fwd; fwd="$(printf '%s' "$TECHNITIUM_FORWARDERS" | tr ' ' ',')"
    local proto="Udp"
    printf '%s' "$fwd" | grep -q 'https://' && proto="Https"
    api "/api/settings/set?token=${tok}&forwarders=$(url "$fwd")&forwarderProtocol=${proto}" >/dev/null
    echo "  forwarders set (${proto}): ${fwd}"
}

# ── 4. blocklists ────────────────────────────────────────────────────────────
set_blocklists() {
    local tok="$1"
    [ -n "${TECHNITIUM_BLOCKLISTS:-}" ] || { echo "  blocklists: none set"; return 0; }
    local bl; bl="$(printf '%s' "$TECHNITIUM_BLOCKLISTS" | tr ' ' ',')"
    api "/api/settings/set?token=${tok}&enableBlocking=true&blockListUrls=$(url "$bl")&blockListUrlUpdateIntervalHours=24" >/dev/null
    echo "  blocklists set + blocking enabled"
    # Trigger an immediate refresh so blocking is live without waiting a day.
    curl -fsS "${API}/api/settings/forceUpdateBlockLists?token=${tok}" >/dev/null 2>&1 || true
}

# ── main ─────────────────────────────────────────────────────────────────────
main() {
    echo "== Technitium provisioning against ${API} =="
    wait_for_api
    local TOKEN; TOKEN="$(get_token)"
    echo "token ready (saved to ${TOKEN_FILE})"

    echo "zone + split-horizon records for ${DOMAIN}:"
    ensure_zone "$TOKEN"
    add_a "$TOKEN" "${TRACKER_SUBDOMAIN}.${DOMAIN}" "$LAN_IP"
    add_a "$TOKEN" "${ACTUAL_SUBDOMAIN}.${DOMAIN}"  "$LAN_IP"
    add_a "$TOKEN" "dns.${DOMAIN}"                   "$LAN_IP"
    # Apex → LAN IP too, so the bare domain works on-LAN.
    add_a "$TOKEN" "${DOMAIN}"                       "$LAN_IP"
    # Optional: BlueMap / map on the MAIN box (only if MAIN_BOX_IP is set).
    if [ -n "${MAIN_BOX_IP:-}" ]; then
        add_a "$TOKEN" "map.${DOMAIN}" "$LAN_IP"   # served by Caddy on THIS box, proxied to main
    fi
    # Tier-2 opt-in subdomains (SR-012): bare labels from EXTRA_SUBDOMAINS in
    # .env (space/comma-separated, e.g. "vault photos music"), one A record each
    # → LAN_IP. Empty = no-op. Pairs with the commented Caddyfile sites.
    if [ -n "${EXTRA_SUBDOMAINS:-}" ]; then
        for sub in $(printf '%s' "$EXTRA_SUBDOMAINS" | tr ',' ' '); do
            add_a "$TOKEN" "${sub}.${DOMAIN}" "$LAN_IP"
        done
    fi

    echo "forwarders:"; set_forwarders "$TOKEN"
    echo "blocklists:"; set_blocklists "$TOKEN"

    echo "== provisioning complete =="
}

main "$@"
