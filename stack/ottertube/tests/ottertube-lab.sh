#!/usr/bin/env bash
# ottertube-lab.sh - the OtterTube downloader, indexer and site against REAL
# containers and ONE real public-domain clip (SR-113, SR-115..SR-117; TC-1441..TC-1443).
#
# A LABELLED LAB SUITE, NOT A HERMETIC ONE. Needs root, docker, loop devices,
# network access to images-assets.nasa.gov, the two pinned images
# (stack/ottertube/ottertube-images.pin) and the local caddy-cloudflare image.
# Meant for WSL Ubuntu on the dev PC or a lab VM - NEVER the hub: it mounts
# loop filesystems at /srv/scratch and refuses if anything is there already.
#
#   sudo bash stack/ottertube/tests/ottertube-lab.sh OTTERTUBE_CHECKOUT
#     OTTERTUBE_CHECKOUT   an OtterTube checkout: its tools/catalog and
#                          app/src/catalog become the indexer, and its dist/web
#                          (if built) the served app
#
# What it proves, all with the real pinned images: ytdl-sub fetches one item
# through ottertube.py's generated subscription (<=480p H.264, the list's
# title, under a read-only root and cap_drop ALL); OtterTube's own indexer
# publishes it as a HARD LINK and writes a catalogue that names it; the media
# server and Caddy's OtterTube site block (the real one, TLS switched to
# `internal`) serve it: the catalogue revalidated, the video with RANGE
# (206), CORS for the app's two origins only, no info.json or dot file
# reachable, 403 off the LAN, / redirected; and on a filesystem too small for
# the clip the real ytdl-sub fails with ENOSPC and the run stops cleanly
# (exit 75, work emptied). The mount guard is stubbed here (no project quota
# on WSL); its refusals are proven by ottertube-quota.root-test.sh.
#
# The clip: NASA MSFC "Engineers Break SLS Rocket Propellant Tank On Purpose"
# (timelapse, ~6 MB at 320x180) - a US Government work, public domain
# (https://www.nasa.gov/nasa-brand-center/images-and-media/). It is fetched
# into the loop filesystems only and deleted with them.
set -u
OTREPO="$(readlink -f "${1:?OTTERTUBE_CHECKOUT}")"
HERE="$(cd "$(dirname "$0")" && pwd)"
STACK="$(cd "$HERE/../.." && pwd)"
PASS=0 FAIL=0
ok()  { PASS=$((PASS + 1)); echo "PASS $*"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL $*"; }
[ "$(id -u)" = 0 ] || { echo "SKIP needs root"; exit 0; }
command -v docker >/dev/null || { echo "SKIP needs docker"; exit 0; }
if [ -e /srv/scratch ] && [ -n "$(ls -A /srv/scratch 2>/dev/null)" ] || mountpoint -q /srv/scratch 2>/dev/null; then
    echo "REFUSED: /srv/scratch exists and is not empty, or is a mount point - this suite is for a lab box only"; exit 2
fi
CLIP_ID="MSFC_2020_XP_SLS_V_0130_PD002-Test-Case-TTF-4697 LOX Tank Test-to-Failure color-1_06242020_timelapse"
CLIP_URL="https://images-assets.nasa.gov/video/$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$CLIP_ID")/$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$CLIP_ID")~mobile.mp4"
CLIP_THUMB="${CLIP_URL%~mobile.mp4}~thumb.jpg"

W="$(mktemp -d /tmp/ottertube-lab.XXXXXX)"
P="$W/stack"              # a throwaway project directory: the real compose file and Caddyfile
mkdir -p "$P/ottertube/config" "$P/ottertube/web" "$P/caddy"
cp "$STACK/docker-compose.yml" "$P/"
cp -r "$STACK/ottertube/ytdl-sub" "$STACK/ottertube/ottertube.py" "$P/ottertube/"
cp -r "$STACK/monitoring" "$STACK/scratch" "$P/"
mkdir -p "$P/ottertube/indexer/tools" "$P/ottertube/indexer/app/src"
cp -r "$OTREPO/tools/catalog" "$P/ottertube/indexer/tools/"
cp -r "$OTREPO/app/src/catalog" "$P/ottertube/indexer/app/src/"
if [ -f "$OTREPO/dist/web/index.html" ]; then cp -r "$OTREPO/dist/web/." "$P/ottertube/web/"; else echo '<!doctype html><title>OtterTube</title>' >"$P/ottertube/web/index.html"; fi
{ cat "$STACK/.env.example"; echo "OTTERTUBE_ENABLED=true"; echo "OTTERTUBE_PAUSE_SEC=0"; } >"$P/.env"
CADDY_TAG="$(sed -n 's/^CADDY_IMAGE_TAG=//p' "$P/.env" | tail -1)"
LOOPS=()
cleanup() {
    docker rm -f homehub-ottertube-media ottertube-lab-caddy ottertube-lab-curl homehub-ottertube-dl homehub-ottertube-index >/dev/null 2>&1
    docker network rm ottertube-lab >/dev/null 2>&1
    (cd "$P" && docker compose --profile ottertube down >/dev/null 2>&1)
    umount -l /srv/scratch 2>/dev/null
    for l in "${LOOPS[@]}"; do losetup -d "$l" 2>/dev/null; done
    rmdir /srv/scratch 2>/dev/null
    rm -rf "$W"
}
trap cleanup EXIT

mkscratch() {  # MiB -> a fresh ext4 at /srv/scratch with ottertube/ in it
    umount -l /srv/scratch 2>/dev/null
    rm -f "$W/scratch.img"; truncate -s "${1}M" "$W/scratch.img"
    mkfs.ext4 -q -m 0 "$W/scratch.img"
    local l; l="$(losetup -f --show "$W/scratch.img")"; LOOPS+=("$l")
    mkdir -p /srv/scratch && mount "$l" /srv/scratch && mkdir -m 0750 /srv/scratch/ottertube /srv/scratch/door /srv/scratch/meetings
}
write_list() {  # the one clip, as the list names it
    cat >"$P/ottertube/config/list.json" <<EOF
{"version": 1, "collections": [{"id": "lab-rockets", "title": "Lab Rockets", "bands": ["g34"], "order": "oldest", "items": [
 {"id": "sls-tank-test", "title": "The Rocket Tank Test", "url": "$CLIP_URL", "thumb": "$CLIP_THUMB", "estimateMiB": 7,
  "licence": "Public domain (US Government work)", "source": "NASA Image and Video Library",
  "basis": "https://www.nasa.gov/nasa-brand-center/images-and-media/"}]}]}
EOF
}
run_runner() {  # real docker, real images; only the guard is stubbed
    (cd "$P" && python3 - "$P" <<'PY'
import sys, os
P = sys.argv[1]
sys.path[:0] = [os.path.join(P, "ottertube"), os.path.join(P, "monitoring"), os.path.join(P, "scratch")]
import ottertube as ot, monitor_common as mc
env = mc.read_knobs(os.path.join(P, ".env"))
r = ot.Runner(ot.Settings(env), ot.Paths("/", P), None, os.path.join(P, ".env"),
              guard=lambda: (True, "lab: guard stubbed"), alert=lambda k, t, b: print("ALERT", k, t))
sys.exit(r.run())
PY
    )
}

# 1 one real item through ytdl-sub, retitled, <= 480p H.264
mkscratch 256; write_list
out1="$(run_runner 2>&1)"; rc1=$?
V=/srv/scratch/ottertube/lib/video/lab-rockets
geo="$(docker run --rm --entrypoint ffprobe -v "$V:/v:ro" "$(sed -n 's/^OTTERTUBE_YTDL_SUB_IMAGE=//p' "$P/.env" | tail -1)" \
       -v error -select_streams v:0 -show_entries stream=codec_name,height -of csv=p=0 /v/sls-tank-test.mp4 2>/dev/null)"
title="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["title"])' "$V/sls-tank-test.info.json" 2>/dev/null)"
if [ "$rc1" = 0 ] && [ -f "$V/sls-tank-test.mp4" ] && [[ "$geo" == h264,* ]] && [ "${geo#h264,}" -le 480 ] \
   && [ "$title" = "The Rocket Tank Test" ] && [ -f "$V/sls-tank-test.jpg" ]; then
    ok "1 ytdl-sub fetched the item: $geo, title from the list, thumbnail from the list"
else bad "1 download: rc=$rc1 geo=$geo title=$title $(echo "$out1" | grep -E 'item |REFUSED|STOPPED' | tail -14)"; fi

# 2 the indexer published it as a HARD LINK and the catalogue names it
cat_json=/srv/scratch/ottertube/catalog/catalog.json
pub="$(find /srv/scratch/ottertube/lib/publish/video -name '*.mp4' 2>/dev/null | head -1)"
if [ -f "$cat_json" ] && [ -n "$pub" ] && [ "$(stat -c %i "$pub")" = "$(stat -c %i "$V/sls-tank-test.mp4")" ] \
   && grep -q '"The Rocket Tank Test"' "$cat_json" && grep -q "media/video/$(basename "$pub")" "$cat_json"; then
    ok "2 indexer: catalogue written, item published as a hard link ($(basename "$pub"), one inode)"
else bad "2 indexer: pub=$pub $(echo "$out1" | grep indexer | tail -5)"; fi

# 3 a second run fetches nothing
out3="$(run_runner 2>&1)"; rc3=$?
[ "$rc3" = 0 ] && [[ "$out3" == *"present 1"* ]] && ok "3 an unchanged list: nothing fetched (present 1)" || bad "3 rc=$rc3 $(echo "$out3" | tail -2)"

# 4 the site: the media server + Caddy with the REAL OtterTube block (tls internal)
up_out="$(cd "$P" && docker compose --profile ottertube up -d --no-deps ottertube-media 2>&1)"
echo "     media server: $(docker ps -a --filter name=homehub-ottertube-media --format '{{.Status}}') $(echo "$up_out" | tail -1)"
awk '/^\{\$OTTERTUBE_HOST\} \{/{on=1} on{print} on && /^\}/{exit}' "$STACK/caddy/Caddyfile" \
    | sed 's/^    import acme_cloudflare$/    tls internal/' >"$P/caddy/ottertube.caddy"
{ printf '{\n    servers {\n        protocols h1 h2\n    }\n}\n'; cat "$P/caddy/ottertube.caddy"; } >"$P/caddy/Caddyfile"
docker network create ottertube-lab >/dev/null
docker network connect ottertube-lab homehub-ottertube-media
start_caddy() {  # LAN_CIDR
    docker rm -f ottertube-lab-caddy >/dev/null 2>&1
    docker run -d --name ottertube-lab-caddy --network ottertube-lab -e OTTERTUBE_HOST=ottertube.lab.test -e LAN_CIDR="$1" \
        -v "$P/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$P/ottertube/web:/srv/ottertube/web:ro" \
        "caddy-cloudflare:$CADDY_TAG" >/dev/null
    sleep 3
}
CURL=(docker run --rm --network ottertube-lab "caddy-cloudflare:$CADDY_TAG" sh -c)
lab_curl() { "${CURL[@]}" "apk add -q --no-cache curl >/dev/null 2>&1; ip=\$(getent hosts ottertube-lab-caddy | cut -d' ' -f1); curl -sk --resolve ottertube.lab.test:443:\$ip $*"; }
start_caddy "0.0.0.0/0"
media_path="/ottertube/media/video/$(basename "$pub")"
cat_h="$(lab_curl -D- -o /dev/null https://ottertube.lab.test/ottertube/catalog.json)"
range_h="$(lab_curl -D- -o /dev/null -r 0-99 https://ottertube.lab.test$media_path)"
cors_app="$(lab_curl -D- -o /dev/null -H "'Origin: app://ottertube'" https://ottertube.lab.test$media_path | grep -i '^access-control-allow-origin')"
cors_bad="$(lab_curl -D- -o /dev/null -H "'Origin: https://evil.example'" https://ottertube.lab.test$media_path | grep -ic '^access-control-allow-origin')"
info_code="$(lab_curl -o /dev/null -w '%{http_code}' https://ottertube.lab.test/ottertube/media/video/sls-tank-test.info.json)"
dot_code="$(lab_curl -o /dev/null -w '%{http_code}' https://ottertube.lab.test/ottertube/media/.publish-state.json)"
root_h="$(lab_curl -D- -o /dev/null https://ottertube.lab.test/)"
web_code="$(lab_curl -o /dev/null -w '%{http_code}' https://ottertube.lab.test/ottertube/)"
if [[ "$cat_h" == *" 200"* && "${cat_h,,}" == *"cache-control: no-cache"* ]]; then ok "4a catalogue: 200, Cache-Control no-cache"; else bad "4a catalogue: $(echo "$cat_h" | head -6)"; fi
if [[ "$range_h" == *" 206"* && "${range_h,,}" == *"content-range: bytes 0-99/"* ]]; then ok "4b media with Range 0-99: 206 Partial Content, $(echo "$range_h" | grep -i '^content-range' | tr -d '\r')"
else bad "4b range: $(echo "$range_h" | head -8)"; fi
if [[ "$cors_app" == *"app://ottertube"* ]] && [ "$cors_bad" = 0 ]; then ok "4c CORS: app://ottertube allowed, another origin not"; else bad "4c CORS: app=[$cors_app] other=$cors_bad"; fi
if [ "$info_code" = 404 ] && [ "$dot_code" = 404 ]; then ok "4d an info.json and the indexer's dot file: 404"; else bad "4d info=$info_code dot=$dot_code"; fi
if [[ "$root_h" == *" 302"* && "$root_h" == *"/ottertube/"* ]] && [ "$web_code" = 200 ]; then ok "4e / redirects to /ottertube/, the web build answers 200"; else bad "4e root=$(echo "$root_h" | head -3) web=$web_code"; fi
start_caddy "10.255.255.0/24"
lan_code="$(lab_curl -o /dev/null -w '%{http_code}' https://ottertube.lab.test/ottertube/catalog.json)"
[ "$lan_code" = 403 ] && ok "4f a client outside LAN_CIDR: 403" || bad "4f off-LAN: $lan_code"
docker rm -f ottertube-lab-caddy >/dev/null 2>&1; docker network disconnect ottertube-lab homehub-ottertube-media >/dev/null 2>&1
(cd "$P" && docker compose --profile ottertube stop ottertube-media >/dev/null 2>&1); docker rm -f homehub-ottertube-media >/dev/null 2>&1

# 5 a filesystem too small for the clip: the REAL ytdl-sub hits ENOSPC, the run stops cleanly
mkscratch 4; write_list
out5="$(run_runner 2>&1)"; rc5=$?
left="$(find /srv/scratch/ottertube/work -mindepth 1 2>/dev/null | wc -l)"
seen="$(echo "$out5" | grep -o 'STOPPED - ENOSPC[^;]*' | head -1)"
if [ "$rc5" = 75 ] && [ -n "$seen" ] && [ "$left" = 0 ] && [[ "$out5" == *"ALERT ottertube-quota"* ]]; then
    ok "5 a full filesystem: the real ytdl-sub's ENOSPC stopped the run (exit 75), work emptied, alert: $seen"
else bad "5 ENOSPC: rc=$rc5 left=$left $(echo "$out5" | tail -6)"; fi

echo "ottertube-lab: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
