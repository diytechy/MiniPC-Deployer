#!/usr/bin/env bash
# provision-mounts.test.sh — the ext4 half of provision-mounts.sh, against a fake
# fstab and stubbed system tools. No root, no real mount.
#
# WHAT IT LOCKS
#   M1  an ext4 row is appended to an fstab that has none
#   M2  a stale NTFS row is replaced once the drive reads as ext4
#   M3  a stale row is LEFT ALONE while the drive still reads as something else
#   M4  an `auto` row in the fragment never replaces an existing row
#   M5  a mounted ext4 volume gets its baseline, at the root only
#   M6  a mounted ext4 volume with no baseline row is reported, not passed
#   M7  an NTFS volume is not given an ext4 baseline
#   M8  a replace that cannot land says so, and leaves the row and no temp file
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../provision-mounts.sh"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
FAILS=0
ok()  { echo "ok   $1"; }
bad() { echo "FAIL $1"; FAILS=$((FAILS + 1)); }

mkdir -p "$W/bin"
# BLKID_TYPE is what the drive reads as; FS_TYPE is what findmnt reports mounted.
cat > "$W/bin/blkid" <<'STUB'
#!/bin/sh
case "$1" in
    -L) echo "/dev/fake-$2"; exit 0 ;;
    -c) echo "${BLKID_TYPE:-ext4}"; exit 0 ;;
esac
exit 2
STUB
cat > "$W/bin/findmnt" <<'STUB'
#!/bin/sh
case "$*" in
    *FSTYPE*)  echo "${FS_TYPE:-ext4}" ;;
    *OPTIONS*) echo "rw,noatime" ;;
esac
STUB
cat > "$W/bin/mountpoint" <<'STUB'
#!/bin/sh
exit 0
STUB
for t in chown chmod setfacl; do
    printf '#!/bin/sh\necho "%s $*" >> "$CALLS"\n' "$t" > "$W/bin/$t"
done
for t in systemctl udevadm; do printf '#!/bin/sh\nexit 0\n' > "$W/bin/$t"; done
chmod +x "$W"/bin/*

run() {
    PATH="$W/bin:$PATH" FSTAB="$W/fstab" DRIVE_BASELINE="$W/baseline" CALLS="$W/calls" \
        bash "$SCRIPT" --fragment "$W/fragment" > "$W/out" 2>&1
}
reset() {
    : > "$W/calls"
    printf 'UUID=abc / ext4 defaults 0 1\n' > "$W/fstab"
    printf '# mountpoint\tuid\tgid\tmode\tacl\n/srv/x\t0\t3000\t2775\td:u::rwx,d:g::rwx,d:o::rx\n' > "$W/baseline"
    printf 'LABEL=X /srv/x ext4 nofail,noatime 0 2\n' > "$W/fragment"
}

# M1
reset; BLKID_TYPE=ext4 FS_TYPE=ext4 run
grep -q '^LABEL=X /srv/x ext4 ' "$W/fstab" && ok M1 || bad "M1: row not appended: $(cat "$W/out")"

# M2
reset; printf 'LABEL=X /srv/x auto uid=0,gid=3000,umask=0002,nofail 0 0\n' >> "$W/fstab"
BLKID_TYPE=ext4 FS_TYPE=ext4 run
if grep -q '^LABEL=X /srv/x ext4 ' "$W/fstab" && ! grep -q 'umask=' "$W/fstab" \
   && grep -q '^UUID=abc / ext4' "$W/fstab" && [ "$(grep -c '/srv/x' "$W/fstab")" = 1 ]; then
    ok M2
else
    bad "M2: stale row not replaced cleanly: $(cat "$W/fstab")"
fi

# M3
reset; printf 'LABEL=X /srv/x auto uid=0,gid=3000,umask=0002,nofail 0 0\n' >> "$W/fstab"
BLKID_TYPE=ntfs FS_TYPE=fuseblk run
grep -q 'umask=0002' "$W/fstab" && grep -q 'row left alone' "$W/out" && ok M3 || bad "M3: an NTFS drive lost its row: $(cat "$W/fstab")"

# M4
reset; printf 'LABEL=X /srv/x ext4 nofail 0 2\n' >> "$W/fstab"
printf 'LABEL=X /srv/x auto uid=1,gid=1,umask=0027,nofail 0 0\n' > "$W/fragment"
BLKID_TYPE=ext4 FS_TYPE=ext4 run
grep -q '^LABEL=X /srv/x ext4 nofail 0 2$' "$W/fstab" && ok M4 || bad "M4: an auto row replaced a real one: $(cat "$W/fstab")"

# M5
reset; BLKID_TYPE=ext4 FS_TYPE=ext4 run; rc=$?
if [ "$rc" = 0 ] && grep -qx 'chown 0:3000 /srv/x' "$W/calls" && grep -qx 'chmod 2775 /srv/x' "$W/calls" \
   && grep -qx 'setfacl -m d:u::rwx,d:g::rwx,d:o::rx /srv/x' "$W/calls" && ! grep -q -- '-R' "$W/calls"; then
    ok M5
else
    bad "M5: rc=$rc calls: $(cat "$W/calls")"
fi

# M6
reset; printf '/srv/other\t0\t0\t0755\td:u::rwx,d:g::rx,d:o::rx\n' > "$W/baseline"
BLKID_TYPE=ext4 FS_TYPE=ext4 run; rc=$?
[ "$rc" = 1 ] && grep -q 'no row for /srv/x' "$W/out" && ok M6 || bad "M6: rc=$rc out: $(cat "$W/out")"

# M7
reset; printf 'LABEL=X /srv/x auto uid=65532,gid=65532,umask=0027,nofail 0 0\n' > "$W/fragment"
BLKID_TYPE=ntfs FS_TYPE=ntfs3 run
[ ! -s "$W/calls" ] || ! grep -q 'setfacl' "$W/calls" && ok M7 || bad "M7: an NTFS volume got a baseline: $(cat "$W/calls")"

# M8
reset; printf 'LABEL=X /srv/x auto uid=0,gid=3000,umask=0002,nofail 0 0
' >> "$W/fstab"
printf '#!/bin/sh
exit 1
' > "$W/bin/mv"; chmod +x "$W/bin/mv"
BLKID_TYPE=ext4 FS_TYPE=ext4 run
rm -f "$W/bin/mv"
if grep -q 'umask=0002' "$W/fstab" && grep -q 'could not replace' "$W/out"    && ! grep -q 'replaced the stale' "$W/out" && [ -z "$(ls "$W"/fstab.* 2>/dev/null)" ]; then
    ok M8
else
    bad "M8: $(cat "$W/out")"
fi

echo "FAILURES=$FAILS"
[ "$FAILS" = 0 ]
