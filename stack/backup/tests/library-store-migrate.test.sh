#!/usr/bin/env bash
# TC-1069 — execute the real library-store-migrate.sh against a temp tree.
#
# The renames are REAL (python3 os.rename on this filesystem) and the folder
# contents are checked byte for byte afterwards. Only the host's answers are
# mocked: findmnt (which mount a path is on) and docker (is a FileBackup
# container up). One case uses a real second filesystem (/dev/shm) to prove a
# cross-device pair is refused even when findmnt is fooled into calling it one
# mount, and is SKIPPED where no such filesystem exists.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
MIGRATE="$ROOT/stack/backup/library-store-migrate.sh"
PASS=0; FAIL=0; SKIP=0
pass() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$*"; }
skip() { SKIP=$((SKIP + 1)); printf 'SKIP  %s\n' "$*"; }

[ -f "$MIGRATE" ] || { echo "FATAL: missing $MIGRATE" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "FATAL: python3 is required (the script under test renames with it)" >&2; exit 2; }
TMP="$(mktemp -d)"
KEEP_TMP="${KEEP_TMP:-0}"
SHM=""
cleanup() {
    [ -n "$SHM" ] && rm -rf "$SHM"
    [ "$KEEP_TMP" = 1 ] && { printf 'tmp kept: %s\n' "$TMP"; return; }
    rm -rf "$TMP"
}
trap cleanup EXIT
mkdir -p "$TMP/bin"

# findmnt -n -o TARGET --target PATH : the longest MOCK_MOUNTS entry that PATH
# is at or under, else "/". MOCK_SAME_MOUNT=1 answers $TMP/drive for anything.
cat >"$TMP/bin/findmnt" <<'EOF'
#!/usr/bin/env bash
last=""; for arg in "$@"; do last="$arg"; done
if [ "${MOCK_SAME_MOUNT:-0}" = 1 ]; then printf '%s\n' "$MOCK_DRIVE"; exit 0; fi
best="/"
for m in ${MOCK_MOUNTS:-}; do
    case "$last" in "$m"|"$m"/*) [ "${#m}" -gt "${#best}" ] && best="$m" ;; esac
done
printf '%s\n' "$best"
EOF
cat >"$TMP/bin/docker" <<'EOF'
#!/usr/bin/env bash
[ "${MOCK_DOCKER_PS_RC:-0}" = 0 ] || exit "${MOCK_DOCKER_PS_RC}"
[ -n "${MOCK_FB_RUNNING:-}" ] && printf '%s\n' "$MOCK_FB_RUNNING"
exit 0
EOF
chmod +x "$TMP/bin"/*

DRIVE="$TMP/drive"
OLD_B="$DRIVE/library"; OLD_C="$DRIVE/library-changes"; STORE="$DRIVE/library-store"
write_config() {
    cat >"$TMP/backup.env" <<EOF
FILEBACKUP_STORE=$STORE
FILEBACKUP_BACKUP=$OLD_B
FILEBACKUP_CHANGES=$OLD_C
FILEBACKUP_STATE=$TMP/state
LIBRARY_BACKUP_LOCK=$TMP/library-backup.lock
EOF
}
write_config

# A fresh tree: an old store with a manifest, an object, a snapshot, an old
# engine's Temp holding an only copy, and the retired source-side record.
seed_old_store() {
    rm -rf "$DRIVE" "$TMP/state"
    mkdir -p "$OLD_B" "$OLD_C/Snapshot_2026_10_01_03_00_00" "$OLD_C/Temp" "$TMP/state"
    printf 'RelativePath,xxH2Hash\nPhotos/a.jpg,abc\n' >"$OLD_B/MANIFEST.csv"
    printf 'object-bytes' >"$OLD_B/0123456789abcdefghijkl_12.jpg"
    printf 'snapshot-manifest\n' >"$OLD_C/Snapshot_2026_10_01_03_00_00/MANIFEST.csv"
    printf 'only-copy' >"$OLD_C/Temp/staged"
    printf 'old-record\n' >"$TMP/state/MANIFEST.csv"
}
tree_sum() { (cd "$1" && find . -type f -print0 | sort -z | xargs -0 sha256sum) 2>/dev/null | sha256sum | cut -c1-16; }

run_migrate() {
    local name="$1"; shift
    PATH="$TMP/bin:$PATH" MOCK_MOUNTS="${MOCK_MOUNTS-$DRIVE}" MOCK_DRIVE="$DRIVE" \
        bash "$MIGRATE" --config "$TMP/backup.env" "$@" >"$TMP/$name.out" 2>&1
    RUN_RC=$?
}
say() { tr '\n' '|' <"$TMP/$1.out" | cut -c1-400; }

# ── the plan: everything checked, nothing changed ────────────────────────────
seed_old_store
before_b="$(tree_sum "$OLD_B")"; before_c="$(tree_sum "$OLD_C")"
run_migrate plan
if [ "$RUN_RC" = 0 ] && grep -qF "rename $OLD_B -> $STORE/backup" "$TMP/plan.out" \
   && grep -qF "rename $OLD_C -> $STORE/changes" "$TMP/plan.out" \
   && grep -qF 'nothing was changed' "$TMP/plan.out" \
   && [ ! -e "$STORE" ] && [ "$(tree_sum "$OLD_B")" = "$before_b" ] && [ "$(tree_sum "$OLD_C")" = "$before_c" ]; then
    pass "TC-1069 without --apply the plan is printed and nothing is created or moved"
else
    fail "TC-1069 plan rc=$RUN_RC: $(say plan)"
fi

# ── apply: two renames, every byte where it was, nothing else touched ────────
run_migrate apply --apply
if [ "$RUN_RC" = 0 ] && [ ! -e "$OLD_B" ] && [ ! -e "$OLD_C" ] \
   && [ "$(tree_sum "$STORE/backup")" = "$before_b" ] && [ "$(tree_sum "$STORE/changes")" = "$before_c" ] \
   && [ "$(cat "$STORE/changes/Temp/staged")" = only-copy ] \
   && [ "$(cat "$TMP/state/MANIFEST.csv")" = old-record ] \
   && grep -qF 'Nothing was copied or deleted' "$TMP/apply.out" \
   && grep -qF "$STORE/changes/Temp came along" "$TMP/apply.out" \
   && grep -qF "delete the retired source-side record by hand: rm -r $TMP/state" "$TMP/apply.out"; then
    pass "TC-1069 --apply renames both folders into the store intact; Temp and the old record are kept and named"
else
    fail "TC-1069 apply rc=$RUN_RC: $(say apply)"
fi

# ── idempotent ────────────────────────────────────────────────────────────────
after_store="$(tree_sum "$STORE")"
run_migrate again --apply
if [ "$RUN_RC" = 0 ] && grep -qF 'nothing to do.' "$TMP/again.out" \
   && grep -qF "already at $STORE/backup" "$TMP/again.out" \
   && [ "$(tree_sum "$STORE")" = "$after_store" ]; then
    pass "TC-1069 a second --apply finds nothing to do and changes nothing"
else
    fail "TC-1069 rerun rc=$RUN_RC: $(say again)"
fi

# ── a migration killed between its two renames carries on ────────────────────
seed_old_store
mkdir -p "$STORE"
python3 -c 'import os,sys; os.rename(sys.argv[1], sys.argv[2])' "$OLD_B" "$STORE/backup"
run_migrate resume --apply
if [ "$RUN_RC" = 0 ] && [ ! -e "$OLD_C" ] && [ "$(tree_sum "$STORE/changes")" = "$before_c" ] \
   && grep -qF "already at $STORE/backup" "$TMP/resume.out" \
   && grep -qF "renamed $OLD_C -> $STORE/changes" "$TMP/resume.out"; then
    pass "TC-1069 after a kill between the renames, --apply does only the one that is left"
else
    fail "TC-1069 resume rc=$RUN_RC: $(say resume)"
fi

# ── two stores: refuse, touch neither ─────────────────────────────────────────
seed_old_store
mkdir -p "$STORE/backup"; printf 'other\n' >"$STORE/backup/MANIFEST.csv"
run_migrate conflict --apply
if [ "$RUN_RC" = 2 ] && grep -qF "both $OLD_B and $STORE/backup hold data" "$TMP/conflict.out" \
   && [ "$(tree_sum "$OLD_B")" = "$before_b" ] && [ "$(cat "$STORE/backup/MANIFEST.csv")" = other ] \
   && [ -d "$OLD_C" ] && [ ! -e "$STORE/changes" ]; then
    pass "TC-1069 an old and a new store that both hold data are refused (exit 2) and neither is touched"
else
    fail "TC-1069 conflict rc=$RUN_RC: $(say conflict)"
fi

# ── a different mount is a copy: refuse before creating anything ─────────────
seed_old_store
mkdir -p "$STORE"
MOCK_MOUNTS="$DRIVE $STORE" run_migrate crossmount --apply
if [ "$RUN_RC" = 2 ] && grep -qF 'this script never copies' "$TMP/crossmount.out" \
   && [ "$(tree_sum "$OLD_B")" = "$before_b" ] && [ "$(tree_sum "$OLD_C")" = "$before_c" ] \
   && [ ! -e "$STORE/backup" ] && [ ! -e "$STORE/changes" ]; then
    pass "TC-1069 a store on another mount is refused (exit 2): a move there would be a copy"
else
    fail "TC-1069 cross-mount rc=$RUN_RC: $(say crossmount)"
fi
rmdir "$STORE" 2>/dev/null || true

# ── a REAL second filesystem, with findmnt fooled: the device check holds ────
SHM_BASE=/dev/shm
if [ -d "$SHM_BASE" ] && [ -w "$SHM_BASE" ] && SHM="$(mktemp -d "$SHM_BASE/migrate-test.XXXXXX" 2>/dev/null)" \
   && [ "$(stat -c %d "$SHM")" != "$(stat -c %d "$TMP")" ]; then
    seed_old_store
    rm -rf "$OLD_B"; mkdir -p "$SHM/library"; printf 'x\n' >"$SHM/library/MANIFEST.csv"
    cat >"$TMP/backup.env" <<EOF
FILEBACKUP_STORE=$STORE
FILEBACKUP_BACKUP=$SHM/library
FILEBACKUP_CHANGES=$OLD_C
FILEBACKUP_STATE=$TMP/state
LIBRARY_BACKUP_LOCK=$TMP/library-backup.lock
EOF
    MOCK_SAME_MOUNT=1 run_migrate crossdev --apply
    if [ "$RUN_RC" = 2 ] && grep -qF 'this script never copies' "$TMP/crossdev.out" \
       && [ "$(cat "$SHM/library/MANIFEST.csv")" = x ] && [ ! -e "$STORE/backup" ] && [ -d "$OLD_C" ]; then
        pass "TC-1069 a pair on two real filesystems is refused by the device check even when findmnt calls it one mount"
    else
        fail "TC-1069 cross-device rc=$RUN_RC: $(say crossdev)"
    fi
    write_config
else
    skip "TC-1069 cross-device case: no writable second filesystem at /dev/shm on this box"
fi

# ── the backup drive not mounted: everything resolves to / ───────────────────
seed_old_store
MOCK_MOUNTS="" run_migrate rootfs --apply
if [ "$RUN_RC" = 2 ] && grep -qF 'ROOT filesystem' "$TMP/rootfs.out" \
   && [ "$(tree_sum "$OLD_B")" = "$before_b" ] && [ ! -e "$STORE" ]; then
    pass "TC-1069 a store that resolves to the root filesystem is refused"
else
    fail "TC-1069 rootfs rc=$RUN_RC: $(say rootfs)"
fi

# ── a running backup, a running container, or no way to tell ─────────────────
seed_old_store
# `exec sleep`, so the process holding the lock fd IS the one killed below; a
# plain `flock FILE sleep 30` would leave its sleep child holding it.
( exec 8>"$TMP/library-backup.lock"; flock 8; exec sleep 30 ) &
holder=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
    flock -n "$TMP/library-backup.lock" true 2>/dev/null || break
    sleep 0.1
done
run_migrate locked --apply
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
if [ "$RUN_RC" = 2 ] && grep -qF 'a library backup is running' "$TMP/locked.out" && [ -d "$OLD_B" ] && [ ! -e "$STORE" ]; then
    pass "TC-1069 a held library-backup lock is refused and nothing moves"
else
    fail "TC-1069 locked rc=$RUN_RC: $(say locked)"
fi
MOCK_FB_RUNNING=abc123 run_migrate container --apply
CONTAINER_RC=$RUN_RC
MOCK_DOCKER_PS_RC=1 run_migrate nodocker --apply
if [ "$CONTAINER_RC" = 2 ] && grep -qF 'a FileBackup container is running (abc123)' "$TMP/container.out" \
   && [ "$RUN_RC" = 2 ] && grep -qF 'cannot ask docker' "$TMP/nodocker.out" \
   && [ -d "$OLD_B" ] && [ ! -e "$STORE" ]; then
    pass "TC-1069 a running FileBackup container, or not being able to ask, is refused (fail closed)"
else
    fail "TC-1069 container rc=$CONTAINER_RC/$RUN_RC: $(say container) $(say nodocker)"
fi

# ── nothing to migrate: a fresh box, and an empty leftover ───────────────────
rm -rf "$DRIVE" "$TMP/state"; mkdir -p "$DRIVE"
run_migrate fresh --apply
if [ "$RUN_RC" = 0 ] && grep -qF 'nothing to do.' "$TMP/fresh.out" && [ ! -e "$STORE" ]; then
    pass "TC-1069 a fresh box has nothing to migrate and nothing is created"
else
    fail "TC-1069 fresh rc=$RUN_RC: $(say fresh)"
fi
mkdir -p "$OLD_B"
run_migrate leftover --apply
if [ "$RUN_RC" = 0 ] && grep -qF "$OLD_B is EMPTY — nothing to move; remove it by hand (rmdir $OLD_B)" "$TMP/leftover.out" \
   && [ -d "$OLD_B" ] && [ ! -e "$STORE" ]; then
    pass "TC-1069 an empty old folder is named for rmdir, not moved and not deleted"
else
    fail "TC-1069 leftover rc=$RUN_RC: $(say leftover)"
fi

# ── a symlinked old folder: renaming it would move the link, not the store ───
rm -rf "$DRIVE"; mkdir -p "$DRIVE/elsewhere"; printf 'y\n' >"$DRIVE/elsewhere/MANIFEST.csv"
ln -s "$DRIVE/elsewhere" "$OLD_B"
run_migrate symlink --apply
if [ "$RUN_RC" = 2 ] && grep -qF "$OLD_B is a symlink" "$TMP/symlink.out" && [ -L "$OLD_B" ] && [ ! -e "$STORE" ]; then
    pass "TC-1069 a symlinked old folder is refused"
else
    fail "TC-1069 symlink rc=$RUN_RC: $(say symlink)"
fi

printf '%s PASS  %s FAIL  %s SKIP\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ]
