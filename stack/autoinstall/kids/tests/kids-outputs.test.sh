#!/usr/bin/env bash
# kids-outputs.test.sh — the kids kiosk's choice of TOUCH OUTPUT. Verifies: TC-1627.
#
# kids-outputs.sh decides which output carries OtterTube and owns touch; every
# other output becomes a mirror. Choosing wrong puts the kids' app on the TV
# and the touches on a screen nobody touches. vmtest/test-kids-session.sh
# cannot see that mistake: its touch output is also the first by name. So the
# pure functions are asserted here, against sway's own get_outputs JSON shape.
#
# HERMETIC: sources the script with --lib (functions only); needs bash and jq.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v jq >/dev/null || { echo "SKIP kids-outputs.test.sh: jq is not installed"; exit 0; }
# shellcheck source=../kids-outputs.sh
. "$HERE/../kids-outputs.sh" --lib
FAIL=0
check() {  # name expected actual
    if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: expected '$2', got '$3'"; FAIL=$((FAIL + 1)); fi
}

# Two HDMI outputs plus a disabled one, in the order sway lists them (not sorted).
OUTPUTS='[
 {"name":"HDMI-A-2","active":true,"make":"Samsung Electric Company","model":"SAMSUNG","serial":"0x01000E00"},
 {"name":"DP-1","active":false,"make":"Unknown","model":"Unknown","serial":"Unknown"},
 {"name":"HDMI-A-1","active":true,"make":"Siliconworks","model":"SiW Touch","serial":"0000"}
]'
pick() { printf '%s' "$OUTPUTS" | active_outputs | touch_output "$1" "${2:-}"; }

check "active outputs only, sorted by name" $'HDMI-A-1\nHDMI-A-2' "$(printf '%s' "$OUTPUTS" | active_outputs | cut -f1)"
check "unset -> the first active output by name" "HDMI-A-1" "$(pick '')"
check "a connector name is honoured" "HDMI-A-2" "$(pick 'HDMI-A-2')"
check "make model serial is honoured" "HDMI-A-2" "$(pick 'Samsung Electric Company SAMSUNG 0x01000E00')"
check "an absent touch output falls back to the first" "HDMI-A-1" "$(pick 'HDMI-A-9')"
check "an inactive output is never chosen" "HDMI-A-1" "$(pick 'DP-1')"
# STICKY when nothing is configured: a TV whose connector sorts first must not
# take the kiosk when it is plugged in after the touch monitor was chosen.
check "unset, remembered and present -> the remembered one, not the first" "HDMI-A-2" "$(pick '' 'HDMI-A-2')"
check "unset, remembered but gone -> the first" "HDMI-A-1" "$(pick '' 'HDMI-A-9')"
check "configured and present beats remembered" "HDMI-A-1" "$(pick 'HDMI-A-1' 'HDMI-A-2')"
check "configured but absent -> the remembered one" "HDMI-A-2" "$(pick 'HDMI-A-9' 'HDMI-A-2')"
check "only one output: it, whatever is configured" "HDMI-A-2" \
    "$(printf '%s' '[{"name":"HDMI-A-2","active":true,"make":"a","model":"b","serial":"c"}]' | active_outputs | touch_output 'HDMI-A-1')"

echo "FAILURES=$FAIL"
[ "$FAIL" -eq 0 ]
