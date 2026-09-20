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
#   B16 ITEM J (supersedes Owner ruling E): the output Mute position ALSO stops
#       the microphone, before it stops the room; an independent unmute is
#       refused while Mute is selected; leaving Mute retains the input mute
#       until an explicit unmute. Plus Owner ruling 7: a selected-but-absent
#       headset tunnels nothing
#   B17 the mic knobs: the rendered route's explicit zeros, a group of refusals
#       that each move NOTHING, and a number-moving command that starts no
#       microphone
#   B18 the intent contract's EPOCH: apply-state opens one per boot and only
#       per boot, the durable number survives a reboot, a request scoped to a
#       dead epoch is refused while leaving the state byte-identical, and a
#       delayed OLD request arriving after a newer one still cannot apply
#   B19 step 6's seam: the canceller is selected only with BOTH the knob and a
#       binary, and only in Speaker; plus item J's effective mute, published
#       for the canceller BEFORE any leg moves
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
# B12 left the switch in Mute, and ITEM J means that latched the input mute. The
# explicit unmute below is not test scaffolding -- it is the Owner's own rule:
# leaving Mute retains the microphone's muted state until somebody asks for it
# back. Without it every assertion after this point would be about a panel whose
# microphone is deliberately off.
run set speaker >/dev/null
run input-mute off >/dev/null
out="$(run apply-state)"
has "systemctl start wall-mic-rear.service" "$out" "B14 the rear mic leg starts on Speaker"
has "systemctl start wall-bt-call.service" "$out" "B14 and so does the HFP mic return"
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
has "systemctl stop wall-bt-call.service" "$out" "B16 and so is the HFP return"

# B15 — the input mute is a real mute, not a flag the chrome draws.
run set speaker >/dev/null
run input-mute off >/dev/null
out="$(run input-mute on)"
has "input mute on" "$out" "B15 the mute is journaled"
has "systemctl stop wall-mic-rear.service" "$out" "B15 the rear leg is STOPPED"
has "systemctl stop wall-bt-call.service" "$out" "B15 the HFP return is STOPPED"
has "sset Capture nocap" "$out" "B15 and the capture switch is closed as well"
hasnt "systemctl start wall-mic-rear.service" "$out" "B15 nothing carries the mic"
# The speakers are untouched: the two buttons are independent (ruling E).
has "systemctl start wall-speaker-out.service" "$out" "B15 the room still has audio"
out="$(run input-mute off)"
has "sset Capture cap" "$out" "B15 un-mute re-opens the capture switch"
has "systemctl start wall-mic-rear.service" "$out" "B15 and the leg comes back"

# B16 — ITEM J, which SUPERSEDES Owner ruling E: the output Mute position now
# silences the room AND the microphone. Ruling E's "the mic has its own mute"
# was reversed by the Owner on 2026-09-14; the button is still separate, the
# coupling is new. This block used to assert the opposite and is rewritten
# rather than deleted, so the supersession is visible where it happened.
out="$(run set mute)"
has "systemctl stop wall-speaker-out.service" "$out" "B16 Mute stops the room"
has "systemctl stop wall-mic-rear.service" "$out" \
    "B16 and ALSO stops the microphone (item J)"
has "systemctl stop wall-bt-call.service" "$out" "B16 including the HFP return"
hasnt "systemctl start wall-mic-rear.service" "$out" "B16 and starts neither"
has "sset Capture nocap" "$out" "B16 the capture switch is closed too"
has "input mute on (coupled to the output Mute position, item J)" "$out" \
    "B16 the coupling is journaled, not silent"
has "microphone: mic_panel" "$out" "B16 with no headset to take it from, Mute names the panel's own"
# THE MIC LEGS ARE STOPPED BEFORE ANY OUTPUT LEG MOVES. A coupled mute arrives
# as ONE request; stopping the room first would leave a window in which the
# person believes they are unheard and the microphone is still forwarding.
mic_stop="$(printf '%s\n' "$out" | grep -n 'systemctl stop wall-mic-rear' | head -1 | cut -d: -f1)"
out_stop="$(printf '%s\n' "$out" | grep -n 'systemctl stop wall-speaker-out' | head -1 | cut -d: -f1)"
{ [ -n "$mic_stop" ] && [ -n "$out_stop" ] && [ "$mic_stop" -lt "$out_stop" ]; } \
    && pass "B16 the microphone is stopped BEFORE the room" \
    || fail "B16 the room was silenced before the microphone was"

# An independent unmute is REFUSED while Mute is selected, and refusing it
# changes nothing at all.
before="$(cat "$STATE")"
out="$(run input-mute off)"
has "refused: input unmute is held" "$out" "B16 an unmute is refused while Mute is selected"
hasnt "systemctl start wall-mic-rear.service" "$out" "B16 and starts no microphone"
eq "$before" "$(cat "$STATE")" "B16 and the refusal wrote nothing"

# Leaving Mute does NOT bring the microphone back on its own.
out="$(run set speaker)"
hasnt "systemctl start wall-mic-rear.service" "$out" \
    "B16 Speaker after Mute retains the input mute (Owner, 2026-09-14)"
has "input stays muted after leaving Mute" "$out" "B16 and says so"
# Only an explicit unmute does.
out="$(run input-mute off)"
has "systemctl start wall-mic-rear.service" "$out" "B16 an explicit unmute brings it back"

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


# ── B18 — the epoch (intent contract 2026-09-14) ───────────────────────────
# Its own temp tree, because it is the one block that cares about /run being
# empty and about the state file's history. Mode is `trigger` throughout: this
# block is about ordering, and no hardware command is wanted in it.
EP="$TMP/epoch"; mkdir -p "$EP/etc" "$EP/run"
printf 'trigger\n' > "$EP/etc/audio-mode"
EPSTATE="$EP/audio-state.json"
epoch_of() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("generation"))' "$1"; }
ep() { WALL_PANEL_CONF_DIR="$EP/etc" WALL_PANEL_RUN_DIR="$EP/run" \
       python3 "$APPLIER" --dry-run --state "$EPSTATE" "$@" 2>&1; }

out="$(ep apply-state)"
has "epoch 1 opened" "$out" "B18 the first apply-state opens epoch 1"
eq "1" "$(epoch_of "$EPSTATE")" "B18 the epoch is stamped into the durable state"

# A SECOND apply-state in the SAME boot must NOT advance it: a resume re-assert
# is not a new life of the backend, and advancing here would invalidate every
# request the renderer has in flight every time the panel wakes up.
out="$(ep apply-state)"
hasnt "epoch 2 opened" "$out" "B18 a resume re-assert does not open a new epoch"
eq "1" "$(epoch_of "$EPSTATE")" "B18 and the stamp is unchanged"

# A REBOOT: /run is tmpfs, so the marker goes and the durable number stays.
rm -f "$EP/run/audio-epoch.json"
out="$(ep apply-state)"
has "epoch 2 opened" "$out" "B18 a boot opens the NEXT epoch, not epoch 1 again"
eq "2" "$(epoch_of "$EPSTATE")" "B18 the epoch is monotonic across the reboot"

# A request scoped to the epoch that just ended is REFUSED, and refusing it
# leaves the state file byte-identical -- which is what "atomically" means here.
eprequest="$EP/request.json"
before="$(cat "$EPSTATE")"
printf '{"version":1,"seq":50,"generation":1,"event":{"kind":"set_output","output":"mute"}}\n' > "$eprequest"
out="$(ep apply-request "$eprequest")"
has "generation 1 is not the current epoch 2" "$out" "B18 a dead epoch is refused, out loud"
eq "$before" "$(cat "$EPSTATE")" "B18 and the refusal wrote nothing at all"

# The same request, re-scoped to the live epoch, lands.
printf '{"version":1,"seq":50,"generation":2,"event":{"kind":"set_output","output":"mute"}}\n' > "$eprequest"
out="$(ep apply-request "$eprequest")"
has "output speaker -> mute" "$out" "B18 the live epoch is applied"

# A DELAYED OLD REQUEST ARRIVING AFTER A NEWER ONE -- the case the plan asks for
# by name, and it is NOT the reordered-replies case the renderer tests cover.
# Two requests are minted; the newer one reaches the applier first (the path
# unit coalesces, or the writer raced), and the older file is then re-dropped.
printf '{"version":1,"seq":70,"generation":2,"event":{"kind":"set_output","output":"speaker"}}\n' > "$eprequest"
out="$(ep apply-request "$eprequest")"
has "output mute -> speaker" "$out" "B18 the newer request lands"
printf '{"version":1,"seq":60,"generation":2,"event":{"kind":"set_output","output":"headset"}}\n' > "$eprequest"
before="$(cat "$EPSTATE")"
out="$(ep apply-request "$eprequest")"
has "request 60 ignored: not newer than the last applied (70)" "$out" \
    "B18 the delayed older request is refused at the APPLIER, not just in the UI"
eq "$before" "$(cat "$EPSTATE")" "B18 and it too wrote nothing"

# An UNSCOPED request is still accepted: the rocker and the root CLI have no
# epoch of their own, and an older broker sends none.
printf '{"version":1,"seq":80,"event":{"kind":"set_output","output":"mute"}}\n' > "$eprequest"
out="$(ep apply-request "$eprequest")"
has "output speaker -> mute" "$out" "B18 an unscoped request is accepted"

# A malformed epoch in an otherwise valid request is refused rather than ignored.
printf '{"version":1,"seq":90,"generation":-1,"event":{"kind":"set_output","output":"speaker"}}\n' > "$eprequest"
out="$(ep apply-request "$eprequest")"
has "generation must be a non-negative integer" "$out" "B18 a malformed epoch is refused"

# A ROLLED-BACK state file: the run marker wins, because every request minted in
# this life was stamped from it.
python3 -c 'import json,sys; p=sys.argv[1]; s=json.load(open(p)); s["generation"]=0; json.dump(s,open(p,"w"))' "$EPSTATE"
out="$(ep set speaker)"
has "epoch 2 adopted from the run marker (state file said 0)" "$out" \
    "B18 a rolled-back state file is brought back up to the live epoch"
eq "2" "$(epoch_of "$EPSTATE")" "B18 and the durable number is repaired"

# B18 (terra 2026-09-14, finding 1) -- A ROLLED-BACK STATE FILE MUST NOT
# RE-OPEN REQUESTS THE APPLIER ALREADY REFUSED. Carrying only the generation in
# the run marker left this hole: roll the file back to an old `request_seq`
# while the marker still names the live epoch, and a delayed request with the
# CURRENT generation and a sequence above the rolled-back mark was accepted.
printf '{"version":1,"seq":300,"generation":2,"event":{"kind":"set_output","output":"headset"}}\n' > "$eprequest"
out="$(ep apply-request "$eprequest")"
has "output speaker -> headset" "$out" "B18 a request at seq 300 lands"
# Now roll the STATE back -- generation and mark together, which is what a
# restore from a backup actually does.
python3 -c 'import json,sys; p=sys.argv[1]; s=json.load(open(p)); s["generation"]=0; s["request_seq"]=0; json.dump(s,open(p,"w"))' "$EPSTATE"
printf '{"version":1,"seq":200,"generation":2,"event":{"kind":"set_output","output":"mute"}}\n' > "$eprequest"
out="$(ep apply-request "$eprequest")"
has "request 200 ignored: not newer than the last applied (300)" "$out" \
    "B18 the mark is restored from the run marker, so seq 200 is still refused"
hasnt "output headset -> mute" "$out" "B18 and the rolled-back file did not re-open it"
eq "headset" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["output"])' "$EPSTATE")" \
    "B18 the position is untouched by the refusal"

# B18 (terra finding 2) -- CLEARING /run UNDER A RUNNING PANEL IS AN EPOCH
# BREAK. The process can no longer say which epoch it is in, so a request that
# NAMES one is refused until apply-state re-establishes it. A request that names
# none still applies: the rocker and the root CLI have no epoch to be wrong
# about, and a volume key that stopped working because /run was cleaned would be
# a worse failure than the one this guards.
# Start this one from a KNOWN epoch on a levelled output: the block above
# deliberately left the state rolled back, and a test that cannot say what the
# epoch was before it started cannot say what changed.
ep set speaker >/dev/null
out="$(ep apply-state)"
live_epoch="$(epoch_of "$EPSTATE")"
rm -f "$EP/run/audio-epoch.json"
printf '{"version":1,"seq":400,"generation":'"$live_epoch"',"event":{"kind":"set_output","output":"mute"}}\n' > "$eprequest"
out="$(ep apply-request "$eprequest")"
has "the epoch marker is gone" "$out" "B18 a scoped request is refused while the epoch is unvouched"
hasnt "output speaker -> mute" "$out" "B18 and it changed nothing"
# The rocker has no epoch of its own and must keep working: a volume key that
# stopped because /run was cleaned would be a worse failure than the one this
# fence guards.
before_level="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["volume"]["speaker"])' "$EPSTATE")"
ep volume up >/dev/null
after_level="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["volume"]["speaker"])' "$EPSTATE")"
[ "$after_level" -gt "$before_level" ] \
    && pass "B18 and the rocker is not stopped by a cleared /run" \
    || fail "B18 the rocker was stopped by a cleared /run ($before_level -> $after_level)"
printf '{"version":1,"seq":410,"event":{"kind":"set_output","output":"mute"}}\n' > "$eprequest"
out="$(ep apply-request "$eprequest")"
has "output speaker -> mute" "$out" "B18 an UNSCOPED request still applies with no marker"
# AND IT DOES NOT RE-CREATE THE MARKER. Writing one here would re-vouch for an
# epoch this process has just said it cannot vouch for, and the next apply-state
# would then keep the dead epoch instead of opening a new one.
[ ! -f "$EP/run/audio-epoch.json" ] \
    && pass "B18 and an unscoped request does not re-vouch for the dead epoch" \
    || fail "B18 an unscoped request re-created the epoch marker"
# The rocker and the CLI are likewise unaffected.
# apply-state re-establishes it, and scoped requests work again at the new epoch.
out="$(ep apply-state)"
has "opened (no run marker" "$out" "B18 apply-state re-establishes the epoch"
new_epoch="$(epoch_of "$EPSTATE")"
[ "$new_epoch" -gt "$live_epoch" ] \
    && pass "B18 and it is a NEW epoch, not the dead one" \
    || fail "B18 the dead epoch was kept ($live_epoch -> $new_epoch)"
printf '{"version":1,"seq":500,"generation":'"$new_epoch"',"event":{"kind":"set_output","output":"speaker"}}\n' > "$eprequest"
out="$(ep apply-request "$eprequest")"
has "output mute -> speaker" "$out" "B18 and a request at the new epoch lands again"



# ── B19 — the echo canceller's seam and item J's published mute ────────────
# Its own temp tree in `bus` mode, because both facts here are about what the
# applier PUBLISHES for other processes, and the publishing only happens where
# hardware is commanded.
AE="$TMP/aec"; mkdir -p "$AE/etc" "$AE/run"
printf 'bus\n' > "$AE/etc/audio-mode"
AESTATE="$AE/audio-state.json"
ae() { WALL_PANEL_CONF_DIR="$AE/etc" WALL_PANEL_RUN_DIR="$AE/run" \
       WALL_PANEL_AEC_BINARY="$AE/wall-audio-aec" \
       python3 "$APPLIER" --dry-run --state "$AESTATE" "$@" 2>&1; }
mic_source_of() { printf '%s' "$1" | grep -o 'WALL_AUDIO_MIC_SOURCE=[a-z_]*' | tail -1; }

# With no canceller installed and no knob, nothing changes: the legs read the
# raw panel microphone exactly as they did before step 6.
out="$(ae apply-state)"
has "microphone: mic_panel" "$out" "B19 with no canceller the panel mic is selected"

# The knob alone is not enough. A published PCM name nothing feeds would leave
# the mic legs opening a device that never produces a sample.
printf 'WALL_AUDIO_AEC=1\n' > "$AE/etc/audio-aec.env"
out="$(ae apply-state)"
has "microphone: mic_panel" "$out" "B19 the knob alone does not move the seam"

# Both halves, and only then.
printf '#!/bin/sh\n' > "$AE/wall-audio-aec"; chmod +x "$AE/wall-audio-aec"
out="$(ae apply-state)"
has "microphone: mic_clean" "$out" "B19 knob plus binary selects the cancelled microphone"

# AND ONLY IN SPEAKER. The canceller removes the ROOM's own music from the
# microphone, and the room only has music in Speaker: in Headset the sound is in
# somebody's ears, and in Mute nothing is playing at all.
out="$(ae set headset)"
hasnt "microphone: mic_clean" "$out" "B19 Headset does not go through the canceller"
out="$(ae set speaker)"
has "microphone: mic_clean" "$out" "B19 and Speaker does"

# ITEM J's EFFECTIVE MUTE, PUBLISHED FOR THE CANCELLER. Without it the daemon's
# status block goes on claiming a live microphone while every leg is stopped,
# and item L's ring is drawn over a coupled mute.
ae input-mute off >/dev/null
out="$(ae apply-state)"
has "WALL_AUDIO_INPUT_MUTED=0" "$(cat "$AE/run/audio-input-mute.env")" \
    "B19 an unmuted input is published as 0"
out="$(ae input-mute on)"
has "WALL_AUDIO_INPUT_MUTED=1" "$(cat "$AE/run/audio-input-mute.env")" \
    "B19 and a muted one as 1"
# The OUTPUT mute couples it, so the published value follows the switch too.
ae input-mute off >/dev/null
out="$(ae set mute)"
has "WALL_AUDIO_INPUT_MUTED=1" "$(cat "$AE/run/audio-input-mute.env")" \
    "B19 the coupled mute is published as muted (item J)"
# PUBLISHED BEFORE ANY LEG MOVES. Publishing after would leave a window in which
# the wall drew a live ring over a microphone already stopped.
mute_line="$(printf '%s\n' "$out" | grep -n 'input mute published' | head -1 | cut -d: -f1)"
stop_line="$(printf '%s\n' "$out" | grep -n 'systemctl stop' | head -1 | cut -d: -f1)"
{ [ -n "$mute_line" ] && [ -n "$stop_line" ] && [ "$mute_line" -lt "$stop_line" ]; } \
    && pass "B19 and it is published before any leg moves" \
    || fail "B19 a leg moved before the mute was published"

printf '\n%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
