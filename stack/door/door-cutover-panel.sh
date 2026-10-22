#!/usr/bin/env bash
# door-cutover-panel.sh - the PANEL half of the door relay cutover and rollback
# (plan W4b task 10; SR-105). Copied to the panel's /tmp and run as root by the
# coordinator, one verb at a time, in the order stack/door/README.md gives.
# It changes nothing on its own initiative: each verb does one step and prints
# what it found. It prints NO secret - credential values travel only through
# `export-upstream`'s stdout, which is meant for a pipe straight into the hub.
#
#   export-upstream [--refuse-host IP]
#                     NUL-separated host/port/path/username/password records of
#                     the credentials the broker uses NOW, for
#                     `door_relay.py import-upstream` on the hub. Refuses when
#                     the host is IP (the panel already points at the relay).
#   prepare           rollback material, as the actual inputs: the DOORBELL_RTSP_*
#                     lines of wall.env and every /run/wall-door-credentials file,
#                     into /var/lib/wall-door-cutover/<UTC stamp>/ (0700 root).
#                     Prints the directory and the files' sha256 only.
#   suppress          runtime-mask and stop the broker; wait until no process of
#                     wall-door-stream remains. The kiosk cannot restart it.
#   point DIR HOST PORT PATH
#                     rewrite wall.env's DOORBELL_RTSP_HOST/PORT/PATH (nothing
#                     else), after checking DIR is a prepared directory.
#   restore DIR       put DIR's saved DOORBELL_RTSP_* lines back in wall.env.
#   regenerate DIR    re-run /usr/local/sbin/wall-firstboot.sh - the supported
#                     way to apply wall.env (its header says so) - log into DIR,
#                     then report, without values, whether the regenerated
#                     host/port/path credentials equal wall.env's and whether
#                     the broker is unmasked. firstboot's step 8d unmasks and
#                     restarts the broker itself.
#   compare DIR       after a rollback: is every regenerated credential file
#                     byte-identical to the one saved in DIR? (cmp, no output)
#   release           unmask (runtime) and start the broker, if a step above
#                     left it masked.
#   probe HOST PORT [SECONDS]
#                     the broker live on HOST:PORT? Starts the broker if it is
#                     inactive, asks it for a raw view on its socket as root, and
#                     waits for `state: live`; reports the panel's established
#                     TCP peers on port 554 and on PORT (addresses only).
#   compat HOST PORT PATH [SECONDS]
#                     the compatibility-gate replay: the INSTALLED broker's own
#                     ffconcat input and ffmpeg arguments (doorstream.service),
#                     pointed at HOST:PORT PATH with the broker's username and
#                     password, run for SECONDS; reports JPEG frames and the
#                     decoder's diagnostic counts. Needs the broker suppressed.
set -euo pipefail

ENV_FILE=/etc/wall-panel/wall.env
CRED=/run/wall-door-credentials
BASE=/var/lib/wall-door-cutover
UNIT=wall-door-stream.service
SOCK=/run/wall-door-stream/service.sock
APP=/opt/wall-panel/app/runtime/resources/app
FIRSTBOOT=/usr/local/sbin/wall-firstboot.sh
FIELDS="host port path username password"

die() { echo "door-cutover-panel: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
need_dir() { [ -d "${1:-}" ] && [ -f "$1/wall.env.door-lines" ] && [ -d "$1/credentials" ] || die "'${1:-}' is not a prepared cutover directory"; }

# The value wall.env gives KEY, as wall-firstboot sees it (the file is sourced).
env_value() { ( set +u; set -a; . "$ENV_FILE" >/dev/null 2>&1; eval "printf '%s' \"\${$1:-}\"" ); }

# Replace (or append) KEY=VALUE in wall.env, atomically, keeping its mode/owner.
set_env_line() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp "$ENV_FILE.cutover.XXXXXX")"
    chmod --reference="$ENV_FILE" "$tmp"; chown --reference="$ENV_FILE" "$tmp"
    awk -v k="$key" -v line="$key=$value" '
        index($0, k "=") == 1 { if (!done) print line; done = 1; next } { print }
        END { if (!done) print line }' "$ENV_FILE" >"$tmp"
    mv -f "$tmp" "$ENV_FILE"
}

case "${1:-}" in
export-upstream)
    refuse="${3:-}"; [ "${2:-}" = "--refuse-host" ] || refuse=""
    for f in $FIELDS; do [ -s "$CRED/$f" ] || die "no $CRED/$f (is the broker configured?)"; done
    if [ -n "$refuse" ] && [ "$(cat "$CRED/host")" = "$refuse" ]; then
        die "the broker already points at $refuse - these are the RELAY's values, not the camera's"
    fi
    for f in $FIELDS; do printf '%s=' "$f"; cat "$CRED/$f"; printf '\0'; done
    ;;
prepare)
    [ -r "$ENV_FILE" ] || die "no $ENV_FILE"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"; dir="$BASE/$stamp"
    install -d -m 0700 -o root -g root "$BASE" "$dir" "$dir/credentials"
    grep '^DOORBELL_RTSP_' "$ENV_FILE" >"$dir/wall.env.door-lines" || die "wall.env has no DOORBELL_RTSP_ lines"
    chmod 0600 "$dir/wall.env.door-lines"
    cp -a "$CRED/." "$dir/credentials/"
    (cd "$dir" && sha256sum wall.env.door-lines credentials/* >SHA256SUMS && chmod 0600 SHA256SUMS)
    echo "prepared $dir"
    cat "$dir/SHA256SUMS"
    ;;
suppress)
    systemctl mask --runtime "$UNIT" >/dev/null
    systemctl stop "$UNIT" || true
    for _ in $(seq 1 20); do
        if ! systemctl is-active --quiet "$UNIT" && ! pgrep -u wall-door-stream >/dev/null; then
            echo "suppressed: $UNIT inactive and masked (runtime); no wall-door-stream process"; exit 0
        fi
        sleep 0.5
    done
    die "the broker did not stop within 10 s"
    ;;
point)
    need_dir "${2:-}"
    host="${3:-}" port="${4:-}" path="${5:-}"
    [[ "$host" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "HOST must be an IPv4 address"
    [[ "$port" =~ ^[0-9]{1,5}$ ]] || die "PORT must be a number"
    [[ "$path" =~ ^/[A-Za-z0-9._/-]*$ ]] || die "PATH must start with / and be plain"
    set_env_line DOORBELL_RTSP_HOST "$host"
    set_env_line DOORBELL_RTSP_PORT "$port"
    set_env_line DOORBELL_RTSP_PATH "$path"
    echo "wall.env now points the broker at $host:$port$path (credential lines untouched)"
    ;;
restore)
    need_dir "${2:-}"
    tmp="$(mktemp "$ENV_FILE.cutover.XXXXXX")"
    chmod --reference="$ENV_FILE" "$tmp"; chown --reference="$ENV_FILE" "$tmp"
    # The saved lines go back where the first DOORBELL_RTSP_ line is now.
    awk -v saved="$2/wall.env.door-lines" '
        /^DOORBELL_RTSP_/ { if (!done) { while ((getline l < saved) > 0) print l; done = 1 } next }
        { print } END { if (!done) while ((getline l < saved) > 0) print l }' "$ENV_FILE" >"$tmp"
    mv -f "$tmp" "$ENV_FILE"
    if diff -q <(grep '^DOORBELL_RTSP_' "$ENV_FILE") "$2/wall.env.door-lines" >/dev/null; then
        echo "wall.env DOORBELL_RTSP_ lines restored from $2 (byte-identical)"
    else die "the restored lines differ from $2/wall.env.door-lines"; fi
    ;;
regenerate)
    need_dir "${2:-}"
    log="$2/firstboot-$(date -u +%H%M%S).log"
    rc=0; "$FIRSTBOOT" >"$log" 2>&1 || rc=$?
    chmod 0600 "$log"
    echo "wall-firstboot.sh exit $rc (log: $log; the verdict is the artifact below, not this code)"
    for f in host port path; do
        key="DOORBELL_RTSP_$(echo "$f" | tr a-z A-Z)"
        want="$(env_value "$key")"
        [ -n "$want" ] || { [ "$f" = port ] && want=554; } || true
        [ -n "$want" ] || { [ "$f" = path ] && want=/H.264; } || true
        if [ "$(cat "$CRED/$f" 2>/dev/null)" = "$want" ]; then echo "  $CRED/$f matches wall.env"
        else echo "  $CRED/$f DOES NOT match wall.env"; rc=99; fi
    done
    state="$(systemctl is-enabled "$UNIT" 2>/dev/null || true)"
    echo "  $UNIT unit file state: $state (masked-runtime means a step left it suppressed)"
    [ "$rc" != 99 ] || exit 1
    ;;
compare)
    need_dir "${2:-}"
    bad=0
    for f in "$2"/credentials/*; do
        n="$(basename "$f")"
        if cmp -s "$f" "$CRED/$n"; then :; else echo "  $n differs from the saved copy"; bad=1; fi
    done
    [ "$bad" = 0 ] && echo "every regenerated credential is byte-identical to $2" || exit 1
    ;;
release)
    systemctl unmask --runtime "$UNIT" >/dev/null 2>&1 || true
    systemctl start "$UNIT"
    systemctl is-active "$UNIT"
    ;;
probe)
    host="${2:-}" port="${3:-}" secs="${4:-25}"
    [ -n "$host" ] && [ -n "$port" ] || die "probe HOST PORT [SECONDS]"
    systemctl is-active --quiet "$UNIT" || systemctl start "$UNIT"
    HOST="$host" PORT="$port" SECS="$secs" SOCK="$SOCK" python3 - <<'PY'
import json, os, socket, subprocess, time
host, port, secs = os.environ["HOST"], int(os.environ["PORT"]), float(os.environ["SECS"])
def peers():
    out = subprocess.run(["ss", "-Htn", "state", "established"], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 4 and (cols[3].endswith(":554") or cols[3].endswith(":%d" % port)):
            found.append(cols[3])
    return sorted(found)
result = {"target": "%s:%d" % (host, port), "live": False, "busy": False}
before = peers()
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(secs)
try:
    s.connect(os.environ["SOCK"])
    s.sendall(b'{"mode":"raw","visible":false}\n')
    buf, t0 = b"", time.time()
    while time.time() - t0 < secs:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
        for line in buf.split(b"\n")[:-1]:
            msg = json.loads(line)
            if msg.get("type") == "status":
                result["last_state"] = msg.get("state")
                if msg.get("state") == "live":
                    result["live"] = True
        buf = buf.split(b"\n")[-1]
        if result["live"]:
            break
except socket.timeout:
    result["busy"] = True
result["peers_while_live"] = peers()
s.close()
result["peers_before"] = before
result["on_target"] = ("%s:%d" % (host, port)) in result["peers_while_live"] + before
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result["on_target"] else 1)
PY
    ;;
compat)
    host="${2:-}" port="${3:-}" path="${4:-}" secs="${5:-15}"
    [ -n "$path" ] || die "compat HOST PORT PATH [SECONDS]"
    systemctl is-active --quiet "$UNIT" && die "suppress the broker first (the camera takes one connection)"
    HOST="$host" PORT="$port" RPATH="$path" SECS="$secs" CRED="$CRED" APP="$APP" python3 - <<'PY'
import json, os, subprocess, sys, time
sys.path.insert(0, os.environ["APP"])
from doorstream.service import (load_credentials, ffconcat_input, ffmpeg_args, JpegFramer,
                                DecoderDiagnostics)
import dataclasses
cfg = dataclasses.replace(load_credentials(os.environ["CRED"]), host=os.environ["HOST"],
                          port=int(os.environ["PORT"]), path=os.environ["RPATH"])
proc = subprocess.Popen(ffmpeg_args(cfg, "raw"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
proc.stdin.write(ffconcat_input(cfg)); proc.stdin.close()
os.set_blocking(proc.stderr.fileno(), False)
framer, diag, frames, sizes, t0 = JpegFramer(), DecoderDiagnostics(), 0, set(), time.time()
while time.time() - t0 < float(os.environ["SECS"]):
    chunk = os.read(proc.stdout.fileno(), 65536)
    if not chunk:
        break
    for frame in framer.feed(chunk):
        frames += 1
    try:
        noise = os.read(proc.stderr.fileno(), 65536)
        if noise:
            diag.feed(noise)
    except BlockingIOError:
        pass
proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
print(json.dumps({"target": "%s:%s%s" % (os.environ["HOST"], os.environ["PORT"], os.environ["RPATH"]),
                  "frames": frames, "seconds": round(time.time() - t0, 1),
                  "decoder": diag.snapshot()["counts"]}, sort_keys=True))
raise SystemExit(0 if frames > 0 else 1)
PY
    ;;
*)
    sed -n '2,/^set -euo/p' "$0" | sed '$d'
    exit 2
    ;;
esac
