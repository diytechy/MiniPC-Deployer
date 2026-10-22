#!/usr/bin/env bash
# door-cutover-panel.test.sh - the file-handling verbs of door-cutover-panel.sh
# against a fake panel tree, as an ordinary user (TC-1398, SR-106).
# The systemctl verbs (suppress, regenerate, release, probe, compat) act on the
# real panel and are exercised there by the coordinator's procedure.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../door-cutover-panel.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export DOOR_CUTOVER_TEST_ROOT="$T"
PASS=0 FAIL=0
ok()  { PASS=$((PASS + 1)); echo "PASS $*"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL $*"; }
run() { bash "$SCRIPT" "$@"; }

mkdir -p "$T/etc/wall-panel" "$T/run/wall-door-credentials"
cat >"$T/etc/wall-panel/wall.env" <<'EOF'
WIFI_SSID=example
DOORBELL_RTSP_HOST=192.0.2.20
DOORBELL_RTSP_PASSWORD='c@m p$ss:1'
DOORBELL_RTSP_PORT=554
DOORBELL_RTSP_PATH=/H.264
DOORBELL_RTSP_USERNAME=admin
DOORBELL_MOTION_ENABLED=true
EOF
chmod 0600 "$T/etc/wall-panel/wall.env"
printf '%s' 192.0.2.20 >"$T/run/wall-door-credentials/host"
printf '%s' 'c@m p$ss:1' >"$T/run/wall-door-credentials/password"
printf '%s' 554 >"$T/run/wall-door-credentials/port"
printf '%s' /H.264 >"$T/run/wall-door-credentials/path"
printf '%s' admin >"$T/run/wall-door-credentials/username"
printf '%s' 960 >"$T/run/wall-door-credentials/width"
chmod 0600 "$T"/run/wall-door-credentials/*
orig="$(cat "$T/etc/wall-panel/wall.env")"

# export-upstream: NUL records of the five fields, nothing else
blob="$(run export-upstream | tr '\0' '\n')"
want="$(printf 'host=192.0.2.20\nport=554\npath=/H.264\nusername=admin\npassword=c@m p$ss:1')"
[ "$blob" = "$want" ] && ok "export-upstream emits the five NUL-separated records" || bad "export-upstream: $blob"
if run export-upstream --refuse-host 192.0.2.20 >/dev/null 2>&1; then bad "export-upstream did not refuse the relay host"
else ok "export-upstream refuses when the broker already points at the relay host"; fi

# prepare: the actual inputs, 0700/0600, checksums printed, no secret printed
out="$(run prepare)"; dir="$(printf '%s\n' "$out" | sed -n 's/^prepared //p')"
if [ -d "$dir" ] && [ "$(stat -c %a "$dir")" = 700 ] && [ "$(stat -c %a "$dir/wall.env.door-lines")" = 600 ] \
   && [ "$(grep -c '^DOORBELL_RTSP_' "$dir/wall.env.door-lines")" = 5 ] && cmp -s "$dir/credentials/password" "$T/run/wall-door-credentials/password" \
   && ! printf '%s' "$out" | grep -qF 'p$ss'; then
    ok "prepare saves the 5 lines and every credential file (0700/0600) and prints no secret"
else bad "prepare: $out"; fi

# point: only HOST/PORT/PATH change
run point "$dir" 192.0.2.10 8554 /door >/dev/null
after="$(cat "$T/etc/wall-panel/wall.env")"
changed="$(diff <(printf '%s\n' "$orig") <(printf '%s\n' "$after") | grep -c '^[<>]')"
if [ "$changed" = 6 ] && grep -qx 'DOORBELL_RTSP_HOST=192.0.2.10' "$T/etc/wall-panel/wall.env" \
   && grep -qx 'DOORBELL_RTSP_PORT=8554' "$T/etc/wall-panel/wall.env" && grep -qx 'DOORBELL_RTSP_PATH=/door' "$T/etc/wall-panel/wall.env" \
   && grep -qxF "DOORBELL_RTSP_PASSWORD='c@m p\$ss:1'" "$T/etc/wall-panel/wall.env" && [ "$(stat -c %a "$T/etc/wall-panel/wall.env")" = 600 ]; then
    ok "point rewrites exactly HOST/PORT/PATH, keeps the credential lines and mode 0600"
else bad "point changed $changed lines"; fi
run point "$dir" not-an-ip 8554 /door >/dev/null 2>&1 && bad "point accepted a hostname" || ok "point refuses a non-address"
run point "$T/nope" 192.0.2.10 8554 /door >/dev/null 2>&1 && bad "point accepted an unprepared directory" || ok "point refuses an unprepared directory"

# restore: byte-identical original
run restore "$dir" >/dev/null
[ "$(cat "$T/etc/wall-panel/wall.env")" = "$orig" ] && ok "restore gives back the original wall.env byte for byte" || bad "restore differs"

# compare: identical, then a changed credential is detected
run compare "$dir" >/dev/null && ok "compare: identical credentials pass" || bad "compare failed on identical files"
printf '%s' 192.0.2.10 >"$T/run/wall-door-credentials/host"
run compare "$dir" >/dev/null 2>&1 && bad "compare missed a changed credential" || ok "compare detects a changed credential"

echo "door-cutover-panel.test: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
