#!/bin/bash
# kids-kiosk.sh — the KIDS KIOSK session, exec'd by the kiosk user's login on
# tty1 (/etc/profile.d/zz-kids-kiosk.sh). Implements: SR-139, LLR-1417.
#
# Runs sway with the kiosk config and restarts it if it exits. Inside sway,
# kids-outputs.sh owns the outputs (touch output + mirrors) and
# kids-app-loop.sh keeps OtterTube running, so an OtterTube crash never takes
# the compositor with it; this loop is for the compositor itself.
#
# When sway keeps failing at start-up, its last words go ON THE SCREEN (tty1 is
# this script's own terminal while sway is not running), the panel's lesson:
# a black screen that restarts every 3 s tells nobody anything.
set -u

KIOSK_ENV=${KIDS_KIOSK_ENV:-/etc/kids-kiosk/kiosk.env}
SWAY_CONFIG=${KIDS_SWAY_CONFIG:-/etc/kids-kiosk/sway.config}

log() { logger -t kids-kiosk -- "$*" 2>/dev/null || echo "[kids-kiosk] $*"; }

KIDS_TOUCH_OUTPUT='' KIDS_APP_CMD=/opt/kids-kiosk/app/ottertube-desktop
# shellcheck source=/dev/null
[ -r "$KIOSK_ENV" ] && . "$KIOSK_ENV"
export KIDS_TOUCH_OUTPUT KIDS_APP_CMD

# wlroots refuses a software renderer unless told; allow it ONLY without a
# render node (a VM), so a kiosk whose GPU stopped binding stays a failure.
if ls /dev/dri/renderD* >/dev/null 2>&1; then
    log "GPU: render node present - hardware rendering expected"
else
    export WLR_RENDERER_ALLOW_SOFTWARE=1
    log "GPU: NO render node - allowing wlroots' software renderer (expected only in a VM)"
fi

FAST_EXIT_SECS=15
FAST_EXIT_LIMIT=3
fast_exits=0
CRASH_LOG="${XDG_RUNTIME_DIR:-/tmp}/kids-kiosk.last"

while true; do
    log "starting sway ($SWAY_CONFIG)"
    started=$(date +%s)
    sway -c "$SWAY_CONFIG" 2>&1 | tee >(logger -t kids-sway 2>/dev/null) | tail -c 4096 > "$CRASH_LOG"
    rc=${PIPESTATUS[0]}
    ran=$(( $(date +%s) - started ))
    log "sway exited ($rc) after ${ran}s - restarting"
    if [ "$ran" -lt "$FAST_EXIT_SECS" ]; then fast_exits=$(( fast_exits + 1 )); else fast_exits=0; fi
    if [ "$fast_exits" -ge "$FAST_EXIT_LIMIT" ]; then
        fast_exits=0
        clear 2>/dev/null
        printf '\n\n  KIDS KIOSK\n\n'
        printf '  The screen session KEEPS EXITING. Its last output:\n\n'
        if [ -s "$CRASH_LOG" ]; then tail -n 14 "$CRASH_LOG"; else printf '  (no output at all)\n'; fi
        printf '\n  Full history: journalctl -t kids-kiosk -t kids-sway\n'
        printf '  Retrying in 5 minutes.\n'
        sleep 300
    fi
    sleep 3
done
