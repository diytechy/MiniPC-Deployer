#!/usr/bin/env bash
# ottertube-updates-lab.sh - the OtterTube update channel's Caddy routes and the
# site knob, against the REAL Caddyfile block and the hub's own Caddy image
# (SR-125, SR-126; TC-1531..TC-1536).
#
# A LABELLED LAB SUITE: needs docker and the local caddy-cloudflare image
# (CADDY_IMAGE_TAG in stack/.env.example), nothing from the network, no root
# beyond what docker needs. Meant for WSL Ubuntu on the dev PC or a lab VM -
# never the hub. Everything it makes is under one temp directory and two
# containers named ottertube-updates-lab-*, all removed at the end.
#
#   bash stack/ottertube/tests/ottertube-updates-lab.sh
#
# What it proves, with the real block (TLS switched to `internal`, nothing else
# changed): update.json and update.json.sig come from the updates folder (not a
# decoy of the same name in web/), 200 with Cache-Control no-cache, the file's
# exact bytes and Content-Length, and NO Content-Encoding even when the client
# offers gzip/br/zstd; the exe the same, and a Range request on it 206; names
# outside the exact patterns are not served from the folder; the reserved
# per-platform manifest serves once present; the web build is still
# compressed (the update handles took `encode` from nobody else); the site knob
# turns the whole block into 404 for anything but exactly `true`; off the LAN
# 403. And docker compose resolves the knob's fallback the way the Caddyfile
# and provision-technitium.sh expect.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
STACK="$(cd "$HERE/../.." && pwd)"
PASS=0 FAIL=0
ok()  { PASS=$((PASS + 1)); echo "PASS $*"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL $*"; }
command -v docker >/dev/null || { echo "SKIP needs docker"; exit 0; }
command -v curl >/dev/null || { echo "SKIP needs curl"; exit 0; }
CADDY_TAG="$(sed -n 's/^CADDY_IMAGE_TAG=//p' "$STACK/.env.example" | tail -1)"
IMAGE="caddy-cloudflare:$CADDY_TAG"
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "SKIP needs the local image $IMAGE (vmtest/export-images.sh builds it)"; exit 0; }

W="$(mktemp -d /tmp/ottertube-updates-lab.XXXXXX)"
NAME=ottertube-updates-lab-caddy
PORT=18443
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1; rm -rf "$W"; }
trap cleanup EXIT
mkdir -p "$W/web" "$W/updates" "$W/caddy" "$W/compose"

# ── fixtures ────────────────────────────────────────────────────────────────
# The web build: an index big enough for encode's minimum length, and a DECOY
# update.json that must never be what /ottertube/update.json serves.
python3 - "$W/web/index.html" <<'PY'
import sys
open(sys.argv[1], "w").write("<!doctype html><title>OtterTube</title>" + "<p>otter</p>" * 400 + "\n")
PY
printf 'DECOY - the web build is not the update channel\n' >"$W/web/update.json"
# The channel: a pretty-printed manifest ending in a newline (as make-update.mjs
# writes it, padded past encode's minimum length), a one-line signature, a 3 MiB exe of random bytes, the README, a
# publish's temporary file, and the reserved Linux manifest.
EXE=OtterTube-abcdef0.exe
head -c 3145728 /dev/urandom >"$W/updates/$EXE"
EXE_SHA="$(sha256sum "$W/updates/$EXE" | cut -d' ' -f1)"
EXE_SIZE="$(stat -c %s "$W/updates/$EXE")"
python3 - "$W/updates/update.json" "$EXE" "$EXE_SIZE" "$EXE_SHA" <<'PY'
import json, sys
m = {"schema": 1, "product": "OtterTube", "version": "0.1.0", "commit": "abcdef0" + "1" * 33, "dirty": False,
     "builtAt": "2026-10-26T12:00:00.000Z", "file": sys.argv[2], "size": int(sys.argv[3]), "sha256": sys.argv[4],
     # an unknown key (clients ignore it) that takes the file past encode's
     # 512-byte minimum, so a stray `encode` on the route would show
     "note": "padding " * 40}
open(sys.argv[1], "wb").write((json.dumps(m, indent=2) + "\n").encode())
PY
printf '%s==\n' "$(head -c 64 /dev/urandom | base64 -w0 | cut -c1-86)" >"$W/updates/update.json.sig"
cp "$STACK/ottertube/updates/README.md" "$W/updates/README.md"
printf 'half a copy\n' >"$W/updates/.$EXE.tmp"
printf '{"schema": 1, "platform": "linux-x64"}\n' >"$W/updates/update-linux-x64.json"
chmod -R a+rX "$W"

# ── the REAL block, TLS internal ────────────────────────────────────────────
awk '/^\{\$OTTERTUBE_HOST\} \{/{on=1} on{print} on && /^\}/{exit}' "$STACK/caddy/Caddyfile" \
    | sed 's/^    import acme_cloudflare$/    tls internal/' >"$W/caddy/ottertube.caddy"
grep -q '^    tls internal$' "$W/caddy/ottertube.caddy" || { bad "0 the OtterTube block was not found in the Caddyfile"; echo "ottertube-updates-lab: $PASS passed, $FAIL failed"; exit 1; }
{ printf '{\n    servers {\n        protocols h1 h2\n    }\n}\n'; cat "$W/caddy/ottertube.caddy"; } >"$W/caddy/Caddyfile"

start_caddy() {  # LAN_CIDR, then -e flags for OTTERTUBE_SITE_ENABLED (none: unset)
    local cidr="$1"; shift
    docker rm -f "$NAME" >/dev/null 2>&1
    docker run -d --name "$NAME" -p "127.0.0.1:$PORT:443" -e OTTERTUBE_HOST=ottertube.lab.test -e LAN_CIDR="$cidr" "$@" \
        -v "$W/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$W/web:/srv/ottertube/web:ro" -v "$W/updates:/srv/ottertube/updates:ro" -v "$STACK/ottertube/landing:/srv/ottertube/landing:ro" \
        "$IMAGE" >/dev/null || return 1
    for _ in $(seq 1 40); do
        curl -sk -o /dev/null --resolve "ottertube.lab.test:$PORT:127.0.0.1" "https://ottertube.lab.test:$PORT/" && return 0
        sleep 0.25
    done
    docker logs "$NAME" 2>&1 | tail -5
    return 1
}
U="https://ottertube.lab.test:$PORT"
AE='Accept-Encoding: gzip, deflate, br, zstd'
lab()  { curl -sk --resolve "ottertube.lab.test:$PORT:127.0.0.1" "$@"; }
code() { lab -o /dev/null -w '%{http_code}' "$@"; }
# headers of a GET offering every encoding, lower-cased, CR stripped
hdrs() { lab -D - -o /dev/null -H "$AE" "$@" | tr -d '\r' | tr 'A-Z' 'a-z'; }
body_sha() { lab -H "$AE" "$@" | sha256sum | cut -d' ' -f1; }
file_sha() { sha256sum "$1" | cut -d' ' -f1; }
hval() { printf '%s\n' "$1" | sed -n "s/^$2: //p" | head -1; }

# 1 the manifest and its signature: from updates/, exact bytes, no-cache, never encoded
if start_caddy "0.0.0.0/0" -e OTTERTUBE_SITE_ENABLED=true; then
    for f in update.json update.json.sig; do
        h="$(hdrs "$U/ottertube/$f")"; sha="$(body_sha "$U/ottertube/$f")"
        size="$(stat -c %s "$W/updates/$f")"
        if [[ "$h" == "http/"*" 200"* ]] && [ "$(hval "$h" cache-control)" = no-cache ] && [ -z "$(hval "$h" content-encoding)" ] \
            && [ "$(hval "$h" content-length)" = "$size" ] && [ "$sha" = "$(file_sha "$W/updates/$f")" ]; then
            ok "1 $f: 200, Cache-Control no-cache, no Content-Encoding (gzip/br/zstd offered), Content-Length $size, the folder's exact bytes"
        else bad "1 $f: sha=$sha (file $(file_sha "$W/updates/$f")) $(printf '%s' "$h" | grep -E '^(http/|cache-control|content-encoding|content-length)' | tr '\n' ' ')"; fi
    done

    # 2 the exe: exact bytes and length, not encoded, ranges work
    h="$(hdrs "$U/ottertube/$EXE")"; sha="$(body_sha "$U/ottertube/$EXE")"
    if [[ "$h" == "http/"*" 200"* ]] && [ -z "$(hval "$h" content-encoding)" ] && [ "$(hval "$h" content-length)" = "$EXE_SIZE" ] && [ "$sha" = "$EXE_SHA" ]; then
        ok "2a $EXE: 200, Content-Length $EXE_SIZE, no Content-Encoding, sha256 = the manifest's"
    else bad "2a exe: sha=$sha $(printf '%s' "$h" | grep -E '^(http/|content-encoding|content-length)' | tr '\n' ' ')"; fi
    r="$(lab -D - -o /dev/null -r 0-99 "$U/ottertube/$EXE" | tr -d '\r' | tr 'A-Z' 'a-z')"
    [[ "$r" == "http/"*" 206"* && "$r" == *"content-range: bytes 0-99/$EXE_SIZE"* ]] && ok "2b the exe with Range 0-99: 206" || bad "2b range: $(printf '%s' "$r" | head -3 | tr '\n' ' ')"

    # 3 nothing else in the folder is reachable; near-miss names are not the channel's
    miss=""
    for p in OtterTube-ABCDEF0.exe OtterTube-abcdef.exe OtterTube-abcdef01.exe "OtterTube-abcdef0.exe.tmp" ".$EXE.tmp" update.json.tmp update-foo-x64.json update-linux-x64.json.sig; do
        c="$(code "$U/ottertube/$p")"; [ "$c" = 404 ] || miss="$miss $p=$c"
    done
    readme="$(lab "$U/ottertube/README.md" | head -1)"
    if [ -z "$miss" ] && [[ "$readme" != *"update channel"* ]]; then
        ok "3 near-miss names, the publish's .tmp and the folder's README: not served from updates/ (404; README.md is the web build's, if any)"
    else bad "3 served:$miss readme=[$readme]"; fi

    # 4 the reserved per-platform manifest serves once present, revalidated
    h="$(hdrs "$U/ottertube/update-linux-x64.json")"
    [[ "$h" == "http/"*" 200"* ]] && [ "$(hval "$h" cache-control)" = no-cache ] && [ -z "$(hval "$h" content-encoding)" ] \
        && ok "4 update-linux-x64.json: 200, no-cache, not encoded (reserved name)" || bad "4 reserved: $(printf '%s' "$h" | head -3 | tr '\n' ' ')"

    # 5 the web build keeps its compression and its 200
    h="$(hdrs "$U/ottertube/")"
    [[ "$h" == "http/"*" 200"* ]] && [ -n "$(hval "$h" content-encoding)" ] \
        && ok "5 the web build: 200 and still encoded ($(hval "$h" content-encoding))" || bad "5 web: $(printf '%s' "$h" | head -4 | tr '\n' ' ')"
else
    bad "1-5 caddy did not start with the real block"
fi

# 9 the landing page at the root: 200, the tracked page, its strict CSP, its script
if start_caddy "0.0.0.0/0" -e OTTERTUBE_SITE_ENABLED=true; then
    h="$(lab -D - -o /dev/null "$U/" | tr -d '\r' | tr 'A-Z' 'a-z')"
    page_sha="$(lab "$U/" | sha256sum | cut -d' ' -f1)"
    js="$(code "$U/landing.js")"; css="$(code "$U/landing.css")"; other="$(code "$U/landing.html")"
    # the catalogue still goes to the media server (none in this lab: 502), never the page
    cat="$(code "$U/ottertube/catalog.json")"
    # the served stylesheet keeps `hidden` stronger than .button's display
    # (tests/test_ottertube_updates.py TC-1540 proves it in a real browser)
    hid="$(lab "$U/landing.css" | tr -d ' ' | grep -c '^\[hidden\]{display:none!important;}$')"
    if [[ "$h" == "http/"*" 200"* ]] && [ "$hid" = 1 ] && [ "$page_sha" = "$(file_sha "$STACK/ottertube/landing/index.html")" ] \
        && [[ "$(hval "$h" content-security-policy)" == "default-src 'none'; script-src 'self';"* ]] \
        && [ "$js$css$other" = 200200404 ] && [ "$cat" = 502 ]; then
        ok "9 /: 200, the tracked landing page byte for byte, CSP default-src 'none'; landing.js and .css 200, another name 404; catalog.json still proxied (502 here)"
    else bad "9 landing: js=$js css=$css hidden-rule=$hid other=$other catalog=$cat $(printf '%s' "$h" | head -3 | tr '\n' ' ')"; fi
else bad "9 caddy did not start"; fi

# 6 the site knob: anything but exactly `true` is a 404 for the whole block
for v in false True "" 'tr"ue' unset; do
    if [ "$v" = unset ]; then start_caddy "0.0.0.0/0"; else start_caddy "0.0.0.0/0" -e "OTTERTUBE_SITE_ENABLED=$v"; fi || { bad "6 caddy did not start with the knob '$v'"; continue; }
    c1="$(code "$U/ottertube/update.json")"; c2="$(code "$U/ottertube/$EXE")"; c3="$(code "$U/ottertube/")"; c4="$(code "$U/")"
    if [ "$c1$c2$c3$c4" = 404404404404 ]; then ok "6 OTTERTUBE_SITE_ENABLED='$v': update.json, the exe, the web build and / all 404"
    else bad "6 knob '$v': update=$c1 exe=$c2 web=$c3 root=$c4"; fi
done

# 7 off the LAN: 403 (the site on)
if start_caddy "10.255.255.0/24" -e OTTERTUBE_SITE_ENABLED=true; then
    c="$(code "$U/ottertube/update.json")"
    [ "$c" = 403 ] && ok "7 a client outside LAN_CIDR: 403 for update.json" || bad "7 off-LAN: $c"
else bad "7 caddy did not start"; fi
docker rm -f "$NAME" >/dev/null 2>&1

# 8 compose resolves the fallback: SITE wins when set; empty or unset follows the library knob
if docker compose version >/dev/null 2>&1; then
    cp "$STACK/docker-compose.yml" "$W/compose/"
    resolve() {  # .env lines -> caddy's OTTERTUBE_SITE_ENABLED
        { grep -v -E '^OTTERTUBE_(SITE_)?ENABLED=' "$STACK/.env.example"; printf '%s\n' "$@"; } >"$W/compose/.env"
        (cd "$W/compose" && docker compose config --format json 2>/dev/null) \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["caddy"]["environment"].get("OTTERTUBE_SITE_ENABLED", "<absent>"))'
    }
    out=""
    for case in "OTTERTUBE_ENABLED=true|true" "OTTERTUBE_ENABLED=true;OTTERTUBE_SITE_ENABLED=|true" \
                "OTTERTUBE_ENABLED=true;OTTERTUBE_SITE_ENABLED=false|false" "OTTERTUBE_ENABLED=false;OTTERTUBE_SITE_ENABLED=true|true" \
                "OTTERTUBE_ENABLED=false|false" "|false"; do
        lines="${case%|*}"; want="${case##*|}"
        # shellcheck disable=SC2086
        got="$(IFS=';'; resolve $lines)"
        [ "$got" = "$want" ] || out="$out [$lines]=$got(want $want)"
    done
    [ -z "$out" ] && ok "8 compose: the site knob when set, else OTTERTUBE_ENABLED, else false (6 cases)" || bad "8 compose:$out"
else
    echo "SKIP 8 no docker compose"
fi

echo "ottertube-updates-lab: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
