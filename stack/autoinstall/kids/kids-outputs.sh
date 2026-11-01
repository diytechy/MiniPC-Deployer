#!/bin/bash
# kids-outputs.sh — keeps the KIDS KIOSK's outputs in order. Implements: SR-139, LLR-1418.
#
# Started by sway (sway.config). The TOUCH OUTPUT shows workspace "kiosk"
# (OtterTube) and owns touch input; every OTHER active output gets one wl-mirror
# of the touch output. Re-run on every sway output event, and every 2 s as a
# backstop for an event that never arrives.
#
# The Owner's requirement (2026-11-01): unplugging the TV, or switching it off,
# must not affect the kiosk. So a mirror whose output is gone is killed BEFORE
# anything else is touched, and every pass ends by putting the kiosk workspace
# back in front on the touch output, whatever sway did on the removal.
#
# WHICH OUTPUT IS THE TOUCH OUTPUT:
#   1. KIDS_TOUCH_OUTPUT (a connector name such as HDMI-A-1, or "make model
#      serial" as sway reports them, which follows the monitor to any socket),
#      when that output is present;
#   2. else the output this session chose before, while it is still present -
#      STICKY, so a TV plugged in later can never take the kiosk just because
#      its connector sorts first (independent review, 2026-11-01);
#   3. else the first active output by name, which is then remembered.
# A kiosk whose touch monitor is unplugged still shows OtterTube somewhere.
#
# IT ENDS WITH SWAY. When sway's IPC goes away this script exits rather than
# polling a dead socket: sway does not kill what it exec'd, and a survivor would
# spin a core (an EOF read never waits) beside the next session's copy.
set -u
log() { logger -t kids-outputs -- "$*" 2>/dev/null || echo "[kids-outputs] $*" >&2; }

# Pure: from `swaymsg -t get_outputs` JSON on stdin, print the active outputs as
# "name<TAB>make model serial", sorted by name.
active_outputs() {
    jq -r '[.[] | select(.active)] | sort_by(.name) | .[] | "\(.name)\t\(.make) \(.model) \(.serial)"'
}

# Pure: given active_outputs lines on stdin, the configured output and the one
# this session remembered, print the touch output's NAME (rules 1-3 above).
touch_output() {
    local want=$1 remembered=${2:-} first='' kept='' name desc
    while IFS=$'\t' read -r name desc; do
        [ -n "$first" ] || first=$name
        if [ -n "$want" ] && { [ "$name" = "$want" ] || [ "$desc" = "$want" ]; }; then
            printf '%s\n' "$name"; return
        fi
        [ -n "$remembered" ] && [ "$name" = "$remembered" ] && kept=$name
    done
    printf '%s\n' "${kept:-$first}"
}

reconcile() {
    local outs names primary o pidf pid src
    outs=$(swaymsg -r -t get_outputs | active_outputs) || return
    [ -n "$outs" ] || { log "no active output"; return; }
    names=$(printf '%s\n' "$outs" | cut -f1)
    primary=$(printf '%s\n' "$outs" | touch_output "${KIDS_TOUCH_OUTPUT:-}" "$(cat "$RUN/touch-output" 2>/dev/null)")
    if [ "$primary" != "$(cat "$RUN/touch-output" 2>/dev/null)" ]; then
        log "touch output: $primary"
        printf '%s\n' "$primary" > "$RUN/touch-output"
    fi

    # 1. Mirrors whose output is gone, that now sit on the touch output, or whose
    #    SOURCE is no longer the touch output: kill. Each pidfile holds
    #    "<pid> <source>", so a mirror of an output that left is never kept.
    for pidf in "$RUN"/*.pid; do
        [ -e "$pidf" ] || continue
        o=$(basename "$pidf" .pid)
        read -r pid src < "$pidf" || true
        if ! printf '%s\n' "$names" | grep -qxF "$o" || [ "$o" = "$primary" ] || [ "${src:-}" != "$primary" ]; then
            kill "$pid" 2>/dev/null && log "mirror on $o removed (pid $pid, source ${src:-?})"
            rm -f "$pidf"
        fi
    done

    # 2. The kiosk workspace and touch input belong to the touch output.
    swaymsg -q "workspace kiosk output $primary; input type:touch map_to_output $primary"
    swaymsg -q "[workspace=\"^kiosk\$\"] move workspace to output $primary" 2>/dev/null

    # 3. Every other output mirrors it. A mirror that keeps dying is retried at
    #    most every 2 s.
    for o in $names; do
        [ "$o" = "$primary" ] && continue
        pidf="$RUN/$o.pid"
        if [ -e "$pidf" ]; then
            read -r pid src < "$pidf" || true
            kill -0 "${pid:-0}" 2>/dev/null && continue
            [ $(( $(date +%s) - $(stat -c %Y "$pidf") )) -lt 2 ] && continue
        fi
        # Process substitution, not a pipe: $! must be the MIRROR's pid, since
        # that is what step 1 kills.
        wl-mirror --fullscreen-output "$o" "$primary" > >(logger -t kids-mirror 2>/dev/null) 2>&1 &
        pid=$!
        echo "$pid $primary" > "$pidf"
        log "mirror $primary -> $o (pid $pid)"
    done

    # 4. Whatever happened, the touch output shows the kiosk.
    swaymsg -q "focus output $primary; workspace kiosk"
}

# Sourced with --lib (the tests): the functions only.
if [ "${1:-}" = "--lib" ]; then return 0; fi

RUN=${XDG_RUNTIME_DIR:?kids-outputs needs XDG_RUNTIME_DIR}/kids-mirrors
# A fresh session starts with no mirrors and no remembered touch output.
rm -rf "$RUN"; mkdir -p "$RUN"
log "configured touch output: ${KIDS_TOUCH_OUTPUT:-none (first active output by name, then sticky)}"
reconcile
( swaymsg -t subscribe -m '["output"]' 2>/dev/null | while read -r _; do echo; done ) |
while true; do
    read -r -t 2 _; rc=$?
    # rc > 128 is the 2 s timeout; any other failure is EOF: sway is gone.
    if [ "$rc" -ne 0 ] && [ "$rc" -le 128 ]; then
        log "sway's IPC closed - stopping, and taking this session's mirrors with it"
        for pidf in "$RUN"/*.pid; do [ -e "$pidf" ] && read -r pid _ < "$pidf" && kill "$pid" 2>/dev/null; done
        exit 0
    fi
    reconcile
done
