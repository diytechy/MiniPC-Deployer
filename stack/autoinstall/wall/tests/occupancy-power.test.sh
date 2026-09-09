#!/usr/bin/env bash
# occupancy-power.test.sh — SN-015/SR-020 asserted on the ARTIFACTS.
#
# WHY THIS SUITE EXISTS, and it is the whole reason it is shaped this way.
# This block is made of timers, and this codebase has repeatedly learned that
# **a green timer says nothing about the thing it schedules**. `systemctl
# is-enabled wall-occupancy.timer` returning `enabled` proves that a unit file
# parses. It does not prove that any backlight went dark, that any RTC alarm was
# armed, or that a suspend would ever fire — and every one of those is a
# requirement here.
#
# So nothing below asserts an exit code as evidence of behaviour. Each check
# runs the REAL `wall-sleep.sh` against:
#   * a fake sysfs backlight tree — asserted by READING BACK the integer that
#     ended up in `brightness`;
#   * a fake `rtcwake` on PATH that RECORDS its argv — asserted by taking the
#     `-t <epoch>` it was actually given and converting it back to a wall-clock
#     time, which must be 06:45;
#   * a fake `systemctl` that RECORDS `suspend` — so "would the machine actually
#     have suspended" is a file that exists or does not.
#
# HERMETIC: a temp directory, three shims on PATH, and a python3 shim pointing at
# whatever python this box has. No unit is installed, no daemon started, no real
# backlight, RTC or suspend is touched — `systemctl` is never the real one.
#
# WHAT IT LOCKS
#   A1  detection OFF: an occupancy tick writes NOTHING (no dim, no suspend),
#       however long the absence — the SLEEP_START schedule stands unchanged
#   A2  detection OFF: the 22:00 `start` still arms the RTC and suspends, and
#       the alarm it arms is 06:45 (the ratified SLEEP_END)
#   A3  detection ON, present, inside the on-period: the backlight is ON and NO
#       suspend was ever recorded — the walk-in needs no resume because nothing
#       ever suspended
#   A4  detection ON, absent 10 h INSIDE the on-period: brightness 0 and still
#       no suspend — the on-period is a hard gate, not a longer timer
#   A5  detection ON, absent 61 min OUTSIDE it: brightness 0, suspend recorded,
#       and the armed alarm resolves to 06:45
#   A6  detection ON, absent 30 min OUTSIDE it: brightness 0, no suspend
#   A7  SN-013 S3 edge: rtcwake CANNOT arm -> NO suspend recorded and the
#       backlight is 0 (the L1 degrade), on the occupancy path too
#   A8  SN-013 mains blip: tmpfs lost the absence clock -> no suspend, and the
#       clock restarts from now
#   A9  no presence writer at all -> reads as PRESENT: lit, awake, no suspend,
#       at 03:00, with an ancient absence clock sitting right there
#   A10 detection ON: the 22:00 boundary hands over instead of suspending a
#       panel somebody is standing at
#   A11 ONE WRITER, asserted as a PROPERTY and not as a spelling: no file in the
#       wall tree may change the backlight or put the machine to sleep by ANY of
#       the mechanisms this image could use, except wall-sleep.sh — and one tick
#       really does invoke the decider exactly once (counted at RUNTIME)
#   A12 ONE KNOB, asserted BEHAVIOURALLY: a single SLEEP_END line in wall.env is
#       the value the decider gates the on-period on AND the value the alarm is
#       armed for, in the same run; and no other variable anywhere in the wall
#       tree carries a wall-clock time
#   A13 an absence that BEGAN inside the on-period does not suspend the moment
#       the boundary passes — the hour must be an hour spent OUTSIDE it
#   A14 a truncated absence clock ("1") is not five decades of absence
#   A15 a presence file whose observedAt is NaN is malformed, not fresh
#   A16 an rtcwake that exits 0 having programmed NOTHING is not an armed alarm
#   A17 the RTC frame is established, not asserted: -u for a UTC RTC, -l for a
#       LOCAL one, and the alarm verifies in both
#   A18 the alarm is the NEXT LOCAL OCCURRENCE of the wake time, so it is still
#       06:45 across a DST boundary and not 05:45
#   A19 a backlight that cannot be turned off blocks the suspend
#
# Usage: bash occupancy-power.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="$(cd "$HERE/.." && pwd)"
SLEEP_SH="$DIR/wall-sleep.sh"
DECIDER="$DIR/wall-occupancy.py"
ENV_EXAMPLE="$DIR/wall.env.example"
for f in "$SLEEP_SH" "$DECIDER" "$ENV_EXAMPLE"; do
    [ -f "$f" ] || { echo "FATAL: $f not found"; exit 2; }
done
PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "FATAL: no python3"; exit 2; }
# Captured BEFORE $BIN goes on PATH, so the date shim below can delegate to the
# real one without recursing into itself.
REAL_DATE="$(command -v date)"
[ -n "$REAL_DATE" ] || { echo "FATAL: no date"; exit 2; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$*"; }
eq() {  # eq EXPECTED ACTUAL LABEL
    if [ "$1" = "$2" ]; then pass "$3"; else fail "$3 (expected '$1', got '$2')"; fi
}

# ── the fakes ────────────────────────────────────────────────────────────────
# Each one RECORDS what it was asked to do into a file. That recording is the
# evidence; the scripts' exit codes are not.
BIN="$TMP/bin"
mkdir -p "$BIN"

cat > "$BIN/systemctl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "\$SYSTEMCTL_LOG"
exit 0
EOF

# rtcwake records its argv AND — this is the part that matters after the review
# — PROGRAMS THE FAKE RTC, exactly as the kernel would: the wakealarm node holds
# seconds-since-epoch computed as if the RTC were UTC, so a `-l` arming lands
# shifted by the local UTC offset. Without this the fake was indistinguishable
# from a firmware that returns 0 and programs nothing, which is precisely the
# failure A16 now reproduces via RTCWAKE_SILENT_NOOP.
#   RTCWAKE_FAIL         — a non-zero exit (SN-013's "cannot even be ARMED", A7)
#   RTCWAKE_SILENT_NOOP  — exit 0, no alarm programmed (A16)
cat > "$BIN/rtcwake" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "\$RTCWAKE_LOG"
[ -n "\${RTCWAKE_FAIL:-}" ] && exit 1
if [ -z "\${RTCWAKE_SILENT_NOOP:-}" ] && [ -n "\${FAKE_RTC_DIR:-}" ]; then
    epoch=""; frame="-u"
    while [ \$# -gt 0 ]; do
        case "\$1" in
            -t) epoch="\$2"; shift ;;
            -l) frame="-l" ;;
            -u) frame="-u" ;;
        esac
        shift
    done
    if [ -n "\$epoch" ]; then
        if [ "\$frame" = "-l" ]; then
            z="\$("$REAL_DATE" -d "@\$epoch" +%z)"
            epoch=\$(( epoch + \${z:0:1}1 * (10#\${z:1:2} * 3600 + 10#\${z:3:2} * 60) ))
        fi
        mkdir -p "\$FAKE_RTC_DIR/rtc0"
        printf '%s\n' "\$epoch" > "\$FAKE_RTC_DIR/rtc0/wakealarm"
    fi
fi
exit 0
EOF

# timedatectl — systemd's answer to "is the RTC in local time", which is the
# FIRST thing wall-sleep.sh asks. FAKE_LOCAL_RTC drives it (default: no, the
# stock Ubuntu configuration this image actually ships).
cat > "$BIN/timedatectl" <<EOF
#!/usr/bin/env bash
# FAKE_NO_TIMEDATECTL makes it answer nothing, which is how the /etc/adjtime
# fallback gets exercised on a box where timedatectl is nonetheless present.
if [ -z "\${FAKE_NO_TIMEDATECTL:-}" ]; then
    case "\$*" in
        *LocalRTC*) printf '%s\n' "\${FAKE_LOCAL_RTC:-no}"; exit 0 ;;
    esac
fi
exit 1
EOF

# date — a passthrough EXCEPT when FAKE_NOW is set, when the three "what time is
# it" forms answer from that fixed epoch instead. Every conversion form is
# delegated untouched, so the arithmetic under test is the real date(1)'s. This
# is what makes the DST case (A18) a deterministic assertion rather than a note
# to re-run the suite in November.
cat > "$BIN/date" <<EOF
#!/usr/bin/env bash
if [ -n "\${FAKE_NOW:-}" ] && [ \$# -eq 1 ]; then
    case "\$1" in
        +%s) printf '%s\n' "\$FAKE_NOW"; exit 0 ;;
        +%F|+%H|+%M|+%H:%M) exec "$REAL_DATE" -d "@\$FAKE_NOW" "\$1" ;;
    esac
fi
exec "$REAL_DATE" "\$@"
EOF

# python3 records the argv it was handed as well, so "the decider was called
# once, with THESE knobs" is a counted fact rather than a grep of the source.
cat > "$BIN/python3" <<EOF
#!/usr/bin/env bash
[ -n "\${PY3_LOG:-}" ] && printf '%s\n' "\$*" >> "\$PY3_LOG"
exec "$PY" "\$@"
EOF
chmod +x "$BIN/systemctl" "$BIN/rtcwake" "$BIN/python3" "$BIN/timedatectl" "$BIN/date"
export PATH="$BIN:$PATH"

# ── the sandbox one scenario runs in ─────────────────────────────────────────
# scenario NAME — build a fresh fake root and return its path in $ROOT.
# The backlight starts LIT at 100 so "off" is a change we can see, not a value
# that was already there. That distinction matters: a check that only asserts
# `brightness == 0` against a tree initialised to 0 asserts nothing.
scenario() {
    ROOT="$TMP/$1"
    rm -rf "$ROOT"
    mkdir -p "$ROOT/sys/class/backlight/intel_backlight" "$ROOT/run" "$ROOT/etc"
    printf '100' > "$ROOT/sys/class/backlight/intel_backlight/brightness"
    printf '100' > "$ROOT/sys/class/backlight/intel_backlight/max_brightness"
    SYSTEMCTL_LOG="$ROOT/systemctl.log"; : > "$SYSTEMCTL_LOG"
    RTCWAKE_LOG="$ROOT/rtcwake.log";     : > "$RTCWAKE_LOG"
    PY3_LOG="$ROOT/python3.log";         : > "$PY3_LOG"
    # Where the fake rtcwake programs its alarm — the same tree wall-sleep.sh
    # reads it back from, so "armed" is one artifact written by one actor and
    # read by another, not a shared variable.
    FAKE_RTC_DIR="$ROOT/sys/class/rtc"
    export SYSTEMCTL_LOG RTCWAKE_LOG PY3_LOG FAKE_RTC_DIR
    unset RTCWAKE_FAIL RTCWAKE_SILENT_NOOP FAKE_LOCAL_RTC FAKE_NOW FAKE_NO_TIMEDATECTL
    ENV_FILE="$ROOT/etc/wall.env"
    PRESENCE_FILE="$ROOT/run/presence.json"
}

# write_env KEY=VALUE... — the panel's wall.env for this scenario.
write_env() {
    : > "$ENV_FILE"
    printf '%s\n' "$@" >> "$ENV_FILE"
    printf 'WALL_PRESENCE_FILE=%s\n' "$PRESENCE_FILE" >> "$ENV_FILE"
}

# presence absent|present [AGE_MS] — write the IF-012 signal file.
presence() {
    local age="${2:-0}"
    "$PY" - "$PRESENCE_FILE" "$1" "$age" <<'PYEOF'
import json, sys, time
path, state, age_ms = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(path, "w", encoding="utf-8") as fh:
    json.dump({"schemaVersion": 1, "presence": state,
               "observedAt": int(time.time() * 1000) - age_ms,
               "ttlMs": 30000, "source": "occupancy-power.test.sh"}, fh)
PYEOF
}

# absent_for MINUTES — pre-seed the absence clock as if absence began then.
absent_for() {
    mkdir -p "$ROOT/run/wall-occupancy"
    printf '%s' "$(( $(date +%s) - $1 * 60 ))" > "$ROOT/run/wall-occupancy/absent-since"
}

# run VERB — the REAL script, in this sandbox.
#
# THE ONE THING THE SANDBOX CHANGES, and why it is safe: wall-sleep.sh reads its
# config from the hard-coded /etc/wall-panel/wall.env, which no test may create
# or touch. So the suite rewrites exactly that ONE assignment line to point at a
# temp wall.env, and A0 below asserts that the rewrite is a one-line diff — the
# decision, the backlight write, arm_rtc and the suspend are the shipped bytes.
# It is re-derived from the tracked file on every call, so it can never be a
# stale copy of a script that has since changed.
SCRIPT_UNDER_TEST="$TMP/wall-sleep.under-test.sh"
prepare_script() {
    # ONE substitution, on the single ENV_FILE assignment line, so the sandbox
    # can point the real logic at a temp wall.env. Everything else — the
    # decision, the backlight write, arm_rtc, the suspend — is the shipped code
    # byte for byte. Asserted below (A0) so this cannot quietly become a rewrite.
    sed 's|^ENV_FILE="/etc/wall-panel/wall.env"$|ENV_FILE="'"$ENV_FILE"'"|' \
        "$SLEEP_SH" > "$SCRIPT_UNDER_TEST"
    # The decider goes NEXT TO the script, which is the fallback location
    # wall-sleep.sh already looks in when /usr/local/sbin has no copy. Nothing
    # is patched to find it — and leaving it out is not a subtle failure either:
    # the script says so in three lines and does nothing, which is how this very
    # omission surfaced while writing this suite.
    cp "$DECIDER" "$(dirname "$SCRIPT_UNDER_TEST")/wall-occupancy.py"
    grep -q "^ENV_FILE=\"$ENV_FILE\"$" "$SCRIPT_UNDER_TEST"
}
run() {
    prepare_script || { fail "could not point the script at the sandbox wall.env"; return 1; }
    PANEL_POWER_TEST_ROOT="$ROOT" bash "$SCRIPT_UNDER_TEST" "$1" >"$ROOT/out.log" 2>&1
}

brightness() { cat "$ROOT/sys/class/backlight/intel_backlight/brightness"; }
suspended()  { grep -qx 'suspend' "$SYSTEMCTL_LOG" && echo yes || echo no; }
# The armed alarm, converted BACK to a wall-clock time. This is the artifact
# assertion the trap in §8 is about: not "rtcwake was called", but "the epoch it
# was handed is 06:45".
armed_hhmm() {
    local epoch
    epoch="$(grep -o -- '-t [0-9]\+' "$RTCWAKE_LOG" | tail -1 | awk '{print $2}')"
    [ -n "$epoch" ] || { echo "NONE"; return; }
    date -d "@$epoch" +%H:%M
}
# The flag rtcwake was actually given for the RTC's frame — the finding that
# started this round was that it was always `-l`.
armed_frame() {
    grep -o -- '-[lu] ' "$RTCWAKE_LOG" | tail -1 | tr -d ' \n'
}
decider_calls() {
    grep -c 'wall-occupancy.py' "$PY3_LOG" 2>/dev/null || echo 0
}
decider_on_start() {
    grep -o -- '--on-start [0-9:]*' "$PY3_LOG" | tail -1 | awk '{print $2}'
}
armed_is_future() {
    local epoch
    epoch="$(grep -o -- '-t [0-9]\+' "$RTCWAKE_LOG" | tail -1 | awk '{print $2}')"
    [ -n "$epoch" ] && [ "$epoch" -gt "$(date +%s)" ] && echo yes || echo no
}

echo "── occupancy power: the artifacts, not the timers ──"

# ── A0: the sandbox runs the SHIPPED logic ───────────────────────────────────
scenario a0
write_env "SLEEP_MODE=suspend"
prepare_script
DIFF_LINES="$(diff "$SLEEP_SH" "$SCRIPT_UNDER_TEST" | grep -c '^[<>]' || true)"
eq "2" "$DIFF_LINES" "A0 the script under test differs from the shipped one by exactly the ENV_FILE line"

# ── A1: detection OFF — the tick writes NOTHING ──────────────────────────────
scenario a1
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=false"
presence absent
absent_for 600
run occupancy
eq "100" "$(brightness)" "A1 detection off: the backlight is untouched by an occupancy tick"
eq "no" "$(suspended)" "A1 detection off: no suspend, after 10 h of absence"
eq "NONE" "$(armed_hhmm)" "A1 detection off: no RTC alarm was armed"

# ── A2: detection OFF — the schedule is unchanged, INCLUDING its morning ────
# The cross-review's point, and it is the whole of V3: the acceptance promises
# that with absence detection off the existing schedule stands COMPLETELY
# unchanged, and the morning wake is part of that schedule. It shipped as 06:30.
# 06:45 is the ratified OCCUPANCY wake (A5, A10, A20), not a change to this path.
scenario a2
write_env "SLEEP_MODE=suspend"
run start
eq "yes" "$(suspended)" "A2 detection off: SLEEP_START still suspends (schedule unchanged)"
eq "06:30" "$(armed_hhmm)" "A2 detection off: the morning is still 06:30 — the shipped schedule"
eq "yes" "$(armed_is_future)" "A2 the armed alarm is in the FUTURE, not this morning"

# ── A3: ON, present, inside the on-period — the walk-in ──────────────────────
# Nothing suspended, so lighting the screen is a backlight write. That IS the
# "without a resume" criterion: there is no resume to perform.
scenario a3
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" "SLEEP_END=00:01" "SLEEP_START=23:59"
presence absent
run occupancy
eq "0" "$(brightness)" "A3 nobody present: the backlight really is off (brightness 0)"
presence present
run occupancy
BL="$(brightness)"
[ "$BL" != "0" ] && pass "A3 the walk-in lights the screen (brightness $BL)" \
                 || fail "A3 the walk-in left the backlight at 0"
eq "no" "$(suspended)" "A3 the walk-in needed NO resume — nothing ever suspended"
eq "NONE" "$(armed_hhmm)" "A3 no RTC alarm was armed inside the on-period"

# ── A4: ON, absent 10 h INSIDE the on-period — dim, never asleep ─────────────
scenario a4
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" "SLEEP_END=00:01" "SLEEP_START=23:59" \
          "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
absent_for 600
run occupancy
eq "0" "$(brightness)" "A4 absent inside the on-period: brightness 0"
eq "no" "$(suspended)" "A4 absent TEN HOURS inside the on-period: still no suspend"

# ── A5: ON, absent 61 min OUTSIDE it — the one case that sleeps ──────────────
# The on-period is squeezed to the single minute 06:45 so "now" is outside it
# whatever time this suite runs at - except, once a day, at 06:45 itself, which
# is guarded here rather than left as a once-per-1440-runs flake.
ONP_START=06:45; ONP_END=06:46
if [ "$(date +%H:%M)" = "06:45" ]; then
    ONP_START=06:47; ONP_END=06:48
    echo "  NOTE: local time is 06:45, so the on-period slot is shifted to $ONP_START"
fi
scenario a5
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
absent_for 61
run occupancy
eq "0" "$(brightness)" "A5 absent outside the on-period: brightness 0"
eq "yes" "$(suspended)" "A5 the suspend REALLY fired (systemctl suspend recorded)"
eq "$ONP_START" "$(armed_hhmm)" "A5 the RTC alarm was armed for $ONP_START — the on-period start"
eq "yes" "$(armed_is_future)" "A5 the alarm epoch is in the future"

# ── A6: ON, absent 30 min OUTSIDE it — the hour is a real hour ───────────────
scenario a6
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
absent_for 30
run occupancy
eq "0" "$(brightness)" "A6 half an hour of absence: dark"
eq "no" "$(suspended)" "A6 half an hour of absence: awake"

# ── A7: SN-013 — the alarm cannot be ARMED, so nothing suspends ─────────────
scenario a7
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
absent_for 61
RTCWAKE_FAIL=1 run occupancy
eq "no" "$(suspended)" "A7 SN-013: rtcwake cannot arm -> the panel REFUSES to suspend"
eq "0" "$(brightness)" "A7 SN-013: it degrades to backlight-off, reachable over the LAN"
# And the same guard on the scheduled path, so the two cannot diverge.
scenario a7b
write_env "SLEEP_MODE=suspend"
RTCWAKE_FAIL=1 run start
eq "no" "$(suspended)" "A7 SN-013: the SCHEDULED path refuses too (one guard, both paths)"
eq "0" "$(brightness)" "A7 SN-013: scheduled path degrades to backlight-off"

# ── A8: SN-013 mains blip — tmpfs lost the absence clock ────────────────────
# quirk 4b: no battery, no UPS. After a blip /run is empty, so the panel comes
# back lit and awake and must serve a FULL timeout before it may suspend again.
# It must never resume a pre-blip countdown and put itself out of reach.
scenario a8
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
rm -rf "$ROOT/run/wall-occupancy"    # what a reboot leaves behind on tmpfs
run occupancy
eq "no" "$(suspended)" "A8 SN-013 mains blip: no absence clock -> no suspend"
eq "NONE" "$(armed_hhmm)" "A8 SN-013 mains blip: no RTC alarm armed either"
[ -f "$ROOT/run/wall-occupancy/absent-since" ] \
    && pass "A8 the absence clock restarts from now" \
    || fail "A8 the absence clock was not restarted"
SINCE="$(cat "$ROOT/run/wall-occupancy/absent-since")"
[ "$(( $(date +%s) - SINCE ))" -lt 120 ] \
    && pass "A8 the restarted clock is NOW, not a pre-blip timestamp" \
    || fail "A8 the restarted clock is stale ($SINCE)"

# ── A9: no presence writer at all — fail safe, in the safe direction ────────
scenario a9
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
absent_for 600                 # an ancient absence clock sitting right there
rm -f "$PRESENCE_FILE"         # ...and nothing asserting absence
run occupancy
eq "no" "$(suspended)" "A9 no presence file: absence is never ASSERTED, so nothing suspends"
eq "100" "$(brightness)" "A9 no presence file: the screen stays lit"
[ -f "$ROOT/run/wall-occupancy/absent-since" ] \
    && fail "A9 the stale absence clock survived a PRESENT reading" \
    || pass "A9 the stale absence clock was cleared by the PRESENT reading"
# Same again for a file that exists but is garbage.
scenario a9b
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
absent_for 600
printf 'not json' > "$PRESENCE_FILE"
run occupancy
eq "no" "$(suspended)" "A9 malformed presence file: still no suspend"
eq "100" "$(brightness)" "A9 malformed presence file: still lit"
# And for a stale one — a writer that died mid-window.
scenario a9c
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
absent_for 600
presence absent 3600000        # an hour old, ttl 30 s
run occupancy
eq "no" "$(suspended)" "A9 stale presence reading: the dead writer wakes the screen"
eq "100" "$(brightness)" "A9 stale presence reading: lit"

# ── A10: ON — 22:00 hands over rather than sleeping on somebody ─────────────
scenario a10
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=06:45" "SLEEP_START=22:00" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence present
run start
eq "no" "$(suspended)" "A10 SLEEP_START with somebody present: handover, NOT a suspend"
eq "NONE" "$(armed_hhmm)" "A10 SLEEP_START with somebody present: no alarm armed"

# ── A11: ONE WRITER — the PROPERTY, not the two spellings it used to have ───
# The defect this block exists to prevent is two parallel sets of logic, and the
# only way to catch the second one is to catch it being ADDED. The previous
# version of this check grepped the literal strings `class/backlight` and
# `systemctl suspend`, so a second writer spelled `brightnessctl`, `loginctl
# suspend` or `echo mem > /sys/power/state` walked straight past it. So the
# check now names the MECHANISMS — every way this image could plausibly dim a
# screen or sleep a machine — and scans every file in the wall tree, units
# included, not just *.sh and *.py.
#
# Comment lines are stripped first, because these units DISCUSS suspending at
# length (wall-sync-suspend.service exists to explain a suspend that failed for
# 40 s) and prose about a mechanism is not a use of it.
BACKLIGHT_MECHANISMS='class/backlight|brightnessctl|xbacklight|ddcutil|wlr-randr|dpms|light[[:space:]]+-S|backlight_set'
SUSPEND_MECHANISMS='systemctl[[:space:]]+(--[a-z=-]+[[:space:]]+)*suspend|loginctl[[:space:]]+(suspend|hibernate)|systemd-run[^|]*suspend|pm-suspend|/sys/power/state|rtcwake[^|]*-m[[:space:]]*(mem|disk|standby|off|freeze)|dbus-send[^|]*Suspend|busctl[^|]*Suspend|zzz'
# power_writers REGEX — the basenames of the wall-tree files whose EXECUTABLE
# lines match, one per line, deduplicated.
power_writers() {
    local re="$1" f names=""
    while IFS= read -r f; do
        if sed -e 's/^[[:space:]]*#.*$//' "$f" 2>/dev/null | grep -qE "$re"; then
            names="$names$(basename "$f")\n"
        fi
    done <<< "$(find "$DIR" -type f ! -path '*/tests/*' ! -path '*__pycache__*' ! -name '*.md')"
    printf '%b' "$names" | grep -v '^$' | sort -u | tr '\n' ' '
}
eq "wall-sleep.sh " "$(power_writers "$BACKLIGHT_MECHANISMS")" \
    "A11 exactly ONE file in the wall tree can change the backlight, by ANY mechanism"
eq "wall-sleep.sh " "$(power_writers "$SUSPEND_MECHANISMS")" \
    "A11 exactly ONE file in the wall tree can suspend, by ANY mechanism"
# And at RUNTIME, not in the source: one tick, one decision. A second call would
# be a second decision, which is the drift this block is built to prevent.
scenario a11
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
absent_for 61
run occupancy
eq "1" "$(decider_calls)" "A11 one occupancy tick invokes the decider EXACTLY once (counted at runtime)"
eq "yes" "$(suspended)" "A11 ...and that single decision is the one that suspended"

# ── A12: ONE KNOB — asserted by MOVING it, not by reading the example file ──
# The previous version grepped wall.env.example for a list of alternative knob
# names and checked one source line of the decider. Neither would have noticed a
# `WALL_ON_START` added inside wall-sleep.sh and defaulted from SLEEP_END: the
# example file would not mention it, the decider would still echo its argument,
# and the two paths would drift with a green suite. So: set ONE SLEEP_END line
# to an unusual value and require the SAME value to appear in BOTH jobs, in the
# same run — the on-period the decider gated on, and the epoch the alarm was
# armed for.
KNOB=04:07
scenario a12
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$KNOB" "SLEEP_START=04:08" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
absent_for 61
run occupancy
eq "$KNOB" "$(decider_on_start)" "A12 the ONE SLEEP_END line is what gated the on-period"
eq "$KNOB" "$(armed_hhmm)" "A12 ...and the SAME value is what the RTC alarm was armed for"
eq "$(decider_on_start)" "$(armed_hhmm)" "A12 both jobs came from one value, in one run"
# And no OTHER variable anywhere in the wall tree carries a wall-clock time.
# This is what a second knob would look like on the day it is introduced,
# whatever it is called and wherever its default comes from.
TIME_KNOBS="$(grep -hoE '^[[:space:]]*:[[:space:]]*"\$\{[A-Z_]+:=(([0-2]?[0-9]:[0-9]{2})|\$\{?SLEEP_(END|START)\}?)"?\}"?' \
    "$SLEEP_SH" "$DIR/wall-firstboot.sh" \
    | grep -oE '\{[A-Z_]+:=' | tr -d '{:=' | sort -u | tr '\n' ' ')"
eq "SLEEP_END SLEEP_START " "$TIME_KNOBS" \
    "A12 exactly two variables in the wall scripts hold a wall-clock time"
# The decider's RTC wake IS its on-period start — asserted by running it, not by
# grepping the line that implements it.
RTC_ECHO="$("$PY" "$DECIDER" --now-epoch 1757000000 --minute-of-day 0 \
    --presence-file "$TMP/nonexistent.json" --on-start 03:21 --on-end 22:00 \
    | grep '^RTC_WAKE=' | cut -d= -f2)"
eq "03:21" "$RTC_ECHO" "A12 the decider reports the RTC wake AS the on-period start it was given"

# ── A13: the hour must be an hour spent OUTSIDE the on-period ───────────────
# THE CENTRAL RULE, and it was broken. Somebody leaves at 12:00 and is still
# away at 22:00. The absence clock was started inside the on-period and carried
# straight across the boundary, so the first tick outside saw 600 minutes and
# suspended AT ONCE — the required hour outside never observed. Tick 1 below is
# inside the on-period with an ancient clock already sitting there; tick 2 is
# outside it, one minute later.
scenario a13
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=00:01" "SLEEP_START=23:59" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
absent_for 600                      # absent since midday, inside the on-period
run occupancy
eq "no" "$(suspended)" "A13 absent 10 h INSIDE the on-period: no suspend (as A4)"
[ -f "$ROOT/run/wall-occupancy/absent-since" ] \
    && fail "A13 the absence clock ran INSIDE the on-period, so it can carry across" \
    || pass "A13 the absence clock does not run inside the on-period"
# Now the boundary passes: same absence, same panel, now outside.
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
run occupancy
eq "no" "$(suspended)" "A13 the boundary passes on a 10 h absence: it does NOT suspend at once"
eq "0" "$(brightness)" "A13 ...it goes dark and stays reachable"
SINCE="$(cat "$ROOT/run/wall-occupancy/absent-since" 2>/dev/null || echo 0)"
[ "$(( $(date +%s) - SINCE ))" -lt 120 ] \
    && pass "A13 the hour starts AT the boundary, not at midday" \
    || fail "A13 the absence clock carried across the boundary ($SINCE)"

# ── A14: a truncated absence clock is not five decades of absence ───────────
# A non-atomic or interrupted write leaves a prefix. "1" reads as 1970, i.e.
# ~2.9 million minutes of absence, and the panel suspends on the next tick.
scenario a14
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
mkdir -p "$ROOT/run/wall-occupancy"
printf '1' > "$ROOT/run/wall-occupancy/absent-since"
run occupancy
eq "no" "$(suspended)" "A14 a truncated absence clock ('1') does NOT suspend the panel"
eq "0" "$(brightness)" "A14 ...it goes dark, and the timer restarts"
SINCE="$(cat "$ROOT/run/wall-occupancy/absent-since" 2>/dev/null || echo 0)"
[ "$(( $(date +%s) - SINCE ))" -lt 120 ] \
    && pass "A14 the nonsense clock was replaced with a real one" \
    || fail "A14 the nonsense clock survived ($SINCE)"

# ── A15: NaN is malformed, not fresh ───────────────────────────────────────
# json.loads accepts NaN, and every comparison against NaN is false — so a
# presence file with "observedAt": NaN passes the stale check, passes the skew
# check, and is believed when it says "absent". That inverts the fail-safe.
scenario a15
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
absent_for 61
printf '%s' '{"schemaVersion":1,"presence":"absent","observedAt":NaN,"ttlMs":30000,"source":"t"}' \
    > "$PRESENCE_FILE"
run occupancy
eq "no" "$(suspended)" "A15 a NaN observedAt is malformed: no suspend"
eq "100" "$(brightness)" "A15 a NaN observedAt reads as PRESENT: the screen stays lit"
scenario a15b
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
absent_for 61
printf '%s' '{"schemaVersion":1,"presence":"absent","observedAt":1,"ttlMs":1e999,"source":"t"}' \
    > "$PRESENCE_FILE"
run occupancy
eq "no" "$(suspended)" "A15 an infinite ttlMs (1e999) is malformed too: no suspend"

# ── A16: a zero exit is not an armed alarm ─────────────────────────────────
# Firmware, a wrapper or a driver that ignores the ioctl can all return success
# having programmed nothing. The old fake could not express that — it only
# recorded arguments — so the suite's "armed" assertion passed in exactly this
# failure mode. Now the alarm is READ BACK, and the fake can lie.
scenario a16
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
absent_for 61
RTCWAKE_SILENT_NOOP=1 run occupancy
eq "no" "$(suspended)" "A16 rtcwake exits 0 but programs NOTHING -> the panel refuses to suspend"
eq "0" "$(brightness)" "A16 ...and degrades to backlight-off, reachable over the LAN"
[ -f "$ROOT/sys/class/rtc/rtc0/wakealarm" ] \
    && fail "A16 the scenario did not actually reproduce an unprogrammed alarm" \
    || pass "A16 the scenario really did leave the RTC unprogrammed"
scenario a16b
write_env "SLEEP_MODE=suspend"
RTCWAKE_SILENT_NOOP=1 run start
eq "no" "$(suspended)" "A16 the SCHEDULED path refuses an unverifiable alarm too"

# ── A17: the RTC's frame is ESTABLISHED, not asserted ──────────────────────
# THE WORST FINDING OF THE ROUND. `-l` says "this RTC keeps LOCAL time". A stock
# Linux box keeps it in UTC, so `-l` programmed the alarm a whole UTC offset
# away: a 06:45 alarm becomes 00:45 or 12:45 in Chicago and the panel suspends
# and never wakes. The flag must follow what the box actually says.
scenario a17
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
absent_for 61
FAKE_LOCAL_RTC=no run occupancy
eq "-u" "$(armed_frame)" "A17 LocalRTC=no (this image's actual configuration) arms with -u"
eq "yes" "$(suspended)" "A17 ...and the alarm verifies, so the suspend proceeds"
eq "$ONP_START" "$(armed_hhmm)" "A17 ...at the local wall-clock time asked for"
scenario a17b
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
absent_for 61
# A box whose RTC really is in local time, with a REAL offset (git-bash has no
# tzdata, so the zone is spelled as a POSIX rule string, which glibc/msys can
# resolve without one).
export TZ='CST6CDT,M3.2.0/2,M11.1.0/2'
FAKE_LOCAL_RTC=yes run occupancy
eq "-l" "$(armed_frame)" "A17 LocalRTC=yes arms with -l"
eq "yes" "$(suspended)" "A17 ...and the readback accounts for the offset, so it verifies"
eq "$ONP_START" "$(armed_hhmm)" "A17 ...still at the local wall-clock time asked for"
unset TZ
# With no timedatectl at all, /etc/adjtime is the authority — hwclock's own
# record, and what util-linux itself reads.
scenario a17c
write_env "SLEEP_MODE=suspend"
printf '0.0 0 0.0\n0\nLOCAL\n' > "$ROOT/etc/adjtime"
FAKE_NO_TIMEDATECTL=1 run start
eq "-l" "$(armed_frame)" "A17 /etc/adjtime saying LOCAL is honoured (timedatectl is only asked first)"
scenario a17d
write_env "SLEEP_MODE=suspend"
printf '0.0 0 0.0\n0\nUTC\n' > "$ROOT/etc/adjtime"
FAKE_NO_TIMEDATECTL=1 run start
eq "-u" "$(armed_frame)" "A17 /etc/adjtime saying UTC is honoured"

# ── A18: the alarm is a WALL-CLOCK promise, across a DST boundary ──────────
# `target + 86400` is only a day when the offset does not move. On the night the
# clocks go back it lands at 05:45 instead of 06:45 — an hour early every autumn
# and an hour late every spring, on the one wake the panel cannot miss.
scenario a18
write_env "SLEEP_MODE=suspend" "SLEEP_END=06:45"
export TZ='CST6CDT,M3.2.0/2,M11.1.0/2'
FAKE_NOW="$("$REAL_DATE" -d '2026-10-31 23:00' +%s)"
export FAKE_NOW
run start
eq "06:45" "$(armed_hhmm)" "A18 the alarm across the autumn DST boundary is still 06:45 local"
eq "2026-11-01" "$("$REAL_DATE" -d "@$(grep -o -- '-t [0-9]\+' "$RTCWAKE_LOG" | tail -1 | awk '{print $2}')" +%F)" \
    "A18 ...on the NEXT CALENDAR DAY, not 86400 seconds later"
unset FAKE_NOW
unset TZ

# ── A19: a backlight that cannot go off blocks the suspend ─────────────────
# The decision being applied is "nobody is here: go dark, THEN sleep". If the
# dark half cannot happen, suspending anyway takes the panel off the LAN having
# satisfied neither half — lit AND unreachable. Staying awake is the direction
# somebody can SSH into and fix.
scenario a19
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
absent_for 61
rm -rf "$ROOT/sys/class/backlight"      # the interface renamed, or gone
run occupancy
eq "no" "$(suspended)" "A19 no backlight node: the panel does NOT suspend having stayed lit"
scenario a19b
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" \
          "SLEEP_END=$ONP_START" "SLEEP_START=$ONP_END" "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
absent_for 61
# A brightness node that cannot be written at all — the read-only-sysfs case.
BL_DIR="$ROOT/sys/class/backlight/intel_backlight"
chmod a-w "$BL_DIR/brightness" 2>/dev/null || true
if [ -w "$BL_DIR/brightness" ]; then
    echo "  NOTE: this filesystem ignores chmod a-w, so the read-only-node case is not run here"
else
    run occupancy
    eq "no" "$(suspended)" "A19 a read-only brightness node blocks the suspend too"
    chmod u+w "$BL_DIR/brightness" 2>/dev/null || true
fi

# ── A20: the ratified 06:45 IS the occupancy wake ──────────────────────────
# The other half of V3: 06:45 must still be what an occupancy panel wakes to,
# with no SLEEP_END line in wall.env at all.
scenario a20
write_env "SLEEP_MODE=suspend" "WALL_ABSENCE_ENABLED=true" "SLEEP_START=22:00" \
          "WALL_ABSENCE_TIMEOUT_MIN=60"
presence absent
absent_for 61
if [ "$(date +%H%M)" -ge 645 ] && [ "$(date +%H%M)" -lt 2200 ]; then
    echo "  NOTE: local time is inside the 06:45-22:00 on-period, so A20 asserts the decider's knob only"
    run occupancy
    eq "06:45" "$(decider_on_start)" "A20 with no SLEEP_END set, occupancy uses the ratified 06:45"
else
    run occupancy
    eq "06:45" "$(decider_on_start)" "A20 with no SLEEP_END set, occupancy uses the ratified 06:45"
    eq "06:45" "$(armed_hhmm)" "A20 ...and arms the RTC for it"
fi

printf '\n%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
