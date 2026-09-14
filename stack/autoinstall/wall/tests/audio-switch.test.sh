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
mkdir -p "$WALL_PANEL_CONF_DIR"
printf 'bus\n' > "$WALL_PANEL_CONF_DIR/audio-mode"

run() { python3 "$APPLIER" --dry-run --state "$STATE" "$@" 2>&1; }

# B1 — speaker, the default position.
out="$(run apply-state)"
has "systemctl start wall-bus-speaker.service" "$out" "B1 speaker leg starts"
has "systemctl start wall-speaker-out.service" "$out" "B1 speaker output leg starts"
has "systemctl stop wall-bus-headset.service" "$out" "B1 headset leg is stopped"
has "sset Speaker unmute" "$out" "B1 the adapter is unmuted"
eq "1" "$(printf '%s\n' "$out" | grep -c 'sset Bus ')" "B1 the level is applied once"
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
has "sset Speaker mute" "$out" "B2 the adapter is muted"
has "UNAVAILABLE" "$out" "B2 the silence is journaled, not silent"
eq "headset" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["output"])' "$STATE")" \
    "B2 the mode stays selected (D3)"

# B3 — the one-shot, from a clean state.
rm -f "$STATE"
out="$(run headset add)"
has "auto-switch speaker -> headset" "$out" "B3 the adapter arriving switches once"
run set speaker >/dev/null
out="$(run headset add)"
has "no auto-switch" "$out" "B3 a second add does not drag the Owner back"
eq "speaker" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["output"])' "$STATE")" \
    "B3 the Owner's choice stands"

# B4 — per-output volume memory (ruling F).
rm -f "$STATE"
run set speaker >/dev/null
run volume 84 >/dev/null
run headset add >/dev/null          # auto-switches to headset
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

# B6 — requests from the broker.
request="$TMP/request.json"
printf '{"version":1,"seq":3,"event":{"kind":"set_output","output":"mute"}}\n' > "$request"
out="$(run apply-request "$request")"
has "output headset -> mute" "$out" "B6 a valid request is applied"
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
