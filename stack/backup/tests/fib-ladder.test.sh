#!/usr/bin/env bash
# fib-ladder.test.sh — the BACKUP_LAYOUT=fib contract, simulated rather than
# reasoned about. Sibling of flat-layout.test.sh, same shape, same dependencies:
# bash, rsync, tar, zstd, sha256sum and a writable temp directory. No root, no
# sim containers, no real drive.
#
# WHY IT EXISTS. On 2026-09-08 the service-state archives moved OFF the library
# drive and back onto the archive drive, because the flat layout's premise had
# failed in a measurable way: all 25 Configs files change every night (they are
# fresh tar+zstd of live service state, so identical input still yields a
# different archive), so all 33 of their rows were superseded into EVERY
# Snapshot_<date> — the config archives were essentially the whole per-night
# snapshot. The replacement keeps 7 dailies plus a CASCADING Fibonacci ladder,
# and its entire contract is about ELAPSED DAYS. A clock that moves at one second
# per second cannot test that, so this drives simulated nights through the
# BACKUP_FIB_TODAY seam instead.
#
# THE LADDER IS A CASCADE. Rung F(n) is refilled from rung F(n-1) every F(n-2)
# days, walked OLDEST FIRST so 233 takes 144's contents before 144 is itself
# overwritten by 89:
#
#     233 <- 144 every 89d      55 <- 34 every 21d      13 <- oldest daily / 5d
#     144 <-  89 every 55d      34 <- 21 every 13d
#      89 <-  55 every 34d      21 <- 13 every  8d
#
# WHAT IT LOCKS
#   B1  a fib full run writes daily/<date> and nothing at the target root
#   B2  a second run on the same day REPLACES that day's copy, not adds to it
#   B3  the daily window is trimmed to BACKUP_FIB_DAILY_KEEP and no deeper
#   B4  the ladder populates from the BOTTOM UP, one rung per night, and does
#       not fill every rung with night one's data
#   B5  THE INVARIANT: content in rung F is never older than F days — which is
#       the Fibonacci identity F(n-1)+F(n-2)=F(n) doing its job
#   B6  and never fresher than the daily window's oldest copy
#   B7  each rung's steady-state refill cadence is exactly F(n-2) days
#   B8  provenance: every CAPTURED date is a night a daily really existed
#   B9  ages increase monotonically UP the ladder — the cascade never lets a
#       higher rung hold fresher state than a lower one
#   B10 a rung inside the daily window, and a one-rung ladder, are both refused
#   B11 restore.sh reconstructs a set straight out of a daily directory
#
# Usage: bash fib-ladder.test.sh [--keep-tmp] [--days N]
#   Exit 0 = every check passed. Any failure exits 1 and names the check.
#
# RUN IT ALONE. Every run this drives takes /run/homehub-backup.lock, which is
# machine-wide by design (the fixed .incoming directory cannot be guarded by an
# exclusive mkdir the way run_<UTC> is). Run this beside flat-layout.test.sh and
# the two suites refuse each other's runs and report failures that are pure
# contention — 5 phantom FAILs in flat-layout, and a seeding run that cannot
# start. That is the lock behaving correctly; it is not a defect in either suite.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="$(cd "$HERE/.." && pwd)"
BACKUP_SH="$BACKUP_DIR/backup.sh"
RESTORE_SH="$BACKUP_DIR/restore.sh"
COMMON_SH="$BACKUP_DIR/common.sh"
KEEP_TMP=0
DAYS=300
while [ $# -gt 0 ]; do
    case "$1" in
        --keep-tmp) KEEP_TMP=1 ;;
        --days)     DAYS="$2"; shift ;;
    esac
    shift
done

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }
head_of() { sed -n '1,12p' "$1" | sed 's/^/      | /'; }

for f in "$BACKUP_SH" "$RESTORE_SH" "$COMMON_SH"; do
    [ -f "$f" ] || { echo "FATAL: $f not found"; exit 2; }
done
for t in rsync tar zstd sha256sum; do
    command -v "$t" >/dev/null 2>&1 || { echo "FATAL: '$t' is required and not on PATH"; exit 2; }
done

TMP="$(mktemp -d)"
cleanup() {
    if [ "$KEEP_TMP" = 1 ]; then echo "tmp kept: $TMP"; return 0; fi
    rm -rf "$TMP"
}
trap cleanup EXIT

SRC="$TMP/src"; mkdir -p "$SRC/svc"
printf 'alpha\n' >"$SRC/svc/a.txt"

TARGET="$TMP/drive/config-history"
DAILY_KEEP=7
SLOTS=(13 21 34 55 89 144 233)
# F(n-2) for each rung — the cadence backup.sh derives from the gaps itself.
CADENCE=(5 8 13 21 34 55 89)

write_env() {
    local keep="${1:-$DAILY_KEEP}" slots="${2:-${SLOTS[*]}}"
    {
        echo "BACKUP_TARGET=$TARGET"
        echo "BACKUP_LAYOUT=fib"
        echo "BACKUP_FIB_DAILY_KEEP=$keep"
        echo "BACKUP_FIB_SLOTS=\"$slots\""
        echo "BACKUP_STAGING=$TMP/staging"
        echo "BACKUP_TARGET_REQUIRE_MOUNT=false"
        echo 'BACKUP_DRIVE_DEVICES=""'
        echo 'NAGLIGHT_FEED_URL=""'
        echo 'BACKUP_WAKE_MAC=""'
        echo 'INGEST_SOURCES=""'
        echo "BACKUP_SOURCES=\"svc=path:$SRC/svc\""
    } >"$TMP/backup.env"
}

run_night() {
    BACKUP_FIB_TODAY="$1" bash "$BACKUP_SH" --config "$TMP/backup.env" >"$TMP/out.txt" 2>&1
    echo $?
}
days_between() { echo $(( ( $(date -u -d "$2" +%s) - $(date -u -d "$1" +%s) ) / 86400 )); }
populated() { local c=0 s; for s in "${SLOTS[@]}"; do [ -f "$TARGET/fib/$s/CAPTURED" ] && c=$((c+1)); done; echo "$c"; }

echo "== B1/B2: one night, then a second run on the same night =="
write_env
D0=2026-01-01
rc="$(run_night "$D0")"
[ "$rc" = 0 ] && pass "B1 the run exits 0" || { fail "B1 the run exited $rc"; head_of "$TMP/out.txt"; }
[ -d "$TARGET/daily/$D0" ] && pass "B1 it wrote daily/$D0" || fail "B1 there is no daily/$D0"
[ -f "$TARGET/daily/$D0/svc.tar.zst" ] && pass "B1 with the archive inside it" || fail "B1 no archive in the daily"
if compgen -G "$TARGET/*.tar*" >/dev/null; then fail "B1 archives leaked to the target root"; else pass "B1 nothing at the target root"; fi
[ "$(find "$TARGET" -mindepth 1 -maxdepth 1 -type d -name 'run_*' | wc -l)" = 0 ] &&
    pass "B1 no run_<ts> directory - that clutter is what this layout replaced" || fail "B1 a run_ directory appeared"
[ ! -d "$TARGET/.incoming" ] && pass "B1 .incoming was promoted away" || fail "B1 .incoming is still there"
roots="$(find "$TARGET" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | tr '\n' ' ')"
[ "$roots" = "daily fib " ] && pass "B1 the target root holds exactly: daily fib" || fail "B1 target root holds: $roots"
rc="$(run_night "$D0")"
[ "$(find "$TARGET/daily" -mindepth 1 -maxdepth 1 -type d | wc -l)" = 1 ] &&
    pass "B2 a second run on the same night leaves ONE daily" || fail "B2 the same night produced more than one daily"

echo
echo "== B4: the ladder climbs from the bottom, one rung a night =="
rm -rf "$TARGET"; write_env
d="$D0"; bootstrap_ok=1; first_night_count=0
for (( i = 0; i < 8; i++ )); do
    run_night "$d" >/dev/null
    got="$(populated)"
    [ "$i" = 0 ] && first_night_count="$got"
    want=$(( i + 1 )); [ "$want" -gt 7 ] && want=7
    [ "$got" = "$want" ] || { bootstrap_ok=0; echo "      night $i: $got rung(s) populated, expected $want"; }
    d="$(date -u -d "$d + 1 day" +%F)"
done
[ "$first_night_count" = 1 ] &&
    pass "B4 night one populates exactly ONE rung, not all seven" ||
    fail "B4 night one populated $first_night_count rungs"
[ "$bootstrap_ok" = 1 ] &&
    pass "B4 and the ladder climbs one rung per night until full" ||
    fail "B4 the bootstrap did not advance one rung per night"
[ "$(cat "$TARGET/fib/13/CAPTURED" 2>/dev/null)" = "$D0" ] &&
    pass "B4 the first rung filled holds night one's daily" ||
    fail "B4 rung 13 holds $(cat "$TARGET/fib/13/CAPTURED" 2>/dev/null), not $D0"

echo
echo "== B3/B5/B6/B7/B8/B9: $DAYS simulated nights =="
rm -rf "$TARGET"; write_env
declare -A last_refill=() gaps=() maxage=() refills=()
viol_max=0; viol_min=0; viol_src=0; viol_mono=0; maxdaily=0
allday=(); d="$D0"
for (( n = 0; n < DAYS; n++ )); do
    # THE SOURCE CHANGES EVERY NIGHT, and that is what makes B12 mean anything.
    # With a constant source every archive is interchangeable, so the whole suite
    # would validate marker choreography while a bug that copied the WRONG rung's
    # files - writing the right CAPTURED beside them - passed every provenance
    # check. Stamping the date into the payload ties contents to markers.
    printf 'state-of-%s\n' "$d" >"$SRC/svc/a.txt"
    rc="$(run_night "$d")"
    if [ "$rc" != 0 ]; then fail "B3 night $d exited $rc"; head_of "$TMP/out.txt"; break; fi
    allday+=("$d")

    cnt=$(find "$TARGET/daily" -mindepth 1 -maxdepth 1 -type d | wc -l)
    (( cnt > maxdaily )) && maxdaily=$cnt

    prev_age=-1
    for s in "${SLOTS[@]}"; do
        cap="$(cat "$TARGET/fib/$s/CAPTURED" 2>/dev/null)"; [ -n "$cap" ] || continue
        ref="$(cat "$TARGET/fib/$s/REFILLED" 2>/dev/null)"
        age="$(days_between "$cap" "$d")"
        (( age > ${maxage[$s]:-0} )) && maxage[$s]=$age

        (( age > s )) && { viol_max=$((viol_max+1)); [ "$viol_max" = 1 ] && echo "      first B5: night $d rung ${s}d holds $cap (${age}d)"; }
        # ONLY ONCE THE DAILY WINDOW HAS FILLED. The bottom rung draws from the
        # OLDEST daily, and for the first week that copy is younger than 6 days
        # simply because nothing older exists yet - the box has not been running
        # long enough. Checking before then asserts something the calendar makes
        # impossible, not something the ladder got wrong.
        if (( n >= DAILY_KEEP )) && (( age < DAILY_KEEP - 1 )); then
            viol_min=$((viol_min+1))
            [ "$viol_min" = 1 ] && echo "      first B6: night $d rung ${s}d holds $cap (${age}d)"
        fi
        (( age < prev_age )) && { viol_mono=$((viol_mono+1)); [ "$viol_mono" = 1 ] && echo "      first B9: night $d rung ${s}d is ${age}d, fresher than the rung below it (${prev_age}d)"; }
        prev_age=$age

        found=0; for x in "${allday[@]}"; do [ "$x" = "$cap" ] && { found=1; break; }; done
        (( found == 0 )) && viol_src=$((viol_src+1))

        if [ -n "$ref" ] && [ "${last_refill[$s]:-}" != "$ref" ]; then
            [ -n "${last_refill[$s]:-}" ] && gaps[$s]="${gaps[$s]:-} $(days_between "${last_refill[$s]}" "$ref")"
            last_refill[$s]="$ref"
            refills[$s]=$(( ${refills[$s]:-0} + 1 ))
        fi
    done
    d="$(date -u -d "$d + 1 day" +%F)"
done

[ "$maxdaily" -le "$DAILY_KEEP" ] &&
    pass "B3 the daily window never exceeded $DAILY_KEEP (peak $maxdaily)" ||
    fail "B3 the daily window reached $maxdaily, over the $DAILY_KEEP budget"
[ "$viol_max" = 0 ] &&
    pass "B5 no rung ever held content older than its label, across $DAYS nights" ||
    fail "B5 $viol_max night/rung pairs held content past their label"
[ "$viol_min" = 0 ] &&
    pass "B6 no rung ever held content fresher than the daily window" ||
    fail "B6 $viol_min night/rung pairs held content too fresh"
[ "$viol_mono" = 0 ] &&
    pass "B9 ages never decrease going up the ladder - the cascade holds" ||
    fail "B9 $viol_mono night/rung pairs were fresher than the rung below"
[ "$viol_src" = 0 ] &&
    pass "B8 every CAPTURED date is a night a daily really existed" ||
    fail "B8 $viol_src CAPTURED dates match no real night"

echo
echo "   rung   cadence seen        want   max age seen / label"
cadence_ok=1
for i in "${!SLOTS[@]}"; do
    s="${SLOTS[$i]}"; want="${CADENCE[$i]}"
    steady="$(echo "${gaps[$s]:-}" | tr ' ' '\n' | grep -v '^$' | sort -u | tr '\n' ',' | sed 's/,$//')"
    printf '   %4sd  %-18s %4s   %s / %s   refills=%s\n' "$s" "${steady:--}" "$want" "${maxage[$s]:-0}" "$s" "${refills[$s]:-0}"
    [ -n "$steady" ] && [ "$steady" != "$want" ] && cadence_ok=0
    # SILENCE IS NOT SUCCESS. A rung that never refilled records no gaps at all,
    # and comparing an empty string against the wanted cadence would pass - which
    # is exactly how a frozen ladder looked healthy before the marker validation
    # went in. Any rung whose cadence fits inside the simulation must have moved.
    if [ "${refills[$s]:-0}" = 0 ] && [ "$want" -lt "$DAYS" ]; then
        echo "      rung ${s}d never refilled in $DAYS nights (cadence ${want}d)"
        cadence_ok=0
    fi
done
[ "$cadence_ok" = 1 ] &&
    pass "B7 every rung's refill cadence is exactly F(n-2) days" ||
    fail "B7 at least one rung did not settle on its F(n-2) cadence"

echo
echo "== B12: a rung's CONTENTS match its CAPTURED marker =="
# End-to-end: restore each populated rung and read the payload back. The archive
# under a rung must be the state of the day its marker names - not merely a
# plausible archive with a plausible marker beside it.
content_ok=1; checked=0
for s in "${SLOTS[@]}"; do
    cap="$(cat "$TARGET/fib/$s/CAPTURED" 2>/dev/null)"; [ -n "$cap" ] || continue
    rm -rf "$TMP/rung"; mkdir -p "$TMP/rung"
    if ! bash "$RESTORE_SH" --run "$TARGET/fib/$s" --set svc --target "$TMP/rung" >"$TMP/rr.txt" 2>&1; then
        echo "      rung ${s}d: restore failed"; content_ok=0; continue
    fi
    got="$(cat "$TMP/rung/a.txt" 2>/dev/null)"
    checked=$(( checked + 1 ))
    [ "$got" = "state-of-$cap" ] || { echo "      rung ${s}d: CAPTURED says $cap but the payload is '$got'"; content_ok=0; }
done
[ "$checked" -ge 5 ] && pass "B12 restored $checked rungs end-to-end" || fail "B12 only $checked rungs could be restored"
[ "$content_ok" = 1 ] &&
    pass "B12 every rung's payload is the state of the day its CAPTURED names" ||
    fail "B12 a rung's contents and provenance disagree"

echo
echo "== B13: the clock moving backwards must not delete tonight's copy =="
rm -rf "$TARGET"; write_env
d="$D0"
for (( i = 0; i < 10; i++ )); do
    printf 'state-of-%s\n' "$d" >"$SRC/svc/a.txt"
    run_night "$d" >/dev/null
    d="$(date -u -d "$d + 1 day" +%F)"
done
printf 'rolled-back\n' >"$SRC/svc/a.txt"
rc="$(run_night "$D0")"           # the clock jumps back to the very first date
[ -d "$TARGET/daily/$D0" ] &&
    pass "B13 the daily this run just made still exists after the trim" ||
    fail "B13 the run deleted the copy it had just taken"
[ "$rc" != 0 ] &&
    pass "B13 and the run reports RED rather than a silent green" ||
    fail "B13 the run exited 0 despite refusing to prune its own copy"
grep -q 'refusing to prune daily' "$TMP/out.txt" &&
    pass "B13 naming what it refused and why" || { fail "B13 nothing in the log names the refusal"; head_of "$TMP/out.txt"; }

echo
echo "== B14: unreadable and future markers =="
rm -rf "$TARGET"; write_env
run_night "$D0" >/dev/null
printf 'not-a-date\n' >"$TARGET/fib/13/REFILLED"
rc="$(run_night "$(date -u -d "$D0 + 1 day" +%F)")"
grep -q 'unreadable REFILLED marker' "$TMP/out.txt" &&
    pass "B14 a corrupt REFILLED is detected rather than fed to date -d" ||
    { fail "B14 the corrupt marker was not reported"; head_of "$TMP/out.txt"; }
[ "$rc" != 0 ] && pass "B14 and the night is reported RED" || fail "B14 the corrupt marker produced a green run"

rm -rf "$TARGET"; write_env
run_night "$D0" >/dev/null
printf '2099-01-01\n' >"$TARGET/fib/13/REFILLED"
rc="$(run_night "$(date -u -d "$D0 + 1 day" +%F)")"
grep -q 'which is AFTER today' "$TMP/out.txt" &&
    pass "B14 a future REFILLED is caught instead of freezing the rung forever" ||
    { fail "B14 the future marker was not caught"; head_of "$TMP/out.txt"; }

echo
echo "== B10: bad ladders are refused at config time =="
write_env 7 "5 13"
rc="$(run_night 2026-06-01)"
[ "$rc" != 0 ] && pass "B10 a 5d rung under a 7d window is refused" || fail "B10 the run accepted a rung inside the window"
grep -q 'BACKUP_FIB_SLOTS has a 5-day rung' "$TMP/out.txt" &&
    pass "B10 and says which rung and why" || { fail "B10 the refusal does not name the rung"; head_of "$TMP/out.txt"; }
write_env 7 "13"
rc="$(run_night 2026-06-01)"
[ "$rc" != 0 ] && pass "B10 a one-rung ladder is refused - nothing to cascade from" || fail "B10 a one-rung ladder was accepted"

write_env 00
rc="$(run_night 2026-06-01)"
[ "$rc" != 0 ] &&
    pass "B10 BACKUP_FIB_DAILY_KEEP=00 is refused, not read as zero" ||
    { fail "B10 '00' passed the zero guard - the prune would delete every daily"; head_of "$TMP/out.txt"; }

write_env 7 "20 21"
rc="$(run_night 2026-06-01)"
[ "$rc" != 0 ] &&
    pass "B10 an ascending but UNSAFE ladder (20 21) is refused" ||
    { fail "B10 '20 21' accepted - its bottom rung would hold 25d against a 20d label"; head_of "$TMP/out.txt"; }
grep -q 'past its own label' "$TMP/out.txt" &&
    pass "B10 and explains which rung could not hold its label" || fail "B10 the refusal does not explain the ceiling"

# A leading zero is octal to bash arithmetic ('013' would mean 11, '08' aborts),
# so slots are forced to base 10 - and then normalised back into the list, since
# the same strings name the rung directories on the drive.
rm -rf "$TARGET"; write_env 7 "013 21 34"
rc="$(run_night 2026-06-01)"
[ "$rc" = 0 ] && pass "B10 a leading-zero rung is normalised, not read as octal" ||
    { fail "B10 '013 21 34' was refused (exit $rc)"; head_of "$TMP/out.txt"; }
[ -d "$TARGET/fib/13" ] && [ ! -d "$TARGET/fib/013" ] &&
    pass "B10 and the rung on disk is named 13, matching its schedule" ||
    fail "B10 the rung directory is $(ls "$TARGET/fib" 2>/dev/null | tr '
' ' ')"

echo
echo "== B15: the target must be a folder ON a drive, not the drive itself =="
# Pointed at a mountpoint, this layout would create and then MANAGE daily/ and
# fib/ at the top of that disk. It refuses. Safe to run: the check sits right
# after a no-op mkdir of an existing directory and nothing is written first.
MNT="$(bash -c ". '$COMMON_SH'; enclosing_mountpoint '$TMP'")"
if [ -n "$MNT" ]; then
    write_env
    sed -i "s#^BACKUP_TARGET=.*#BACKUP_TARGET=$MNT#" "$TMP/backup.env"
    rc="$(run_night 2026-06-01)"
    [ "$rc" != 0 ] && pass "B15 a mountpoint target ($MNT) is refused" ||
        { fail "B15 the run accepted a whole drive as its target"; head_of "$TMP/out.txt"; }
    grep -q 'IS the mountpoint' "$TMP/out.txt" &&
        pass "B15 and explains that it manages the tree it is given" ||
        { fail "B15 the refusal does not explain itself"; head_of "$TMP/out.txt"; }
else
    echo "      (skipped: could not resolve the enclosing mountpoint of $TMP)"
fi

echo
echo "== B16: strays under daily/ are neither counted nor pruned =="
rm -rf "$TARGET"; write_env
d="$D0"
for (( i = 0; i < 8; i++ )); do
    printf 'state-of-%s
' "$d" >"$SRC/svc/a.txt"
    run_night "$d" >/dev/null
    d="$(date -u -d "$d + 1 day" +%F)"
done
mkdir -p "$TARGET/daily/NOT-A-DATE"; printf 'precious
' >"$TARGET/daily/NOT-A-DATE/keepme"
before="$(find "$TARGET/daily" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*' | wc -l)"
printf 'state-of-%s
' "$d" >"$SRC/svc/a.txt"
run_night "$d" >/dev/null
[ -f "$TARGET/daily/NOT-A-DATE/keepme" ] &&
    pass "B16 a non-dated directory under daily/ survives the prune untouched" ||
    fail "B16 the prune deleted a directory this service did not write"
grep -q 'ignoring non-dated entries' "$TMP/out.txt" &&
    pass "B16 and the run names what it is ignoring" || { fail "B16 the stray is not reported"; head_of "$TMP/out.txt"; }
after="$(find "$TARGET/daily" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*' | wc -l)"
[ "$after" = "$DAILY_KEEP" ] &&
    pass "B16 and the stray did not displace a real daily from the window" ||
    fail "B16 the window holds $after dated cop(ies), expected $DAILY_KEEP (was $before)"

echo
echo "== B11: restore straight out of a daily =="
rm -rf "$TARGET"; write_env
rc="$(run_night 2026-03-01)"
[ "$rc" = 0 ] && pass "B11 the seeding run exits 0" || { fail "B11 the seeding run exited $rc"; head_of "$TMP/out.txt"; }
mkdir -p "$TMP/restored"
if bash "$RESTORE_SH" --run "$TARGET/daily/2026-03-01" --set svc --target "$TMP/restored" >"$TMP/restore.txt" 2>&1; then
    pass "B11 restore.sh reconstructs a set from a daily directory"
else
    fail "B11 restore.sh exited $? against a daily"; head_of "$TMP/restore.txt"
fi
cmp -s "$TMP/restored/a.txt" "$SRC/svc/a.txt" && pass "B11 and the restored bytes match the source" ||
    fail "B11 the restored copy differs from the source"

echo
echo "-----------------------------------------------------------"
printf 'PASS %d   FAIL %d\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ] || exit 1
