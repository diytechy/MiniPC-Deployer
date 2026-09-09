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
#   A11 ONE KNOB SET: nothing else in the wall tree writes the backlight or
#       suspends, so the two behaviours cannot drift apart
#   A12 SLEEP_END is the single source of the RTC wake AND the on-period start —
#       there is no second on-period knob to disagree with it
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

# rtcwake honours RTCWAKE_FAIL so A7 can reproduce SN-013's "the alarm cannot
# even be ARMED" case without needing hardware that refuses.
cat > "$BIN/rtcwake" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "\$RTCWAKE_LOG"
[ -n "\${RTCWAKE_FAIL:-}" ] && exit 1
exit 0
EOF

cat > "$BIN/python3" <<EOF
#!/usr/bin/env bash
exec "$PY" "\$@"
EOF
chmod +x "$BIN/systemctl" "$BIN/rtcwake" "$BIN/python3"
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
    export SYSTEMCTL_LOG RTCWAKE_LOG
    unset RTCWAKE_FAIL
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

# ── A2: detection OFF — the 22:00 schedule is unchanged, and wakes at 06:45 ──
scenario a2
write_env "SLEEP_MODE=suspend"
run start
eq "yes" "$(suspended)" "A2 detection off: SLEEP_START still suspends (schedule unchanged)"
eq "06:45" "$(armed_hhmm)" "A2 the armed RTC alarm is 06:45 — the ratified SLEEP_END"
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

# ── A11: ONE KNOB SET — there is no second writer to drift from ─────────────
# The defect this block exists to prevent is two parallel sets of logic. The
# check is structural because that is the only way to catch the SECOND one being
# added later: exactly one file in the wall tree may write the backlight, and
# exactly one may suspend.
BL_WRITERS="$(grep -rl 'class/backlight' "$DIR" --include='*.sh' --include='*.py' \
    | grep -v '/tests/' | sed 's|.*/||' | sort -u | tr '\n' ' ')"
eq "wall-sleep.sh " "$BL_WRITERS" "A11 exactly ONE file in the wall tree writes the backlight"
SUSPENDERS="$(grep -rl 'systemctl suspend' "$DIR" --include='*.sh' --include='*.py' \
    | grep -v '/tests/' | sed 's|.*/||' | sort -u | tr '\n' ' ')"
eq "wall-sleep.sh " "$SUSPENDERS" "A11 exactly ONE file in the wall tree suspends"
# And within that file, both halves come from ONE decider call.
DECIDE_CALLS="$(grep -c 'python3 "$DECIDER"' "$SLEEP_SH")"
eq "1" "$DECIDE_CALLS" "A11 wall-sleep.sh invokes the decider exactly once"

# ── A12: SLEEP_END is the single source of both jobs ────────────────────────
eq "SLEEP_END=06:45" "$(grep -E '^SLEEP_END=' "$ENV_EXAMPLE")" \
    "A12 wall.env.example ships the ratified SLEEP_END=06:45"
ONPERIOD_KNOBS="$(grep -cE '^#?\s*(ON_PERIOD|WALL_ON_PERIOD|SLEEP_RTC_WAKE_TIME)' "$ENV_EXAMPLE" || true)"
eq "0" "$ONPERIOD_KNOBS" "A12 there is NO second on-period / RTC-time knob to disagree with it"
# The decider echoes the RTC wake back from the on-period start, so the shell
# cannot arm the alarm from a different value than the one it gated on.
grep -q 'RTC_WAKE={}".format(args.on_start)' "$DECIDER" \
    && pass "A12 the decider derives RTC_WAKE from the on-period start itself" \
    || fail "A12 the decider no longer derives RTC_WAKE from the on-period start"

printf '\n%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
