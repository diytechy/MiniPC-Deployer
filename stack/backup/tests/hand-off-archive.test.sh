#!/usr/bin/env bash
# hand-off-archive.test.sh — backup.sh hands its output to the drive's owner on ext4
# and to nobody anywhere else. The function is lifted from the shipping script and
# run against stubbed tools, so no root and no real drive.
#
# WHAT IT LOCKS
#   H1  on ext4, the archive goes to the owner of the MOUNT ROOT, recursively
#   H2  on any other filesystem nothing is chowned: ownership there is synthesized
#   H3  a target on the root filesystem is never walked
#   H4  a failed chown warns and does not fail the run
#   H5  the EXIT trap runs it before the drives are allowed to spin down
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../backup.sh"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
FAILS=0
ok()  { echo "ok   $1"; }
bad() { echo "FAIL $1"; FAILS=$((FAILS + 1)); }

sed -n '/^hand_off_archive() {$/,/^}$/p' "$SCRIPT" > "$W/fn.sh"
[ -s "$W/fn.sh" ] || { echo "FAIL could not lift hand_off_archive"; exit 1; }

mkdir -p "$W/bin" "$W/target"
cat > "$W/bin/findmnt" <<'STUB'
#!/bin/sh
echo "${FS_TYPE:-ext4}"
STUB
cat > "$W/bin/stat" <<'STUB'
#!/bin/sh
echo 65532:65532
STUB
cat > "$W/bin/chown" <<'STUB'
#!/bin/sh
echo "chown $*" >> "$CALLS"
exit "${CHOWN_RC:-0}"
STUB
chmod +x "$W"/bin/*

call() {
    : > "$W/calls"
    (
        export PATH="$W/bin:$PATH" CALLS="$W/calls"
        warn() { echo "WARN: $*"; }
        TARGET_MOUNT="$1"
        BACKUP_TARGET="$W/target"
        # shellcheck disable=SC1091
        . "$W/fn.sh"
        hand_off_archive
    ) > "$W/out" 2>&1
}

# H1
FS_TYPE=ext4 call /mnt/backup-drive; rc=$?
[ "$rc" = 0 ] && grep -qx "chown -R 65532:65532 $W/target" "$W/calls" && ok H1 || bad "H1: rc=$rc calls: $(cat "$W/calls")"
# H2
FS_TYPE=fuseblk call /mnt/backup-drive; rc=$?
[ "$rc" = 0 ] && [ ! -s "$W/calls" ] && ok H2 || bad "H2: rc=$rc calls: $(cat "$W/calls")"
# H3
FS_TYPE=ext4 call /; rc=$?
[ "$rc" = 0 ] && [ ! -s "$W/calls" ] && ok H3 || bad "H3: rc=$rc calls: $(cat "$W/calls")"
# H4
FS_TYPE=ext4 CHOWN_RC=1 call /mnt/backup-drive; rc=$?
[ "$rc" = 0 ] && grep -q 'WARN: could not hand' "$W/out" && ok H4 || bad "H4: rc=$rc out: $(cat "$W/out")"
# H5
grep -qx "trap 'quiesce_restore; hand_off_archive; drive_power_restore' EXIT" "$SCRIPT" && ok H5 || bad "H5: the EXIT trap does not run the hand-off before the drive restore"

echo "FAILURES=$FAILS"
[ "$FAILS" = 0 ]
