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
# The touch output is KIDS_TOUCH_OUTPUT (a connector name such as HDMI-A-1, or
# "make model serial" as sway reports them) when it is present, else the first
# active output by name. When it is ABSENT the kiosk shows on the first output,
# so a kiosk whose touch monitor is unplugged still shows something.
set -u
log() { logger -t kids-outputs -- "$*" 2>/dev/null || echo "[kids-outputs] $*" >&2; }

# Pure: from `swaymsg -t get_outputs` JSON on stdin, print the active outputs as
# "name<TAB>make model serial", sorted by name.
active_outputs() {
    jq -r '[.[] | select(.active)] | sort_by(.name) | .[] | "\(.name)\t\(.make) \(.model) \(.serial)"'
}

# Pure: given active_outputs lines on stdin and the configured name, print the
# touch output's NAME.
touch_output() {
    local want=$1 first='' name desc
    while IFS=$'\t' read -r name desc; do
        [ -n "$first" ] || first=$name
        if [ -n "$want" ] && { [ "$name" = "$want" ] || [ "$desc" = "$want" ]; }; then
            printf '%s\n' "$name"; return
        fi
    done
    printf '%s\n' "$first"
}

reconcile() {
    local outs names primary o pidf pid
    outs=$(swaymsg -r -t get_outputs | active_outputs) || return
    [ -n "$outs" ] || { log "no active output"; return; }
    names=$(printf '%s\n' "$outs" | cut -f1)
    primary=$(printf '%s\n' "$outs" | touch_output "${KIDS_TOUCH_OUTPUT:-}")

    # 1. Mirrors whose output is gone, or that now sit on the touch output: kill.
    for pidf in "$RUN"/*.pid; do
        [ -e "$pidf" ] || continue
        o=$(basename "$pidf" .pid)
        if ! printf '%s\n' "$names" | grep -qxF "$o" || [ "$o" = "$primary" ]; then
            pid=$(cat "$pidf")
            kill "$pid" 2>/dev/null && log "mirror on $o removed (pid $pid)"
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
        if [ -e "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then continue; fi
        if [ -e "$pidf" ] && [ $(( $(date +%s) - $(stat -c %Y "$pidf") )) -lt 2 ]; then continue; fi
        # Process substitution, not a pipe: $! must be the MIRROR's pid, since
        # that is what step 1 kills.
        wl-mirror --fullscreen-output "$o" "$primary" > >(logger -t kids-mirror 2>/dev/null) 2>&1 &
        pid=$!
        echo "$pid" > "$pidf"
        log "mirror $primary -> $o (pid $pid)"
    done

    # 4. Whatever happened, the touch output shows the kiosk.
    swaymsg -q "focus output $primary; workspace kiosk"
}

# Sourced with --lib (the tests): the functions only.
if [ "${1:-}" = "--lib" ]; then return 0; fi

RUN=${XDG_RUNTIME_DIR:?kids-outputs needs XDG_RUNTIME_DIR}/kids-mirrors
mkdir -p "$RUN"
log "touch output: ${KIDS_TOUCH_OUTPUT:-first active output by name}"
reconcile
( swaymsg -t subscribe -m '["output"]' 2>/dev/null | while read -r _; do echo; done ) |
while true; do
    read -r -t 2 _
    reconcile
done
