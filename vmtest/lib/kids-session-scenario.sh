#!/bin/bash
# vmtest/lib/kids-session-scenario.sh — runs as the `kiosk` user inside
# test-kids-session.sh's container: the REAL kids-kiosk.sh (which starts sway
# with the image's sway.config) on sway's headless backend.
#
#   main          one output at start; a TV plugged, unplugged and replugged,
#                 each sampled every 100 ms; then sway killed mid-session
#   tv-at-start   the TV is there before OtterTube and its connector sorts
#                 FIRST; KIDS_TOUCH_OUTPUT names the other one
#
# Its exit status is the number of failed checks.
set -u
MODE=${1:-main}
FAIL=0
ok()  { printf 'PASS %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAIL=$((FAIL + 1)); }

export XDG_RUNTIME_DIR=/tmp/xdg-kiosk-$MODE
mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman
if [ "$MODE" = tv-at-start ]; then
    export WLR_HEADLESS_OUTPUTS=2
    # HEADLESS-1 is the TV and sorts first; the touch monitor is HEADLESS-2.
    sed 's/^KIDS_TOUCH_OUTPUT=.*/KIDS_TOUCH_OUTPUT=HEADLESS-2/' /etc/kids-kiosk/kiosk.env > /tmp/kiosk-tv-at-start.env
    export KIDS_KIOSK_ENV=/tmp/kiosk-tv-at-start.env
    TOUCH=HEADLESS-2; TV=HEADLESS-1
else
    export WLR_HEADLESS_OUTPUTS=1
    TOUCH=$(sed -n 's/^KIDS_TOUCH_OUTPUT=//p' /etc/kids-kiosk/kiosk.env)
fi

/usr/local/bin/kids-kiosk.sh > "/tmp/kiosk-$MODE.log" 2>&1 &
for _ in $(seq 50); do ls "$XDG_RUNTIME_DIR"/sway-ipc.* >/dev/null 2>&1 && break; sleep 0.2; done
SWAYSOCK=$(ls "$XDG_RUNTIME_DIR"/sway-ipc.* | head -n1); export SWAYSOCK WAYLAND_DISPLAY=wayland-1
swaymsg -q "output $TOUCH resolution 1920x1080"

state() {
    swaymsg -r -t get_tree | jq -c '
      def apps: [.. | objects | select(.app_id? != null)];
      [.nodes[] | select(.type=="output" and .name!="__i3")] as $outs
      | {outputs: [$outs[] | {name, ws: .current_workspace}],
         apps: [$outs[] as $o | $o.nodes[] as $w | $w | apps[] | {app_id, pid, output: $o.name, ws: $w.name, fs: .fullscreen_mode, visible}]}'
}
kiosk_ok() {   # OtterTube alone, fullscreen and visible on the touch output, whose workspace is kiosk
    state | jq -e --arg t "$TOUCH" '
      [.apps[] | select(.app_id=="ottertube")] as $a
      | ($a|length)==1 and $a[0].output==$t and $a[0].visible and $a[0].fs==1
        and ([.outputs[]|select(.name==$t)][0].ws=="kiosk")' >/dev/null
}
mirror_on() { state | jq -e --arg o "$1" '.apps[]|select(.app_id=="at.yrlf.wl_mirror" and .output==$o and .visible)' >/dev/null; }
app_pid()   { state | jq '.apps[]|select(.app_id=="ottertube")|.pid'; }
wait_app()  { for _ in $(seq 150); do [ -n "$(app_pid 2>/dev/null)" ] && return 0; sleep 0.2; done; return 1; }
sample() {  # tc label count: kiosk_ok every 100 ms
    local n=0; for _ in $(seq "$3"); do kiosk_ok || n=$((n + 1)); sleep 0.1; done
    [ "$n" -eq 0 ] && ok "$1 kiosk untouched $2 ($3 samples)" || bad "$1 kiosk disturbed in $n of $3 samples $2: $(state)"
}
count() { pgrep -u "$(id -u)" -fc "$1" 2>/dev/null || echo 0; }
finish() { pkill -u "$(id -u)" -f /usr/local/bin/kids-kiosk.sh; pkill -u "$(id -u)" -x sway; sleep 2; pkill -u "$(id -u)" -f 'kids-(outputs|app-loop)\.sh|ottertube-desktop|wl-mirror'; exit "$FAIL"; }

if ! wait_app; then
    bad "TC-1624 [$MODE] OtterTube never appeared (sandbox ON). The launcher's log:"; tail -20 "/tmp/kiosk-$MODE.log"
    finish
fi

if [ "$MODE" = tv-at-start ]; then
    swaymsg -q "output $TV resolution 1280x720"
    sample TC-1625 "with the TV present at start and sorting first" 50
    mirror_on "$TV" && ok "TC-1625 the TV present at start shows a mirror" || bad "TC-1625 no mirror on the TV present at start: $(state)"
    finish
fi

sleep 2
kiosk_ok && ok "TC-1624 OtterTube fullscreen on $TOUCH (setuid sandbox on, as kiosk, via kids-kiosk.sh)" || bad "TC-1624 not fullscreen on $TOUCH: $(state)"
pid0=$(app_pid)
grim -o "$TOUCH" /out/1-touch.png 2>/dev/null

swaymsg -q create_output; swaymsg -q 'output HEADLESS-2 resolution 1280x720'
sample TC-1625 "while a TV is plugged" 40
mirror_on HEADLESS-2 && ok "TC-1625 the TV shows a mirror" || bad "TC-1625 no mirror on the TV: $(state)"
grim -o HEADLESS-2 /out/2-tv.png 2>/dev/null

swaymsg -q 'output HEADLESS-2 unplug'
sample TC-1626 "through the unplug" 30
[ "$(pgrep -u "$(id -u)" -c -x wl-mirror)" -eq 0 ] && ok "TC-1626 the TV's mirror was removed" || bad "TC-1626 a mirror outlived its TV"
grim -o "$TOUCH" /out/3-touch.png 2>/dev/null

swaymsg -q create_output; swaymsg -q 'output HEADLESS-3 resolution 1280x720'
sample TC-1625 "while a second TV is plugged" 40
mirror_on HEADLESS-3 && ok "TC-1625 the new TV shows a mirror" || bad "TC-1625 no mirror on the new TV: $(state)"
grim -o HEADLESS-3 /out/4-tv.png 2>/dev/null

[ "$(app_pid)" = "$pid0" ] && ok "TC-1626 OtterTube is the same process throughout (pid $pid0)" \
    || bad "TC-1626 OtterTube restarted: $pid0 -> $(app_pid)"

# ── the compositor dies: the session must come back clean ─────────────────────
# Each script is several processes (its own subshells share its command line),
# so the healthy session's counts are the baseline, not "one".
old_pids=$(pgrep -u "$(id -u)" -f '/usr/local/bin/kids-(outputs|app-loop)\.sh' | tr '\n' ' ')
base_out=$(count '/usr/local/bin/kids-outputs\.sh'); base_app=$(count '/usr/local/bin/kids-app-loop\.sh')
snap=$(ps -o pid=,args= -p "$(echo "$old_pids" | tr ' ' ',' | sed 's/,$//')" 2>/dev/null)
pkill -u "$(id -u)" -x sway
killed_at=$(date +%s)
# The scripts stop on their own when sway's IPC closes, and kids-kiosk.sh
# kills any survivor as soon as sway returns; 10 s covers both.
for _ in $(seq 100); do
    alive=""; for p in $old_pids; do kill -0 "$p" 2>/dev/null && alive="$alive $p"; done
    [ -z "$alive" ] && break; sleep 0.1
done
if [ -z "$alive" ]; then
    ok "TC-1624 every process of the old session's scripts ended within $(( $(date +%s) - killed_at ))s of sway"
else
    bad "TC-1624 still alive 10 s after sway died:$alive"; printf '%s\n' "$snap" | grep -wE "$(echo "$alive" | sed 's/^ //; s/ /|/g')"
fi
for _ in $(seq 50); do ls "$XDG_RUNTIME_DIR"/sway-ipc.* >/dev/null 2>&1 && break; sleep 0.2; done
SWAYSOCK=$(ls -t "$XDG_RUNTIME_DIR"/sway-ipc.* 2>/dev/null | head -n1); export SWAYSOCK
swaymsg -q "output $TOUCH resolution 1920x1080" 2>/dev/null
if wait_app; then
    sleep 3
    kiosk_ok && ok "TC-1624 kids-kiosk.sh restarted sway and OtterTube is back on $TOUCH" || bad "TC-1624 after the restart: $(state)"
else
    bad "TC-1624 OtterTube did not come back after sway was killed. The launcher's log:"; tail -10 "/tmp/kiosk-$MODE.log"
fi
n_out=$(count '/usr/local/bin/kids-outputs\.sh'); n_app=$(count '/usr/local/bin/kids-app-loop\.sh')
[ "$n_out" -eq "$base_out" ] && [ "$n_app" -eq "$base_app" ] \
    && ok "TC-1624 the restarted session has the healthy shape ($n_out/$n_app processes, as before the kill)" \
    || bad "TC-1624 after the restart: $n_out/$n_app kids-outputs/kids-app-loop processes, the healthy session had $base_out/$base_app"
finish
