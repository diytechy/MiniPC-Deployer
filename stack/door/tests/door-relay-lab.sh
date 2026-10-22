#!/usr/bin/env bash
# door-relay-lab.sh - the door relay, recorder and gate commands against a FAKE
# camera, end to end, on one Linux host with docker (SR-105..SR-107).
#
# A LABELLED SUITE, NOT A HERMETIC ONE: it needs root (docker), the pinned image
# (pulled if absent) and a non-loopback IPv4 address on this host to bind the
# relay to (go2rtc exempts loopback clients from RTSP authentication, so the
# test must use an address a LAN client would use). Run it in WSL Ubuntu:
#     wsl -d Ubuntu -u root -e bash stack/door/tests/door-relay-lab.sh
# It never contacts the real camera, hub or panel.
#
# THE FAKE CAMERA is a second go2rtc publishing a 1536x1536 10 fps H.264
# test pattern WITH an audio track, behind a username and a password holding
# '@', ' ', '$' and ':' (the characters that break a naive URL or .env), on a
# bridge network with one published port - so `ss` on this host sees exactly
# the relay's connection to it and nothing else.
#
# What it proves, each a numbered check (TC-1385..TC-1392, and 9: the cost tool):
#   1 the rendered config is 0600 and the relay holds exactly two listeners:
#     RTSP on the LAN address and the API on loopback;
#   2 no credential / a wrong credential -> 401 at RTSP; the API -> 401;
#   3 the probe decodes h264 at 1536x1536 with the doorbell's credential;
#   4 the hub half of the compatibility gate passes: two consumers, ONE
#     upstream session, one producer; a consumer leaving keeps the upstream;
#   5 the panel's own broker input path (OfficeWallNaglight doorstream
#     ffconcat + ffmpeg_args, raw mode) produces JPEG frames from the relay;
#   6 the recorder cuts copy-only, video-only segments at keyframes that decode
#     with `ffmpeg -v error` and carry no audio stream;
#   7 a relay restart mid-recording ends the run; the next run resumes, and
#     continuity reports exactly that one gap;
#   8 nothing in the relay's or the recorder's logs carries the password;
#   9 the cost sampler measures the relay and a 5 fps grey decoder;
#  10 the relay run EXACTLY as its unit runs it (the unit file's own
#     ExecStartPre/ExecStart/ExecStop/ExecStopPost, /opt/homehub/stack moved to
#     a lab stack holding the real docker-compose.yml) with the CAMERA killed
#     under a live consumer - go2rtc's producer then warns with its whole
#     upstream URL: the unit's output (what the journal gets) shows that warning
#     redacted, and neither it nor docker holds the password or the camera's
#     host:port (the container has no log driver to hold anything).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
DOOR="$(cd "$HERE/.." && pwd)"
OWN_REPO="${OWN_REPO:-$(cd "$DOOR/../../.." && pwd)/OfficeWallNaglight}"
IMAGE="$(awk -F= '/^IMAGE=/{i=$2} /^VERSION=/{v=$2} /^DIGEST=/{d=$2} END{print i":"v"@"d}' "$DOOR/door-relay-image.pin")"
WORK="$(mktemp -d /tmp/door-lab.XXXXXX)"
PASS=0 FAIL=0
ok()   { PASS=$((PASS + 1)); echo "PASS $*"; }
bad()  { FAIL=$((FAIL + 1)); echo "FAIL $*"; }
RELAY_RUN=/run/homehub-door-relay   # the unit's RuntimeDirectory; check 10 renders into it
RELAY_RUN_OURS=""
cleanup() {
    docker rm -f door-lab-cam door-lab-relay homehub-door-recorder homehub-door-probe \
        homehub-door-gate-1 homehub-door-gate-2 homehub-door-gate-3 homehub-door-relay >/dev/null 2>&1
    [ -z "$RELAY_RUN_OURS" ] || rm -rf "$RELAY_RUN"
    rm -rf "$WORK"
}
trap cleanup EXIT

[ "$(id -u)" = 0 ] || { echo "SKIP: needs root (docker)"; exit 0; }
command -v docker >/dev/null || { echo "SKIP: no docker"; exit 0; }
LAN="$(ip -4 -o addr show scope global | awk '{split($4,a,"/"); print a[1]; exit}')"
[ -n "$LAN" ] || { echo "SKIP: no non-loopback IPv4 address to bind the relay to"; exit 0; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull -q "$IMAGE" >/dev/null || { echo "FAIL cannot pull $IMAGE"; exit 1; }
export HOMEHUB_MONITOR_OVERRIDE_FILE=/nonexistent

PW='c@m p$ss:1'
cat >"$WORK/cam.json" <<'JSON'
{"api": {"listen": ""}, "rtsp": {"listen": ":8654", "username": "admin", "password": "c@m p$ss:1"},
 "webrtc": {"listen": ""}, "srtp": {"listen": ""}, "log": {"level": "info"},
 "streams": {"H.264": "exec:ffmpeg -hide_banner -v error -re -f lavfi -i testsrc2=size=1536x1536:rate=10 -f lavfi -i sine=frequency=440:sample_rate=8000 -c:v libx264 -preset ultrafast -tune zerolatency -g 20 -pix_fmt yuv420p -c:a pcm_mulaw -f rtsp {output}"}}
JSON
# The .env exactly as Materialize-Deploy would write it: `$` doubled, and the
# whitespace-bearing value in one pair of double quotes.
cat >"$WORK/env" <<EOF
LAN_IP=$LAN
DOOR_RELAY_RTSP_PORT=18554
DOOR_RELAY_API_PORT=19840
DOOR_RELAY_STREAM=door
DOOR_RELAY_UPSTREAM_HOST=127.0.0.1
DOOR_RELAY_UPSTREAM_PORT=8654
DOOR_RELAY_UPSTREAM_PATH=/H.264
DOOR_RELAY_UPSTREAM_USERNAME=admin
DOOR_RELAY_UPSTREAM_PASSWORD="c@m p\$\$ss:1"
EOF
docker rm -f door-lab-cam door-lab-relay >/dev/null 2>&1
docker run -d --name door-lab-cam -p 127.0.0.1:8654:8654 --read-only --tmpfs /tmp \
    -v "$WORK/cam.json:/config/go2rtc.yaml:ro" "$IMAGE" >/dev/null
relay() { python3 "$DOOR/door_relay.py" --env-file "$WORK/env" "$@"; }
start_relay() {
    docker run -d --name door-lab-relay --network host --read-only --cap-drop ALL \
        --security-opt no-new-privileges -v "$WORK/run/go2rtc.yaml:/config/go2rtc.yaml:ro" "$IMAGE" >/dev/null
}

# 1 render + listeners
relay render --out "$WORK/run/go2rtc.yaml" >/dev/null
mode="$(stat -c %a "$WORK/run/go2rtc.yaml")"
start_relay; sleep 4
listeners="$(ss -Hltnp | grep -c '"go2rtc"' || true)"
if [ "$mode" = 600 ] && [ "$listeners" = 2 ] && ss -Hltn | grep -q " $LAN:18554 " && ss -Hltn | grep -q " 127.0.0.1:19840 "; then
    ok "1 config 0600, exactly two listeners ($LAN:18554 rtsp, 127.0.0.1:19840 api)"
else bad "1 mode=$mode listeners=$listeners: $(ss -Hltnp | grep go2rtc | awk '{print $4}' | tr '\n' ' ')"; fi

# 2 authentication
noauth="$(docker run --rm --network host --entrypoint ffprobe "$IMAGE" -v error -rtsp_transport tcp "rtsp://$LAN:18554/door" 2>&1)"
wrong="$(docker run --rm --network host --entrypoint ffprobe "$IMAGE" -v error -rtsp_transport tcp "rtsp://admin:nope@$LAN:18554/door" 2>&1)"
api="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:19840/api/streams)"
case "$noauth$wrong" in *401*401*) [ "$api" = 401 ] && ok "2 no/wrong credential -> 401 (rtsp), api -> 401" || bad "2 api=$api";; *) bad "2 rtsp without the credential was not refused";; esac

# 3 probe
p="$(relay probe --seconds 5)"
echo "$p" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if (d.get("codec"),d.get("width"),d.get("height"))==("h264",1536,1536) and d["packets"]>0 else 1)' \
    && ok "3 probe: h264 1536x1536, packets > 0" || bad "3 probe: $p"

# 4 gate (hub half)
if relay gate --seconds 6 >"$WORK/gate.jsonl"; then ok "4 gate: two consumers, one upstream, one producer; one leaves, upstream kept"
else bad "4 gate: $(cat "$WORK/gate.jsonl")"; fi

# 5 the panel's own input path
if [ -r "$OWN_REPO/doorstream/service.py" ]; then
    frames="$(OWN_REPO="$OWN_REPO" LAN="$LAN" IMAGE="$IMAGE" python3 - <<'PY'
import os, subprocess, sys, time
sys.path.insert(0, os.environ["OWN_REPO"])
from doorstream.service import Config, ffconcat_input, ffmpeg_args, JpegFramer
cfg = Config(host=os.environ["LAN"], password="c@m p$ss:1", port=18554, path="/door", username="admin")
argv = ffmpeg_args(cfg, "raw")
proc = subprocess.Popen(["docker", "run", "--rm", "-i", "--network", "host", "--name", "homehub-door-probe",
                         "--entrypoint", argv[0], os.environ["IMAGE"]] + argv[1:],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
proc.stdin.write(ffconcat_input(cfg)); proc.stdin.close()
framer, n, t0 = JpegFramer(), 0, time.time()
while time.time() - t0 < 8:
    chunk = os.read(proc.stdout.fileno(), 65536)
    if not chunk: break
    n += len(framer.feed(chunk))
subprocess.run(["docker", "rm", "-f", "homehub-door-probe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(n)
PY
)"
    [ "${frames:-0}" -gt 10 ] && ok "5 the panel broker's ffconcat input path: $frames JPEG frames from the relay" \
        || bad "5 panel input path produced ${frames:-0} frames"
else
    echo "SKIP 5 (no OfficeWallNaglight at $OWN_REPO)"
fi

# 6 + 7 recorder: two runs around a relay restart, guard stubbed (no scratch
# volume here; the guard has its own suite), 20 s segments.
mkdir -p "$WORK/door"
record() {  # SECONDS LOG
    DOOR="$DOOR" WORK="$WORK" SECS="$1" python3 - >>"$2" 2>&1 <<'PY'
import os, subprocess, sys, threading, time
sys.path.insert(0, os.environ["DOOR"])
import door_recorder as dr, door_relay
relay = door_relay.RelaySettings(os.path.join(os.environ["WORK"], "env"))
rec = dr.Recorder("/nonexistent", os.path.join(os.environ["WORK"], "door"),
                  dr.RecorderSettings({"DOOR_RECORDER_SEGMENT_SEC": "20", "DOOR_RECORDER_STALL_SEC": "10"}),
                  None, alert=lambda *a: print("ALERT", a))
rec.guard = lambda: (True, "ok (lab stub)")
rec.recover_parts()
envp = os.path.join(os.environ["WORK"], "source.env")
with open(envp, "w") as h:
    h.write("DOOR_URL=%s\n" % relay.consumer_url())
proc = subprocess.Popen(dr.ffmpeg_argv(envp), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
threading.Thread(target=lambda: [print("ffmpeg:", door_relay.redact(l.decode().rstrip())) for l in proc.stderr], daemon=True).start()
threading.Timer(float(os.environ["SECS"]), lambda: setattr(rec, "stopping", True)).start()
code = rec.consume(proc.stdout)
rec.finalise()
subprocess.run(["docker", "rm", "-f", dr.CONTAINER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("consume returned", code)
PY
}
T_SINCE="$(date +%s)"
record 50 "$WORK/rec1.log"
( sleep 15; docker restart door-lab-relay >/dev/null ) &
record 45 "$WORK/rec2.log"; wait
record 30 "$WORK/rec3.log"
T_UNTIL="$(date +%s)"
segs="$(find "$WORK/door" -name 'door-*.ts' | sort)"
nseg="$(printf '%s\n' "$segs" | grep -c . || true)"
# Decode every segment, and count audio streams in each (there must be none).
badseg=0 audio=0
for s in $segs; do
    err="$(docker run --rm --network none -v "$s:/s.ts:ro" --entrypoint ffmpeg "$IMAGE" -v error -i /s.ts -f null - 2>&1)"
    [ -z "$err" ] || { badseg=$((badseg + 1)); echo "  decode errors in $(basename "$s"): $(echo "$err" | head -2)"; }
    a="$(docker run --rm --network none -v "$s:/s.ts:ro" --entrypoint ffprobe "$IMAGE" -v error -select_streams a -show_entries stream=index -of csv=p=0 /s.ts | grep -c . || true)"
    audio=$((audio + a))
done
if [ "$nseg" -ge 5 ] && [ "$badseg" = 0 ] && [ "$audio" = 0 ]; then
    ok "6 recorder: $nseg segments, every one decodes with ffmpeg -v error, no audio stream"
else bad "6 recorder: segments=$nseg undecodable=$badseg audio-streams=$audio"; fi
grep -q "consume returned 1" "$WORK/rec2.log" && ok "7a the relay restart ended the second run (systemd restarts it)" \
    || bad "7a run 2 did not end on the relay restart: $(tail -3 "$WORK/rec2.log")"
# The window is the three runs' own (--since/--until): the gaps BETWEEN segments
# are the restarts; the edges are the first keyframe wait and the last stop.
cont="$(python3 "$DOOR/door_recorder.py" continuity --root "$WORK/door" --since "$T_SINCE" --until "$T_UNTIL")"
echo "$cont" | python3 -c '
import json,sys; d=json.load(sys.stdin)
between = [g for g in d["gap_list"] if g["kind"] == "between"]
sys.exit(0 if 1 <= len(between) <= 3 and d["segments"] >= 5 else 1)' \
    && ok "7b continuity sees the restarts as gaps between segments: $cont" || bad "7b continuity: $cont"

# 8 no secret in any log
logs="$(docker logs door-lab-relay 2>&1; cat "$WORK"/rec*.log)"
if printf '%s' "$logs" | grep -qF -e 'p$ss' -e 'p%24ss'; then bad "8 a log line carries the password"
else ok "8 no relay or recorder log line carries the password"; fi

# 9 the cost sampler (task 11's tool): the relay and a 5 fps grey decoder, measured
c="$(relay cost --seconds 20 --container door-lab-relay --decode-grey-fps 5)"
echo "$c" | python3 -c '
import json,sys; d=json.load(sys.stdin)
ok = all("cpu_pct_one_core" in d[k] for k in ("door-lab-relay", "homehub-door-costprobe")) and d["decoder_still_running"]
sys.exit(0 if ok else 1)  # PSI is reported when the kernel has it' \
    && ok "9 cost: per-cgroup CPU for the relay and the grey decoder, PSI sampled: $c" || bad "9 cost: $c"

# 10 the relay exactly as its UNIT runs it, the camera killed under a consumer
if [ -e "$RELAY_RUN" ]; then
    echo "SKIP 10 ($RELAY_RUN already exists on this host: not ours to render into)"
else
    RELAY_RUN_OURS=1
    docker rm -f door-lab-relay >/dev/null 2>&1   # checks 1-9's relay holds the same ports
    docker start door-lab-cam >/dev/null 2>&1
    UNIT_FILE="$DOOR/homehub-door-relay.service"
    LS="$WORK/stack"                              # /opt/homehub/stack, for the lab
    mkdir -p "$LS/llm-gateway"; : >"$LS/llm-gateway/llm-gateway.env"
    ln -s "$DOOR" "$LS/door"
    cp "$DOOR/../docker-compose.yml" "$LS/docker-compose.yml"
    { cat "$DOOR/../.env.example"; cat "$WORK/env"
      echo "DOOR_RELAY_IMAGE=$IMAGE"; echo "COMPOSE_PROJECT_NAME=doorlab"; } >"$LS/.env"
    unit_lines() { sed -n "s/^$1=//p" "$UNIT_FILE" | sed "s#/opt/homehub/stack#$LS#g"; }
    run_unit_line() {  # one Exec line as systemd would run it: no shell; a leading '-' ignores failure
        local line="$1" ignore=""; local -a argv
        [ "${line#-}" != "$line" ] && { ignore=1; line="${line#-}"; }
        read -ra argv <<<"$line"
        ( cd "$LS" && exec "${argv[@]}" ) || [ -n "$ignore" ]
    }
    JOURNAL="$WORK/journal.log"; : >"$JOURNAL"; pre_ok=1
    while IFS= read -r l; do run_unit_line "$l" >>"$JOURNAL" 2>&1 || pre_ok=0; done < <(unit_lines ExecStartPre)
    run_unit_line "$(unit_lines ExecStart)" >>"$JOURNAL" 2>&1 & upid=$!
    for _ in $(seq 1 60); do ss -Hltn | grep -q " $LAN:18554 " && break; sleep 0.5; done
    # A live consumer, so the relay is holding the camera when it dies.
    lsenv="$WORK/env.unit"; cp "$LS/.env" "$lsenv"
    python3 "$DOOR/door_relay.py" --env-file "$lsenv" probe --seconds 30 >"$WORK/probe10.json" 2>&1 & ppid=$!
    sleep 8
    docker kill door-lab-cam >/dev/null           # the camera drops
    sleep 15                                       # go2rtc's producer warns and retries
    docker rm -f homehub-door-probe >/dev/null 2>&1; wait "$ppid" 2>/dev/null
    while IFS= read -r l; do run_unit_line "$l" >>"$JOURNAL" 2>&1; done < <(unit_lines ExecStop)
    wait "$upid"; urc=$?
    driver="$(docker inspect -f '{{.HostConfig.LogConfig.Type}}' homehub-door-relay 2>&1)"
    dlogs="$(docker logs homehub-door-relay 2>&1)"
    while IFS= read -r l; do run_unit_line "$l" >>"$JOURNAL" 2>&1; done < <(unit_lines ExecStopPost)
    token="$(printf '%s' "admin:$PW" | base64 -w0)"
    leaks="$(printf '%s\n%s\n' "$(cat "$JOURNAL")" "$dlogs" | grep -cF -e 'p$ss' -e 'p%24ss' -e "$token" -e '127.0.0.1:8654' || true)"
    warned="$(grep -c 'url=rtsp://' "$JOURNAL" || true)"
    if [ "$pre_ok" = 1 ] && [ "$warned" -ge 1 ] && [ "$leaks" = 0 ] && [ "$driver" = none ]; then
        ok "10 the unit's relay, camera killed: $warned producer warning(s) in its output, redacted; no password or camera host:port in it or in docker (log driver: $driver; up exited $urc)"
    else
        bad "10 pre_ok=$pre_ok warned=$warned leaking-lines=$leaks driver=$driver up-rc=$urc"
        grep -n 'url=\|WRN\|ERR' "$JOURNAL" | head -5 | sed -e 's/p\$ss/<PW>/g' -e 's/p%24ss/<PW>/g' -e 's/^/    /'
    fi
fi

echo "door-relay-lab: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
