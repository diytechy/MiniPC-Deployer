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
# EVERY REPLACEMENT OF A RUNNING SUPERVISOR GOES THROUGH THE VERIFIED DISARM
# (review C1/C2): off, full -> contain, a different reset device, any changed
# knob. Setup decides everything first, then - only if something changes -
# runs `homehub-recovery disarm`, which stops the unit only after the device is
# proven inactive, and only THEN writes files and starts the new supervisor. A
# disarm that cannot be proven exits 1 with nothing changed. An unchanged
# re-run (every ordinary boot) leaves the running supervisor alone. The
# non-magic hand-off is reserved for real shutdowns.
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
# RECOVERY_DEVICE_WAIT (seconds to wait for /dev/watchdog0 after modprobe),
# RECOVERY_ACK_WAIT (seconds the CLI waits for the supervisor's disarm ack).
#
# Usage: setup-recovery.sh
# Exit 0 = converged (on or off). 1 = failed. 2 = refused (bad knob).
# Implements: SR-050, SR-051, LLR-1006, LLR-1010
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

supervisor_active() { systemctl is-active --quiet homehub-recoveryd.service 2>/dev/null; }

# ── THE ONLY WAY SETUP STOPS OR REPLACES A SUPERVISOR (review C1, C2) ────────
# `homehub-recovery disarm` stops the device, has the supervisor acknowledge
# it, reads sysfs itself, and only then stops the unit; with nothing running and
# nothing armed it is a no-op, and a device left active by a dead supervisor is
# disarmed directly. A plain `systemctl restart` here would hand the running
# timer to a "shutdown" that never comes - full -> contain, reset device ->
# none, or a report-only replacement would each leave a ~300 s countdown on a
# healthy box. If the disarm cannot be PROVEN, setup exits 1 having changed
# NOTHING: no unit disabled, no file removed, the old supervisor still petting.
disarm_first() {
    if ! "$PY" "$CLI" --root "${ROOT:-/}" disarm; then
        say "FAILED: the reset device could not be verified disarmed. NOTHING was changed:"
        say "  the supervisor (if any) is still running and petting it. Investigate with"
        say "  'sudo homehub-recovery status', then re-run this script."
        exit 1
    fi
}

# differs PATH WANT -> 0 if PATH does not already hold WANT ("" = must be absent)
differs() {
    if [ -z "$2" ]; then [ -e "$1" ]; return; fi
    [ ! -f "$1" ] || [ "$(cat "$1")" != "$2" ]
}

# put PATH WANT -> write it, or remove it when WANT is ""; 0 if anything changed
put() {
    differs "$1" "$2" || return 1
    if [ -z "$2" ]; then rm -f "$1"; else
        mkdir -p "$(dirname "$1")"
        printf '%s\n' "$2" >"$1.tmp"
        mv -f "$1.tmp" "$1"
    fi
    return 0
}

conf_text() {
    printf '%s\n%s\n%s\n%s' "$normalised" \
        "# The reset device the supervisor opens; empty = no hardware reset (contain tier," \
        "# or no device on this box)." "HUB_WD_DEVICE=$1"
}

# ── tailscaled: protect the RUNNING process, not only its next start (C7) ────
# systemd applies OOMScoreAdjust when it spawns a service; a drop-in plus a
# daemon-reload leaves the tailscaled that is already running at its old score.
# So the live process is set and READ BACK before earlyoom is allowed to run.
# Returns 1 only when a running tailscaled could not be protected.
protect_running_tailscaled() {
    local pid file
    pid="$(systemctl show -p MainPID --value tailscaled.service 2>/dev/null || true)"
    case "$pid" in
        ''|0|*[!0-9]*)
            say "  tailscaled is not running; its drop-in protects it from its next start"
            return 0 ;;
    esac
    file="$ROOT/proc/$pid/oom_score_adj"
    if { printf '%s\n' -1000 >"$file"; } 2>/dev/null && [ "$(cat "$file" 2>/dev/null)" = "-1000" ]; then
        say "  tailscaled (pid $pid) oom_score_adj -1000, verified"
        return 0
    fi
    say "  WARNING: could not set the running tailscaled (pid $pid) to oom_score_adj -1000"
    return 1
}

unit_exists() { systemctl cat "$1" >/dev/null 2>&1; }

# ── the reset device (C7): wdat_wdt -> iTCO_wdt -> softdog -> none ───────────
# Echoes the module that produced /dev/watchdog0, or "none". Persisted in
# modules-load.d so the classifier can read bootstatus early on later boots.
# Loading a module opens nothing, so this is safe while a supervisor holds the
# device; it runs before anything is decided.
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
    disarm_first
    systemctl disable homehub-recoveryd.service >/dev/null 2>&1 || true
    local reexec=0
    put "$SYSTEM_DROPIN" "" && reexec=1
    put "$TAILSCALED_DROPIN" "" || true
    put "$MODULES_FILE" "" || true
    if unit_exists earlyoom.service; then
        systemctl disable --now earlyoom.service >/dev/null 2>&1 || true
    fi
    put "$CONF_FILE" "$(conf_text "")" || true
    sysctl -q -w kernel.panic=0 >/dev/null 2>&1 || say "WARN: could not set kernel.panic=0"
    systemctl daemon-reload
    if [ "$reexec" = 1 ]; then
        systemctl daemon-reexec
    fi
    say "recovery supervisor OFF: disarmed (verified), disabled, drop-ins removed, kernel.panic=0."
    say "  Still on (record-only): hub-pressure samples and boot accounting."
}

converge_on() {
    say "HUB_WATCHDOG_ENABLED=true - converging the recovery supervisor ON (tiers: $TIERS)."
    local device="" module="none" reexec=0 system_dropin="" modules="" changed=0
    local earlyoom_text tailscaled_text conf

    # 1. DECIDE everything first; nothing on disk changes yet.
    if [ "$TIERS" = full ]; then
        module="$(choose_reset_device "$RESET_DEVICE")"
        if [ "$module" != none ]; then
            device=/dev/watchdog0
            modules="$module"
            system_dropin="$(sed -e "s|@SHUTDOWN_SECONDS@|$SHUTDOWN_SECONDS|" -e "s|@DEVICE@|$device|" \
                "$HERE/dropins/system-reboot-watchdog.conf.tmpl")"
            say "  reset device: $module ($device); reset-on-expiry is UNPROVEN until the P4 test"
        else
            say "  WARNING: no reset device - graceful reboots (L3) only, no hardware reset (L4)"
        fi
    else
        say "  tiers=contain: kills, sshd restarts and quarantine only; no reboot, no device"
    fi
    earlyoom_text="$(sed -e "s/@MEM_PCT@/$MEM_PCT/" -e "s/@SWAP_PCT@/$SWAP_PCT/" "$HERE/earlyoom.default.tmpl")"
    tailscaled_text="$(cat "$HERE/dropins/tailscaled-oom.conf")"
    conf="$(conf_text "$device")"
    local pair var
    for pair in "$CONF_FILE|conf" "$SYSTEM_DROPIN|system_dropin" "$MODULES_FILE|modules" \
                "$EARLYOOM_DEFAULT|earlyoom_text" "$TAILSCALED_DROPIN|tailscaled_text"; do
        var="${pair##*|}"
        if differs "${pair%%|*}" "${!var}"; then changed=1; fi
    done

    # 2. Nothing to change and the supervisor is running: leave it running.
    #    Re-running setup on every boot must not disarm and restart it each time.
    if [ "$changed" = 0 ] && supervisor_active; then
        protect_running_tailscaled || true
        say "recovery supervisor already converged and running; nothing restarted."
        return 0
    fi

    # 3. Anything else replaces the supervisor, so disarm it - verified - FIRST.
    disarm_first

    put "$CONF_FILE" "$conf" || true
    put "$EARLYOOM_DEFAULT" "$earlyoom_text" || true
    put "$TAILSCALED_DROPIN" "$tailscaled_text" || true
    put "$MODULES_FILE" "$modules" || true
    put "$SYSTEM_DROPIN" "$system_dropin" && reexec=1
    systemctl daemon-reload
    if [ "$reexec" = 1 ]; then
        # RebootWatchdogSec is read by PID 1 at start; a reload does not apply it.
        systemctl daemon-reexec
    fi
    if unit_exists earlyoom.service; then
        if protect_running_tailscaled; then
            systemctl enable earlyoom.service >/dev/null 2>&1 || true
            systemctl restart earlyoom.service || say "WARN: earlyoom did not restart"
        else
            say "  earlyoom NOT started: it could pick the remote-access tunnel. Fix the above and re-run."
        fi
    else
        protect_running_tailscaled || true
        say "  WARNING: earlyoom is not installed - no fast-spike backstop (C3)."
        say "    It is in packages.list; this image was baked before it was added."
    fi
    systemctl enable homehub-recoveryd.service >/dev/null
    # Recompute this boot's brake and kernel.panic under the new knobs (the
    # classifier is idempotent within a boot), then START the supervisor: the
    # old one, if any, was stopped by the verified disarm above.
    systemctl restart homehub-recovery-boot.service || say "WARN: boot accounting did not re-run"
    systemctl start homehub-recoveryd.service
    say "recovery supervisor ON. Check: homehub-recovery status"
}

if [ "$ENABLED" = true ]; then
    converge_on
else
    converge_off
fi
