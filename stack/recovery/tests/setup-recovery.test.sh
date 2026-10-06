#!/usr/bin/env bash
# setup-recovery.test.sh - the opt-in converger, run for real against a temp root.
#
# Stubs replace only what touches the live system (systemctl, modprobe, sysctl);
# the script under test, its knob validation (the real homehub-recovery CLI in
# python3) and every file it writes are real. Each case asserts on the files
# left behind and on the ORDER of the system calls, because "disarm before
# disable" is the property that keeps turning the knob off from resetting the box.
#
# TC-1066..TC-1069 (docs/test/test-cases.csv). Run by stack/run-hermetic-tests.sh.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP="$HERE/../setup-recovery.sh"
PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$*"; }

command -v python3 >/dev/null 2>&1 || { echo "SKIP: no python3"; printf '0 PASS  0 FAIL\n'; exit 0; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

cat >"$TMP/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >>"$CALLS"
case "$1" in
    is-active) [ "${MOCK_ACTIVE:-0}" = 1 ] && { echo active; exit 0; }; echo inactive; exit 3 ;;
    show)      [ -n "${MOCK_INVOCATION:-}" ] && { echo "$MOCK_INVOCATION"; exit 0; }; exit 1 ;;
    cat)       [ "$2" = earlyoom.service ] && [ "${MOCK_EARLYOOM:-0}" = 1 ] && exit 0; exit 1 ;;
esac
exit 0
EOF
cat >"$TMP/bin/modprobe" <<'EOF'
#!/usr/bin/env bash
printf 'modprobe %s\n' "$*" >>"$CALLS"
[ "$1" = -r ] && exit 0
case " ${MOCK_MODPROBE_OK:-} " in
    *" $1 "*) mkdir -p "$RECOVERY_ROOT/sys/class/watchdog/watchdog0"; exit 0 ;;
esac
exit 1
EOF
cat >"$TMP/bin/sysctl" <<'EOF'
#!/usr/bin/env bash
printf 'sysctl %s\n' "$*" >>"$CALLS"
EOF
chmod +x "$TMP/bin"/*

# new_root NAME [env lines...] - a fresh root and .env for one case
new_root() {
    ROOT="$TMP/$1"; shift
    mkdir -p "$ROOT/proc/sys/kernel/random" "$ROOT/etc"
    printf 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n' >"$ROOT/proc/sys/kernel/random/boot_id"
    printf '%s\n' "$@" >"$ROOT/stack.env"
    CALLS="$ROOT/calls"; : >"$CALLS"
}
run_setup() {
    PATH="$TMP/bin:$PATH" RECOVERY_ROOT="$ROOT" RECOVERY_ENV_FILE="$ROOT/stack.env" \
        RECOVERY_DEVICE_WAIT=0 CALLS="$CALLS" bash "$SETUP" >"$ROOT/out" 2>&1
    RC=$?
}
line_of() { grep -n -F -- "$1" "$CALLS" | head -1 | cut -d: -f1; }

# ── TC-1066: a bad knob is refused and NOTHING changes ──────────────────────
new_root bad HUB_WATCHDOG_ENABLED=true HUB_WD_TIERS=most
run_setup
if [ "$RC" = 2 ] && grep -q 'REFUSED: HUB_WD_TIERS' "$ROOT/out" \
   && [ ! -s "$CALLS" ] && [ ! -e "$ROOT/etc/homehub-recovery/recovery.env" ]; then
    pass "TC-1066 a bad knob is refused by name (exit 2) and nothing is changed"
else
    fail "TC-1066 bad knob rc=$RC calls=$(tr '\n' '|' <"$CALLS") out=$(head -3 "$ROOT/out" | tr '\n' '|')"
fi

# ── TC-1067: OFF disarms FIRST, then removes everything ─────────────────────
new_root off HUB_WATCHDOG_ENABLED=false
mkdir -p "$ROOT/etc/systemd/system.conf.d" "$ROOT/etc/systemd/system/tailscaled.service.d" "$ROOT/etc/modules-load.d"
: >"$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf"
: >"$ROOT/etc/systemd/system/tailscaled.service.d/60-homehub-recovery-oom.conf"
printf 'wdat_wdt\n' >"$ROOT/etc/modules-load.d/homehub-recovery.conf"
MOCK_ACTIVE=1 MOCK_INVOCATION=ffffffffffffffffffffffffffffffff MOCK_EARLYOOM=1 run_setup
stop_at="$(line_of 'systemctl stop homehub-recoveryd.service')"
disable_at="$(line_of 'systemctl disable homehub-recoveryd.service')"
if [ "$RC" = 0 ] && [ -n "$stop_at" ] && [ -n "$disable_at" ] && [ "$stop_at" -lt "$disable_at" ] \
   && [ "$(cat "$ROOT/run/homehub-recovery/disarm" 2>/dev/null)" = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ffffffffffffffffffffffffffffffff" ] \
   && [ ! -e "$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf" ] \
   && [ ! -e "$ROOT/etc/systemd/system/tailscaled.service.d/60-homehub-recovery-oom.conf" ] \
   && [ ! -e "$ROOT/etc/modules-load.d/homehub-recovery.conf" ] \
   && grep -qx 'systemctl disable --now earlyoom.service' "$CALLS" \
   && grep -qx 'sysctl -q -w kernel.panic=0' "$CALLS" \
   && grep -qx 'systemctl daemon-reexec' "$CALLS" \
   && grep -qx 'HUB_WATCHDOG_ENABLED=false' "$ROOT/etc/homehub-recovery/recovery.env"; then
    pass "TC-1067 OFF disarms with this boot and invocation BEFORE disabling, removes every drop-in, kernel.panic=0"
else
    fail "TC-1067 off rc=$RC stop@$stop_at disable@$disable_at calls=$(tr '\n' '|' <"$CALLS")"
fi

new_root off-nodisarm HUB_WATCHDOG_ENABLED=false
mkdir -p "$ROOT/etc/systemd/system.conf.d"
: >"$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf"
MOCK_ACTIVE=1 MOCK_INVOCATION='' run_setup
if [ "$RC" = 1 ] && ! grep -q 'systemctl stop\|systemctl disable' "$CALLS" \
   && [ -e "$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf" ]; then
    pass "TC-1067 a supervisor that cannot be disarmed is left running, never stopped armed"
else
    fail "TC-1067 undisarmable rc=$RC calls=$(tr '\n' '|' <"$CALLS")"
fi

new_root off-idle HUB_WATCHDOG_ENABLED=false
run_setup
if [ "$RC" = 0 ] && ! grep -q 'systemctl stop' "$CALLS" && ! grep -q 'systemctl show' "$CALLS"; then
    pass "TC-1067 OFF with the supervisor not running asks nothing to disarm"
else
    fail "TC-1067 off-idle rc=$RC calls=$(tr '\n' '|' <"$CALLS")"
fi

# ── TC-1068: ON, contain tier ───────────────────────────────────────────────
new_root contain HUB_WATCHDOG_ENABLED=true
MOCK_EARLYOOM=1 run_setup
if [ "$RC" = 0 ] \
   && grep -qx 'EARLYOOM_ARGS="-m 5,2 -s 10,5 -r 3600"' "$ROOT/etc/default/earlyoom" \
   && grep -qx 'OOMScoreAdjust=-1000' "$ROOT/etc/systemd/system/tailscaled.service.d/60-homehub-recovery-oom.conf" \
   && [ ! -e "$ROOT/etc/systemd/system/ssh.service.d" ] \
   && [ ! -e "$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf" ] \
   && [ ! -e "$ROOT/etc/modules-load.d/homehub-recovery.conf" ] \
   && ! grep -q '^modprobe' "$CALLS" \
   && grep -qx 'HUB_WD_DEVICE=' "$ROOT/etc/homehub-recovery/recovery.env" \
   && grep -qx 'HUB_WD_TIERS=contain' "$ROOT/etc/homehub-recovery/recovery.env" \
   && grep -qx 'systemctl restart earlyoom.service' "$CALLS" \
   && grep -qx 'systemctl enable homehub-recoveryd.service' "$CALLS" \
   && grep -qx 'systemctl restart homehub-recoveryd.service' "$CALLS"; then
    pass "TC-1068 contain: earlyoom and the tailscaled pass, no ssh drop-in, no device, supervisor enabled"
else
    fail "TC-1068 contain rc=$RC calls=$(tr '\n' '|' <"$CALLS") out=$(tail -3 "$ROOT/out" | tr '\n' '|')"
fi

new_root oom HUB_WATCHDOG_ENABLED=true HUB_OOM_MEM_PCT=8,3 HUB_OOM_SWAP_PCT=12,6
run_setup
if [ "$RC" = 0 ] && grep -qx 'EARLYOOM_ARGS="-m 8,3 -s 12,6 -r 3600"' "$ROOT/etc/default/earlyoom" \
   && grep -q 'earlyoom is not installed' "$ROOT/out"; then
    pass "TC-1068 earlyoom thresholds follow the knobs; a missing package is said, not hidden"
else
    fail "TC-1068 oom rc=$RC $(cat "$ROOT/etc/default/earlyoom" 2>/dev/null)"
fi

# ── TC-1069: ON, full tier, the reset-device choice ─────────────────────────
new_root wdat HUB_WATCHDOG_ENABLED=true HUB_WD_TIERS=full
mkdir -p "$ROOT/sys/firmware/acpi/tables"; : >"$ROOT/sys/firmware/acpi/tables/WDAT"
MOCK_MODPROBE_OK="wdat_wdt softdog" run_setup
if [ "$RC" = 0 ] && [ "$(cat "$ROOT/etc/modules-load.d/homehub-recovery.conf")" = wdat_wdt ] \
   && grep -qx 'RebootWatchdogSec=300s' "$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf" \
   && grep -qx 'WatchdogDevice=/dev/watchdog0' "$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf" \
   && grep -qx 'HUB_WD_DEVICE=/dev/watchdog0' "$ROOT/etc/homehub-recovery/recovery.env" \
   && grep -qx 'systemctl daemon-reexec' "$CALLS" \
   && [ "$(grep -c '^modprobe' "$CALLS")" = 1 ]; then
    pass "TC-1069 full: wdat_wdt chosen first, persisted, RebootWatchdogSec hand-off installed, PID 1 re-executed"
else
    fail "TC-1069 wdat rc=$RC calls=$(tr '\n' '|' <"$CALLS")"
fi
: >"$CALLS"
MOCK_MODPROBE_OK="wdat_wdt softdog" run_setup
if [ "$RC" = 0 ] && ! grep -q 'daemon-reexec' "$CALLS"; then
    pass "TC-1069 an unchanged re-run does not re-execute PID 1"
else
    fail "TC-1069 re-run reexec'd: $(tr '\n' '|' <"$CALLS")"
fi

new_root fallback HUB_WATCHDOG_ENABLED=true HUB_WD_TIERS=full
MOCK_MODPROBE_OK="softdog" run_setup
if [ "$RC" = 0 ] && [ "$(cat "$ROOT/etc/modules-load.d/homehub-recovery.conf")" = softdog ] \
   && [ "$(grep '^modprobe' "$CALLS" | tr '\n' '|')" = 'modprobe iTCO_wdt|modprobe softdog|' ] \
   && grep -q 'skip wdat_wdt - no ACPI WDAT table' "$ROOT/out"; then
    pass "TC-1069 no WDAT table and no TCO: falls back to softdog, in the design's order"
else
    fail "TC-1069 fallback rc=$RC calls=$(tr '\n' '|' <"$CALLS")"
fi

new_root nodevice HUB_WATCHDOG_ENABLED=true HUB_WD_TIERS=full HUB_WD_RESET_DEVICE=none
run_setup
if [ "$RC" = 0 ] && grep -qx 'HUB_WD_DEVICE=' "$ROOT/etc/homehub-recovery/recovery.env" \
   && [ ! -e "$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf" ] \
   && grep -q 'no hardware reset (L4)' "$ROOT/out" && ! grep -q '^modprobe' "$CALLS"; then
    pass "TC-1069 HUB_WD_RESET_DEVICE=none: graceful reboots only, said out loud, no device, no hand-off drop-in"
else
    fail "TC-1069 none rc=$RC calls=$(tr '\n' '|' <"$CALLS")"
fi

printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
