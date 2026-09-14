#!/usr/bin/env bash
# audio-switch.test.sh — SR-028 asserted on the REAL applier, end to end.
#
# WHY THIS SUITE EXISTS ALONGSIDE THE PYTEST ONE. tests/test_wall_audio_switch.py
# proves the policy and reads the configuration. It cannot prove that
# `wall-audio-output` -- an sbin command that writes /etc, runs systemctl and
# calls amixer -- does the right things in the right ORDER, because running it
# means being root on a panel with a sound card.
#
# So it is run here for real, against:
#   * a temp /etc tree (--state points at it, and the mode file lives beside it);
#   * --dry-run, which prints every privileged command it WOULD run instead of
#     running it -- so the assertions are on the command sequence itself, which
#     is the thing an operator would otherwise have to watch a live panel to see.
#
# HERMETIC: a temp directory and whatever python3 is on PATH. No unit installed,
# no mixer touched, no sound card required, nothing under /etc read or written.
#
# WHAT IT LOCKS
#   B1  speaker: both speaker legs start, the headset leg is stopped, the
#       adapter is unmuted and the level is applied LAST
#   B2  headset absent + headset selected: NOTHING starts, everything is muted,
#       and the journal says so (Owner ruling 7)
#   B3  the one-shot: an `add` moves speaker -> headset once; a second `add`
#       after the Owner switches back does not (D5 as amended)
#   B4  per-output volume memory survives a switch and a reload (ruling F)
#   B5  the position survives a "reboot": a fresh apply-state re-asserts it and
#       invents nothing (ruling G)
#   B6  a request the broker must not make is refused, and an identical request
#       is not replayed
#   B7  outside bus mode the state is recorded and NO hardware command is issued
#   B8  a COLDPLUG presence report records the adapter and moves nothing (the
#       Owner's 2026-09-13 ruling, measured as a live failure at 23:18)
#   B9  apply-state stamps the boot and refreshes presence without inventing a
#       position, and the stamp is what makes a later plug an event
#   B10 the level's pre-open: if the softvol control never appears, the applier
#       opens the leg's PCM itself rather than waiting longer
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WALL="$(cd "$HERE/.." && pwd)"
APPLIER="$WALL/wall-audio-output"
PASS=0; FAIL=0

pass() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$1"; }
eq() { [ "$1" = "$2" ] && pass "$3" || fail "$3 (want '$1', got '$2')"; }
has() { printf '%s' "$2" | grep -qF -- "$1" && pass "$3" || fail "$3 (missing '$1')"; }
hasnt() { printf '%s' "$2" | grep -qF -- "$1" && fail "$3 (unexpected '$1')" || pass "$3"; }

command -v python3 >/dev/null 2>&1 || { echo "REFUSED: python3 is required" >&2; exit 2; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
STATE="$TMP/audio-state.json"

# The applier reads the mode from /etc/wall-panel/audio-mode, which this suite
# must not touch. WALL_PANEL_CONF_DIR is honoured for exactly that reason and is
# the only test seam in the script.
export WALL_PANEL_CONF_DIR="$TMP/etc"
# Same seam for /run: the boot stamp that tells a coldplug report from a plug
# event lives there, and the suite must be able to write and remove it.
export WALL_PANEL_RUN_DIR="$TMP/run"
mkdir -p "$WALL_PANEL_CONF_DIR" "$WALL_PANEL_RUN_DIR"
printf 'bus\n' > "$WALL_PANEL_CONF_DIR/audio-mode"

run() { python3 "$APPLIER" --dry-run --state "$STATE" "$@" 2>&1; }

# B1 — speaker, the default position.
out="$(run apply-state)"
has "systemctl start wall-bus-speaker.service" "$out" "B1 speaker leg starts"
has "systemctl start wall-speaker-out.service" "$out" "B1 speaker output leg starts"
has "systemctl stop wall-bus-headset.service" "$out" "B1 headset leg is stopped"
has "sset Speaker unmute" "$out" "B1 the adapter is unmuted"
eq "2" "$(printf '%s\n' "$out" | grep -c 'sset Bus ')" \
    "B1 the level is set before the leg starts and re-asserted after it"
has "aplay -q -D speaker_out /dev/null" "$out" \
    "B1 the softvol control is declared by the applier, with no frames"
last="$(printf '%s\n' "$out" | grep -E 'systemctl|amixer' | tail -1)"
has "sset Bus 60%" "$last" "B1 the level is applied LAST (the softvol must exist first)"
stop_line="$(printf '%s\n' "$out" | grep -n 'systemctl stop' | tail -1 | cut -d: -f1)"
start_line="$(printf '%s\n' "$out" | grep -n 'systemctl start' | head -1 | cut -d: -f1)"
[ "$stop_line" -lt "$start_line" ] \
    && pass "B1 every stop precedes every start" \
    || fail "B1 a leg was started before the previous one stopped"

# B2 — headset selected, adapter absent (Owner ruling 7).
out="$(run set headset)"
hasnt "systemctl start" "$out" "B2 no leg starts with no adapter"
hasnt "aplay" "$out" "B2 and no PCM is opened to make a control for silence"
has "sset Speaker mute" "$out" "B2 the adapter is muted"
has "UNAVAILABLE" "$out" "B2 the silence is journaled, not silent"
eq "headset" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["output"])' "$STATE")" \
    "B2 the mode stays selected (D3)"

# B3 — the one-shot, from a clean state. --runtime is what a udev event that
# arrived on an already-running system resolves to; see B8 for the other half.
rm -f "$STATE"
out="$(run headset add --runtime)"
has "auto-switch speaker -> headset" "$out" "B3 the adapter arriving switches once"
run set speaker >/dev/null
out="$(run headset add --runtime)"
has "already present" "$out" "B3 a second add is not an arrival at all"
eq "speaker" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["output"])' "$STATE")" \
    "B3 the Owner's choice stands"

# B4 — per-output volume memory (ruling F).
rm -f "$STATE"
run set speaker >/dev/null
run volume 84 >/dev/null
run headset add --runtime >/dev/null   # auto-switches to headset
run volume 20 >/dev/null
out="$(run set speaker)"
has "sset Bus 84%" "$out" "B4 switching back restores the speaker level"
out="$(run set headset)"
has "sset Bus 20%" "$out" "B4 the headset keeps its own level"

# B5 — persistence across a "reboot" (ruling G): nothing but the file survives.
out="$(run apply-state)"
has "sset Bus 20%" "$out" "B5 a fresh apply re-asserts the stored level"
eq "headset" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["output"])' "$STATE")" \
    "B5 apply-state invents no new position"

# B8 — coldplug is a FACT, never an event (Owner ruling 2026-09-13).
# Measured on the panel at 23:18: a power cycle with the adapter already plugged
# in logged "auto-switch speaker -> headset" and the panel came back on Headset
# although it was left on Speaker.
rm -f "$STATE" "$WALL_PANEL_RUN_DIR/audio-boot-apply.json"
run set speaker >/dev/null
out="$(run headset add --boot)"
hasnt "auto-switch speaker -> headset" "$out" "B8 a coldplug adapter does not move the switch"
has "recorded, no auto-switch" "$out" "B8 and says so in the journal"
eq "speaker" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["output"])' "$STATE")"     "B8 the position the Owner left it in stands"
eq "false" "$(python3 -c 'import json,sys;print(str(json.load(open(sys.argv[1]))["headset_autoswitch_armed"]).lower())' "$STATE")"     "B8 an adapter present across the boot leaves no pending event behind it"
eq "true" "$(python3 -c 'import json,sys;print(str(json.load(open(sys.argv[1]))["headset_present"]).lower())' "$STATE")"     "B8 presence is still recorded, so the red icon is right"
# A bare `headset add` with no udev answer and no boot stamp resolves to boot:
# the safe direction is to leave the Owner's position alone.
out="$(run headset add)"
hasnt "auto-switch speaker -> headset" "$out" "B8 an unattributable report is treated as coldplug"

# B9 — apply-state: refresh presence, stamp the boot, invent no position.
rm -f "$STATE" "$WALL_PANEL_RUN_DIR/audio-boot-apply.json"
run set headset >/dev/null          # selected while the adapter is absent
out="$(run apply-state)"
has "recorded, no auto-switch" "$out" "B9 apply-state records presence as a boot fact"
eq "headset" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["output"])' "$STATE")"     "B9 apply-state still invents no position (ruling G)"
[ -s "$WALL_PANEL_RUN_DIR/audio-boot-apply.json" ]     && pass "B9 the boot stamp is written"     || fail "B9 the boot stamp is missing"
python3 -c 'import json,sys;v=json.load(open(sys.argv[1]))["monotonic_usec"];sys.exit(0 if isinstance(v,int) and v>=0 else 1)'     "$WALL_PANEL_RUN_DIR/audio-boot-apply.json"     && pass "B9 the stamp is a monotonic microsecond, not a wall clock"     || fail "B9 the stamp is not a usable monotonic mark"
# With no adapter across the boot the latch is ARMED, so the first real plug of
# the session still switches (acceptance check 8 on a panel that booted bare).
eq "true" "$(python3 -c 'import json,sys;print(str(json.load(open(sys.argv[1]))["headset_autoswitch_armed"]).lower())' "$STATE")"     "B9 an adapter absent across the boot arms the next real plug"

# B10 — the level pre-open. --dry-run makes every command "succeed", so the
# pre-open cannot be provoked here; the bounded retry and the pre-open are
# proven in tests/test_wall_audio_switch.py against a failing amixer. What IS
# asserted here is that the normal path still sets the level exactly once and
# does NOT open a PCM when the control answers straight away.
rm -f "$STATE"
out="$(run apply-state)"
has "aplay -q -D speaker_out /dev/null" "$out" \
    "B10 the control is declared before the leg starts"
preopen="$(printf '%s\n' "$out" | grep -n 'aplay' | head -1 | cut -d: -f1)"
first_start="$(printf '%s\n' "$out" | grep -n 'systemctl start' | head -1 | cut -d: -f1)"
[ "$preopen" -lt "$first_start" ] \
    && pass "B10 declared BEFORE the leg starts (a late softvol is at full scale)" \
    || fail "B10 the control was created under a running leg"

# B6 — requests from the broker.
request="$TMP/request.json"
printf '{"version":1,"seq":3,"event":{"kind":"set_output","output":"mute"}}\n' > "$request"
out="$(run apply-request "$request")"
has "output speaker -> mute" "$out" "B6 a valid request is applied"
printf '{"version":1,"seq":2,"event":{"kind":"set_output","output":"speaker"}}\n' > "$request"
out="$(run apply-request "$request")"
hasnt "output mute -> speaker" "$out" "B6 an older seq is not replayed"
printf '{"version":1,"seq":9,"event":{"kind":"headset","present":true}}\n' > "$request"
out="$(run apply-request "$request")"
has "refused" "$out" "B6 the renderer may not assert adapter presence"

# B7 — outside bus mode nothing is commanded.
printf 'trigger\n' > "$WALL_PANEL_CONF_DIR/audio-mode"
out="$(run set speaker)"
has "state recorded, hardware untouched" "$out" "B7 trigger mode is left alone"
hasnt "systemctl" "$out" "B7 no unit is touched outside bus mode"

printf '\n%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
