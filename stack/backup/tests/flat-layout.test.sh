#!/usr/bin/env bash
# flat-layout.test.sh — the BACKUP_LAYOUT=flat contract, exercised rather than
# reasoned about. Sibling of plan-and-retention.test.sh, same shape, same
# dependencies: bash, rsync, tar, zstd, sha256sum and a writable temp directory.
# No root, no sim containers, no real drive.
#
# WHY IT EXISTS. On 2026-09-01 the service-state archives moved off the archive
# drive's root and onto the LIBRARY drive, into /srv/library/Configs, with no
# dated directory at all — history there is the library backup's Snapshot_<date>
# series. That change touches the two places this service is least able to test
# in production: WHERE the target is (a folder on a drive, not the drive) and
# WHAT happens to the copy already sitting in it. Both are silent when wrong —
# an unmounted drive that still has a folder path, or a failed run that has just
# deleted the only archive — which is the family of fault this whole service is
# built to refuse.
#
# WHAT IT LOCKS
#   F1  a flat full run writes archives into BACKUP_TARGET itself, no run_<ts>
#   F2  a flat plan run writes ONE plan_latest slot and no archives
#   F3  a second plan run does not add a second slot
#   F4  restore.sh and newest_run_with_set resolve the flat target itself
#   F5  a second full run replaces the copy rather than accumulating
#   F6  a set dropped from BACKUP_SOURCES loses its stale archive at promotion
#   F7  a run that FAILS leaves the previous good copy in place, sets itself
#       aside as .last-failed, and does not block the next run with .incoming
#   F8  BACKUP_KEEP/BACKUP_PLAN_KEEP are not read, and the run says so
#   F9  an unknown BACKUP_LAYOUT is refused rather than defaulted
#   F10 enclosing_mountpoint / backup_target_ready answer for a FOLDER target —
#       the helpers the whole move depends on, and the ones `mountpoint -q`
#       could not provide
#   F11 fstab_mount_for picks the ancestor line, which is what firstboot mounts
#   F12 the dated layout still behaves exactly as it did
#
# Usage: bash flat-layout.test.sh [--keep-tmp]
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

SRC="$TMP/src"; mkdir -p "$SRC/docs" "$SRC/photos"
printf 'alpha\n' >"$SRC/docs/a.txt"
printf 'beta\n'  >"$SRC/docs/b.txt"
head -c 4096 /dev/urandom >"$SRC/photos/p.bin"

TARGET="$TMP/drive/Configs"

# write_env LAYOUT [SOURCES...] : a minimal, hermetic backup.env.
#   BACKUP_TARGET_REQUIRE_MOUNT=false — the target is a plain directory here.
#     The MOUNT half of the change is covered by F10/F11 against the real
#     /proc/self/mountinfo instead, because a test that needed a spare mount
#     would need root and would then never run.
write_env() {
    local layout="$1"; shift
    {
        echo "BACKUP_TARGET=$TARGET"
        [ -n "$layout" ] && echo "BACKUP_LAYOUT=$layout"
        echo "BACKUP_STAGING=$TMP/staging"
        echo "BACKUP_KEEP=7"
        echo "BACKUP_PLAN_KEEP=7"
        echo "BACKUP_TARGET_REQUIRE_MOUNT=false"
        echo 'BACKUP_DRIVE_DEVICES=""'
        echo 'NAGLIGHT_FEED_URL=""'
        echo 'BACKUP_WAKE_MAC=""'
        echo 'INGEST_SOURCES=""'
        if [ $# -gt 0 ]; then
            printf 'BACKUP_SOURCES="%s' "$1"; shift
            for s in "$@"; do printf '\n%s' "$s"; done
            printf '"\n'
        else
            echo "BACKUP_SOURCES=\"docs=path:$SRC/docs"
            echo "photos=path:$SRC/photos\""
        fi
    } >"$TMP/backup.env"
}

run_backup() {
    bash "$BACKUP_SH" --config "$TMP/backup.env" "$@" >"$TMP/out.txt" 2>&1
    echo $?
}

ndirs() { find "$TARGET" -mindepth 1 -maxdepth 1 -type d -name "$1" 2>/dev/null | wc -l | tr -d ' '; }

echo "== F2/F3: a flat plan run gets ONE plan_latest slot =="
write_env flat
rc="$(run_backup --plan)"
[ "$rc" = 0 ] && pass "F2 the plan run exits 0" || { fail "F2 the plan run exited $rc"; head_of "$TMP/out.txt"; }
[ -d "$TARGET/plan_latest" ] && pass "F2 it wrote plan_latest" || fail "F2 there is no plan_latest under $TARGET"
[ -f "$TARGET/plan_latest/MANIFEST.tsv" ] && pass "F2 with a manifest in it" || fail "F2 plan_latest holds no manifest"
if compgen -G "$TARGET/*.tar*" >/dev/null; then fail "F2 the plan run wrote archives"; else pass "F2 and no archives anywhere"; fi
grep -q 'layout=flat' "$TMP/out.txt" && pass "F2 the run banner names the layout" || fail "F2 the banner does not say layout=flat"
rc="$(run_backup --plan)"
[ "$(ndirs 'plan_*')" = 1 ] && pass "F3 a second plan run still leaves exactly one slot" || fail "F3 $(ndirs 'plan_*') plan directories"
[ "$(ndirs 'run_*')" = 0 ] && pass "F3 and never a run_ directory" || fail "F3 a run_ directory appeared"

echo
echo "== F1/F8: a flat full run writes into the target itself =="
rc="$(run_backup)"
[ "$rc" = 0 ] && pass "F1 the full run exits 0" || { fail "F1 the full run exited $rc"; head_of "$TMP/out.txt"; }
if [ -f "$TARGET/docs.tar.zst" ] && compgen -G "$TARGET/photos.tar*" >/dev/null; then
    pass "F1 both archives are in $TARGET itself"
else
    fail "F1 the archives are not at the top of the target"; ls -a "$TARGET" | sed 's/^/      | /'
fi
[ -f "$TARGET/MANIFEST.tsv" ] && [ -f "$TARGET/RUN.json" ] &&
    pass "F1 MANIFEST.tsv and RUN.json are there too" || fail "F1 no manifest/RUN.json at the top"
[ "$(ndirs 'run_*')" = 0 ] && pass "F1 no dated run directory was created" || fail "F1 a run_ directory was created"
[ ! -d "$TARGET/.incoming" ] && pass "F1 .incoming was promoted away" || fail "F1 .incoming is still there"
grep -q 'retention: none' "$TMP/out.txt" && pass "F8 the run says retention did nothing" || fail "F8 no retention note in the log"
grep -q 'BACKUP_KEEP=7 and BACKUP_PLAN_KEEP=7 are NOT read' "$TMP/out.txt" &&
    pass "F8 and names the knobs it is ignoring rather than ignoring them quietly" ||
    fail "F8 the log does not name the ignored knobs"

echo
echo "== F4: the flat target IS the run, to everything that looks for one =="
R="$(bash -c ". '$COMMON_SH'; newest_run_with_set '$TARGET' docs")"
[ "$R" = "$TARGET" ] && pass "F4 newest_run_with_set returns the target ($R)" ||
    fail "F4 newest_run_with_set returned '$R'"
mkdir -p "$TMP/restored"
if bash "$RESTORE_SH" --run "$TARGET" --set docs --target "$TMP/restored" >"$TMP/restore.txt" 2>&1; then
    pass "F4 restore.sh reconstructs a set straight from the flat target"
else
    fail "F4 restore.sh exited $? against the flat target"; head_of "$TMP/restore.txt"
fi
cmp -s "$TMP/restored/a.txt" "$SRC/docs/a.txt" && pass "F4 and the restored bytes match the source" ||
    fail "F4 the restored copy differs from the source"

echo
echo "== F5: a second run REPLACES the copy =="
printf 'alpha2\n' >"$SRC/docs/a.txt"
before="$(find "$TARGET" -maxdepth 1 -type f | wc -l | tr -d ' ')"
rc="$(run_backup)"
after="$(find "$TARGET" -maxdepth 1 -type f | wc -l | tr -d ' ')"
[ "$rc" = 0 ] && pass "F5 the second full run exits 0" || { fail "F5 exited $rc"; head_of "$TMP/out.txt"; }
[ "$before" = "$after" ] && pass "F5 the file count is unchanged ($after) — replaced, not accumulated" ||
    fail "F5 the target went from $before to $after files"
rm -rf "$TMP/restored"; mkdir -p "$TMP/restored"
bash "$RESTORE_SH" --run "$TARGET" --set docs --target "$TMP/restored" >/dev/null 2>&1
grep -q alpha2 "$TMP/restored/a.txt" 2>/dev/null && pass "F5 and the copy is the NEW content" ||
    fail "F5 the target still holds the previous content"

echo
echo "== F6: a set dropped from the table loses its stale archive =="
write_env flat "docs=path:$SRC/docs"
rc="$(run_backup)"
[ "$rc" = 0 ] && pass "F6 the run exits 0 with the smaller table" || { fail "F6 exited $rc"; head_of "$TMP/out.txt"; }
! compgen -G "$TARGET/photos.tar*" >/dev/null &&
    pass "F6 the dropped set's archive was removed at promotion" ||
    fail "F6 photos.tar is still on the drive, looking current"
[ -f "$TARGET/docs.tar.zst" ] && pass "F6 and the set that remains is untouched" ||
    fail "F6 docs.tar.zst went missing"

echo
echo "== F7: a run that fails does NOT cost the copy already on the drive =="
sum_before="$(sha256sum "$TARGET/docs.tar.zst" | cut -d' ' -f1)"
write_env flat "docs=path:$SRC/docs" "gone=path:$TMP/no-such-directory"
rc="$(run_backup)"
[ "$rc" != 0 ] && pass "F7 a missing source still fails the run (exit $rc)" || fail "F7 the run exited 0"
[ -f "$TARGET/docs.tar.zst" ] && pass "F7 the previous good copy is still in place" ||
    fail "F7 the good copy was destroyed by a failed run"
[ "$(sha256sum "$TARGET/docs.tar.zst" | cut -d' ' -f1)" = "$sum_before" ] &&
    pass "F7 and it is byte-identical — it was never touched" ||
    fail "F7 the copy on the drive changed during a failed run"
[ -d "$TARGET/.last-failed" ] && pass "F7 the failed run was set aside as .last-failed" ||
    fail "F7 there is no .last-failed to look at"
[ -f "$TARGET/.last-failed/RUN.json" ] && pass "F7 with its RUN.json as evidence" ||
    fail "F7 .last-failed holds no RUN.json"
[ ! -d "$TARGET/.incoming" ] && pass "F7 and .incoming is not left blocking the next run" ||
    fail "F7 .incoming was left behind"
write_env flat "docs=path:$SRC/docs"
rc="$(run_backup)"
[ "$rc" = 0 ] && pass "F7 the NEXT run succeeds without anyone clearing up" ||
    { fail "F7 the following run exited $rc"; head_of "$TMP/out.txt"; }

echo
echo "== F9: an unknown layout is refused, not defaulted =="
write_env dayted
rc="$(run_backup)"
[ "$rc" != 0 ] && pass "F9 BACKUP_LAYOUT=dayted is refused (exit $rc)" || fail "F9 a typo'd layout ran anyway"
grep -q "BACKUP_LAYOUT='dayted'" "$TMP/out.txt" && pass "F9 and the refusal names the value" ||
    fail "F9 the refusal does not name the bad value"

echo
echo "== F10/F11: the helpers a folder target depends on =="
# shellcheck disable=SC1090
. "$COMMON_SH"
ROOTMP="$(enclosing_mountpoint /definitely/not/a/mountpoint/here)"
[ "$ROOTMP" = "/" ] && pass "F10 enclosing_mountpoint walks up to / for an ordinary path" ||
    fail "F10 enclosing_mountpoint gave '$ROOTMP'"
[ "$(enclosing_mountpoint /)" = "/" ] && pass "F10 and answers / for / itself" || fail "F10 / did not resolve to /"
# `/` rather than $TARGET: whether a temp directory sits on the root filesystem
# or on a tmpfs differs per box, and this must assert the RULE, not the runner.
if backup_target_ready /; then
    fail "F10 backup_target_ready accepted a target the ROOT filesystem carries"
else
    pass "F10 backup_target_ready refuses a target the root filesystem carries"
fi
if backup_target_ready /proc/self/nonexistent-dir; then
    fail "F10 backup_target_ready accepted a path that does not exist"
else
    pass "F10 and refuses one that does not exist"
fi
cat >"$TMP/fstab" <<EOF
# device          mountpoint      type  options
LABEL=Library     /srv/library    auto  nofail 0 0
LABEL=PriBackup   /mnt/backup-drive auto nofail 0 0
EOF
[ "$(fstab_mount_for /srv/library/Configs "$TMP/fstab")" = "/srv/library" ] &&
    pass "F11 fstab_mount_for finds the ANCESTOR line for a folder target" ||
    fail "F11 fstab_mount_for did not resolve /srv/library/Configs"
[ "$(fstab_mount_for /mnt/backup-drive "$TMP/fstab")" = "/mnt/backup-drive" ] &&
    pass "F11 and still finds an exact line for a mountpoint target" ||
    fail "F11 fstab_mount_for missed the exact match"
if fstab_mount_for /some/other/path "$TMP/fstab" >/dev/null; then
    fail "F11 fstab_mount_for invented a mount for an unlisted path"
else
    pass "F11 and says nothing for a path fstab does not cover"
fi

echo
echo "== F12: the dated layout is unchanged =="
TARGET="$TMP/dated"
write_env ""
rc="$(run_backup)"
[ "$rc" = 0 ] && pass "F12 a run with no BACKUP_LAYOUT exits 0" || { fail "F12 exited $rc"; head_of "$TMP/out.txt"; }
[ "$(ndirs 'run_*')" = 1 ] && pass "F12 and writes exactly one dated run directory" ||
    fail "F12 $(ndirs 'run_*') run_ directories"
[ ! -f "$TARGET/MANIFEST.tsv" ] && pass "F12 with nothing loose at the top of the target" ||
    fail "F12 a manifest appeared at the top of a dated target"
R="$(bash -c ". '$COMMON_SH'; newest_run_with_set '$TARGET' docs")"
case "$R" in
    "$TARGET"/run_*) pass "F12 newest_run_with_set still picks the dated run ($(basename "$R"))" ;;
    *)               fail "F12 newest_run_with_set returned '$R'" ;;
esac

echo
printf '%s\n' "──────────────────────────────────────────────────────────────"
printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
