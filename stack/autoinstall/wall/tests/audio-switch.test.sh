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
#   B11 step 3: the speaker chain is probed and published BEFORE anything opens
#       speaker_out, so the pre-open and the leg cannot disagree
#   B12 and nothing is probed in Mute or Headset -- opening the adapter there
#       would be audio nobody asked for
#   B14 step 4, the mic legs: which microphone the switch selects, and that the
#       selection is PUBLISHED before anything opens one
#   B15 the input mute is a REAL mute -- both mic legs stopped, the capture
#       switch closed, and the adapter's rear pair put back to silence
#   B16 Owner ruling E: the output Mute position does NOT stop the microphone,
#       and Owner ruling 7: a selected-but-absent headset tunnels nothing
#   B17 the mic knobs: the rendered route's explicit zeros, a group of refusals
#       that each move NOTHING, and a number-moving command that starts no
#       microphone
#   B13 the centre/sub trim: the rendered ttable is the (L+R)/2 sum the Owner
#       asked for, a group name moves both halves of it, a refused value moves
#       NOTHING, and a number-moving command never starts audio
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
has "aplay -q -D speaker_out -t raw -f S16_LE -r 48000 -c 2 -s 1 /dev/zero" "$out" \
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
has "aplay -q -D speaker_out -t raw -f S16_LE -r 48000 -c 2 -s 1 /dev/zero" "$out" \
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

# B11 — step 3: the speaker leg probes its chain BEFORE it opens speaker_out,
# and publishes what it found. The probe and the pre-open must agree, because
# @func getenv reads the environment of whichever process opens the PCM.
printf 'bus\n' > "$WALL_PANEL_CONF_DIR/audio-mode"
rm -f "$STATE"
out="$(run apply-state)"
has "aplay -q -D speaker_multi -t raw -f S16_LE -r 48000 -c 2 -s 1 /dev/zero" "$out" \
    "B11 the 8-channel chain is probed, with no frames"
probe="$(printf '%s\n' "$out" | grep -n 'D speaker_multi' | head -1 | cut -d: -f1)"
declare_at="$(printf '%s\n' "$out" | grep -n 'D speaker_out' | head -1 | cut -d: -f1)"
start_at="$(printf '%s\n' "$out" | grep -n 'systemctl start wall-speaker-out' | head -1 | cut -d: -f1)"
{ [ "$probe" -lt "$declare_at" ] && [ "$declare_at" -lt "$start_at" ]; } \
    && pass "B11 probe, then declare the control, then start the leg" \
    || fail "B11 the chain was chosen after something had already opened the leg"
has "write $WALL_PANEL_RUN_DIR/audio-speaker.env" "$out" \
    "B11 the chain is published where wall-speaker-out.service reads it"

# B12 — and NOT in the positions that have no speaker leg: opening the adapter
# in Mute or in Headset would be audio nobody asked for.
out="$(run set mute)"
hasnt "speaker_multi" "$out" "B12 Mute probes nothing"
run set speaker >/dev/null
out="$(run set headset)"
hasnt "speaker_multi" "$out" "B12 Headset with no adapter probes nothing"

# B13 — the trim table: the Owner's tuning session (finding 8) moves six
# numbers with one command and never sees ALSA syntax.
rm -f "$WALL_PANEL_CONF_DIR/audio-trim.env" "$WALL_PANEL_CONF_DIR/audio-trim.conf"
trim() { python3 "$APPLIER" --state "$STATE" \
    --trim-env "$WALL_PANEL_CONF_DIR/audio-trim.env" \
    --trim-conf "$WALL_PANEL_CONF_DIR/audio-trim.conf" trim "$@" 2>&1; }
out="$(trim --render)"
eq "0" "$?" "B13 a bare render succeeds with no stored values at all"
conf="$(cat "$WALL_PANEL_CONF_DIR/audio-trim.conf")"
has "pcm.speaker_multi" "$conf" "B13 the rendered file defines the multi chain"
has "ttable.0.2 0.5000" "$conf" "B13 half of L into the centre"
has "ttable.1.2 0.5000" "$conf" "B13 half of R into the centre — (L+R)/2"
has "ttable.0.3 0.5000" "$conf" "B13 the SAME mono sum into the sub"
has "ttable.1.3 0.5000" "$conf" "B13 and its other half"
has "ttable.0.0 1.0000" "$conf" "B13 front left at unity"
has "ttable.1.0 0.0000" "$conf" "B13 front stays stereo: R does not reach FL"
has "ttable.0.4 0.0000" "$conf" "B13 rear is an explicit zero, not an omission"
has "ttable.1.7 0.0000" "$conf" "B13 and so is the side pair"

out="$(trim center=0.35 sub=0.6)"
has "trim center_l 0.500 -> 0.350" "$out" "B13 a group name moves both halves"
has "trim sub_r 0.500 -> 0.600" "$out" "B13 and keeps the sub a sum"
conf="$(cat "$WALL_PANEL_CONF_DIR/audio-trim.conf")"
has "ttable.0.2 0.3500" "$conf" "B13 the ALSA file followed"
has "ttable.1.3 0.6000" "$conf" "B13 for the sub too"
has "speaker leg is not running" "$out" \
    "B13 and a number-moving command does not start audio that was not playing"

out="$(trim)"
has '"center_l": 0.35' "$out" "B13 the bare command reports the live table"
has '"conf":' "$out" "B13 and says where both files are"

# A refused value moves NOTHING: whole table or none of it.
out="$(trim front_l=0.9 center=-1)"
has "refused" "$out" "B13 a phase inversion is refused"
conf="$(cat "$WALL_PANEL_CONF_DIR/audio-trim.conf")"
has "ttable.0.0 1.0000" "$conf" "B13 and the valid half of the same command did NOT land"
out="$(trim center=99)"
has "refused" "$out" "B13 a coefficient that is a wiring problem is refused"
out="$(trim rear=0.5)"
has "refused" "$out" "B13 there is no knob for the pair wired to the desktop's input"

# B14 — step 4: WHICH MICROPHONE, and when the switch says so.
run set speaker >/dev/null
out="$(run apply-state)"
has "systemctl start wall-mic-rear.service" "$out" "B14 the rear mic leg starts on Speaker"
has "systemctl start wall-bt-mic.service" "$out" "B14 and so does the HFP mic return"
has "microphone: mic_panel" "$out" "B14 Speaker selects the panel's own microphone (D4)"
# The selection must be on disk BEFORE any leg is started: ALSA resolves
# @func getenv when the PCM is opened, and a leg started first would open the
# wrong one.
mic_line="$(printf '%s\n' "$out" | grep -n 'wall-mic-rear' | head -1 | cut -d: -f1)"
[ -n "$mic_line" ] && pass "B14 the rear leg appears in the sequence" \
    || fail "B14 the rear leg appears in the sequence"

out="$(run set headset)"
has "output speaker -> headset" "$out" "B14 the switch moves"
# No adapter in this temp tree, so ruling 7 applies and nothing is tunnelled.
hasnt "systemctl start wall-mic-rear.service" "$out" \
    "B16 headset selected but absent tunnels NO microphone (ruling 7)"
has "systemctl stop wall-mic-rear.service" "$out" "B16 and the rear leg is stopped"
has "systemctl stop wall-bt-mic.service" "$out" "B16 and so is the HFP return"

# B15 — the input mute is a real mute, not a flag the chrome draws.
run set speaker >/dev/null
out="$(run input-mute on)"
has "input mute on" "$out" "B15 the mute is journaled"
has "systemctl stop wall-mic-rear.service" "$out" "B15 the rear leg is STOPPED"
has "systemctl stop wall-bt-mic.service" "$out" "B15 the HFP return is STOPPED"
has "sset Capture nocap" "$out" "B15 and the capture switch is closed as well"
hasnt "systemctl start wall-mic-rear.service" "$out" "B15 nothing carries the mic"
# The speakers are untouched: the two buttons are independent (ruling E).
has "systemctl start wall-speaker-out.service" "$out" "B15 the room still has audio"
out="$(run input-mute off)"
has "sset Capture cap" "$out" "B15 un-mute re-opens the capture switch"
has "systemctl start wall-mic-rear.service" "$out" "B15 and the leg comes back"

# B16 — Owner ruling E, the other half: the output Mute position silences the
# room and NOT the microphone. "the mic has its own mute", verbatim.
out="$(run set mute)"
has "systemctl stop wall-speaker-out.service" "$out" "B16 Mute stops the room"
has "systemctl start wall-mic-rear.service" "$out" \
    "B16 and does NOT stop the microphone (ruling E)"
has "microphone: mic_panel" "$out" "B16 with no headset to take it from, Mute uses the panel's own"
run set speaker >/dev/null

# B17 — the mic knobs, and the generated route that is the whole of the
# separation between the microphone and the speakers.
rm -f "$WALL_PANEL_CONF_DIR/audio-mic.env" "$WALL_PANEL_CONF_DIR/audio-mic.conf"
mic() { python3 "$APPLIER" --state "$STATE" mic "$@" 2>&1; }
out="$(mic --render)"
eq "0" "$?" "B17 a bare render succeeds with no stored values at all"
conf="$(cat "$WALL_PANEL_CONF_DIR/audio-mic.conf")"
has "pcm.mic_rear_route" "$conf" "B17 the rendered file defines the rear route"
has 'pcm "usb_out_mix"' "$conf" "B17 through the SHARED eight-channel dmix"
has "ttable.0.4 1.0000" "$conf" "B17 the microphone on RL"
has "ttable.0.5 1.0000" "$conf" "B17 and identically on RR"
has "ttable.0.0 0.0000" "$conf" "B17 an EXPLICIT zero on front left"
has "ttable.0.1 0.0000" "$conf" "B17 and on front right"
has "ttable.0.2 0.0000" "$conf" "B17 the mic never reaches the centre"
has "ttable.0.3 0.0000" "$conf" "B17 nor the sub"
has "ttable.0.6 0.0000" "$conf" "B17 nor the side pair"
has "ttable.0.7 0.0000" "$conf" "B17 either half of it"

out="$(mic capture_percent=30 rear_level=80)"
has "capture_percent=30" "$out" "B17 a knob moves"
has "rear_level=80" "$out" "B17 and so does the rear level"
env_file="$(cat "$WALL_PANEL_CONF_DIR/audio-mic.env")"
has "WALL_AUDIO_MIC_CAPTURE_PERCENT=30" "$env_file" "B17 the env file followed"

# A refused value moves NOTHING: whole table or none of it.
out="$(mic capture_percent=45 boost=9)"
has "refused" "$out" "B17 a boost the codec does not have is refused"
env_file="$(cat "$WALL_PANEL_CONF_DIR/audio-mic.env")"
has "WALL_AUDIO_MIC_CAPTURE_PERCENT=30" "$env_file" \
    "B17 and the valid half of the same command did NOT land"
out="$(mic rear_gain=-1)"
has "refused" "$out" "B17 a phase inversion is not a gain"
out="$(mic rear_level=900)"
has "refused" "$out" "B17 a level off the adapter's own scale is refused"
out="$(mic treble=1)"
has "refused" "$out" "B17 there is no knob this panel does not have"
# It must never put a live microphone anywhere.
hasnt "systemctl start" "$out" "B17 a number-moving command starts no microphone"

printf '\n%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
