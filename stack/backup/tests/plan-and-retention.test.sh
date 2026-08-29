#!/usr/bin/env bash
# plan-and-retention.test.sh — the Q1/Q3 contract, exercised rather than reasoned about.
#
# WHY IT IS HERMETIC. Every other backup test in this repo needs the sim
# containers, a Samba share or a real drive, and that is why the C25 defect
# survived: the one property nobody could assert cheaply was "what does a plan
# run LEAVE BEHIND". This needs bash, rsync, tar, zstd, sha256sum and a writable
# temp directory — nothing else — so it runs on the hub, in WSL, and on a CI
# runner, and it runs in seconds.
#
# WHAT IT LOCKS
#   P1  a --plan run writes plan_<ts>/ and NO run_<ts>/
#   P2  a full run writes run_<ts>/ with archives
#   P3  --dry-run is REFUSED, naming --plan (Q3: the alias was removed)
#   P4  plan retention bounds plan_* at BACKUP_PLAN_KEEP, run by the PLAN itself
#   P5  plan retention NEVER touches run_* — the property the whole rename buys
#   P6  a green full run also prunes plan litter
#   P7  BACKUP_PLAN_KEEP=0 is legal; BACKUP_KEEP=0 is still refused
#   P8  the pruner REFUSES a plan_* directory that holds an archive or RUN.json
#   P9  restore.sh exits 3 on a plan_ directory BY NAME, with no log at all
#   P10 restore.sh still exits 3 on a legacy run_<ts> carrying dry_run=1 —
#       twelve of those are on the household's stick and must not read as damage
#   P11 newest_run_with_set cannot return a plan directory
#   P12 full-run retention keeps BACKUP_KEEP good runs and prunes the rest
#
# Usage: bash plan-and-retention.test.sh [--keep-tmp]
#   Exit 0 = every check passed. Any failure exits 1 and names the check.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="$(cd "$HERE/.." && pwd)"
BACKUP_SH="$BACKUP_DIR/backup.sh"
RESTORE_SH="$BACKUP_DIR/restore.sh"
COMMON_SH="$BACKUP_DIR/common.sh"
KEEP_TMP=0
[ "${1:-}" = "--keep-tmp" ] && KEEP_TMP=1

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }

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

SRC="$TMP/src"; mkdir -p "$SRC/docs" "$SRC/photos"
printf 'alpha\n' >"$SRC/docs/a.txt"
printf 'beta\n'  >"$SRC/docs/b.txt"
head -c 4096 /dev/urandom >"$SRC/photos/p.bin"

# write_env TARGET KEEP [PLAN_KEEP] : a minimal, hermetic backup.env.
#   NAGLIGHT_FEED_URL empty            -> the feed is a logged skip, no tracker
#   BACKUP_TARGET_REQUIRE_MOUNT=false  -> the target is a plain directory
#   BACKUP_DRIVE_DEVICES empty         -> drive power is a clean no-op
write_env() {
    local target="$1" keep="$2" plankeep="${3:-}"
    mkdir -p "$target"
    {
        echo "BACKUP_TARGET=$target"
        echo "BACKUP_STAGING=$TMP/staging"
        echo "BACKUP_KEEP=$keep"
        if [ -n "$plankeep" ]; then echo "BACKUP_PLAN_KEEP=$plankeep"; fi
        echo "BACKUP_TARGET_REQUIRE_MOUNT=false"
        echo 'BACKUP_DRIVE_DEVICES=""'
        echo 'NAGLIGHT_FEED_URL=""'
        echo 'BACKUP_WAKE_MAC=""'
        echo 'INGEST_SOURCES=""'
        echo "BACKUP_SOURCES=\"docs=path:$SRC/docs"
        echo "photos=path:$SRC/photos\""
    } >"$TMP/backup.env"
}

# run_backup ENV [ARGS...] -> echoes the exit code; output lands in $TMP/out.txt
run_backup() {
    local env="$1"; shift
    bash "$BACKUP_SH" --config "$env" "$@" >"$TMP/out.txt" 2>&1
    echo $?
}
count_dirs() { find "$1" -mindepth 1 -maxdepth 1 -type d -name "$2" 2>/dev/null | wc -l | tr -d ' '; }

echo "== P1/P2: the prefix says which kind of run wrote the directory =="
T1="$TMP/t1"; write_env "$T1" 7
rc="$(run_backup "$TMP/backup.env" --plan)"
if [ "$rc" = 0 ]; then pass "P1 plan run exits 0"; else fail "P1 plan run exit=$rc"; tail -20 "$TMP/out.txt"; fi
if [ "$(count_dirs "$T1" 'plan_*')" = 1 ]; then pass "P1 wrote exactly one plan_<ts>"; else fail "P1 plan_* count = $(count_dirs "$T1" 'plan_*')"; fi
if [ "$(count_dirs "$T1" 'run_*')" = 0 ]; then pass "P1 wrote NO run_<ts>"; else fail "P1 left a run_* directory behind"; fi
PD="$(find "$T1" -maxdepth 1 -type d -name 'plan_*' | head -1)"
if [ -f "$PD/backup.log" ] && [ -f "$PD/MANIFEST.tsv" ]; then pass "P1 the plan directory holds its evidence (log + header manifest)"; else fail "P1 plan directory is missing backup.log/MANIFEST.tsv"; fi
NARCH="$(find "$PD" -name '*.tar' -o -name '*.tar.zst' | wc -l | tr -d ' ')"
if [ "$NARCH" = 0 ]; then pass "P1 the plan directory holds NO archive"; else fail "P1 a plan directory holds $NARCH archive(s)"; fi
if grep -q 'mode=plan' "$PD/backup.log"; then pass "P1 the log says mode=plan"; else fail "P1 no mode=plan line in the log"; fi

rc="$(run_backup "$TMP/backup.env")"
if [ "$rc" = 0 ]; then pass "P2 full run exits 0"; else fail "P2 full run exit=$rc"; tail -20 "$TMP/out.txt"; fi
if [ "$(count_dirs "$T1" 'run_*')" = 1 ]; then pass "P2 wrote exactly one run_<ts>"; else fail "P2 run_* count = $(count_dirs "$T1" 'run_*')"; fi
RD="$(find "$T1" -maxdepth 1 -type d -name 'run_*' | head -1)"
if [ -f "$RD/RUN.json" ]; then pass "P2 the run directory holds RUN.json"; else fail "P2 no RUN.json"; fi
NDOC="$(find "$RD" -name 'docs.tar*' | wc -l | tr -d ' ')"
if [ "$NDOC" = 1 ]; then pass "P2 the run directory holds the docs archive"; else fail "P2 docs archive missing"; fi

echo
echo "== P3: --dry-run is gone, and says what to type instead =="
rc="$(run_backup "$TMP/backup.env" --dry-run)"
if [ "$rc" != 0 ]; then pass "P3 --dry-run is refused (exit $rc)"; else fail "P3 --dry-run was ACCEPTED — the alias is still alive"; fi
if grep -qi -- '--plan' "$TMP/out.txt"; then pass "P3 the refusal names --plan"; else fail "P3 the refusal does not name the replacement"; tail -3 "$TMP/out.txt"; fi
if [ "$(count_dirs "$T1" 'plan_*')" = 1 ]; then pass "P3 the refused run wrote nothing new"; else fail "P3 the refused run left a directory behind"; fi

echo
echo "== P4/P5: plan retention is bounded, and cannot reach a real run =="
T4="$TMP/t4"; write_env "$T4" 7 2
rc="$(run_backup "$TMP/backup.env")"
if [ "$rc" != 0 ]; then fail "P5 setup: full run exit=$rc"; tail -20 "$TMP/out.txt"; fi
for i in 1 2 3 4 5; do run_backup "$TMP/backup.env" --plan >/dev/null; sleep 1; done
N="$(count_dirs "$T4" 'plan_*')"
if [ "$N" -le 3 ]; then pass "P4 five plan runs left $N plan director(ies) at BACKUP_PLAN_KEEP=2 (<= keep+1)"; else fail "P4 plan litter unbounded: $N directories"; fi
if [ "$(count_dirs "$T4" 'run_*')" = 1 ]; then pass "P5 the real run is untouched by plan retention"; else fail "P5 plan retention removed a run_* directory"; fi

echo
echo "== P6: a green nightly also tidies plan litter =="
T6="$TMP/t6"; write_env "$T6" 7 1
for i in 1 2 3; do run_backup "$TMP/backup.env" --plan >/dev/null; sleep 1; done
before="$(count_dirs "$T6" 'plan_*')"
rc="$(run_backup "$TMP/backup.env")"
after="$(count_dirs "$T6" 'plan_*')"
if [ "$rc" = 0 ] && [ "$after" -lt "$before" ] && [ "$after" -le 1 ]; then
    pass "P6 a full run pruned plan litter $before -> $after at BACKUP_PLAN_KEEP=1"
else
    fail "P6 full-run plan tidy: rc=$rc before=$before after=$after"
fi

echo
echo "== P7: 0 means different things for the two budgets, deliberately =="
T7="$TMP/t7"; write_env "$T7" 7 0
for i in 1 2 3; do run_backup "$TMP/backup.env" --plan >/dev/null; sleep 1; done
if [ "$(count_dirs "$T7" 'plan_*')" -le 1 ]; then pass "P7 BACKUP_PLAN_KEEP=0 keeps only the current plan run"; else fail "P7 PLAN_KEEP=0 left $(count_dirs "$T7" 'plan_*') directories"; fi
T7B="$TMP/t7b"; write_env "$T7B" 0
rc="$(run_backup "$TMP/backup.env")"
if [ "$rc" != 0 ] && grep -q 'BACKUP_KEEP=0' "$TMP/out.txt"; then pass "P7 BACKUP_KEEP=0 is still refused, naming itself"; else fail "P7 BACKUP_KEEP=0 was not refused (exit $rc)"; fi

echo
echo "== P8: the pruner refuses litter that is not litter =="
T8="$TMP/t8"; write_env "$T8" 7 0
run_backup "$TMP/backup.env" --plan >/dev/null; sleep 1
mkdir -p "$T8/plan_19700101_000000"
printf 'not an archive, but named like one\n' >"$T8/plan_19700101_000000/docs.tar"
run_backup "$TMP/backup.env" --plan >/dev/null
if [ -d "$T8/plan_19700101_000000" ]; then
    pass "P8 a plan_ directory holding an archive was NOT pruned"
    if grep -q 'REFUSING to prune' "$TMP/out.txt"; then pass "P8 and the run said so, loudly"; else fail "P8 it was kept silently — the operator would never know"; fi
else
    fail "P8 the pruner deleted a plan_ directory holding an archive"
fi

echo
echo "== P9/P10/P11: restore.sh tells 'no copy here' from 'damaged' =="
PD9="$(find "$T1" -maxdepth 1 -type d -name 'plan_*' | head -1)"
rm -f "$PD9/backup.log"
bash "$RESTORE_SH" --run "$PD9" --set docs --target "$TMP/r9" >"$TMP/r9.txt" 2>&1
rc=$?
if [ "$rc" = 3 ]; then pass "P9 restore.sh exits 3 on a plan_ directory with NO log (name alone is enough)"; else fail "P9 restore.sh exit=$rc, want 3"; tail -5 "$TMP/r9.txt"; fi

LEG="$T1/run_19700102_000000"; mkdir -p "$LEG"
printf 'set\tsource\tarchive\talgo\tarchive_sha256\tfiles\tbytes\treason\texcludes\n' >"$LEG/MANIFEST.tsv"
printf 'config=/x target=/y keep=7 dry_run=1\n' >"$LEG/backup.log"
bash "$RESTORE_SH" --run "$LEG" --set docs --target "$TMP/r10" >"$TMP/r10.txt" 2>&1
rc=$?
if [ "$rc" = 3 ]; then pass "P10 restore.sh still exits 3 on a legacy run_ carrying dry_run=1"; else fail "P10 restore.sh exit=$rc, want 3 — the stick's existing directories would read as DAMAGE"; tail -5 "$TMP/r10.txt"; fi

NEWEST="$(bash -c '. "$1"; newest_run_with_set "$2" docs' _ "$COMMON_SH" "$T1")"
case "$(basename "${NEWEST:-none}")" in
    plan_*) fail "P11 newest_run_with_set returned a PLAN directory: $NEWEST" ;;
    run_*)  pass "P11 newest_run_with_set returned a real run ($(basename "$NEWEST"))" ;;
    *)      fail "P11 newest_run_with_set returned '$NEWEST'" ;;
esac

echo
echo "== P12: full-run retention still bounds real runs =="
T12="$TMP/t12"; write_env "$T12" 2 0
for i in 1 2 3 4; do
    rc="$(run_backup "$TMP/backup.env")"
    if [ "$rc" != 0 ]; then fail "P12 run $i exit=$rc"; tail -10 "$TMP/out.txt"; break; fi
    sleep 1
done
N="$(count_dirs "$T12" 'run_*')"
if [ "$N" = 2 ]; then pass "P12 four runs at BACKUP_KEEP=2 left exactly 2"; else fail "P12 left $N run director(ies), want 2"; fi
WITH="$(find "$T12" -maxdepth 1 -type d -name 'run_*' -exec test -e '{}/RUN.json' ';' -print | wc -l | tr -d ' ')"
if [ "$WITH" = "$N" ]; then pass "P12 every surviving run holds a RUN.json"; else fail "P12 $WITH of $N survivors hold a RUN.json"; fi

echo
echo "──────────────────────────────────────────────────────────────"
printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then exit 0; fi
exit 1
