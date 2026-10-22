#!/usr/bin/env bash
# door-cutover-panel.test.sh - the file-handling verbs of door-cutover-panel.sh
# against a fake panel tree, as an ordinary user (TC-1398, SR-106).
# The systemctl verbs (suppress, regenerate, release, probe, compat) act on the
# real panel and are exercised there by the coordinator's procedure; the
# verdicts of probe and compat are exercised HERE by running each verb's own
# Python body (extracted from the script, not copied) against a fake broker
# socket, a fake `ss` and a fake doorstream package.
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

# ── probe: exit 0 only when live AND the target is a peer while live ────────
heredoc() {  # VERB -> the Python body of that verb's heredoc in the script
    awk -v verb="$1" '$0 ~ "^" verb "\\)$" { inverb = 1; next }
        inverb && /<<.PY.$/ { body = 1; next }
        body && /^PY$/ { exit }
        body { print }' "$SCRIPT"
}
heredoc probe >"$T/probe.py"; heredoc compat >"$T/compat.py"
[ -s "$T/probe.py" ] && [ -s "$T/compat.py" ] || bad "could not extract the probe/compat bodies from the script"
mkdir -p "$T/bin"
# A fake ss: the target peer is established on every call, or (SS_FIRST_ONLY)
# only on the first - before the view was asked for.
cat >"$T/bin/ss" <<'SH'
#!/bin/sh
n=$(cat "$SS_COUNT" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" >"$SS_COUNT"
if [ -n "${SS_FIRST_ONLY:-}" ]; then [ "$n" = 1 ] && echo "0 0 192.0.2.17:40000 192.0.2.10:8554"
else echo "0 0 192.0.2.17:40000 192.0.2.10:8554"; fi
exit 0
SH
chmod +x "$T/bin/ss"
cat >"$T/broker.py" <<'PY'
import socket, sys, time
path, mode = sys.argv[1], sys.argv[2]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.bind(path); s.listen(1)
c, _ = s.accept(); c.recv(1024)
if mode == "live":
    c.sendall(b'{"type":"status","state":"starting"}\n{"type":"status","state":"live"}\n')
    time.sleep(1)
c.close()
PY
probe_case() {  # BROKER_MODE [SS_FIRST_ONLY] -> rc; output in $T/probe.out
    rm -f "$T/sock" "$T/ss.count"
    python3 "$T/broker.py" "$T/sock" "$1" & local bpid=$!
    for _ in $(seq 1 50); do [ -S "$T/sock" ] && break; sleep 0.1; done
    HOST=192.0.2.10 PORT=8554 SECS=3 SOCK="$T/sock" SS_COUNT="$T/ss.count" SS_FIRST_ONLY="${2:-}" \
        PATH="$T/bin:$PATH" timeout 20 python3 "$T/probe.py" >"$T/probe.out" 2>&1
    local rc=$?; kill "$bpid" 2>/dev/null; wait "$bpid" 2>/dev/null; return $rc
}
probe_case eof; rc=$?
if [ "$rc" = 1 ] && grep -q '"live": false' "$T/probe.out" && grep -q '"on_target": true' "$T/probe.out"; then
    ok "probe: an established target peer with an EOF broker (never live) exits 1"
else bad "probe with an EOF broker: rc=$rc $(cat "$T/probe.out")"; fi
probe_case live first-only; rc=$?
if [ "$rc" = 1 ] && grep -q '"live": true' "$T/probe.out" && grep -q '"on_target": false' "$T/probe.out"; then
    ok "probe: a target peer seen only BEFORE the view was asked for does not count (exit 1)"
else bad "probe with a before-only peer: rc=$rc $(cat "$T/probe.out")"; fi
probe_case live; rc=$?
if [ "$rc" = 0 ] && grep -q '"live": true' "$T/probe.out" && grep -q '"on_target": true' "$T/probe.out"; then
    ok "probe: live, with the target a peer while live, exits 0"
else bad "probe live on target: rc=$rc $(cat "$T/probe.out")"; fi

# ── compat: the deadline holds against a silent stream; the decoder always dies
mkdir -p "$T/app/doorstream"; : >"$T/app/doorstream/__init__.py"
cat >"$T/app/doorstream/service.py" <<'PY'
# A stand-in for OfficeWallNaglight doorstream.service: the same names, and a
# "decoder" whose behaviour FAKE_DECODER picks. One byte F on stdout = a frame.
import dataclasses, sys
@dataclasses.dataclass
class Config:
    host: str = "192.0.2.20"
    port: int = 554
    path: str = "/H.264"
    username: str = "admin"
    password: str = "x"
def load_credentials(directory): return Config()
def ffconcat_input(cfg): return b"ffconcat version 1.0\n"
DECODER = r"""
import os, sys, time
sys.stdin.read()
open(os.environ["DECODER_PID"], "w").write(str(os.getpid()))
mode, n = os.environ["FAKE_DECODER"], 0
while True:
    if mode == "frames" or (mode == "frames-then-stall" and n < 3):
        os.write(1, b"F"); n += 1
    time.sleep(0.2)   # "stall", and the tail of "frames-then-stall": open and silent
"""
def ffmpeg_args(cfg, mode): return [sys.executable, "-c", DECODER]
class JpegFramer:
    def feed(self, chunk): return [b"F"] * chunk.count(b"F")
class DecoderDiagnostics:
    def feed(self, noise): pass
    def snapshot(self): return {"counts": {}}
PY
compat_case() {  # FAKE_DECODER SECS -> rc; output in $T/compat.out, wall seconds in $took
    rm -f "$T/decoder.pid"; local t0; t0=$(date +%s)
    HOST=192.0.2.10 PORT=8554 RPATH=/door SECS="$2" CRED="$T/run/wall-door-credentials" APP="$T/app" \
        FAKE_DECODER="$1" DECODER_PID="$T/decoder.pid" timeout 20 python3 "$T/compat.py" >"$T/compat.out" 2>&1
    local rc=$?; took=$(( $(date +%s) - t0 )); return $rc
}
decoder_gone() { local p; p="$(cat "$T/decoder.pid" 2>/dev/null)"; [ -n "$p" ] && ! kill -0 "$p" 2>/dev/null; }
reap_decoder() { decoder_gone || kill -9 "$(cat "$T/decoder.pid" 2>/dev/null)" 2>/dev/null; }
compat_case stall 2; rc=$?
if [ "$rc" = 1 ] && [ "$took" -le 10 ] && grep -q '"stalled": true' "$T/compat.out" \
   && grep -q '"frames": 0' "$T/compat.out" && decoder_gone; then
    ok "compat: a session that stays open and sends nothing ends at the deadline (${took}s), stalled, exit 1, decoder killed"
else bad "compat against a silent stream: rc=$rc took=${took}s $(cat "$T/compat.out")"; reap_decoder; fi
compat_case frames-then-stall 4; rc=$?
if [ "$rc" = 1 ] && [ "$took" -le 12 ] && grep -q '"stalled": true' "$T/compat.out" \
   && grep -q '"frames": 3' "$T/compat.out" && decoder_gone; then
    ok "compat: frames that stop mid-window are a stall (exit 1), counted so far, decoder killed"
else bad "compat frames then stall: rc=$rc took=${took}s $(cat "$T/compat.out")"; reap_decoder; fi
compat_case frames 2; rc=$?
if [ "$rc" = 0 ] && grep -q '"stalled": false' "$T/compat.out" && decoder_gone; then
    ok "compat: a steady stream passes, and the decoder is still killed at the end"
else bad "compat steady stream: rc=$rc $(cat "$T/compat.out")"; reap_decoder; fi

printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ]
