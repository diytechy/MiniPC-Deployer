#!/usr/bin/env bash
# setup-recovery.test.sh - the opt-in converger, run for real against a temp root.
#
# Stubs replace only what touches the live system (systemctl, modprobe, sysctl);
# the script under test, its knob validation and the verified-disarm protocol
# (the real homehub-recovery CLI in python3) and every file it writes are real.
#
# THE STUBBED systemctl MODELS THE RUNNING SUPERVISOR'S SIDE OF THE DISARM
# (review C2): on `kill -s SIGUSR1` it consumes the authorisation, stops the
# fake device (sysfs state inactive) when MOCK_DISARM=ok, leaves it running when
# MOCK_DISARM=fail, and acknowledges accordingly; MOCK_DISARM=silent never
# answers. Every start/restart of the supervisor is logged WITH the device
# state at that moment, so "the replacement started while the old timer was
# still running" (review C1) is an assertion, not an inference.
#
# TC-1066..TC-1069, TC-1081..TC-1082 (docs/test/test-cases.csv).
# Run by stack/run-hermetic-tests.sh.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP="$HERE/../setup-recovery.sh"
PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$*"; }

command -v python3 >/dev/null 2>&1 || { echo "SKIP: no python3"; printf '0 PASS  0 FAIL\n'; exit 0; }
TMP="$(mktemp -d)"
trap 'chmod -R u+w "$TMP" 2>/dev/null; rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

cat >"$TMP/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
R="$RECOVERY_ROOT"
STATE="$R/sys/class/watchdog/watchdog0/state"
dev_state() { if [ -f "$STATE" ]; then cat "$STATE"; else echo absent; fi; }
unit="${*: -1}"
case "$1" in
    start|restart)
        if [ "$unit" = homehub-recoveryd.service ]; then
            printf 'systemctl %s device=%s\n' "$*" "$(dev_state)" >>"$CALLS"
            echo active >"$R/.supervisor"
            exit 0
        fi ;;
esac
printf 'systemctl %s\n' "$*" >>"$CALLS"
case "$1" in
    is-active)
        if [ "$(cat "$R/.supervisor" 2>/dev/null)" = active ]; then echo active; exit 0; fi
        echo "${MOCK_INACTIVE_WORD:-inactive}"; exit 3 ;;
    show)
        case "$*" in
            *InvocationID*) [ -n "${MOCK_INVOCATION:-}" ] && { echo "$MOCK_INVOCATION"; exit 0; }; exit 1 ;;
            *MainPID*tailscaled*) echo "${MOCK_TS_PID:-0}"; exit 0 ;;
        esac
        exit 1 ;;
    kill)
        auth="$R/run/homehub-recovery/disarm"
        read -r boot inv <"$auth"; rm -f "$auth"
        case "${MOCK_DISARM:-ok}" in
            ok)   [ -f "$STATE" ] && echo inactive >"$STATE"; ok=true ;;
            fail) ok=false ;;
            silent) exit 0 ;;
        esac
        printf '{"boot_id": "%s", "invocation_id": "%s", "ok": %s, "detail": "stub"}\n' \
            "$boot" "$inv" "$ok" >"$R/run/homehub-recovery/disarm-ack"
        exit 0 ;;
    stop)
        [ "$unit" = homehub-recoveryd.service ] && echo inactive >"$R/.supervisor"
        exit 0 ;;
    cat) [ "$2" = earlyoom.service ] && [ "${MOCK_EARLYOOM:-0}" = 1 ] && exit 0; exit 1 ;;
esac
exit 0
EOF
cat >"$TMP/bin/modprobe" <<'EOF'
#!/usr/bin/env bash
printf 'modprobe %s\n' "$*" >>"$CALLS"
[ "$1" = -r ] && exit 0
case " ${MOCK_MODPROBE_OK:-} " in
    *" $1 "*)
        d="$RECOVERY_ROOT/sys/class/watchdog/watchdog0"
        mkdir -p "$d"
        [ -f "$d/state" ] || echo inactive >"$d/state"
        echo 0 >"$d/nowayout"
        exit 0 ;;
esac
exit 1
EOF
cat >"$TMP/bin/sysctl" <<'EOF'
#!/usr/bin/env bash
printf 'sysctl %s\n' "$*" >>"$CALLS"
EOF
chmod +x "$TMP/bin"/*

INV=ffffffffffffffffffffffffffffffff
# new_root NAME [env lines...] - a fresh root and .env for one case
new_root() {
    ROOT="$TMP/$1"; shift
    mkdir -p "$ROOT/proc/sys/kernel/random" "$ROOT/etc" "$ROOT/run/homehub-recovery"
    printf 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n' >"$ROOT/proc/sys/kernel/random/boot_id"
    printf '%s\n' "$@" >"$ROOT/stack.env"
    CALLS="$ROOT/calls"; : >"$CALLS"
}
# an_armed_supervisor - the state a full-tier box is in: running, device active
an_armed_supervisor() {
    echo active >"$ROOT/.supervisor"
    mkdir -p "$ROOT/sys/class/watchdog/watchdog0" "$ROOT/etc/homehub-recovery" \
             "$ROOT/etc/systemd/system.conf.d" "$ROOT/etc/modules-load.d"
    echo active >"$ROOT/sys/class/watchdog/watchdog0/state"
    echo 0 >"$ROOT/sys/class/watchdog/watchdog0/nowayout"
    printf 'HUB_WD_DEVICE=/dev/watchdog0\n' >"$ROOT/etc/homehub-recovery/recovery.env"
    : >"$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf"
    printf 'wdat_wdt\n' >"$ROOT/etc/modules-load.d/homehub-recovery.conf"
}
dev_state() { cat "$ROOT/sys/class/watchdog/watchdog0/state" 2>/dev/null || echo absent; }
run_setup() {
    PATH="$TMP/bin:$PATH" RECOVERY_ROOT="$ROOT" RECOVERY_ENV_FILE="$ROOT/stack.env" \
        RECOVERY_DEVICE_WAIT=0 RECOVERY_ACK_WAIT=1 CALLS="$CALLS" MOCK_INVOCATION="$INV" \
        bash "$SETUP" >"$ROOT/out" 2>&1
    RC=$?
}
line_of() { grep -n -F -- "$1" "$CALLS" | head -1 | cut -d: -f1; }
SIGUSR1='systemctl kill -s SIGUSR1 --kill-whom=main homehub-recoveryd.service'

# ── TC-1066: a bad knob is refused and NOTHING changes ──────────────────────
new_root bad HUB_WATCHDOG_ENABLED=true HUB_WD_TIERS=most
run_setup
if [ "$RC" = 2 ] && grep -q 'REFUSED: HUB_WD_TIERS' "$ROOT/out" \
   && [ ! -s "$CALLS" ] && [ ! -e "$ROOT/etc/homehub-recovery/recovery.env" ]; then
    pass "TC-1066 a bad knob is refused by name (exit 2) and nothing is changed"
else
    fail "TC-1066 bad knob rc=$RC calls=$(tr '\n' '|' <"$CALLS") out=$(head -3 "$ROOT/out" | tr '\n' '|')"
fi

# ── TC-1067: OFF disarms - VERIFIED - before anything else ──────────────────
new_root off HUB_WATCHDOG_ENABLED=false
an_armed_supervisor
mkdir -p "$ROOT/etc/systemd/system/tailscaled.service.d"
: >"$ROOT/etc/systemd/system/tailscaled.service.d/60-homehub-recovery-oom.conf"
MOCK_EARLYOOM=1 run_setup
sig_at="$(line_of "$SIGUSR1")"
stop_at="$(line_of 'systemctl stop homehub-recoveryd.service')"
disable_at="$(line_of 'systemctl disable homehub-recoveryd.service')"
if [ "$RC" = 0 ] && [ -n "$sig_at" ] && [ -n "$stop_at" ] && [ -n "$disable_at" ] \
   && [ "$sig_at" -lt "$stop_at" ] && [ "$stop_at" -lt "$disable_at" ] \
   && [ "$(dev_state)" = inactive ] \
   && [ ! -e "$ROOT/run/homehub-recovery/disarm" ] \
   && [ ! -e "$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf" ] \
   && [ ! -e "$ROOT/etc/systemd/system/tailscaled.service.d/60-homehub-recovery-oom.conf" ] \
   && [ ! -e "$ROOT/etc/modules-load.d/homehub-recovery.conf" ] \
   && grep -qx 'systemctl disable --now earlyoom.service' "$CALLS" \
   && grep -qx 'sysctl -q -w kernel.panic=0' "$CALLS" \
   && grep -qx 'systemctl daemon-reexec' "$CALLS" \
   && grep -qx 'HUB_WATCHDOG_ENABLED=false' "$ROOT/etc/homehub-recovery/recovery.env"; then
    pass "TC-1067 OFF: SIGUSR1, verified-inactive ack, THEN stop, then disable and remove; kernel.panic=0"
else
    fail "TC-1067 off rc=$RC sig@$sig_at stop@$stop_at disable@$disable_at dev=$(dev_state) calls=$(tr '\n' '|' <"$CALLS")"
fi

for mode in fail silent; do
    new_root "off-$mode" HUB_WATCHDOG_ENABLED=false
    an_armed_supervisor
    MOCK_DISARM="$mode" run_setup
    if [ "$RC" = 1 ] && grep -qxF "$SIGUSR1" "$CALLS" \
       && ! grep -q 'systemctl stop\|systemctl disable' "$CALLS" \
       && [ "$(dev_state)" = active ] && [ "$(cat "$ROOT/.supervisor")" = active ] \
       && [ -e "$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf" ] \
       && [ -e "$ROOT/etc/modules-load.d/homehub-recovery.conf" ] \
       && grep -qx 'HUB_WD_DEVICE=/dev/watchdog0' "$ROOT/etc/homehub-recovery/recovery.env" \
       && grep -q 'NOTHING was changed' "$ROOT/out"; then
        pass "TC-1067 a disarm that is $mode leaves the supervisor running, its device armed and every file in place"
    else
        fail "TC-1067 $mode disarm rc=$RC dev=$(dev_state) calls=$(tr '\n' '|' <"$CALLS")"
    fi
done

new_root off-orphan HUB_WATCHDOG_ENABLED=false
an_armed_supervisor
echo failed >"$ROOT/.supervisor"  # the supervisor died; its timer did not
mkdir -p "$ROOT/dev"; : >"$ROOT/dev/watchdog0"
MOCK_INACTIVE_WORD=failed run_setup
if [ "$RC" = 1 ] && grep -q 'no supervisor is running' "$ROOT/out" \
   && ! grep -q 'systemctl disable' "$CALLS" \
   && [ -e "$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf" ]; then
    pass "TC-1067 a device left ACTIVE by a dead supervisor is not ignored: setup fails closed, nothing removed"
else
    fail "TC-1067 orphan rc=$RC calls=$(tr '\n' '|' <"$CALLS") out=$(tail -3 "$ROOT/out" | tr '\n' '|')"
fi

new_root off-idle HUB_WATCHDOG_ENABLED=false
run_setup
if [ "$RC" = 0 ] && ! grep -q 'systemctl stop\|systemctl show\|systemctl kill' "$CALLS"; then
    pass "TC-1067 OFF with nothing running and nothing armed asks nothing to disarm"
else
    fail "TC-1067 off-idle rc=$RC calls=$(tr '\n' '|' <"$CALLS")"
fi

# ── TC-1081: full -> contain leaves NO active device (review C1) ────────────
new_root full-to-contain HUB_WATCHDOG_ENABLED=true HUB_WD_TIERS=contain
an_armed_supervisor
run_setup
sig_at="$(line_of "$SIGUSR1")"
start_at="$(line_of 'systemctl start homehub-recoveryd.service')"
if [ "$RC" = 0 ] && [ -n "$sig_at" ] && [ -n "$start_at" ] && [ "$sig_at" -lt "$start_at" ] \
   && grep -qx 'systemctl start homehub-recoveryd.service device=inactive' "$CALLS" \
   && ! grep -q 'systemctl restart homehub-recoveryd.service' "$CALLS" \
   && [ "$(dev_state)" = inactive ] \
   && [ ! -e "$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf" ] \
   && [ ! -e "$ROOT/etc/modules-load.d/homehub-recovery.conf" ] \
   && grep -qx 'HUB_WD_DEVICE=' "$ROOT/etc/homehub-recovery/recovery.env"; then
    pass "TC-1081 full -> contain: verified disarm first; the replacement starts with the device INACTIVE"
else
    fail "TC-1081 full->contain rc=$RC dev=$(dev_state) calls=$(tr '\n' '|' <"$CALLS")"
fi

new_root full-to-none HUB_WATCHDOG_ENABLED=true HUB_WD_TIERS=full HUB_WD_RESET_DEVICE=none
an_armed_supervisor
MOCK_DISARM=fail run_setup
if [ "$RC" = 1 ] && [ "$(dev_state)" = active ] && ! grep -q 'homehub-recoveryd.service device=' "$CALLS" \
   && [ -e "$ROOT/etc/systemd/system.conf.d/60-homehub-recovery.conf" ]; then
    pass "TC-1081 a reconfiguration whose disarm fails replaces nothing"
else
    fail "TC-1081 failed-disarm reconfigure rc=$RC calls=$(tr '\n' '|' <"$CALLS")"
fi

# ── TC-1068: ON, contain tier, from nothing ─────────────────────────────────
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
   && grep -q '^systemctl start homehub-recoveryd.service' "$CALLS"; then
    pass "TC-1068 contain: earlyoom and the tailscaled pass, no ssh drop-in, no device, supervisor started"
else
    fail "TC-1068 contain rc=$RC calls=$(tr '\n' '|' <"$CALLS") out=$(tail -3 "$ROOT/out" | tr '\n' '|')"
fi
: >"$CALLS"
MOCK_EARLYOOM=1 run_setup
if [ "$RC" = 0 ] && ! grep -Eq '^systemctl (kill|stop|start|restart) .*homehub-recoveryd' "$CALLS" \
   && grep -q 'already converged' "$ROOT/out"; then
    pass "TC-1068 an unchanged re-run with the supervisor running neither disarms nor restarts it"
else
    fail "TC-1068 unchanged re-run touched the supervisor: $(tr '\n' '|' <"$CALLS")"
fi

new_root oom HUB_WATCHDOG_ENABLED=true HUB_OOM_MEM_PCT=8,3 HUB_OOM_SWAP_PCT=12,6
run_setup
if [ "$RC" = 0 ] && grep -qx 'EARLYOOM_ARGS="-m 8,3 -s 12,6 -r 3600"' "$ROOT/etc/default/earlyoom" \
   && grep -q 'earlyoom is not installed' "$ROOT/out"; then
    pass "TC-1068 earlyoom thresholds follow the knobs; a missing package is said, not hidden"
else
    fail "TC-1068 oom rc=$RC $(cat "$ROOT/etc/default/earlyoom" 2>/dev/null)"
fi

# ── TC-1082: the RUNNING tailscaled is protected before earlyoom (review C7) ─
new_root ts HUB_WATCHDOG_ENABLED=true
mkdir -p "$ROOT/proc/4242"; echo 0 >"$ROOT/proc/4242/oom_score_adj"
MOCK_EARLYOOM=1 MOCK_TS_PID=4242 run_setup
pid_at="$(line_of 'systemctl show -p MainPID --value tailscaled.service')"
eo_at="$(line_of 'systemctl restart earlyoom.service')"
if [ "$RC" = 0 ] && [ "$(cat "$ROOT/proc/4242/oom_score_adj")" = -1000 ] \
   && [ -n "$pid_at" ] && [ -n "$eo_at" ] && [ "$pid_at" -lt "$eo_at" ] \
   && grep -q 'tailscaled (pid 4242) oom_score_adj -1000, verified' "$ROOT/out"; then
    pass "TC-1082 the running tailscaled is set to -1000 and read back before earlyoom starts"
else
    fail "TC-1082 live tailscaled rc=$RC adj=$(cat "$ROOT/proc/4242/oom_score_adj") calls=$(tr '\n' '|' <"$CALLS")"
fi

for how in refused ignored; do
    new_root "ts-$how" HUB_WATCHDOG_ENABLED=true
    mkdir -p "$ROOT/proc/4242"
    if [ "$how" = refused ]; then
        echo 0 >"$ROOT/proc/4242/oom_score_adj"; chmod 0444 "$ROOT/proc/4242/oom_score_adj"
    else
        # The write "succeeds" and the value does not stick: only the READ-BACK sees it.
        ln -s /dev/null "$ROOT/proc/4242/oom_score_adj"
    fi
    MOCK_EARLYOOM=1 MOCK_TS_PID=4242 run_setup
    if [ "$RC" = 0 ] && ! grep -q 'systemctl restart earlyoom.service' "$CALLS" \
       && grep -q 'earlyoom NOT started' "$ROOT/out"; then
        pass "TC-1082 a running tailscaled whose protection is $how keeps earlyoom off, and says so"
    else
        fail "TC-1082 $how tailscaled rc=$RC calls=$(tr '\n' '|' <"$CALLS")"
    fi
done

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
    fail "TC-1069 wdat rc=$RC calls=$(tr '\n' '|' <"$CALLS") out=$(tail -3 "$ROOT/out" | tr '\n' '|')"
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
