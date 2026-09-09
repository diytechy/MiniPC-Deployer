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
# 06:45 — ratified 2026-09-08 (SN-015), replacing 06:30. This ONE value is the
# RTC alarm target AND the start of the on-period; do not introduce a second
# knob for either job.
: "${SLEEP_END:=06:45}"
: "${SLEEP_RTC_WAKE:=true}"
# ── occupancy knobs (SN-015/SR-020) ──────────────────────────────────────────
# Default false: an image with no presence writer must behave exactly as it did
# before this feature existed.
: "${WALL_ABSENCE_ENABLED:=false}"
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
ABSENCE_STATE_DIR="${POWER_TEST_ROOT}/run/wall-occupancy"
BACKLIGHT_PREV="${POWER_TEST_ROOT}/run/wall-backlight.prev"
ABSENT_SINCE_FILE="$ABSENCE_STATE_DIR/absent-since"
# A bare carriage return, built rather than escaped so it survives every editor
# and every quoting layer between here and the panel.
CR="$(printf '\r')"
DECIDER="/usr/local/sbin/wall-occupancy.py"
[ -x "$DECIDER" ] || [ -f "$DECIDER" ] || DECIDER="$(dirname "$0")/wall-occupancy.py"

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
backlight_set() {   # backlight_set off|on
    local d cur max
    if ! d="$(backlight_dir)"; then
        log "WARNING: no writable $BACKLIGHT_ROOT/* — cannot change the backlight."
        log "WARNING: with SLEEP_MODE=backlight the panel will stay lit all night."
        return 1
    fi
    max="$(cat "$d/max_brightness" 2>/dev/null || echo 100)"
    if [ "$1" = "off" ]; then
        # Remember the level so `on` restores what the user actually had.
        cur="$(cat "$d/brightness" 2>/dev/null || echo "$max")"
        printf '%s' "$cur" > "$BACKLIGHT_PREV" 2>/dev/null || true
        echo 0 > "$d/brightness" 2>/dev/null && log "backlight off ($d)"
    else
        cur="$(cat "$BACKLIGHT_PREV" 2>/dev/null || echo "$max")"
        [ -n "$cur" ] || cur="$max"
        echo "$cur" > "$d/brightness" 2>/dev/null && log "backlight on ($d, level $cur)"
    fi
}

# ── arm the RTC alarm for the next occurrence of SLEEP_END ───────────────────
# `rtcwake -m no` sets the alarm WITHOUT suspending, which is what lets us arm
# first and suspend as a separate, verifiable step. `-l` says the RTC is in local
# time; getting that wrong is a wake at the wrong hour, not a failure, so it is
# worth stating explicitly rather than relying on the default.
# arm_rtc [HH:MM] — arm for the given wall-clock time, defaulting to SLEEP_END.
# The occupancy path passes the RTC_WAKE the decision reported, so the alarm is
# armed from the SAME value that defined the on-period rather than from a second
# read of the knob.
arm_rtc() {
    local wake="${1:-$SLEEP_END}" target now
    now="$(date +%s)"
    target="$(date -d "today $wake" +%s 2>/dev/null || echo "")"
    if [ -z "$target" ]; then
        log "WARNING: wake time '$wake' is not a time date(1) understands — NOT arming the RTC"
        return 1
    fi
    # The wake time is normally the next morning, i.e. already past for "today".
    [ "$target" -le "$now" ] && target=$((target + 86400))
    if command -v rtcwake >/dev/null 2>&1; then
        if rtcwake -m no -l -t "$target" >/dev/null 2>&1; then
            log "RTC alarm armed for $(date -d "@$target" '+%F %T') (primary wake)"
            return 0
        fi
        log "WARNING: rtcwake failed to arm the alarm"
    else
        log "WARNING: rtcwake not installed (util-linux) — cannot arm the primary wake"
    fi
    return 1
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
    log "suspending now"
    systemctl suspend || log "WARNING: systemctl suspend failed — the panel stays awake"
}

# ── the occupancy evaluation (SN-015/SR-020) ─────────────────────────────────
# ONE call to the decider, ONE record applied. Read this and note what is NOT
# here: there is no `if present` around the backlight write and no second
# `if absent` around the suspend. Both come out of $BACKLIGHT and $POWER, which
# came out of the same invocation, so they cannot disagree.
run_occupancy() {
    local now minute out line k v
    local PRESENCE= BACKLIGHT= POWER= ON_PERIOD= RTC_WAKE= REASON= PRESENCE_REASON=

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
            PRESENCE|BACKLIGHT|POWER|ON_PERIOD|RTC_WAKE|REASON|PRESENCE_REASON)
                printf -v "$k" '%s' "$v" ;;
        esac
    done <<EOF
$out
EOF

    # The absence clock is bookkeeping, not a second decision: it only records
    # WHEN the current absence began so the next evaluation can measure it.
    if [ "$PRESENCE" = "absent" ]; then
        [ -f "$ABSENT_SINCE_FILE" ] || printf '%s' "$now" > "$ABSENT_SINCE_FILE" 2>/dev/null || true
    else
        rm -f "$ABSENT_SINCE_FILE" 2>/dev/null || true
    fi

    log "occupancy: presence=$PRESENCE on_period=$ON_PERIOD backlight=$BACKLIGHT power=$POWER"
    log "occupancy: $PRESENCE_REASON"
    log "occupancy: $REASON"

    case "$BACKLIGHT" in
        on)  backlight_set on ;;
        off) backlight_set off ;;
        *)   : ;;   # "unchanged" — absence detection is off; touch nothing.
    esac
    if [ "$POWER" = "suspend" ]; then
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
