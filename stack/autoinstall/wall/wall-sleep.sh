#!/usr/bin/env bash
# The wall panel's sleep window (D-W4). ONE script, two verbs, so that switching
# SLEEP_MODE is a one-line config change rather than a rewrite:
#
#   wall-sleep.sh start   — enter the window (at SLEEP_START, via wall-sleep.timer)
#   wall-sleep.sh end     — leave  the window (at SLEEP_END,   via wall-wake.timer)
#   wall-sleep.sh occupancy — evaluate presence NOW (every minute, via
#                           wall-occupancy.timer). A no-op unless
#                           WALL_ABSENCE_ENABLED=true.
#
# OCCUPANCY (SN-015/SR-020), and the one thing to understand before editing:
# when WALL_ABSENCE_ENABLED=true the SUSPEND TRIGGER MOVES from the clock to the
# absence timer, and BOTH the backlight and the suspend/RTC action come from a
# SINGLE call to wall-occupancy.py `decide`. There is no second presence test
# anywhere in this file. That is the acceptance criterion, not a style choice:
# two conditions that agree today drift tomorrow, and the failure is either a
# dark screen on an awake machine or a machine that sleeps with somebody in
# front of it. `start` therefore HANDS OVER to the occupancy evaluation instead
# of suspending on the hour, and `SLEEP_END` is one value doing three jobs —
# the RTC alarm, the wake timer, and the start of the on-period.
#
# With WALL_ABSENCE_ENABLED=false (the default) NONE of that runs and the
# behaviour below is exactly what it was: the 22:00 schedule, unchanged.
#
# SLEEP_MODE=suspend (default, "L2"):
#   start -> arm the RTC alarm for SLEEP_END, THEN suspend. Arming FIRST is the
#            whole design: the morning wake must not depend on anyone nudging the
#            mouse, and a failed USB wake must not leave the panel dark all day.
#            Cost: no LAN presence at all while asleep — no SSH, no remote fix,
#            for the whole window. The RTC alarm is what bounds that worst case.
#   end   -> the machine is already awake (the RTC did it, or a mouse did it
#            earlier); just restore the backlight in case an earlier `start` in
#            backlight mode left it off, and log.
#
# SLEEP_MODE=backlight ("L1", the graceful degradation if S3 resume misbehaves on
# this 2016 firmware — the classic failure mode):
#   start -> backlight off. The machine stays fully up, LAN-present and SSH-able,
#            and (unlike L2) can still play music through the window.
#   end   -> backlight on.
#
# Never fatal by design: a panel that cannot suspend must stay usable, so every
# failure is a loud journal line and exit 0. `journalctl -u wall-sleep` /
# `-u wall-wake` is the record.
set -u

ENV_FILE="/etc/wall-panel/wall.env"
log() { echo "[wall-sleep] $*"; }

# shellcheck disable=SC1090
# load_env_file FILE — export every KEY=VALUE in FILE **literally**.
#
# NEVER `source` a compose .env. Its values are literal text, and every
# basic_auth hash in this project is bcrypt — `$2a$14$…`. Sourcing makes bash
# expand them: under `set -u` it aborts on the unbound `$2` (which is exactly
# how first boot died), and WITHOUT `set -u` it is worse — `$2`/`$1` expand to
# nothing, the hash is silently corrupted, and auth then fails with nothing
# anywhere explaining why.
load_env_file() {
    local __f="$1" __line __k __v
    [ -f "$__f" ] || return 0
    while IFS= read -r __line || [ -n "$__line" ]; do
        case "$__line" in ''|'#'*) continue ;; esac
        case "$__line" in *=*) ;; *) continue ;; esac
        __k=${__line%%=*}
        __v=${__line#*=}
        __k=${__k#"${__k%%[![:space:]]*}"}
        __k=${__k%"${__k##*[![:space:]]}"}
        case "$__k" in ''|*[!A-Za-z0-9_]*) continue ;; esac
        case "$__v" in
            \"*\") __v=${__v#\"}; __v=${__v%\"} ;;
            \'*\') __v=${__v#\'}; __v=${__v%\'} ;;
            # UNQUOTED: strip a trailing ` # comment`, exactly as shell
            # sourcing and docker compose's own .env parser both do. Without
            # this, LAN_IP=0.0.0.0 followed by an explanatory comment reached
            # Technitium's API as part of the address and curl rejected the
            # URL. A `#` with no space before it is kept — it may be part of a
            # password.
            *) __v=${__v%%[[:space:]]#*}
               __v=${__v%"${__v##*[![:space:]]}"} ;;
        esac
        # Compose stores a literal '$' as '$$' (see compose_escape) because it
        # interpolates .env values. Collapse it back, so a script reading this
        # file sees exactly what the containers receive.
        __v=${__v//\$\$/\$}
        printf -v "$__k" '%s' "$__v" 2>/dev/null && export "$__k"
    done < "$__f"
}
load_env_file "$ENV_FILE"
: "${SLEEP_MODE:=suspend}"
: "${SLEEP_START:=22:00}"
: "${SLEEP_RTC_WAKE:=true}"
# ── occupancy knobs (SN-015/SR-020) ──────────────────────────────────────────
# Default false: an image with no presence writer must behave exactly as it did
# before this feature existed.
: "${WALL_ABSENCE_ENABLED:=false}"
# SLEEP_END — ONE knob doing all three jobs (the RTC alarm target, the morning
# wake timer, and the start of the on-period), with ONE default, 06:45, on both
# power paths.
#
# History, so nobody re-derives a rule that no longer exists: this briefly
# defaulted to 06:45 with absence detection on and 06:30 with it off, so that
# SN-015's "the disabled path is completely unchanged" line could hold to the
# minute. The Owner ruled on 2026-09-09 that two shipped defaults for one knob
# is a thing nobody will remember in a year, and knowingly relaxed that
# acceptance line on this one value: the schedule-only morning wake moves
# 06:30 -> 06:45 too. See docs/status.md.
#
# Keep this line identical to wall-firstboot.sh's — a panel whose wall.env
# predates the knob must get the same answer from both scripts, and A12 asserts
# that the two agree.
: "${SLEEP_END:=06:45}"
: "${WALL_ABSENCE_TIMEOUT_MIN:=60}"
: "${WALL_PRESENCE_FILE:=/run/wall-presence/state.json}"

# Where the current absence started (epoch seconds). Deliberately on /run — a
# tmpfs — so a reboot forgets it. A mains blip during the window (SN-013,
# quirk 4b: no battery, no UPS) therefore lands on a panel that must observe a
# FULL absence timeout again before it may suspend, instead of resuming a
# pre-blip countdown and suspending itself out of reach seconds after boot.
# ── the one test seam in this file, and what it is NOT ───────────────────────
# PANEL_POWER_TEST_ROOT re-roots the two directories this script WRITES to (the
# sysfs backlight tree and the absence clock) so the hermetic suite can assert
# the real ARTIFACTS — a brightness file that actually contains 0, an absence
# clock that actually persists — instead of an exit code. It is EMPTY in
# production and systemd passes no environment, so the units cannot set it.
#
# Deliberately narrow, unlike the MEDIA_*_SOURCE_OVERRIDE hooks this repo
# retired: it re-points where a brightness INTEGER is written and where a
# timestamp is kept. It reads nothing, copies nothing, and cannot re-point a
# mount, a credential or a suspend. backlight_dir() additionally requires the
# directory to look like a real backlight (a writable `brightness` NEXT TO a
# `max_brightness`), so a wrong value finds nothing and warns rather than
# scribbling a 0 into an unrelated file.
POWER_TEST_ROOT="${PANEL_POWER_TEST_ROOT:-}"
BACKLIGHT_ROOT="${POWER_TEST_ROOT}/sys/class/backlight"
# Read-only, and both are how the alarm is ESTABLISHED rather than assumed: the
# RTC's own wakealarm node (so an armed alarm is read back rather than inferred
# from an exit code) and hwclock's record of which frame that RTC keeps.
RTC_ROOT="${POWER_TEST_ROOT}/sys/class/rtc"
ADJTIME_FILE="${POWER_TEST_ROOT}/etc/adjtime"
ABSENCE_STATE_DIR="${POWER_TEST_ROOT}/run/wall-occupancy"
BACKLIGHT_PREV="${POWER_TEST_ROOT}/run/wall-backlight.prev"
ABSENT_SINCE_FILE="$ABSENCE_STATE_DIR/absent-since"
# A bare carriage return, built rather than escaped so it survives every editor
# and every quoting layer between here and the panel.
CR="$(printf '\r')"
DECIDER="/usr/local/sbin/wall-occupancy.py"
[ -x "$DECIDER" ] || [ -f "$DECIDER" ] || DECIDER="$(dirname "$0")/wall-occupancy.py"

# The display lifecycle is also the Door source lifecycle (WSN-019). Stopping
# the broker destroys its active client and the FFmpeg child before the panel
# goes dark or suspends. Starting the broker only recreates the idle Unix socket;
# it never opens RTSP — the unlocked Door tab still requires an explicit start.
stop_door_stream() {
    systemctl cat wall-door-stream.service >/dev/null 2>&1 || return 0
    systemctl stop wall-door-stream.service >/dev/null 2>&1
}
start_door_broker() {
    systemctl cat wall-door-stream.service >/dev/null 2>&1 || return 0
    # Firstboot owns the volatile Door-only credential set. If current config
    # was removed or rejected, display-on must not resurrect an older source.
    [ -r /run/wall-door-credentials/host ] || return 0
    [ -r /run/wall-door-credentials/password ] || return 0
    systemctl start wall-door-stream.service >/dev/null 2>&1
}

# ── backlight helpers ────────────────────────────────────────────────────────
# The interface name is hardware-specific (intel_backlight / acpi_video0 / …), so
# it is DISCOVERED rather than assumed — the hardware baseline records which one
# this panel has, but a firmware update can change it and a hard-coded path would
# fail silently at 22:00.
backlight_dir() {
    local d
    for d in "$BACKLIGHT_ROOT"/*; do
        # BOTH files, not just a writable brightness: that pairing is what makes
        # "this is a backlight" a check rather than an assumption (see the test
        # seam note above).
        [ -w "$d/brightness" ] && [ -f "$d/max_brightness" ] && { printf '%s' "$d"; return 0; }
    done
    return 1
}
# backlight_set off|on — write the level and READ IT BACK.
#
# The readback is the point. A missing interface, a read-only sysfs node, a
# firmware that accepts the write and ignores it: every one of those leaves the
# screen lit while the exit code says otherwise, and the caller that matters
# (run_occupancy, about to suspend) would then have suspended a panel that never
# satisfied "backlight off when nobody is present". So this returns non-zero
# unless `brightness` actually holds the value we asked for.
backlight_set() {   # backlight_set off|on
    local d cur max want got
    if ! d="$(backlight_dir)"; then
        log "WARNING: no writable $BACKLIGHT_ROOT/* — cannot change the backlight."
        log "WARNING: with SLEEP_MODE=backlight the panel will stay lit all night."
        return 1
    fi
    max="$(cat "$d/max_brightness" 2>/dev/null || echo 100)"
    if [ "$1" = "off" ]; then
        if ! stop_door_stream; then
            log "WARNING: Door stream broker could not be stopped — refusing to turn the display off."
            return 1
        fi
        # Remember the level so `on` restores what the user actually had.
        cur="$(cat "$d/brightness" 2>/dev/null || echo "$max")"
        printf '%s' "$cur" > "$BACKLIGHT_PREV" 2>/dev/null || true
        want=0
    else
        cur="$(cat "$BACKLIGHT_PREV" 2>/dev/null || echo "$max")"
        [ -n "$cur" ] || cur="$max"
        want="$cur"
    fi
    if ! echo "$want" > "$d/brightness" 2>/dev/null; then
        log "WARNING: could not write $d/brightness (read-only? gone?) — the backlight is UNCHANGED."
        return 1
    fi
    got="$(cat "$d/brightness" 2>/dev/null | tr -d '[:space:]')"
    if [ "$got" != "$want" ]; then
        log "WARNING: $d/brightness reads '$got' after asking for '$want' — the backlight did NOT change."
        return 1
    fi
    log "backlight $1 ($d, level $want)"
    if [ "$1" = "on" ] && ! start_door_broker; then
        log "WARNING: the display is on but the idle Door broker could not be readied."
    fi
    return 0
}

# ── which frame does THIS box's RTC keep time in? ────────────────────────────
# rtcwake converts the absolute epoch it is handed into the RTC's own
# broken-down fields, so it must be told whether that hardware clock reads UTC
# or local wall-clock time. Get it wrong and the alarm is programmed a whole UTC
# offset away: in America/Chicago a 06:45 alarm becomes 00:45 or 12:45, and the
# panel suspends and does NOT wake when it should — the worst outcome this
# script has, because there is no battery, no UPS and no LAN presence while
# asleep. So it is ESTABLISHED, in the order of authority, never asserted:
#
#   1. `timedatectl show -p LocalRTC` — systemd's own answer on a systemd box,
#      and exactly what `timedatectl set-local-rtc` would have changed;
#   2. /etc/adjtime's third line (LOCAL|UTC) — hwclock's record, which is what
#      systemd derives that answer from and what util-linux itself consults;
#   3. UTC. This image never runs `timedatectl set-local-rtc 1` and ships no
#      /etc/adjtime, so a stock Ubuntu autoinstall leaves the RTC in UTC — the
#      standard Linux configuration, and the one this panel is actually in.
#
# Returns 0 for LOCAL, 1 for UTC.
rtc_is_local() {
    local value
    if command -v timedatectl >/dev/null 2>&1; then
        value="$(timedatectl show -p LocalRTC --value 2>/dev/null || true)"
        case "$value" in
            yes|true|1) return 0 ;;
            no|false|0) return 1 ;;
        esac
    fi
    if [ -f "$ADJTIME_FILE" ]; then
        value="$(sed -n '3p' "$ADJTIME_FILE" 2>/dev/null | tr -d '[:space:]')"
        [ "$value" = "LOCAL" ] && return 0
        [ "$value" = "UTC" ] && return 1
    fi
    return 1
}

# utc_offset_at EPOCH — the local UTC offset, in seconds, AT that instant.
# "At that instant" matters: the offset on the far side of a DST boundary is not
# the offset now.
utc_offset_at() {
    local z sign hh mm
    z="$(date -d "@$1" +%z 2>/dev/null || echo "")"
    case "$z" in [+-][0-9][0-9][0-9][0-9]) ;; *) return 1 ;; esac
    sign="${z:0:1}"; hh="${z:1:2}"; mm="${z:3:2}"
    printf '%s' "$(( ${sign}1 * (10#$hh * 3600 + 10#$mm * 60) ))"
}

# next_wake_epoch HH:MM — the epoch of the NEXT LOCAL OCCURRENCE of that
# wall-clock time.
#
# Not "now + 86400". A fixed day of seconds is only a day when the offset does
# not move: on the night the clocks change it lands an hour early or an hour
# late (05:45 or 07:45 for a 06:45 wake), which for a panel that must be awake
# before the office is is a real miss. Stepping the CALENDAR DAY and re-reading
# the wall-clock time is what makes the alarm a wall-clock promise instead of a
# duration.
next_wake_epoch() {
    local wake="$1" today target now
    now="$(date +%s)"
    today="$(date +%F)"
    target="$(date -d "$today $wake" +%s 2>/dev/null || echo "")"
    [ -n "$target" ] || return 1
    case "$target" in ''|*[!0-9]*) return 1 ;; esac
    if [ "$target" -le "$now" ]; then
        target="$(date -d "$today $wake tomorrow" +%s 2>/dev/null || echo "")"
        [ -n "$target" ] || return 1
        case "$target" in ''|*[!0-9]*) return 1 ;; esac
    fi
    printf '%s' "$target"
}

# rtc_alarm_ok TARGET — READ THE ALARM BACK, because a zero exit is not evidence.
#
# rtcwake returning 0 says a call succeeded, not that an alarm exists: firmware,
# a wrapper, a container shim or a driver that quietly ignores the ioctl can all
# report success and program nothing at all. The panel would then suspend with
# no way back, which is the single failure this whole script is arranged to make
# impossible. So the alarm is verified against the kernel's own view of it.
#
# The kernel exposes wakealarm as seconds-since-epoch computed AS IF the RTC
# were UTC. When the RTC is in LOCAL time the readback is therefore shifted by
# exactly the local UTC offset — the same shift rtcwake applied when writing it
# — so the EXPECTATION is shifted with it rather than the check being loosened.
rtc_alarm_ok() {
    local target="$1" node="" candidate raw expected off drift
    for candidate in "$RTC_ROOT/rtc0/wakealarm" "$RTC_ROOT"/rtc*/wakealarm; do
        [ -r "$candidate" ] && { node="$candidate"; break; }
    done
    if [ -z "$node" ]; then
        log "WARNING: no readable wakealarm under $RTC_ROOT — the armed alarm cannot be VERIFIED."
        return 1
    fi
    raw="$(cat "$node" 2>/dev/null | tr -d '[:space:]')"
    if [ -z "$raw" ] || [ "$raw" = "0" ]; then
        log "WARNING: rtcwake reported success but $node is empty — NO alarm is programmed."
        return 1
    fi
    case "$raw" in *[!0-9]*) log "WARNING: $node reads '$raw', which is not an epoch"; return 1 ;; esac
    expected="$target"
    if rtc_is_local; then
        off="$(utc_offset_at "$target")" || off=0
        expected=$((target + off))
    fi
    drift=$((raw - expected)); [ "$drift" -lt 0 ] && drift=$(( - drift ))
    if [ "$drift" -gt 90 ]; then
        log "WARNING: the RTC alarm reads back as $raw, ${drift}s away from the $expected asked for."
        return 1
    fi
    return 0
}

# ── arm the RTC alarm for the next occurrence of SLEEP_END ───────────────────
# `rtcwake -m no` sets the alarm WITHOUT suspending, which is what lets us arm
# first and suspend as a separate, VERIFIED step.
# arm_rtc [HH:MM] — arm for the given wall-clock time, defaulting to SLEEP_END.
# The occupancy path passes the RTC_WAKE the decision reported, so the alarm is
# armed from the SAME value that defined the on-period rather than from a second
# read of the knob.
#
# Returns 0 only when an alarm has been armed AND read back. Every other outcome
# is a 1, and the caller's answer to a 1 is to stay awake.
arm_rtc() {
    local wake="${1:-$SLEEP_END}" target frame flag
    if ! target="$(next_wake_epoch "$wake")"; then
        log "WARNING: wake time '$wake' is not a time date(1) understands — NOT arming the RTC"
        return 1
    fi
    if ! command -v rtcwake >/dev/null 2>&1; then
        log "WARNING: rtcwake not installed (util-linux) — cannot arm the primary wake"
        return 1
    fi
    if rtc_is_local; then frame="LOCAL"; flag="-l"; else frame="UTC"; flag="-u"; fi
    if ! rtcwake -m no "$flag" -t "$target" >/dev/null 2>&1; then
        log "WARNING: rtcwake failed to arm the alarm"
        return 1
    fi
    if ! rtc_alarm_ok "$target"; then
        log "WARNING: rtcwake exited 0 but no alarm could be verified — treating the"
        log "WARNING: morning wake as UNAVAILABLE rather than assuming it exists."
        return 1
    fi
    # ASSERTED in local terms, because local wall-clock is what the requirement
    # is written in and what the Owner reads off the journal. The one case where
    # these can honestly differ is a wake time inside the hour a spring-forward
    # skips, which has no local instant at all — so it is said out loud rather
    # than refused, once a year, with the alarm at the nearest real instant.
    if [ "$(date -d "@$target" +%H:%M)" != "$wake" ]; then
        log "WARNING: the armed alarm reads back as $(date -d "@$target" +%H:%M) local, not the"
        log "WARNING: $wake requested — a DST-skipped hour, or a wake time date(1) read loosely."
    fi
    # Stated in LOCAL terms, because local wall-clock is what the requirement is
    # written in and what the Owner reads off the journal.
    log "RTC alarm armed for $(date -d "@$target" '+%F %H:%M %Z') — local wall-clock $(date -d "@$target" +%H:%M), requested $wake, RTC frame $frame (primary wake)"
    return 0
}

# ── suspend, with SN-013's fail-safe in ONE place ────────────────────────────
# suspend_now [HH:MM] — arm the RTC for the given wake time, THEN suspend.
#
# Both the scheduled path (`start`) and the occupancy path call this, so the
# SN-013 edge case — "if the RTC alarm cannot even be ARMED, the panel refuses
# to suspend at all and falls back to backlight-off for that window" — is
# implemented once and cannot be true of one path and false of the other. A
# reachable panel beats a dark one, and an UNREACHABLE dark one is the outcome
# this guard exists to make impossible.
suspend_now() {
    local wake="${1:-$SLEEP_END}"
    if [ "$SLEEP_RTC_WAKE" = "true" ]; then
        if ! arm_rtc "$wake"; then
            # No guaranteed way back. Suspending anyway would risk a panel
            # that is dark until someone walks over and nudges a mouse —
            # so degrade to the L1 behaviour instead and say why.
            log "WARNING: RTC alarm unavailable — NOT suspending. Falling back to"
            log "WARNING: backlight-off for this window so the panel stays reachable."
            backlight_set off
            return 0
        fi
    else
        log "SLEEP_RTC_WAKE=false — suspending with the USB mouse as the ONLY wake."
        log "The internal touchscreen will NOT wake it from S3. This is a test mode."
    fi
    if ! stop_door_stream; then
        log "WARNING: Door stream broker could not be stopped — NOT suspending while a camera source may still be active."
        return 0
    fi
    log "suspending now"
    systemctl suspend || log "WARNING: systemctl suspend failed — the panel stays awake"
}

# write_absent_since EPOCH — record the absence start ATOMICALLY.
#
# A half-written clock is not a harmless glitch: a `1` left behind by a write
# interrupted at the wrong instant reads as 1970, i.e. five decades of absence,
# and the very next tick outside the on-period suspends the panel. So the value
# lands in a temp file in the same directory and is renamed over the target,
# which is atomic on the same filesystem — a reader sees either the old clock or
# the new one, never a prefix of one. (The decider range-checks it as well; this
# is the write half of the same guard.)
write_absent_since() {
    local tmp="$ABSENT_SINCE_FILE.$$"
    if ! printf '%s\n' "$1" > "$tmp" 2>/dev/null; then
        rm -f "$tmp" 2>/dev/null || true
        log "WARNING: cannot write the absence clock — the absence timer will keep restarting."
        return 1
    fi
    if ! mv -f "$tmp" "$ABSENT_SINCE_FILE" 2>/dev/null; then
        rm -f "$tmp" 2>/dev/null || true
        log "WARNING: cannot replace the absence clock — the absence timer will keep restarting."
        return 1
    fi
    return 0
}

# ── the occupancy evaluation (SN-015/SR-020) ─────────────────────────────────
# ONE call to the decider, ONE record applied. Read this and note what is NOT
# here: there is no `if present` around the backlight write and no second
# `if absent` around the suspend. Both come out of $BACKLIGHT and $POWER, which
# came out of the same invocation, so they cannot disagree.
run_occupancy() {
    local now minute out line k v backlight_failed=
    local PRESENCE= BACKLIGHT= POWER= ON_PERIOD= RTC_WAKE= REASON= PRESENCE_REASON=
    local ABSENCE_CLOCK= CLOCK_REASON=

    if [ ! -f "$DECIDER" ]; then
        log "WARNING: $DECIDER is missing — occupancy cannot decide anything."
        log "WARNING: doing NOTHING (the panel stays awake and lit). This is the"
        log "WARNING: fail-safe direction: never suspend on a broken install."
        return 0
    fi

    now="$(date +%s)"
    minute="$(( 10#$(date +%H) * 60 + 10#$(date +%M) ))"
    mkdir -p "$ABSENCE_STATE_DIR" 2>/dev/null || true

    if ! out="$(python3 "$DECIDER"             --now-epoch "$now" --minute-of-day "$minute"             --presence-file "$WALL_PRESENCE_FILE"             --absent-since "$(cat "$ABSENT_SINCE_FILE" 2>/dev/null || echo "")"             --absence-enabled "$WALL_ABSENCE_ENABLED"             --absence-timeout-min "$WALL_ABSENCE_TIMEOUT_MIN"             --on-start "$SLEEP_END" --on-end "$SLEEP_START" 2>&1)"; then
        log "WARNING: the occupancy decider refused: $out"
        log "WARNING: doing NOTHING. A panel is never suspended on an undecided state."
        return 0
    fi
    while IFS= read -r line; do
        # Strip a trailing CR. python3 on a host whose stdout is CRLF-translated
        # (the dev box the hermetic suite runs on) would otherwise deliver
        # BACKLIGHT=$'off\r', which matches no case arm and silently does
        # NOTHING — a panel that decided to dim and then didn't, with a perfectly
        # green journal line saying "backlight=off". Cheap, and it removes a
        # whole class of invisible no-op.
        line=${line%$CR}
        k=${line%%=*}; v=${line#*=}
        case "$k" in
            PRESENCE|BACKLIGHT|POWER|ON_PERIOD|RTC_WAKE|REASON|PRESENCE_REASON|ABSENCE_CLOCK|CLOCK_REASON)
                printf -v "$k" '%s' "$v" ;;
        esac
    done <<EOF
$out
EOF

    # The absence clock is bookkeeping, and — like the backlight and the power
    # action — it is bookkeeping the DECIDER dictates. What the shell must not do
    # is run the clock on its own rule: "absent, so start counting" would keep
    # counting straight through the on-period, and an absence that began at
    # midday would then arrive at 22:00 already carrying ten hours and suspend on
    # the first tick outside — the hour OUTSIDE the on-period, which is the whole
    # rule, never observed. ABSENCE_CLOCK says start, clear, or leave it alone.
    #
    # An unrecognised or empty value (an older decider) is deliberately the
    # do-nothing arm: a clock that never starts is a panel that never suspends.
    case "$ABSENCE_CLOCK" in
        start)   [ -f "$ABSENT_SINCE_FILE" ] || write_absent_since "$now" ;;
        # "restart" and not "start": the file may EXIST and still be unusable —
        # a truncated "1", a stamp from the future — and leaving it in place
        # would mean the decider rejects it again on every tick while the panel
        # never accumulates any absence at all. It is replaced with now.
        restart) write_absent_since "$now" ;;
        clear)   rm -f "$ABSENT_SINCE_FILE" 2>/dev/null || true ;;
        *)       : ;;
    esac

    log "occupancy: presence=$PRESENCE on_period=$ON_PERIOD backlight=$BACKLIGHT power=$POWER clock=$ABSENCE_CLOCK"
    [ -n "$CLOCK_REASON" ] && log "occupancy: $CLOCK_REASON"
    log "occupancy: $PRESENCE_REASON"
    log "occupancy: $REASON"

    case "$BACKLIGHT" in
        on)  backlight_set on || log "WARNING: the screen could not be lit — see above." ;;
        off) backlight_set off || backlight_failed=1 ;;
        *)   : ;;   # "unchanged" — absence detection is off; touch nothing.
    esac
    if [ "$POWER" = "suspend" ]; then
        # THE BACKLIGHT IS A PRECONDITION OF THIS SUSPEND, not a cosmetic step
        # beside it. The decision being applied is "nobody is here: go dark, then
        # sleep", and SR-020 states the dark half as a requirement. If the node is
        # missing or read-only the dark half did not happen, and suspending anyway
        # would take the panel off the LAN having satisfied neither half — the one
        # combination that is both wrong and unreachable. Staying awake with a lit
        # screen is a visible nuisance somebody can SSH into and fix, so that is
        # the direction this fails in.
        #
        # Note the asymmetry, and it is deliberate: the SCHEDULED path below is
        # untouched by this: it never claimed to dim anything, and its behaviour
        # with absence detection off must stay exactly what it was.
        if [ -n "$backlight_failed" ]; then
            log "WARNING: the decision was backlight-off THEN suspend, but the backlight"
            log "WARNING: could not be turned off. NOT suspending: a panel that sleeps"
            log "WARNING: without having gone dark is unreachable AND still lit."
            return 0
        fi
        if [ "$SLEEP_MODE" = "suspend" ]; then
            suspend_now "$RTC_WAKE"
        else
            # SLEEP_MODE=backlight is SN-013's graceful degradation for a
            # firmware whose S3 resume misbehaves. The occupancy decision is the
            # same; only the mechanism it is allowed to use is narrower, and the
            # backlight is already off from the write above.
            log "SLEEP_MODE=backlight — the absence timeout expired, staying awake with the"
            log "screen dark rather than suspending."
        fi
    fi
}

case "${1:-}" in
    start)
        # HANDOVER. With absence detection on, 22:00 is no longer a suspend: the
        # absence timer owns the suspend and this boundary is just another
        # occupancy evaluation, so nobody standing at the panel at 22:00 gets
        # slept on. With it off, everything below is byte-for-byte the old path.
        if [ "$WALL_ABSENCE_ENABLED" = "true" ]; then
            log "SLEEP_START reached with absence detection ENABLED — the absence timer"
            log "owns the suspend from here (WALL_ABSENCE_TIMEOUT_MIN=$WALL_ABSENCE_TIMEOUT_MIN)."
            run_occupancy
            exit 0
        fi
        log "entering the sleep window (SLEEP_MODE=$SLEEP_MODE, until $SLEEP_END)"
        if [ "$SLEEP_MODE" = "suspend" ]; then
            suspend_now "$SLEEP_END"
        else
            backlight_set off
        fi
        ;;
    occupancy)
        run_occupancy
        ;;
    end)
        log "leaving the sleep window (SLEEP_MODE=$SLEEP_MODE)"
        # With absence detection on, 06:45 opens the ON-PERIOD rather than
        # commanding a lit screen: whether the backlight comes on is the
        # occupancy decision's to make, exactly as it is at every other minute.
        # Deferring here is what stops this boundary from being a SECOND writer
        # of the backlight that disagrees with the decider a minute later.
        if [ "$WALL_ABSENCE_ENABLED" = "true" ]; then
            run_occupancy
            exit 0
        fi
        # Restoring the backlight is safe and idempotent in BOTH modes: in suspend
        # mode it is a no-op unless a failed arm_rtc degraded us to L1 above.
        backlight_set on
        ;;
    *)
        echo "usage: $0 start|end|occupancy" >&2
        exit 2
        ;;
esac
exit 0
