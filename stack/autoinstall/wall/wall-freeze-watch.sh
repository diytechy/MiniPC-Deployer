#!/bin/bash
# Wall panel freeze watchdog.
#
# WHY. Three wedges on 2026-09-15 produced no log line at all. The only trace
# was that the compositor's chronic EBUSY noise STOPPED -- a signal you can read
# only in hindsight, buried in thousands of lines. Every wedge had the same
# signature: `grim` never returns, `cage` sits in ep_poll (idle, NOT blocked in
# a DRM ioctl), the Electron main process keeps logging, and the screen holds
# its last frame. That combination says the CLIENT stopped submitting frames and
# the compositor is waiting for one -- not a GPU hang, not OOM, not a crash.
#
# So this does two things the journal could not:
#   1. Emits a positive heartbeat every cycle. Its ABSENCE is now an explicit
#      freeze marker rather than something you have to infer.
#   2. On detection, captures the forensic bundle that had to be gathered by
#      hand -- BEFORE anybody restarts the session and destroys it.
#
# Deliberately does NOT recover by default: a restart wipes the evidence, and
# these freezes are still unexplained. Set RECOVER=1 in the env file once you
# would rather have the panel back than the next bundle.
set -uo pipefail

ENV_FILE=/etc/wall-panel/freeze-watch.env
# shellcheck disable=SC1090
[[ -r $ENV_FILE ]] && . "$ENV_FILE"

INTERVAL=${INTERVAL:-30}        # seconds between probes
PROBE_TIMEOUT=${PROBE_TIMEOUT:-8}
FAIL_THRESHOLD=${FAIL_THRESHOLD:-2}   # consecutive failures before "frozen"
RECOVER=${RECOVER:-0}           # 1 = restart getty@tty1 after capturing
KEEP=${KEEP:-20}                # bundles to retain
SEAT_USER=${SEAT_USER:-panel}
SEAT_UID=${SEAT_UID:-1000}
OUT_ROOT=${OUT_ROOT:-/var/log/wall-freeze}
RUN_DIR=/run/wall-freeze-watch
# The watchdog's last verdict as one small JSON file (2026-10-26, for the hub's
# metrics log: the meeting account's forced command reads it for `status`).
# Its own root-owned directory, not RUN_DIR: the probe writes RUN_DIR as the
# seat user, who must not be able to rewrite the verdict on itself.
STATUS_DIR=${STATUS_DIR:-/run/wall-freeze-state}

mkdir -p "$OUT_ROOT"
# The probe runs AS THE SEAT USER, so the directory it writes into must belong
# to that user. Left root-owned, every probe fails on a perfectly healthy panel
# -- and the failure is invisible if you only ever test against a frozen one.
install -d -o "$SEAT_USER" -g "$SEAT_USER" -m 0755 "$RUN_DIR"

install -d -o root -g root -m 0755 "$STATUS_DIR" 2>/dev/null || true

log() { printf '[freeze-watch] %s\n' "$*"; }

# write_status RESULT DRAW_S -> $STATUS_DIR/status.json, replaced atomically.
# A failure to write it never touches the watchdog itself.
write_status() {
    local tmp="$STATUS_DIR/.status.$$"
    if printf '{"ts":%s,"result":"%s","draw_s":%s,"fails":%d,"threshold":%d,"captured":%d,"recover":%d}\n' \
            "$(date +%s)" "$1" "${2:-null}" "${fails:-0}" "${FAIL_THRESHOLD:-0}" "${captured:-0}" "${RECOVER:-0}" \
            > "$tmp" 2>/dev/null && chmod 0644 "$tmp" 2>/dev/null; then
        mv -f "$tmp" "$STATUS_DIR/status.json" 2>/dev/null || rm -f "$tmp" 2>/dev/null
    else
        rm -f "$tmp" 2>/dev/null
    fi
    return 0
}

# cage picks its own socket name; guessing wayland-0 is how you get a hang that
# has nothing to do with the bug you are chasing.
wayland_socket() {
    local sock
    sock=$(find "/run/user/$SEAT_UID" -maxdepth 1 -name 'wayland-[0-9]*' ! -name '*.lock' \
           -printf '%f\n' 2>/dev/null | sort | head -1)
    printf '%s' "${sock:-wayland-0}"
}

# Returns 0 and prints the elapsed seconds when the compositor produced a frame.
# On failure, leaves grim's own complaint in PROBE_ERR: a wedged compositor says
# nothing and simply times out, whereas a BROKEN PROBE (bad socket, unwritable
# output path, missing binary) says exactly what is wrong. Without this the two
# are indistinguishable, and a broken probe reads as a permanently frozen panel.
PROBE_ERR=""
probe() {
    local sock start end status
    sock=$(wayland_socket)
    start=$(date +%s.%N)
    PROBE_ERR=$(runuser -u "$SEAT_USER" -- env "XDG_RUNTIME_DIR=/run/user/$SEAT_UID" \
            "WAYLAND_DISPLAY=$sock" timeout "$PROBE_TIMEOUT" \
            grim "$RUN_DIR/probe.png" 2>&1 >/dev/null)
    status=$?
    if (( status == 0 )); then
        end=$(date +%s.%N)
        awk -v a="$start" -v b="$end" 'BEGIN{printf "%.2f", b-a}'
        return 0
    fi
    # 124 is timeout(1)'s "it never returned" -- the genuine wedge signature.
    [[ $status != 124 && -z $PROBE_ERR ]] && PROBE_ERR="exit $status"
    return 1
}

capture() {
    local dir="$OUT_ROOT/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$dir"
    local cage_pid
    cage_pid=$(pgrep -x cage | head -1)

    {
        echo "captured: $(date -Is)"
        echo "cage pid: ${cage_pid:-none}"
        echo "wayland socket: $(wayland_socket)"
        echo "probe timeout: ${PROBE_TIMEOUT}s, consecutive failures: $fails"
    } > "$dir/summary.txt"

    # The discriminator. ep_poll => compositor idle waiting for a client frame
    # (client-side stall). A DRM/fence wchan => compositor stuck in the kernel.
    if [[ -n ${cage_pid:-} ]]; then
        cat "/proc/$cage_pid/wchan" > "$dir/cage.wchan" 2>/dev/null; echo >> "$dir/cage.wchan"
        cat "/proc/$cage_pid/stack" > "$dir/cage.stack" 2>/dev/null
        cat "/proc/$cage_pid/status" > "$dir/cage.status" 2>/dev/null
    fi

    cat /sys/kernel/debug/dri/1/state > "$dir/drm-state.txt" 2>/dev/null
    ps -eo pid,ppid,stat,pcpu,pmem,etime,wchan:24,args --sort=-pcpu \
        > "$dir/ps.txt" 2>/dev/null
    top -bn2 -d 2 -w 200 > "$dir/top.txt" 2>/dev/null
    free -m > "$dir/mem.txt" 2>/dev/null
    dmesg -T 2>/dev/null | tail -120 > "$dir/dmesg.txt"
    fuser -v /dev/dri/card* > "$dir/dri-holders.txt" 2>&1
    journalctl -b -t wall-kiosk --no-pager 2>/dev/null | tail -400 > "$dir/kiosk.log"
    journalctl -b --no-pager --since "-10 min" 2>/dev/null \
        | grep -v "Atomic commit failed" | tail -400 > "$dir/system.log"
    # The last [door] tuple carries the whole gate state in one line.
    journalctl -b -t wall-kiosk --no-pager 2>/dev/null | grep '\[door\]' | tail -5 \
        > "$dir/door-last.log"

    log "CAPTURED $dir"
    # Prune oldest bundles; this runs on a panel, not a log server.
    local count
    count=$(find "$OUT_ROOT" -maxdepth 1 -mindepth 1 -type d | wc -l)
    if (( count > KEEP )); then
        find "$OUT_ROOT" -maxdepth 1 -mindepth 1 -type d -printf '%T@ %p\n' \
            | sort -n | head -n $(( count - KEEP )) | cut -d' ' -f2- \
            | xargs -r rm -rf
    fi
}

fails=0
captured=0
last_check=$(date -Is)
log "started: interval=${INTERVAL}s probe_timeout=${PROBE_TIMEOUT}s threshold=$FAIL_THRESHOLD recover=$RECOVER"

while true; do
    # Roll the compositor's chronic EBUSY noise up into one number per cycle, so
    # the journal stays readable AND a missing line is itself a freeze marker.
    ebusy=$(journalctl -b -t wall-kiosk --no-pager --since "$last_check" 2>/dev/null \
            | grep -c "Atomic commit failed")
    last_check=$(date -Is)

    if elapsed=$(probe); then
        if (( fails >= FAIL_THRESHOLD )); then
            log "RECOVERED after $fails failed probes"
        fi
        fails=0
        captured=0
        log "ok draw=${elapsed}s ebusy=${ebusy}"
        write_status ok "$elapsed"
    else
        fails=$(( fails + 1 ))
        log "NO FRAME (${fails}/${FAIL_THRESHOLD}) probe>${PROBE_TIMEOUT}s ebusy=${ebusy}${PROBE_ERR:+ err=\"$PROBE_ERR\"}"
        if (( fails >= FAIL_THRESHOLD && captured == 0 )); then
            log "FROZEN - compositor produced no frame for $(( fails * (INTERVAL + PROBE_TIMEOUT) ))s"
            capture
            captured=1
            if (( RECOVER == 1 )); then
                log "RECOVER=1, restarting getty@tty1"
                systemctl restart getty@tty1.service
            fi
        fi
        write_status no-frame ""
    fi
    sleep "$INTERVAL"
done
