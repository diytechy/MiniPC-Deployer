#!/usr/bin/env bash
# setup-recovery.sh - converge the OPT-IN half of the hub recovery capability.
#
# Called by firstboot step 6g on EVERY boot, with the knob either way, because
# "off" is also a state to converge to: turning HUB_WATCHDOG_ENABLED to false
# and re-running this must DISARM the hardware timer (never just stop the unit,
# which would leave it running for the 300 s hand-off and reset the box), then
# disable the supervisor, remove every drop-in it added and restore
# kernel.panic=0.
#
# The ALWAYS-ON half (pressure sampler, boot accounting, journald and
# hung-task settings, the backup slice) is installed by firstboot itself and is
# untouched here: it is record-only and the knob does not govern it.
#
# WHAT "ON" INSTALLS (HUB_WATCHDOG_ENABLED=true):
#   /etc/homehub-recovery/recovery.env    the validated knobs, for every unit
#   /etc/default/earlyoom                 C3, from earlyoom.default.tmpl
#   tailscaled OOMScoreAdjust=-1000       §6 / D9 (ssh needs none: OpenSSH puts
#                                         its own listener at -1000 and restores
#                                         sessions to the inherited value - a
#                                         drop-in would make every SSH session,
#                                         and anything run from one, unkillable)
#   and with HUB_WD_TIERS=full:
#   /etc/modules-load.d/homehub-recovery.conf   the chosen reset device (C7)
#   /etc/systemd/system.conf.d/...              RebootWatchdogSec hand-off (§3.4)
#
# THE KNOBS ARE VALIDATED BY THE SAME CODE THAT READS THEM: this script hands
# them to `homehub-recovery check-config`, so there is one definition of a
# legal value and it is in recovery_common.py. A bad knob is a REFUSAL, exit 2,
# reported by firstboot as a WARNING with the reason - and nothing is changed.
#
# Test seams: RECOVERY_ENV_FILE, RECOVERY_ROOT (prefix for /etc, /sys, /proc),
# RECOVERY_DEVICE_WAIT (seconds to wait for /dev/watchdog0 after modprobe).
#
# Usage: setup-recovery.sh
# Exit 0 = converged (on or off). 1 = failed. 2 = refused (bad knob).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${RECOVERY_ENV_FILE:-$HERE/../.env}"
ROOT="${RECOVERY_ROOT:-}"
ETC="$ROOT/etc"
SYS="$ROOT/sys"
DEVICE_WAIT="${RECOVERY_DEVICE_WAIT:-5}"
PY="${RECOVERY_PYTHON:-python3}"
CLI="$HERE/homehub-recovery"

CONF_DIR="$ETC/homehub-recovery"
CONF_FILE="$CONF_DIR/recovery.env"
SYSTEM_DROPIN="$ETC/systemd/system.conf.d/60-homehub-recovery.conf"
TAILSCALED_DROPIN="$ETC/systemd/system/tailscaled.service.d/60-homehub-recovery-oom.conf"
MODULES_FILE="$ETC/modules-load.d/homehub-recovery.conf"
EARLYOOM_DEFAULT="$ETC/default/earlyoom"

say() { printf '%s\n' "$*"; }
refuse() { say "REFUSED: $*"; exit 2; }

# THE SAME sed IDIOM AS firstboot.sh's _env_val, and for the same reason: this
# file is never `source`d (it holds secrets), and validate_config.py's host-knob
# check reads every _env_val call here to prove each knob is declared in
# stack/.env.example.
_env_val() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | head -1 | tr -d '\r' | tr -d '"'; }

[ -f "$ENV_FILE" ] || refuse "no .env at $ENV_FILE"

candidate="$(mktemp)"
trap 'rm -f "$candidate"' EXIT
{
    printf 'HUB_WATCHDOG_ENABLED=%s\n'      "$(_env_val HUB_WATCHDOG_ENABLED)"
    printf 'HUB_WD_TIERS=%s\n'              "$(_env_val HUB_WD_TIERS)"
    printf 'HUB_WD_PSI_FULL=%s\n'           "$(_env_val HUB_WD_PSI_FULL)"
    printf 'HUB_WD_KILL_SECONDS=%s\n'       "$(_env_val HUB_WD_KILL_SECONDS)"
    printf 'HUB_WD_KILL_MEASURED_PCT=%s\n'  "$(_env_val HUB_WD_KILL_MEASURED_PCT)"
    printf 'HUB_WD_REBOOT_SECONDS=%s\n'     "$(_env_val HUB_WD_REBOOT_SECONDS)"
    printf 'HUB_WD_SSH_FAILS=%s\n'          "$(_env_val HUB_WD_SSH_FAILS)"
    printf 'HUB_WD_MAX_RECOVERY_BOOTS=%s\n' "$(_env_val HUB_WD_MAX_RECOVERY_BOOTS)"
    printf 'HUB_WD_PROTECT=%s\n'            "$(_env_val HUB_WD_PROTECT)"
    printf 'HUB_WD_HW_TIMEOUT=%s\n'         "$(_env_val HUB_WD_HW_TIMEOUT)"
    printf 'HUB_WD_SHUTDOWN_SECONDS=%s\n'   "$(_env_val HUB_WD_SHUTDOWN_SECONDS)"
    printf 'HUB_WD_RESET_DEVICE=%s\n'       "$(_env_val HUB_WD_RESET_DEVICE)"
    printf 'HUB_OOM_MEM_PCT=%s\n'           "$(_env_val HUB_OOM_MEM_PCT)"
    printf 'HUB_OOM_SWAP_PCT=%s\n'          "$(_env_val HUB_OOM_SWAP_PCT)"
} >"$candidate"

if ! normalised="$("$PY" "$CLI" check-config "$candidate" 2>&1)"; then
    refuse "${normalised#REFUSED: } - fix it in $ENV_FILE; nothing was changed"
fi
knob() { printf '%s\n' "$normalised" | sed -n "s/^$1=//p"; }
ENABLED="$(knob HUB_WATCHDOG_ENABLED)"
TIERS="$(knob HUB_WD_TIERS)"
RESET_DEVICE="$(knob HUB_WD_RESET_DEVICE)"
SHUTDOWN_SECONDS="$(knob HUB_WD_SHUTDOWN_SECONDS)"
MEM_PCT="$(knob HUB_OOM_MEM_PCT)"
SWAP_PCT="$(knob HUB_OOM_SWAP_PCT)"

# write_if_changed PATH CONTENT -> 0 if the file changed, 1 if it already held it
write_if_changed() {
    local path="$1" content="$2"
    if [ -f "$path" ] && [ "$(cat "$path")" = "$content" ]; then
        return 1
    fi
    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$content" >"$path.tmp"
    mv -f "$path.tmp" "$path"
    return 0
}

# remove_if_present PATH -> 0 if something was removed
remove_if_present() {
    if [ -e "$1" ]; then rm -f "$1"; return 0; fi
    return 1
}

install_conf() {
    write_if_changed "$CONF_FILE" "${normalised}
# The reset device the supervisor opens; empty = no hardware reset (contain tier,
# or no device on this box).
HUB_WD_DEVICE=$1" || true
}

unit_exists() { systemctl cat "$1" >/dev/null 2>&1; }

# ── the reset device (C7): wdat_wdt -> iTCO_wdt -> softdog -> none ───────────
# Echoes the module that produced /dev/watchdog0, or "none". Persisted in
# modules-load.d so the classifier can read bootstatus early on later boots.
choose_reset_device() {
    local wanted="$1" candidates m i
    case "$wanted" in
        auto) candidates="wdat_wdt iTCO_wdt softdog" ;;
        none) echo none; return 0 ;;
        *)    candidates="$wanted" ;;
    esac
    for m in $candidates; do
        if [ "$m" = wdat_wdt ] && [ ! -e "$SYS/firmware/acpi/tables/WDAT" ]; then
            say "  reset device: skip wdat_wdt - no ACPI WDAT table" >&2
            continue
        fi
        if ! modprobe "$m" >/dev/null 2>&1; then
            say "  reset device: modprobe $m failed" >&2
            continue
        fi
        for i in $(seq 0 "$DEVICE_WAIT"); do
            [ -e "$SYS/class/watchdog/watchdog0" ] && break
            [ "$i" -lt "$DEVICE_WAIT" ] && sleep 1
        done
        if [ -e "$SYS/class/watchdog/watchdog0" ]; then
            echo "$m"
            return 0
        fi
        say "  reset device: $m loaded but no watchdog0 appeared; unloading" >&2
        modprobe -r "$m" >/dev/null 2>&1 || true
    done
    echo none
}

converge_off() {
    say "HUB_WATCHDOG_ENABLED is not true - converging the recovery supervisor OFF."
    if systemctl is-active --quiet homehub-recoveryd.service 2>/dev/null; then
        # DISARM FIRST, AND REFUSE TO STOP WITHOUT IT. A plain stop hands the
        # running timer to the shutdown hand-off, and with no shutdown coming
        # the board would reset 300 s later.
        if ! "$PY" "$CLI" --root "${ROOT:-/}" disarm; then
            say "FAILED: could not disarm homehub-recoveryd; leaving it running rather than"
            say "  stopping it armed. Investigate, then: sudo homehub-recovery disarm"
            exit 1
        fi
    fi
    systemctl disable homehub-recoveryd.service >/dev/null 2>&1 || true
    local reexec=0
    remove_if_present "$SYSTEM_DROPIN" && reexec=1
    remove_if_present "$TAILSCALED_DROPIN" || true
    remove_if_present "$MODULES_FILE" || true
    if unit_exists earlyoom.service; then
        systemctl disable --now earlyoom.service >/dev/null 2>&1 || true
    fi
    install_conf ""
    sysctl -q -w kernel.panic=0 >/dev/null 2>&1 || say "WARN: could not set kernel.panic=0"
    systemctl daemon-reload
    if [ "$reexec" = 1 ]; then
        systemctl daemon-reexec
    fi
    say "recovery supervisor OFF: disarmed, disabled, drop-ins removed, kernel.panic=0."
    say "  Still on (record-only): hub-pressure samples and boot accounting."
}

converge_on() {
    say "HUB_WATCHDOG_ENABLED=true - converging the recovery supervisor ON (tiers: $TIERS)."
    local device="" module="none" reexec=0 content
    content="$(sed -e "s/@MEM_PCT@/$MEM_PCT/" -e "s/@SWAP_PCT@/$SWAP_PCT/" "$HERE/earlyoom.default.tmpl")"
    write_if_changed "$EARLYOOM_DEFAULT" "$content" || true
    write_if_changed "$TAILSCALED_DROPIN" "$(cat "$HERE/dropins/tailscaled-oom.conf")" || true

    if [ "$TIERS" = full ]; then
        module="$(choose_reset_device "$RESET_DEVICE")"
        if [ "$module" != none ]; then
            device=/dev/watchdog0
            write_if_changed "$MODULES_FILE" "$module" || true
            content="$(sed -e "s|@SHUTDOWN_SECONDS@|$SHUTDOWN_SECONDS|" -e "s|@DEVICE@|$device|" \
                "$HERE/dropins/system-reboot-watchdog.conf.tmpl")"
            write_if_changed "$SYSTEM_DROPIN" "$content" && reexec=1
            say "  reset device: $module ($device); reset-on-expiry is UNPROVEN until the P4 test"
        else
            say "  WARNING: no reset device - graceful reboots (L3) only, no hardware reset (L4)"
            remove_if_present "$SYSTEM_DROPIN" && reexec=1
            remove_if_present "$MODULES_FILE" || true
        fi
    else
        say "  tiers=contain: kills, sshd restarts and quarantine only; no reboot, no device"
        remove_if_present "$SYSTEM_DROPIN" && reexec=1
        remove_if_present "$MODULES_FILE" || true
    fi
    install_conf "$device"

    systemctl daemon-reload
    if [ "$reexec" = 1 ]; then
        # RebootWatchdogSec is read by PID 1 at start; a reload does not apply it.
        systemctl daemon-reexec
    fi
    if unit_exists earlyoom.service; then
        systemctl enable earlyoom.service >/dev/null 2>&1 || true
        systemctl restart earlyoom.service || say "WARN: earlyoom did not restart"
    else
        say "  WARNING: earlyoom is not installed - no fast-spike backstop (C3)."
        say "    It is in packages.list; this image was baked before it was added."
    fi
    systemctl enable homehub-recoveryd.service >/dev/null
    # Recompute this boot's brake and kernel.panic under the new knobs (the
    # classifier is idempotent within a boot), then (re)start the supervisor.
    # A restart is a plain stop + start: it hands off and re-arms (L-12).
    systemctl restart homehub-recovery-boot.service || say "WARN: boot accounting did not re-run"
    systemctl restart homehub-recoveryd.service
    say "recovery supervisor ON. Check: homehub-recovery status"
}

if [ "$ENABLED" = true ]; then
    converge_on
else
    converge_off
fi
