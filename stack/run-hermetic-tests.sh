#!/usr/bin/env bash
# run-hermetic-tests.sh — every test in stack/ that needs nothing but a shell.
#
# WHAT "HERMETIC" MEANS HERE, precisely: no docker daemon, no VM, no Samba, no
# network, no real drive. A temp directory, and for two of them a mock `docker`
# on PATH. That is the whole point — this repo's expensive suites (the sim
# containers, the FileBackup drills, the two-VM gate) are the reason its cheap
# properties went unasserted for months, because there was nowhere to put a check
# that costs a second.
#
# It runs on the hub, in WSL, and on a CI runner, and it is what
# `tests/test_hermetic_shell_suites.py` drives so the same checks gate a PR.
#
# Requires: bash, tar, gzip, zstd, rsync, sha256sum, gpg, awk, find, flock.
# Refuses to run as root (one suite stashes and restores paths under $HOME, and
# doing that to /root in a test is not a risk worth taking).
#
# Usage: run-hermetic-tests.sh [--list]
# Exit 0 = every suite passed. 1 = at least one failed. 2 = preconditions.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SUITES=(
    "backup/tests/plan-and-retention.test.sh"
    "backup/tests/flat-layout.test.sh"
    "tracker/tests/defs-guard.test.sh"
    "provision/tests/restore-volumes.test.sh"
    "icedrive/tests/profile.test.sh"
    "remote-ui/tests/icedrive-gate.test.sh"
)

if [ "${1:-}" = "--list" ]; then
    printf '%s\n' "${SUITES[@]}"
    exit 0
fi

[ "$(id -u)" != 0 ] || { echo "REFUSED: run as an ordinary user, not root (profile.test.sh stashes paths under \$HOME)" >&2; exit 2; }
missing=""
for t in bash tar gzip zstd rsync sha256sum gpg awk find; do
    command -v "$t" >/dev/null 2>&1 || missing="${missing:+$missing }$t"
done
[ -z "$missing" ] || { echo "REFUSED: missing tool(s): $missing" >&2; exit 2; }

TOTAL_PASS=0; TOTAL_FAIL=0; FAILED_SUITES=""
for s in "${SUITES[@]}"; do
    printf '\n═══ %s ═══\n' "$s"
    if [ ! -f "$HERE/$s" ]; then
        echo "MISSING: $HERE/$s"
        TOTAL_FAIL=$((TOTAL_FAIL + 1)); FAILED_SUITES="${FAILED_SUITES:+$FAILED_SUITES }$s"
        continue
    fi
    out="$(cd "$HERE/$(dirname "$s")" && bash "$(basename "$s")" 2>&1)"
    rc=$?
    # The last line of every suite is "<n> PASS  <n> FAIL".
    tally="$(printf '%s\n' "$out" | grep -E '^[0-9]+ PASS' | tail -1)"
    p="$(printf '%s' "$tally" | awk '{print $1+0}')"
    f="$(printf '%s' "$tally" | awk '{print $3+0}')"
    TOTAL_PASS=$((TOTAL_PASS + p)); TOTAL_FAIL=$((TOTAL_FAIL + f))
    if [ "$rc" -ne 0 ] || [ "${f:-0}" -ne 0 ]; then
        FAILED_SUITES="${FAILED_SUITES:+$FAILED_SUITES }$s"
        # Print the whole suite when it fails; the passing case stays quiet so
        # the summary is readable.
        printf '%s\n' "$out"
    else
        printf '%s\n' "$out" | grep -E '^(FAIL|SKIP)' || true
        printf '  %s\n' "$tally"
    fi
done

printf '\n══════════════════════════════════════════════════════════════\n'
printf '%s PASS  %s FAIL across %s suite(s)\n' "$TOTAL_PASS" "$TOTAL_FAIL" "${#SUITES[@]}"
if [ -n "$FAILED_SUITES" ]; then
    printf 'failed: %s\n' "$FAILED_SUITES"
    exit 1
fi
exit 0
