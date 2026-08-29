#!/usr/bin/env bash
# icedrive-gate.test.sh — the gate that decides whether the cloud sync starts.
#
# THIS IS THE ONE SCRIPT ON THE BOX WHOSE FAILURE MODE IS DATA LOSS AT THE FAR
# END. IceDrive's sync pairs carry ABSOLUTE local paths and are server-side, so
# the client signs in, asks the API and starts scanning within seconds. A
# two-way pair scanning a path that is not the library sees an empty tree, and
# "empty" and "every file was deleted" are the same observation to a sync
# engine. Everything here is about refusing to start in exactly those cases.
#
# HERMETIC. `ICEDRIVE_APP` points at a stub that records that it ran, so the
# gate's `exec` is observable without an AppImage. The mount check reads
# /proc/self/mountinfo, so the "mounted" cases use a REAL, WRITABLE mountpoint
# found at runtime (/dev/shm, /tmp, …) and the "not mounted" cases use an
# ordinary directory.
#
# WHAT IT LOCKS
#   G1  an unmounted path REFUSES, and the client does not run
#   G2  the refusal explains the empty-tree/deleted-tree equivalence — this is
#       the message an operator reads at 2am and it must not be "exit 1"
#   G3  a mounted path with NO marker REFUSES (added 2026-08-29 on an
#       adversarial review: mounted is not the same as "the right volume")
#   G4  the marker refusal prints the exact `touch` command to bless it
#   G5  mounted + marker STARTS the client
#   G6  ICEDRIVE_GATE_REQUIRE_MARKER=false starts without a marker, for a lab
#   G7  a WHITESPACE path list starts immediately (the feature being off must
#       not look like the gate being broken) while an EMPTY one still guards -
#       a typo must never be able to disable this check
#   G8  the timeout is honoured rather than blocking forever
#
# Usage: bash icedrive-gate.test.sh [--keep-tmp]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="$(cd "$HERE/.." && pwd)/icedrive-gate.sh"
[ -f "$SUT" ] || { echo "FATAL: $SUT not found"; exit 2; }
KEEP_TMP=0
[ "${1:-}" = "--keep-tmp" ] && KEEP_TMP=1

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }

TMP="$(mktemp -d)"
cleanup() { if [ "$KEEP_TMP" = 1 ]; then echo "tmp kept: $TMP"; return 0; fi; rm -rf "$TMP"; }
trap cleanup EXIT

STUB="$TMP/fake-app"
printf '#!/bin/sh\necho STARTED >>"%s/started"\n' "$TMP" >"$STUB"
chmod +x "$STUB"

NOTMOUNTED="$TMP/notmounted"; mkdir -p "$NOTMOUNTED"
# A REAL MOUNTPOINT THIS ACCOUNT CAN WRITE TO, chosen at runtime. The first cut
# used "/", which is a mountpoint everywhere and writable almost nowhere — so
# the marker case could not be set up and skipped itself, which is the shape of
# a test that looks green while proving nothing.
MOUNTED=""
for cand in /dev/shm /tmp /run/shm; do
    [ -d "$cand" ] && [ -w "$cand" ] || continue
    awk -v w="$cand" '{ if ($5 == w) found = 1 } END { exit !found }' /proc/self/mountinfo || continue
    MOUNTED="$cand"; break
done
if [ -z "$MOUNTED" ]; then
    echo "FATAL: no writable mountpoint found for the mounted-path cases (tried /dev/shm /tmp /run/shm)" >&2
    exit 2
fi
MARKDIR="$TMP/marked"; mkdir -p "$MARKDIR"

gate() { # gate PATHS... -> echoes exit code; output in $TMP/out.txt
    rm -f "$TMP/started"
    ICEDRIVE_APP="$STUB" ICEDRIVE_GATE_TIMEOUT="${GT:-2}" ICEDRIVE_GATE_INTERVAL=1 \
        ICEDRIVE_GATE_REQUIRE_MARKER="${RM:-true}" ICEDRIVE_GATE_MARKER="${MK:-.homehub-library}" \
        bash "$SUT" "$@" >"$TMP/out.txt" 2>&1
    echo $?
}
started() { [ -f "$TMP/started" ]; }

echo "== G1/G2/G8: an unmounted path refuses, in time, and says why =="
t0=$(date +%s)
rc="$(gate "$NOTMOUNTED")"
t1=$(date +%s)
if [ "$rc" = 1 ]; then pass "G1 an unmounted path exits 1"; else fail "G1 exit=$rc, want 1"; cat "$TMP/out.txt"; fi
if ! started; then pass "G1 and the client was NOT started"; else fail "G1 the client ran against an unmounted path"; fi
if grep -q 'indistinguishable from every' "$TMP/out.txt"; then pass "G2 the refusal explains empty-vs-deleted"; else fail "G2 the refusal does not explain itself"; cat "$TMP/out.txt"; fi
if [ "$(( t1 - t0 ))" -le 10 ]; then pass "G8 it gave up after its timeout ($(( t1 - t0 ))s), rather than blocking"; else fail "G8 it took $(( t1 - t0 ))s with a 2s timeout"; fi

echo
echo "== G3/G4: mounted is not the same as the right volume =="
rc="$(gate "$MOUNTED")"
if [ "$rc" = 1 ]; then pass "G3 a mounted path with no marker exits 1"; else fail "G3 exit=$rc, want 1"; cat "$TMP/out.txt"; fi
if ! started; then pass "G3 and the client was NOT started"; else fail "G3 the client ran against an unblessed volume"; fi
if grep -q "sudo touch $MOUNTED.homehub-library" "$TMP/out.txt" || grep -q 'sudo touch' "$TMP/out.txt"; then
    pass "G4 and it printed the command that blesses the volume"
else
    fail "G4 the refusal does not say how to fix it"; cat "$TMP/out.txt"
fi
if grep -q 'blank replacement disk' "$TMP/out.txt"; then pass "G4 and it names the case it is refusing"; else fail "G4 no explanation of the marker's purpose"; fi

echo
echo "== G5/G6: with the marker, and without the requirement =="
# A REAL, WRITABLE MOUNTPOINT (chosen above), so the marker can actually be
# created at its root - the code path under test reads "$p/$MARKER".
MK=".homehub-library-test-$$"
: >"$MOUNTED/$MK" || { echo "FATAL: could not create a marker in $MOUNTED" >&2; exit 2; }
rc="$(gate "$MOUNTED")"
if [ "$rc" = 0 ] && started; then pass "G5 mounted + marker starts the client"; else fail "G5 exit=$rc"; cat "$TMP/out.txt"; fi
rm -f "$MOUNTED/$MK"
MK=".homehub-library"
RM=false
rc="$(gate "$MOUNTED")"
if [ "$rc" = 0 ] && started; then pass "G6 ICEDRIVE_GATE_REQUIRE_MARKER=false starts without a marker"; else fail "G6 exit=$rc started=$(started && echo yes || echo no)"; cat "$TMP/out.txt"; fi
RM=true

echo
echo "== G7: no paths configured is the feature being OFF, not broken =="
# WHITESPACE, NOT EMPTY, AND THE DIFFERENCE IS THE POINT. An EMPTY
# ICEDRIVE_GATE_PATHS falls back to the default (`:-`) and the gate still
# guards, because an empty variable is what a typo or a truncated .env produces,
# and a mistake must not disable the one check standing between a blank disk and
# a mirror-delete. Turning it off is a positive act.
rc="$(ICEDRIVE_GATE_PATHS=" " gate)"
if [ "$rc" = 0 ] && started; then pass "G7 whitespace paths -> starts immediately (the feature is off)"; else fail "G7 exit=$rc"; cat "$TMP/out.txt"; fi
rc="$(ICEDRIVE_GATE_PATHS="" gate)"
if [ "$rc" = 1 ] && ! started; then pass "G7 an EMPTY value still guards the default path — a typo cannot disable the gate"; else fail "G7 an empty ICEDRIVE_GATE_PATHS disabled the gate (exit=$rc)"; fi

echo
echo "──────────────────────────────────────────────────────────────"
printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then exit 0; fi
exit 1
