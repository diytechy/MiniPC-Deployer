#!/usr/bin/env bash
# The wall panel's sleep window (D-W4). ONE script, two verbs, so that switching
# SLEEP_MODE is a one-line config change rather than a rewrite:
#
#   wall-sleep.sh start   — enter the window (at SLEEP_START, via wall-sleep.timer)
#   wall-sleep.sh end     — leave  the window (at SLEEP_END,   via wall-wake.timer)
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
        printf -v "$__k" '%s' "$__v" 2>/dev/null && export "$__k"
    done < "$__f"
}
load_env_file "$ENV_FILE"
: "${SLEEP_MODE:=suspend}"
: "${SLEEP_END:=06:30}"
: "${SLEEP_RTC_WAKE:=true}"

# ── backlight helpers ────────────────────────────────────────────────────────
# The interface name is hardware-specific (intel_backlight / acpi_video0 / …), so
# it is DISCOVERED rather than assumed — the hardware baseline records which one
# this panel has, but a firmware update can change it and a hard-coded path would
# fail silently at 22:00.
backlight_dir() {
    local d
    for d in /sys/class/backlight/*; do
        [ -w "$d/brightness" ] && { printf '%s' "$d"; return 0; }
    done
    return 1
}
backlight_set() {   # backlight_set off|on
    local d cur max
    if ! d="$(backlight_dir)"; then
        log "WARNING: no writable /sys/class/backlight/* — cannot change the backlight."
        log "WARNING: with SLEEP_MODE=backlight the panel will stay lit all night."
        return 1
    fi
    max="$(cat "$d/max_brightness" 2>/dev/null || echo 100)"
    if [ "$1" = "off" ]; then
        # Remember the level so `on` restores what the user actually had.
        cur="$(cat "$d/brightness" 2>/dev/null || echo "$max")"
        printf '%s' "$cur" > /run/wall-backlight.prev 2>/dev/null || true
        echo 0 > "$d/brightness" 2>/dev/null && log "backlight off ($d)"
    else
        cur="$(cat /run/wall-backlight.prev 2>/dev/null || echo "$max")"
        [ -n "$cur" ] || cur="$max"
        echo "$cur" > "$d/brightness" 2>/dev/null && log "backlight on ($d, level $cur)"
    fi
}

# ── arm the RTC alarm for the next occurrence of SLEEP_END ───────────────────
# `rtcwake -m no` sets the alarm WITHOUT suspending, which is what lets us arm
# first and suspend as a separate, verifiable step. `-l` says the RTC is in local
# time; getting that wrong is a wake at the wrong hour, not a failure, so it is
# worth stating explicitly rather than relying on the default.
arm_rtc() {
    local target now
    now="$(date +%s)"
    target="$(date -d "today $SLEEP_END" +%s 2>/dev/null || echo "")"
    if [ -z "$target" ]; then
        log "WARNING: SLEEP_END='$SLEEP_END' is not a time date(1) understands — NOT arming the RTC"
        return 1
    fi
    # SLEEP_END is normally the next morning, i.e. already past for "today".
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

case "${1:-}" in
    start)
        log "entering the sleep window (SLEEP_MODE=$SLEEP_MODE, until $SLEEP_END)"
        if [ "$SLEEP_MODE" = "suspend" ]; then
            if [ "$SLEEP_RTC_WAKE" = "true" ]; then
                if ! arm_rtc; then
                    # No guaranteed way back. Suspending anyway would risk a panel
                    # that is dark until someone walks over and nudges a mouse —
                    # so degrade to the L1 behaviour instead and say why.
                    log "WARNING: RTC alarm unavailable — NOT suspending. Falling back to"
                    log "WARNING: backlight-off for this window so the panel stays reachable."
                    backlight_set off
                    exit 0
                fi
            else
                log "SLEEP_RTC_WAKE=false — suspending with the USB mouse as the ONLY wake."
                log "The internal touchscreen will NOT wake it from S3. This is a test mode."
            fi
            log "suspending now"
            systemctl suspend || log "WARNING: systemctl suspend failed — the panel stays awake"
        else
            backlight_set off
        fi
        ;;
    end)
        log "leaving the sleep window (SLEEP_MODE=$SLEEP_MODE)"
        # Restoring the backlight is safe and idempotent in BOTH modes: in suspend
        # mode it is a no-op unless a failed arm_rtc degraded us to L1 above.
        backlight_set on
        ;;
    *)
        echo "usage: $0 start|end" >&2
        exit 2
        ;;
esac
exit 0
