#!/usr/bin/env bash
# camera-view-lab.sh - the camera page end to end on one Linux host with docker:
# a FAKE doorbell, the real door relay, the viewer run from its own unit lines
# through the real docker-compose.yml, and the hub's own Caddy image serving the
# real cameras.<domain> block (SN-057, SR-170..SR-174; TC-1838..TC-1843).
#
# A LABELLED SUITE, NOT A HERMETIC ONE: it needs root (docker), the pinned
# go2rtc image, the hub's Caddy image (caddy-cloudflare:${CADDY_IMAGE_TAG},
# built by scripts/ensure-local-images.sh), python3 with bcrypt, and a
# non-loopback IPv4 address. Run it in WSL Ubuntu:
#     wsl -d Ubuntu -u root -e bash stack/camera-remote/tests/camera-view-lab.sh
# It never contacts the real camera, hub or panel. It uses (and removes)
# /run/homehub-camera-view and /var/lib/homehub-camera-view, the unit's paths,
# and refuses to start if either exists.
#
# What changes from the hub, and only this: the Caddyfile's (acme_cloudflare)
# snippet becomes `tls internal`, the global block moves Caddy to ports
# 18080/18443, and the site block's EMPTY list of streams behind the login
# names one lab stream so the wired login can be exercised; the rest of the
# site block and the (throttle) snippet are the real file's, cut out by name. The doorbell is a test pattern. The graphics: this host's
# render node does not take the image's VA-API driver, so door_flat as shipped
# FAILS here (check 7 proves that failure is clean); check 8 re-renders the
# same pipeline with a software encoder (camera_remote.flat_argv's lab-only
# "software"), which is what proves the straightening pipe to MSE.
#
# Checks:
#   1  render with no login set (the door needs none): 0600 files in a 0700
#      directory; no rtsp:// in the exec line; a private_ranges LAN_CIDR is
#      refused and writes nothing; import-login doubles each `$`, prints nothing
#   2  the viewer from the unit's own ExecCondition/ExecStartPre/ExecStart
#      lines: go2rtc holds NO listening socket; on its socket /api/streams,
#      /api/config and /api are 404 and /stream.html is 200
#   3  caddy from the compose defaults (no CAMERA_VIEW_* in .env): it loads
#      (the login handle wired, with the lock) and the site answers 404 (off)
#   4  on with no login set: the door's page and both door streams open with no
#      login; a stream behind the login (a lab name put in the shipped empty
#      list) is a 401 to anyone, the lock's own text included
#   5  on with the login set: the door still needs none; / and /raw 302 to the
#      MSE player; the login stream: none or wrong 401, right 101
#   6  refused (403): other API paths and pages, and the websocket with src
#      absent, repeated, percent-encoded, foreign, an rtsp:// URL, upper-case
#      SRC, an extra name=, a trailing & or a ;
#   7  door_flat as shipped on a host without the hub's graphics: the
#      websocket answers an error that holds no credential, and no FFmpeg is
#      left running
#   8  on_demand: src=door and (software-encoded) src=door_flat play over MSE
#      (avc1, video bytes); the relay serves 0 consumers idle, at least 1 while
#      watched, 0 after; while they play no process's argv or environment and
#      no `docker inspect` of the viewer holds the relay credential, and FFmpeg
#      reads /config/door.ffconcat
#   9  always: the warm unit's own lines (its condition skips on_demand and
#      passes always); with no page open the relay serves a consumer and the
#      FFmpeg runs; a page joining gets video (time to first video printed);
#      the warm unit reconnects after a viewer restart; stopped, 0 consumers
#  10  a source on a docker bridge (outside LAN_CIDR) -> 403
#  11  the relay killed under a live viewer: go2rtc's dropout line reaches the
#      unit's output redacted (no credential spelling, no userinfo)
#  12  `camera_remote.py check` against this lab (caddy's own environment
#      read back) passes; with caddy's LAN_CIDR made broad it fails
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
CR="$(cd "$HERE/.." && pwd)"
STACK="$(cd "$CR/.." && pwd)"
IMAGE="$(awk -F= '/^IMAGE=/{i=$2} /^VERSION=/{v=$2} /^DIGEST=/{d=$2} END{print i":"v"@"d}' "$STACK/door/door-relay-image.pin")"
CADDY_IMAGE="caddy-cloudflare:$(awk -F= '/^CADDY_IMAGE_TAG=/{print $2}' "$STACK/.env.example")"
RUN=/run/homehub-camera-view
STATE=/var/lib/homehub-camera-view
NAME=cameras.lab.test
PORT=18443
PASS=0 FAIL=0
ok()  { PASS=$((PASS + 1)); echo "PASS $*"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL $*"; }
WORK=""
UNIT_PID=""
WARM_PID=""
cleanup() {
    [ -z "$UNIT_PID" ] || kill "$UNIT_PID" 2>/dev/null
    [ -z "$WARM_PID" ] || pkill -f "camera_remote.py --env-file $WORK/stack/.env keep-warm" 2>/dev/null
    docker rm -f cvlab-cam cvlab-relay homehub-camera-view "$CADDY" >/dev/null 2>&1
    [ -z "$WORK" ] || { rm -rf "$RUN" "$STATE" "$WORK"; }
}

[ "$(id -u)" = 0 ] || { echo "SKIP: needs root (docker)"; exit 0; }
command -v docker >/dev/null || { echo "SKIP: no docker"; exit 0; }
python3 -c 'import bcrypt' 2>/dev/null || { echo "SKIP: python3 bcrypt is missing"; exit 0; }
docker image inspect "$CADDY_IMAGE" >/dev/null 2>&1 || { echo "SKIP: $CADDY_IMAGE is not built here"; exit 0; }
if [ -e "$RUN" ] || [ -e "$STATE" ]; then echo "SKIP: $RUN or $STATE exists (a real viewer?)"; exit 0; fi
LAN="$(ip -4 -o addr show scope global | awk '{split($4,a,"/"); print a[1]; exit}')"
LANNET="$(ip -4 -o addr show scope global | awk '{print $4; exit}' | python3 -c 'import ipaddress,sys; print(ipaddress.ip_network(sys.stdin.read().strip(), strict=False))')"
[ -n "$LAN" ] || { echo "SKIP: no non-loopback IPv4 address"; exit 0; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull -q "$IMAGE" >/dev/null || { echo "FAIL cannot pull $IMAGE"; exit 1; }
CADDY=caddy   # `check` reads the container named caddy; refuse to touch a real one
if docker inspect caddy >/dev/null 2>&1; then CADDY=cvlab-caddy; fi
WORK="$(mktemp -d /tmp/cvlab.XXXXXX)"
trap cleanup EXIT
export HOMEHUB_MONITOR_OVERRIDE_FILE=/nonexistent

# The lab stack: this repo's files at their hub paths, and a .env written the
# way Materialize-Deploy writes one (`$` doubled; whitespace quoted).
LS="$WORK/stack"
mkdir -p "$LS"
cp -r "$STACK/camera-remote" "$STACK/door" "$STACK/monitoring" "$STACK/docker-compose.yml" "$LS/"
PW='c@m p$ss:1'
VIEWER_PW='lab viewer pass'
PAT="$WORK/secret.pat"     # a FILE, so grep's own argv holds no secret
printf '%s\n' 'p$ss' 'p%24ss' "$(printf '%s' "admin:$PW" | base64 -w0)" >"$PAT"
mkdir -p "$LS/llm-gateway"; : >"$LS/llm-gateway/llm-gateway.env"   # an env_file the compose file names
# The template first, so every variable the compose file names is set; the
# lab's values after it (the last assignment wins, for compose and for us).
{ cat "$STACK/.env.example"; cat <<EOF
COMPOSE_PROJECT_NAME=cvlab
DOMAIN=lab.test
LAN_IP=$LAN
LAN_CIDR=$LANNET 100.64.0.0/10
DOOR_RELAY_IMAGE=$IMAGE
DOOR_RELAY_RTSP_PORT=18554
DOOR_RELAY_API_PORT=19840
DOOR_RELAY_STREAM=door
DOOR_RELAY_UPSTREAM_HOST=127.0.0.1
DOOR_RELAY_UPSTREAM_PORT=8654
DOOR_RELAY_UPSTREAM_PATH=/H.264
DOOR_RELAY_UPSTREAM_USERNAME=admin
DOOR_RELAY_UPSTREAM_PASSWORD="c@m p\$\$ss:1"
EOF
} >"$LS/.env"
cr() { python3 "$LS/camera-remote/camera_remote.py" --env-file "$LS/.env" "$@"; }
relay() { python3 "$LS/door/door_relay.py" --env-file "$LS/.env" "$@"; }
probe() { python3 "$HERE/cv_probe.py" "$@"; }
field() { python3 -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(d.get(sys.argv[1], ""))' "$1"; }
consumers() { relay streams 2>/dev/null | field consumers; }

# The fake doorbell and the real door relay (door-relay-lab.sh's pair).
cat >"$WORK/cam.json" <<'JSON'
{"api": {"listen": ""}, "rtsp": {"listen": ":8654", "username": "admin", "password": "c@m p$ss:1"},
 "webrtc": {"listen": ""}, "srtp": {"listen": ""}, "log": {"level": "info"},
 "streams": {"H.264": "exec:ffmpeg -hide_banner -v error -re -f lavfi -i testsrc2=size=1536x1536:rate=15 -c:v libx264 -preset ultrafast -tune zerolatency -g 15 -pix_fmt yuv420p -f rtsp {output}"}}
JSON
docker rm -f cvlab-cam cvlab-relay homehub-camera-view >/dev/null 2>&1
docker run -d --name cvlab-cam -p 127.0.0.1:8654:8654 --read-only --tmpfs /tmp \
    -v "$WORK/cam.json:/config/go2rtc.yaml:ro" "$IMAGE" >/dev/null
relay render --out "$WORK/relay/go2rtc.yaml" >/dev/null
docker run -d --name cvlab-relay --network host --read-only --cap-drop ALL --security-opt no-new-privileges \
    -v "$WORK/relay/go2rtc.yaml:/config/go2rtc.yaml:ro" "$IMAGE" >/dev/null

# The shared login: a bcrypt hash of the viewer password, through import-login.
HASH="$(printf '%s' "$VIEWER_PW" | python3 -c 'import bcrypt,sys; print(bcrypt.hashpw(sys.stdin.buffer.read(), bcrypt.gensalt(10)).decode())')"

# ── 1 render ──────────────────────────────────────────────────────────────────
# The door needs no login (2026-11-12), so render needs no hash; the import
# verb is proven on a copy. on_demand for checks 2-8; always is check 9.
sed -i 's/^CAMERA_VIEW_DOOR_FLAT_MODE=.*/CAMERA_VIEW_DOOR_FLAT_MODE=on_demand/' "$LS/.env"
cp "$LS/.env" "$WORK/env.broad"
sed -i 's|^LAN_CIDR=.*|LAN_CIDR=private_ranges|' "$WORK/env.broad"
python3 "$LS/camera-remote/camera_remote.py" --env-file "$WORK/env.broad" render --out-dir "$WORK/refused" >"$WORK/refused.log" 2>&1
rc_broad=$?
cp "$LS/.env" "$WORK/env.login"
printf '%s\n' "$HASH" | python3 "$LS/camera-remote/camera_remote.py" --env-file "$WORK/env.login" import-login >"$WORK/import.log" 2>&1
cr render >"$WORK/render.log"; rc_render=$?
modes="$(stat -c %a "$RUN" "$RUN/go2rtc.yaml" "$RUN/door.ffconcat" | tr '\n' ' ')"
if [ "$rc_broad" = 2 ] && [ ! -e "$WORK/refused" ] && [ "$rc_render" = 0 ] && [ "$modes" = "700 600 600 " ] \
   && ! grep -q 'exec:[^"]*rtsp://' "$RUN/go2rtc.yaml" && grep -q '^CAMERA_VIEW_AUTH_HASH=\$\$2' "$WORK/env.login" \
   && ! grep -qF "$HASH" "$WORK/import.log"; then
    ok "1 render with no login set: modes $modes; exec line holds no URL; private_ranges refused (rc 2, nothing written); import-login doubles each \$ and prints no hash"
else bad "1 render: rc_broad=$rc_broad rc=$rc_render modes=$modes $(cat "$WORK/refused.log" "$WORK/render.log")"; fi

# ── 2 the viewer, from the unit's own lines ──────────────────────────────────
mkdir -m 0700 -p "$STATE"
sed -i 's/^CAMERA_VIEW_ENABLED=.*/CAMERA_VIEW_ENABLED=true/' "$LS/.env"
unit_line() {  # FILE KEY [N]: the Nth KEY= line of a unit file, its hub paths moved to the lab stack
    grep "^$2=" "$1" | head -n "${3:-1}" | tail -n 1 | sed "s|^$2=||; s|/opt/homehub/stack|$LS|g"
}
VIEW_UNIT="$CR/homehub-camera-view.service"
WARM_UNIT="$CR/homehub-camera-view-warm.service"
run_unit_line() {  # one Exec line as systemd runs it: no shell; a leading '-' ignores failure
    local line="$1" ignore=""; local -a argv
    [ "${line#-}" != "$line" ] && { ignore=1; line="${line#-}"; }
    read -ra argv <<<"$line"
    ( cd "$LS" && exec "${argv[@]}" ) || [ -n "$ignore" ]
}
start_viewer() {
    rm -f "$STATE/api.sock"
    run_unit_line "$(unit_line "$VIEW_UNIT" ExecStart)" >>"$WORK/unit.log" 2>&1 &
    UNIT_PID=$!
    for _ in $(seq 1 60); do [ -S "$STATE/api.sock" ] && break; sleep 0.5; done
}
stop_viewer() {
    run_unit_line "$(unit_line "$VIEW_UNIT" ExecStop)" >/dev/null 2>&1
    docker rm -f homehub-camera-view >/dev/null 2>&1
    wait "$UNIT_PID" 2>/dev/null; UNIT_PID=""
}
run_unit_line "$(unit_line "$VIEW_UNIT" ExecCondition)" >/dev/null 2>&1 && cond=on || cond=off
run_unit_line "$(unit_line "$VIEW_UNIT" ExecStartPre 1)" >>"$WORK/unit.log" 2>&1   # render, as the unit runs it
run_unit_line "$(unit_line "$VIEW_UNIT" ExecStartPre 2)" >>"$WORK/unit.log" 2>&1
start_viewer
sock() { curl -s -o /dev/null -w '%{http_code}' --unix-socket "$STATE/api.sock" "http://x$1"; }
listening=0
for vpid in $(docker top homehub-camera-view -eo pid 2>/dev/null | tail -n +2); do
    n="$(ss -Hlntup 2>/dev/null | grep -c "pid=$vpid," || true)"; listening=$((listening + n))
done
s_streams="$(sock /api/streams)"; s_config="$(sock /api/config)"; s_api="$(sock /api)"; s_page="$(sock /stream.html)"
if [ "$cond" = on ] && [ -S "$STATE/api.sock" ] && [ "$listening" = 0 ] && [ "$s_streams$s_config$s_api" = 404404404 ] && [ "$s_page" = 200 ]; then
    ok "2 viewer: no listening socket; on its socket /api/streams /api/config /api -> 404, /stream.html -> 200"
else bad "2 viewer: condition=$cond sock=$([ -S "$STATE/api.sock" ] && echo y) listening=$listening streams=$s_streams config=$s_config api=$s_api page=$s_page"; fi

# ── caddy, as compose would create it ────────────────────────────────────────
cut_block() {  # the real Caddyfile's block that starts with $1 at column 0
    awk -v start="$1" 'index($0, start) == 1 {on=1} on {print} on && /^}/ {exit}' "$STACK/caddy/Caddyfile"
}
# The site block as shipped, except that the (empty) list of streams behind
# the login names one lab stream, so the wired login can be exercised.
{
    printf '{\n    order rate_limit before basic_auth\n    servers {\n        protocols h1 h2\n    }\n'
    printf '    admin localhost:12019\n    http_port 18080\n    https_port %s\n    auto_https disable_redirects\n    skip_install_trust\n}\n' "$PORT"
    printf '(acme_cloudflare) {\n    tls internal\n}\n'
    cut_block '(throttle) {'
    cut_block '{$CAMERA_VIEW_HOST} {' | sed 's/{query} in \[\]/{query} in ["src=labprivate"]/'
} >"$WORK/Caddyfile"
grep -q 'in \["src=labprivate"\]' "$WORK/Caddyfile" || bad "lab: the login list was not found in the site block"
caddy_env() {  # the caddy service's environment as compose interpolates it from $1
    docker compose -f "$LS/docker-compose.yml" --project-directory "$LS" --env-file "$1" config --format json 2>/dev/null \
      | python3 -c '
import json,sys
env=json.load(sys.stdin)["services"]["caddy"]["environment"]
for k in ("LAN_CIDR","CAMERA_VIEW_HOST","CAMERA_VIEW_ENABLED","CAMERA_VIEW_AUTH_USER","CAMERA_VIEW_AUTH_HASH"):
    print("%s=%s" % (k, (env.get(k) or "").replace("$$", "$")))'
}
start_caddy() {  # $1: an env file for compose
    docker rm -f "$CADDY" >/dev/null 2>&1
    caddy_env "$1" >"$WORK/caddy.env"
    docker run -d --name "$CADDY" --network host --env-file "$WORK/caddy.env" \
        -v "$WORK/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$STATE:/srv/camera-view:ro" "$CADDY_IMAGE" >/dev/null
    for _ in $(seq 1 40); do
        [ "$(probe http "$LAN" "$PORT" "$NAME" / | field status)" != -1 ] && return 0; sleep 0.5
    done
}
GOOD="viewer:$VIEWER_PW"
st() { probe http "$LAN" "$PORT" "$NAME" "$1" ${2:+--auth "$2"} | field status; }
wst() { probe ws "$LAN" "$PORT" "$NAME" "$1" ${2:+--auth "$2"} | field status; }

# ── 3 the compose defaults: caddy loads, the site is off ─────────────────────
grep -v '^CAMERA_VIEW_' "$LS/.env" >"$WORK/env.defaults"
start_caddy "$WORK/env.defaults"
off="$(st /) $(st /stream.html) $(wst '/api/ws?src=door')"
if grep -qx 'CAMERA_VIEW_AUTH_HASH=bG9ja2Vk' "$WORK/caddy.env" && grep -qx 'CAMERA_VIEW_ENABLED=false' "$WORK/caddy.env" \
   && [ "$off" = "404 404 404" ]; then
    ok "3 compose defaults: caddy loads (the login wired, locked) and the site answers 404 ($off)"
else bad "3 defaults: $off; $(docker logs "$CADDY" 2>&1 | tail -n 3)"; fi

# ── 4 on with no hash: the door is open, a login stream is locked ────────────
{ grep -v '^CAMERA_VIEW_' "$LS/.env"; echo CAMERA_VIEW_ENABLED=true; } >"$WORK/env.lock"
start_caddy "$WORK/env.lock"
four="$(st /) $(st /stream.html) $(wst '/api/ws?src=door') $(wst '/api/ws?src=door_flat') $(wst '/api/ws?src=labprivate') $(wst '/api/ws?src=labprivate' 'viewer:bG9ja2Vk')"
[ "$four" = "302 200 101 101 401 401" ] && ok "4 on with no login set: the door's page and both door streams open with no login; a stream behind the login is a 401 to anyone ($four)" \
    || bad "4 on, no hash: $four"

# ── 5 on with the login set ──────────────────────────────────────────────────
cat "$WORK/env.login" >"$LS/.env.with-login"
sed -i 's/^CAMERA_VIEW_ENABLED=.*/CAMERA_VIEW_ENABLED=true/' "$LS/.env.with-login"
start_caddy "$LS/.env.with-login"
root="$(probe http "$LAN" "$PORT" "$NAME" /)"
raw="$(probe http "$LAN" "$PORT" "$NAME" /raw)"
five="$(st /stream.html) $(st /video-stream.js) $(st /video-rtc.js) $(wst '/api/ws?src=door') $(wst '/api/ws?src=labprivate') $(wst '/api/ws?src=labprivate' 'viewer:wrong') $(wst '/api/ws?src=labprivate' "$GOOD")"
if [ "$five" = "200 200 200 101 401 401 101" ] && [ "$(echo "$root" | field location)" = "/stream.html?src=door_flat&mode=mse" ] \
   && [ "$(echo "$raw" | field location)" = "/stream.html?src=door&mode=mse" ]; then
    ok "5 with the login set: the door still needs none (/ and /raw 302 to the MSE player; page, scripts, websocket open); the login stream: none/wrong 401, right 101"
else bad "5 with login: $five root=$root raw=$raw"; fi

# ── 6 refused (no login involved) ────────────────────────────────────────────
refused=""
for p in /api/streams /api/config /api /index.html /add.html /config.html '/api/frame.mp4?src=door' \
         '/api/stream.mp4?src=door' '/api/stream.m3u8?src=door' '/api/frame.jpeg?src=door' /api/log; do
    s="$(st "$p")"; [ "$s" = 403 ] || refused="$refused $p=$s"
done
for q in '' '?' '?src=door&src=door' '?src=%64oor' '?src=door%5Fflat' '?src=livingroom' \
         '?src=rtsp%3A%2F%2F127.0.0.1%3A8654%2FH.264' '?SRC=door' '?src=door&name=door' '?name=door&src=door' \
         '?src=door&' '?src=door;name=x' '?src=door_flat&src=door'; do
    s="$(wst "/api/ws$q")"; [ "$s" = 403 ] || refused="$refused ws$q=$s"
done
[ -z "$refused" ] && ok "6 refused: 11 other paths and 13 websocket queries (src absent, repeated, encoded, foreign, a URL, SRC, name=, trailing &, ;) -> 403" \
    || bad "6 not refused:$refused"

# ── 7 door_flat as shipped, without the hub's graphics ───────────────────────
flat="$(probe ws "$LAN" "$PORT" "$NAME" '/api/ws?src=door_flat' --mse 8)"
sleep 2
left="$(docker top homehub-camera-view -eo pid,comm 2>/dev/null | grep -c ffmpeg || true)"
if echo "$flat" | grep -q '"status": 101' && echo "$flat" | grep -q 'error' && ! printf '%s' "$flat" | grep -qF -f "$PAT" \
   && ! echo "$flat" | grep -q 'admin:' && [ "$left" = 0 ]; then
    ok "7 door_flat with no usable VA-API: an error on the websocket, no credential in it, no FFmpeg left"
else bad "7 door_flat (vaapi): $flat ffmpeg_left=$left"; fi

# ── 8 MSE plays; consumers; no credential anywhere ───────────────────────────
stop_viewer
render_software() {
    python3 - "$LS" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/camera-remote")
import camera_remote as c
c.render(c.ViewSettings(sys.argv[1] + "/.env"), c.RUN_DIR, encoder="software")
PY
}
render_software
start_viewer
idle="$(consumers)"
scan() {
    cam=" $(docker top cvlab-cam -eo pid 2>/dev/null | tail -n +2 | tr -s ' \n' '  ') "
    for f in $(grep -laF -f "$PAT" /proc/[0-9]*/cmdline /proc/[0-9]*/environ 2>/dev/null); do
        pid="${f#/proc/}"; pid="${pid%%/*}"
        case "$cam" in *" $pid "*) continue;; esac
        echo "LEAK ${f##*/} pid=$pid comm=$(cat "/proc/$pid/comm" 2>/dev/null)"
    done
    docker inspect -f '{{json .Config.Env}} {{json .Config.Cmd}} {{json .Args}}' homehub-camera-view | grep -qF -f "$PAT" && echo "LEAK inspect"
    docker top homehub-camera-view -eo pid,args 2>/dev/null | grep ffmpeg | grep -q 'rtsp://' && echo "LEAK url-in-argv"
    docker top homehub-camera-view -eo pid,args 2>/dev/null | grep ffmpeg | grep -q -- '-i /config/door.ffconcat' && echo "SEEN ffconcat"
}
probe ws "$LAN" "$PORT" "$NAME" '/api/ws?src=door' --mse 10 >"$WORK/door.json" & p1=$!
probe ws "$LAN" "$PORT" "$NAME" '/api/ws?src=door_flat' --mse 10 >"$WORK/flat.json" & p2=$!
sleep 6
busy="$(consumers)"; scan >"$WORK/scan.log"
wait "$p1" "$p2"   # never a bare wait: the viewer's unit process is a child too
after=""
for _ in $(seq 1 30); do after="$(consumers)"; [ "$after" = 0 ] && break; sleep 1; done
bytes() { field binary_bytes <"$1"; }
if grep -q 'avc1' "$WORK/door.json" && grep -q 'avc1' "$WORK/flat.json" && [ "$(bytes "$WORK/door.json")" -gt 10000 ] \
   && [ "$(bytes "$WORK/flat.json")" -gt 10000 ] && [ "$idle" = 0 ] && [ "${busy:-0}" -ge 1 ] && [ "$after" = 0 ] \
   && ! grep -q LEAK "$WORK/scan.log" && grep -q 'SEEN ffconcat' "$WORK/scan.log"; then
    ok "8 on_demand MSE: door $(bytes "$WORK/door.json") B and door_flat $(bytes "$WORK/flat.json") B of avc1 (first video after $(field first_binary_s <"$WORK/flat.json") s); relay consumers $idle idle, $busy watched, $after after; no credential in any argv, environment or inspect; FFmpeg reads /config/door.ffconcat"
else bad "8 MSE: door=$(cat "$WORK/door.json") flat=$(cat "$WORK/flat.json") consumers=$idle/$busy/$after scan=$(tr '\n' ' ' <"$WORK/scan.log")"; fi

# ── 9 always: the warm unit holds door_flat running, across a viewer restart ──
stop_warm() {  # the python the unit line exec'd, not only the shell function around it
    pkill -f "$LS/camera-remote/camera_remote.py --env-file $LS/.env keep-warm" 2>/dev/null
    [ -z "$WARM_PID" ] || wait "$WARM_PID" 2>/dev/null; WARM_PID=""
}
warm_cond() { run_unit_line "$(unit_line "$WARM_UNIT" ExecCondition)" >/dev/null 2>&1 && echo 0 || echo 1; }
c_od="$(warm_cond)"
sed -i 's/^CAMERA_VIEW_DOOR_FLAT_MODE=.*/CAMERA_VIEW_DOOR_FLAT_MODE=always/' "$LS/.env"
c_al="$(warm_cond)"
run_unit_line "$(unit_line "$WARM_UNIT" ExecStart)" >"$WORK/warm.log" 2>&1 & WARM_PID=$!
held=""
for _ in $(seq 1 40); do held="$(consumers)"; [ "${held:-0}" -ge 1 ] && grep -q 'is flowing' "$WORK/warm.log" && break; sleep 1; done
running="$(docker top homehub-camera-view -eo pid,comm 2>/dev/null | grep -c ffmpeg || true)"
sleep 3
join="$(probe ws "$LAN" "$PORT" "$NAME" '/api/ws?src=door_flat' --mse 4)"
stop_viewer; render_software; start_viewer   # the viewer restarts under the warm unit
back=""
for _ in $(seq 1 60); do
    back="$(consumers)"; [ "${back:-0}" -ge 1 ] && [ "$(grep -c 'is flowing' "$WORK/warm.log")" -ge 2 ] && break; sleep 1
done
stop_warm
gone=""
for _ in $(seq 1 30); do gone="$(consumers)"; [ "$gone" = 0 ] && break; sleep 1; done
if [ "$c_od$c_al" = 10 ] && [ "${held:-0}" -ge 1 ] && [ "$running" -ge 1 ] && [ "$(echo "$join" | field binary_bytes)" -gt 0 ] \
   && [ "$(grep -c 'is flowing' "$WORK/warm.log")" -ge 2 ] && [ "${back:-0}" -ge 1 ] && [ "$gone" = 0 ] \
   && ! grep -qF -f "$PAT" "$WORK/warm.log"; then
    ok "9 always: the warm unit's condition skips on_demand and passes always; with no page open the relay serves $held consumer(s) and FFmpeg runs; a page joining gets video after $(echo "$join" | field first_binary_s) s; the warm unit reconnects after a viewer restart; stopping it leaves 0 consumers"
else bad "9 always: cond=$c_od/$c_al held=$held ffmpeg=$running join=$join back=$back gone=$gone log=$(tail -n 4 "$WORK/warm.log" | tr '\n' '|')"; fi
sed -i 's/^CAMERA_VIEW_DOOR_FLAT_MODE=.*/CAMERA_VIEW_DOOR_FLAT_MODE=on_demand/' "$LS/.env"

# ── 10 a docker bridge source ────────────────────────────────────────────────
bridge="$(docker run --rm --add-host "$NAME:$LAN" --entrypoint wget "$CADDY_IMAGE" -q -S -O /dev/null \
          --no-check-certificate "https://$NAME:$PORT/" 2>&1 | awk '/HTTP\//{print $2; exit}')"
[ "$bridge" = 403 ] && ok "10 a docker bridge source -> 403" || bad "10 bridge source: $bridge"

# ── 11 a relay dropout reaches the unit's output redacted ────────────────────
probe ws "$LAN" "$PORT" "$NAME" '/api/ws?src=door' --mse 12 >/dev/null & p1=$!
sleep 4; docker rm -f cvlab-relay >/dev/null 2>&1; sleep 6; wait "$p1"
if grep -qiE 'rtsp|door' "$WORK/unit.log" && ! grep -qF -f "$PAT" "$WORK/unit.log" && ! grep -q 'admin:' "$WORK/unit.log"; then
    ok "11 the relay dropped under a viewer: the unit's output ($(wc -l <"$WORK/unit.log") lines) holds no credential spelling and no userinfo"
else bad "11 unit output: $(grep -iE 'rtsp|door' "$WORK/unit.log" | tail -n 3)"; fi
docker run -d --name cvlab-relay --network host --read-only --cap-drop ALL --security-opt no-new-privileges \
    -v "$WORK/relay/go2rtc.yaml:/config/go2rtc.yaml:ro" "$IMAGE" >/dev/null

# ── 12 check against the lab ─────────────────────────────────────────────────
if [ "$CADDY" = caddy ]; then
    start_caddy "$LS/.env"
    cr check >"$WORK/check.json"; rc_ok=$?
    sed -i "s|^LAN_CIDR=.*|LAN_CIDR=10.0.0.0/8 100.64.0.0/10|" "$WORK/caddy.env"
    docker rm -f caddy >/dev/null 2>&1
    docker run -d --name caddy --network host --env-file "$WORK/caddy.env" \
        -v "$WORK/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$STATE:/srv/camera-view:ro" "$CADDY_IMAGE" >/dev/null
    cr check >"$WORK/check-broad.json"; rc_broad=$?
    if [ "$rc_ok" = 0 ] && [ "$rc_broad" = 1 ] && grep -q "caddy's own LAN_CIDR entry 1" "$WORK/check-broad.json"; then
        ok "12 check: passes against the lab; fails once caddy's own LAN_CIDR holds 10.0.0.0/8"
    else bad "12 check: rc=$rc_ok $(cat "$WORK/check.json") / rc=$rc_broad $(cat "$WORK/check-broad.json")"; fi
else
    echo "SKIP 12 a container named caddy exists here; check reads that one"
fi

stop_viewer
echo "camera-view-lab: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
