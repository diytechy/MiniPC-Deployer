#!/bin/bash
# Prototype output reconciler. The touch output (KIDS_TOUCH_OUTPUT, else the
# first active output by name) shows workspace "kiosk" and owns touch input;
# every other active output gets one wl-mirror of it on its own workspace.
# Runs on every sway output event, and every 2 s as a backstop.
set -u
LOG=${KIDS_LOG:-/tmp/kids-outputs.log}
RUN=${XDG_RUNTIME_DIR:?}/kids-mirrors
mkdir -p "$RUN"
log() { printf '%s %s\n' "$(date +%T.%3N)" "$*" >> "$LOG"; }

touch_output() {
    local outs=$1
    if [ -n "${KIDS_TOUCH_OUTPUT:-}" ] && printf '%s\n' "$outs" | grep -qxF "$KIDS_TOUCH_OUTPUT"; then
        printf '%s\n' "$KIDS_TOUCH_OUTPUT"
    else
        printf '%s\n' "$outs" | head -n1
    fi
}

reconcile() {
    local outs primary o pidf pid
    outs=$(swaymsg -r -t get_outputs | jq -r '[.[] | select(.active)] | sort_by(.name) | .[].name') || return
    [ -n "$outs" ] || { log "no active output"; return; }
    primary=$(touch_output "$outs")
    # Mirrors whose output is gone: kill first, before anything is moved.
    for pidf in "$RUN"/*.pid; do
        [ -e "$pidf" ] || continue
        o=$(basename "$pidf" .pid)
        if ! printf '%s\n' "$outs" | grep -qxF "$o" || [ "$o" = "$primary" ]; then
            pid=$(cat "$pidf"); kill "$pid" 2>/dev/null && log "mirror on $o gone: killed $pid"
            rm -f "$pidf"
        fi
    done
    swaymsg -q "workspace kiosk output $primary; input type:touch map_to_output $primary"
    swaymsg -q "[workspace=\"^kiosk\$\"] move workspace to output $primary" 2>/dev/null
    for o in $outs; do
        [ "$o" = "$primary" ] && continue
        pidf="$RUN/$o.pid"
        if [ -e "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then continue; fi
        # a mirror that keeps dying is retried at most every 2 s
        if [ -e "$pidf" ] && [ $(( $(date +%s) - $(stat -c %Y "$pidf") )) -lt 2 ]; then continue; fi
        swaymsg -q "workspace mirror-$o output $o"
        wl-mirror --fullscreen-output "$o" "$primary" >> "$LOG" 2>&1 &
        echo $! > "$pidf"
        log "mirror $primary -> $o (pid $!)"
    done
    # Whatever happened, the touch output shows the kiosk.
    swaymsg -q "focus output $primary; workspace kiosk"
}

reconcile
( swaymsg -t subscribe -m '["output"]' 2>/dev/null | while read -r _; do echo; done ) |
while true; do
    read -r -t 2 _ ; reconcile
done
