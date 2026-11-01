#!/bin/bash
# vmtest/lib/kids-session-scenario.sh — runs as the `kiosk` user inside
# test-kids-session.sh's container: the image's sway.config on sway's headless
# backend, then the TV plug/unplug/replug the Owner's requirement is about.
# Its exit status is the number of failed checks.
set -u
FAIL=0
ok()  { printf 'PASS %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAIL=$((FAIL + 1)); }

export XDG_RUNTIME_DIR=/tmp/xdg-kiosk
mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman WLR_HEADLESS_OUTPUTS=1
# What kids-kiosk.sh exports from kiosk.env.
# shellcheck source=/dev/null
. /etc/kids-kiosk/kiosk.env
export KIDS_TOUCH_OUTPUT KIDS_APP_CMD
TOUCH=$KIDS_TOUCH_OUTPUT

sway -c /etc/kids-kiosk/sway.config > /tmp/sway.log 2>&1 &
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

for _ in $(seq 150); do [ -n "$(app_pid)" ] && break; sleep 0.2; done
if [ -z "$(app_pid)" ]; then
    bad "TC-1624 OtterTube never appeared (sandbox ON). Its output:"
    grep -rh . /tmp/sway.log 2>/dev/null | tail -20
    exit "$FAIL"
fi
sleep 3
kiosk_ok && ok "TC-1624 OtterTube fullscreen on $TOUCH (setuid sandbox on, as kiosk)" || bad "TC-1624 not fullscreen on $TOUCH: $(state)"
pid0=$(app_pid)
grim -o "$TOUCH" /out/1-touch.png 2>/dev/null

swaymsg -q create_output; sleep 0.5; swaymsg -q 'output HEADLESS-2 resolution 1280x720'; sleep 4
kiosk_ok && ok "TC-1625 kiosk untouched with a TV plugged" || bad "TC-1625 kiosk moved when the TV was plugged: $(state)"
mirror_on HEADLESS-2 && ok "TC-1625 the TV shows a mirror" || bad "TC-1625 no mirror on the TV: $(state)"
grim -o HEADLESS-2 /out/2-tv.png 2>/dev/null

swaymsg -q 'output HEADLESS-2 unplug'
badsamples=0
for _ in $(seq 30); do kiosk_ok || badsamples=$((badsamples + 1)); sleep 0.1; done
[ "$badsamples" -eq 0 ] && ok "TC-1626 kiosk untouched through the unplug (30 samples / 3 s)" \
    || bad "TC-1626 kiosk disturbed in $badsamples of 30 samples after the unplug"
[ "$(pgrep -u "$(id -u)" -c -x wl-mirror)" -eq 0 ] && ok "TC-1626 the TV's mirror was removed" || bad "TC-1626 a mirror outlived its TV"
grim -o "$TOUCH" /out/3-touch.png 2>/dev/null

swaymsg -q create_output; sleep 0.5; swaymsg -q 'output HEADLESS-3 resolution 1280x720'; sleep 4
kiosk_ok && ok "TC-1625 kiosk untouched by a second TV" || bad "TC-1625 kiosk moved on replug: $(state)"
mirror_on HEADLESS-3 && ok "TC-1625 the new TV shows a mirror" || bad "TC-1625 no mirror on the new TV: $(state)"
grim -o HEADLESS-3 /out/4-tv.png 2>/dev/null

[ "$(app_pid)" = "$pid0" ] && ok "TC-1626 OtterTube is the same process throughout (pid $pid0)" \
    || bad "TC-1626 OtterTube restarted: $pid0 -> $(app_pid)"
exit "$FAIL"
