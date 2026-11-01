#!/bin/bash
# Drive the prototype: boot with the touch output only, plug a "TV", unplug it,
# plug it again, and check after every step (and every 100 ms around the
# unplug) that OtterTube stays visible, fullscreen and focused on the touch
# output, with the same process.
set -u
export XDG_RUNTIME_DIR=/tmp/xdg; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman WLR_HEADLESS_OUTPUTS=1
export KIDS_TOUCH_OUTPUT=HEADLESS-1
OUT=/out; FAIL=0
sway -c /proto/sway.config > /tmp/sway.log 2>&1 &
for _ in $(seq 50); do ls $XDG_RUNTIME_DIR/sway-ipc.* >/dev/null 2>&1 && break; sleep 0.2; done
SWAYSOCK=$(ls "$XDG_RUNTIME_DIR"/sway-ipc.* | head -n1); export SWAYSOCK WAYLAND_DISPLAY=wayland-1
swaymsg -q 'output HEADLESS-1 resolution 1920x1080'

# app_id, its output, whether fullscreen+visible, focused app_id, each output's workspace
state() {
    swaymsg -r -t get_tree | jq -c '
      def apps: [.. | objects | select(.app_id? != null)];
      [.nodes[] | select(.type=="output" and .name!="__i3")] as $outs
      | {outputs: [$outs[] | {name, ws: .current_workspace}],
         apps: [$outs[] as $o | $o.nodes[] as $w | $w | apps[] | {app_id, pid, output: $o.name, ws: $w.name, fs: .fullscreen_mode, visible, focused}]}'
}
check() {  # $1 = label
    local s ok; s=$(state)
    ok=$(printf '%s' "$s" | jq --arg t "$KIDS_TOUCH_OUTPUT" '
      [.apps[] | select(.app_id|test("^ottertube"))] as $a
      | ($a|length)==1 and $a[0].output==$t and $a[0].visible and $a[0].fs==1
        and ([.outputs[]|select(.name==$t)][0].ws=="kiosk")')
    printf '%-28s %s %s\n' "$1" "$ok" "$s"
    [ "$ok" = true ] || FAIL=$((FAIL+1))
}
wait_app() { for _ in $(seq 150); do state | jq -e '.apps[]|select(.app_id|test("^ottertube"))' >/dev/null && return 0; sleep 0.2; done; return 1; }

wait_app || { echo "APP NEVER APPEARED"; tail -40 /tmp/kids-app.log; exit 2; }
sleep 3; check boot-touch-only
pid0=$(state | jq '.apps[]|select(.app_id|test("^ottertube"))|.pid')
grim -o HEADLESS-1 $OUT/1-touch.png

swaymsg -q create_output; sleep 0.5; swaymsg -q 'output HEADLESS-2 resolution 1280x720'; sleep 4
check tv-plugged
state | jq -e '.apps[]|select(.app_id=="at.yrlf.wl_mirror" and .output=="HEADLESS-2" and .visible)' >/dev/null \
  && echo "mirror-on-tv                 true" || { echo "mirror-on-tv                 false"; FAIL=$((FAIL+1)); }
grim -o HEADLESS-1 $OUT/2-touch.png; grim -o HEADLESS-2 $OUT/2-tv.png

swaymsg -q 'output HEADLESS-2 unplug'
for i in $(seq 30); do check "unplug+$((i*100))ms" ; sleep 0.1; done > /tmp/unplug.txt
bad=$(grep -c ' false ' /tmp/unplug.txt); echo "unplug samples: 30, bad: $bad"; FAIL=$((FAIL+bad))
grep ' false ' /tmp/unplug.txt | head -3
check tv-unplugged
grim -o HEADLESS-1 $OUT/3-touch.png

swaymsg -q create_output; sleep 0.5; swaymsg -q 'output HEADLESS-3 resolution 1280x720'; sleep 4
check tv-replugged
state | jq -e '.apps[]|select(.app_id=="at.yrlf.wl_mirror" and .output=="HEADLESS-3" and .visible)' >/dev/null \
  && echo "mirror-on-new-tv             true" || { echo "mirror-on-new-tv             false"; FAIL=$((FAIL+1)); }
grim -o HEADLESS-3 $OUT/4-tv.png

pid1=$(state | jq '.apps[]|select(.app_id|test("^ottertube"))|.pid')
echo "app pid before/after: $pid0 / $pid1"; [ "$pid0" = "$pid1" ] || FAIL=$((FAIL+1))
echo "--- app log"; grep -E "start|exit|display:" /tmp/kids-app.log | tail -8
echo "--- outputs log"; tail -12 /tmp/kids-outputs.log
echo "FAILURES=$FAIL"
