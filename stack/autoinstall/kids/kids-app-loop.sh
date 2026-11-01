#!/bin/bash
# kids-app-loop.sh — keeps OtterTube running inside the kids kiosk's sway
# session. Implements: SR-139, LLR-1417.
#
# Started by sway (sway.config). An OtterTube crash or exit (Alt+F4 still
# closes it) brings it straight back. Three exits inside 15 s back off to one
# attempt a minute, so a broken build does not spin the CPU.
set -u
APP=${KIDS_APP_CMD:-/opt/kids-kiosk/app/ottertube-desktop}
log() { logger -t kids-app -- "$*" 2>/dev/null || echo "[kids-app] $*" >&2; }

fast=0
while true; do
    if [ ! -x "$APP" ]; then
        log "FATAL: $APP is not executable - OtterTube is NOT INSTALLED; retrying hourly"
        sleep 3600; continue
    fi
    started=$(date +%s)
    log "starting $APP"
    "$APP" --ozone-platform=wayland 2>&1 | logger -t kids-app-out 2>/dev/null
    rc=${PIPESTATUS[0]}
    ran=$(( $(date +%s) - started ))
    log "OtterTube exited ($rc) after ${ran}s"
    if [ "$ran" -lt 15 ]; then fast=$(( fast + 1 )); else fast=0; fi
    if [ "$fast" -ge 3 ]; then log "OtterTube keeps exiting at start-up - next attempt in 60 s"; fast=0; sleep 60; fi
    sleep 2
done
