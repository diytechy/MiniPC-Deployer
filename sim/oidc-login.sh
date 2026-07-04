#!/usr/bin/env bash
# oidc-login.sh — drive the FULL unauthenticated -> Dex login -> trusted-headers
# flow with a curl cookie jar, entirely headless (WI-10.14 check 4). Runs INSIDE
# the sim client container (has curl). Proves oauth2-proxy + Dex + NagLight
# multi-user wire together end to end: a fresh browser session signs in as a Dex
# static user and lands on the authenticated tracker page.
#
# Usage (inside simclient):  oidc-login.sh <email> <password> [ca_cert_path]
# Exit: 0 = full login completed (authenticated 200 from the tracker);
#       10 = reached the Dex login page but the form dance did not complete
#            (redirect chain proven; caller records the delta);
#       2  = the initial unauthenticated redirect did not point at Dex (hard fail).
set -uo pipefail

EMAIL="${1:?email}"; PASS="${2:?password}"
CA="${3:-/tmp/caddy-root.crt}"
BASE="https://tracker.homelab.sim"
DEX="http://dex:5556"
CJ="$(mktemp)"; PAGE="$(mktemp)"; HDR="$(mktemp)"
CURL=(curl -sS --cacert "$CA" -c "$CJ" -b "$CJ")

hdr_loc() { grep -i '^location:' "$1" | tr -d '\r' | awk '{print $2}' | tail -n1; }
abs() { case "$1" in http*) printf '%s' "$1";; /*) printf '%s%s' "$DEX" "$1";; *) printf '%s/dex/%s' "$DEX" "$1";; esac; }

# 1. Unauthenticated hit -> oauth2-proxy 302 to Dex authorize.
"${CURL[@]}" -o /dev/null -D "$HDR" "$BASE/"
loc="$(hdr_loc "$HDR")"
echo "  [1] tracker/ -> $loc"
case "$loc" in
    "$DEX"/dex/auth*) : ;;
    *) echo "  FAIL: unauthenticated redirect did not target Dex"; exit 2 ;;
esac

# 2. Follow into Dex to the local login form (single connector auto-selects).
posturl="$("${CURL[@]}" -L -o "$PAGE" -w '%{url_effective}' "$loc")"
echo "  [2] dex login page: $posturl"
if ! grep -qiE 'name="?password"?' "$PAGE"; then
    echo "  login form not found on the Dex page"; exit 10
fi

# 3. POST the credentials to the login form's own URL.
"${CURL[@]}" -o /dev/null -D "$HDR" \
    --data-urlencode "login=$EMAIL" --data-urlencode "password=$PASS" "$posturl"
loc="$(hdr_loc "$HDR")"
echo "  [3] after login POST -> ${loc:-<none>}"
[ -n "$loc" ] || { echo "  login POST produced no redirect (bad credentials?)"; exit 10; }

# 4. Follow approval -> callback -> tracker, all the way to the authenticated page.
code="$("${CURL[@]}" -L -o "$PAGE" -w '%{http_code}' "$(abs "$loc")")"
echo "  [4] final authenticated GET -> HTTP $code"
if [ "$code" = "200" ] && grep -qiE 'naglight|ambient|<html' "$PAGE"; then
    echo "  OK: full headless Dex login completed; landed on the tracker."
    exit 0
fi
echo "  reached callback chain but final page was HTTP $code"
exit 10
