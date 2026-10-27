#!/usr/bin/env bash
# scratch-fs.root-test.sh - the scratch volume on a REAL ext4 with project quotas
# (SR-102..SR-104, SR-107; TC-1376).
#
# A LABELLED ROOT SUITE, NOT A HERMETIC ONE. It needs root, loop devices,
# device-mapper/LVM, mkfs.ext4, util-linux mount/setpriv and python3, and a
# kernel whose ext4 supports quotas (CONFIG_QFMT_V2). The WSL2 kernel does not
# have QFMT_V2, so on WSL it SKIPS and says so; run it
#   * under the hub's own kernel in QEMU: stack/scratch/tests/run-root-suite-in-qemu.sh
#   * or on the hub itself before the volume is created (loop files under /tmp,
#     a throwaway VG on them, everything removed on exit): sudo bash <this file>
# Nothing outside its own temp directory, its own loop devices and its own VG
# (`hhscratchtest`) is touched.
#
# Scale: the real volume is 18 GiB with 8/2/6 GiB budgets and a 2 GiB floor;
# here the same code runs on a 640 MiB LV with 200/60/300 MiB budgets and a
# 64 MiB floor, so filling every budget takes seconds.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(cd "$HERE/.." && pwd)"
DOOR="$(cd "$HERE/../../door" && pwd)"
PY="${PYTHON:-python3}"
PASS=0 FAIL=0
ok()  { PASS=$((PASS + 1)); echo "PASS $*"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL $*"; }
skip() { echo "SKIP $*"; exit 0; }

[ "$(id -u)" = 0 ] || skip "needs root"
for t in losetup mkfs.ext4 pvcreate vgcreate lvcreate blkid dumpe2fs mount umount setpriv "$PY"; do
    command -v "$t" >/dev/null 2>&1 || skip "needs $t"
done
export DM_DISABLE_UDEV=1 HOMEHUB_MONITOR_OVERRIDE_FILE=/nonexistent
W="$(mktemp -d /tmp/scratch-root-test.XXXXXX)"
R="$W/root"; MP="$R/srv/scratch"
mkdir -p "$R/etc" /dev/disk/by-uuid
: >"$R/etc/fstab"
LOOPS=() MADE_LINKS=()
cleanup() {
    umount -l "$MP" 2>/dev/null; umount -l "$W/other" 2>/dev/null
    vgchange -an hhscratchtest >/dev/null 2>&1; vgremove -ff -y hhscratchtest >/dev/null 2>&1
    for l in "${LOOPS[@]}"; do losetup -d "$l" 2>/dev/null; done
    for l in "${MADE_LINKS[@]}"; do rm -f "$l"; done
    rm -rf "$W"
}
trap cleanup EXIT

# Kernel support first: a 32 MiB ext4 with quota,project must mount with prjquota.
truncate -s 32M "$W/probe.img"
mkfs.ext4 -q -O quota,project -E quotatype=prjquota "$W/probe.img" >/dev/null 2>&1
mkdir -p "$W/probe"
if ! mount -o loop,prjquota "$W/probe.img" "$W/probe" 2>/dev/null; then
    skip "this kernel cannot mount ext4 with project quotas (no CONFIG_QFMT_V2?) - $(uname -r)"
fi
umount "$W/probe"

# udev makes /dev/disk/by-uuid on a real host; a bare initramfs has no udev.
link_uuid() {  # DEVICE
    local u; u="$(blkid -p -s UUID -o value "$1")"
    [ -e "/dev/disk/by-uuid/$u" ] || { ln -s "$(readlink -f "$1")" "/dev/disk/by-uuid/$u"; MADE_LINKS+=("/dev/disk/by-uuid/$u"); }
    printf '%s' "$u"
}

# A throwaway VG on a loop file, standing in for ubuntu-vg.
truncate -s 1G "$W/pv.img"
PV="$(losetup -f --show "$W/pv.img")"; LOOPS+=("$PV")
pvcreate -q "$PV" >/dev/null && vgcreate -q hhscratchtest "$PV" >/dev/null || { bad "could not make the test VG"; exit 1; }

cat >"$W/env" <<'EOF'
HOMEHUB_SCRATCH_SIZE_MIB=640
HOMEHUB_SCRATCH_VG_RESERVE_MIB=100
HOMEHUB_SCRATCH_FREE_FLOOR_MIB=64
HOMEHUB_SCRATCH_MIN_QUOTA_MIB=16
HOMEHUB_SCRATCH_QUOTA_DOOR_MIB=200
HOMEHUB_SCRATCH_QUOTA_MEETINGS_MIB=60
HOMEHUB_SCRATCH_QUOTA_CLIPS_MIB=40
HOMEHUB_SCRATCH_QUOTA_OTTERTUBE_MIB=400
EOF
hs() { "$PY" "$SCRATCH/homehub_scratch.py" --env-file "$W/env" --root "$R" --vg hhscratchtest --mount-with mount "$@"; }
nocap() { setpriv --bounding-set -sys_resource --inh-caps -sys_resource "$@"; }

# 1 a VG that cannot keep its reserve: STOP, nothing created
{ cat "$W/env"; echo HOMEHUB_SCRATCH_VG_RESERVE_MIB=900; } >"$W/env.big"   # 640 + 900 > the 1 GiB VG
"$PY" "$SCRATCH/homehub_scratch.py" --env-file "$W/env.big" --root "$R" --vg hhscratchtest --mount-with mount ensure >"$W/stop.log" 2>&1
rc=$?
if [ "$rc" = 3 ] && ! lvs hhscratchtest/hub-scratch >/dev/null 2>&1; then ok "1 capacity: STOP (exit 3), no LV created"
else bad "1 capacity: rc=$rc $(tail -2 "$W/stop.log")"; fi

# 2 ensure: LV, ext4 with quota+project, fstab line, mount 000 underneath, three projects
hs ensure >"$W/ensure.log" 2>&1 || bad "2 ensure failed: $(tail -3 "$W/ensure.log")"
DEV="$(lvs --noheadings -o lv_path hhscratchtest/hub-scratch | tr -d ' ')"
UUID="$(link_uuid "$DEV")"
echo "HOMEHUB_SCRATCH_UUID=$UUID" >>"$W/env"
under="$(umount "$MP" && stat -c %a "$MP"; mount -T "$R/etc/fstab" "$MP")"
feat="$(dumpe2fs -h "$DEV" 2>/dev/null | grep '^Filesystem features')"
opts="$(findmnt -no OPTIONS "$MP")"
if grep -q "^UUID=$UUID $MP ext4 .*prjquota" "$R/etc/fstab" && [ "$under" = 0 ] \
   && [[ "$feat" == *quota* && "$feat" == *project* ]] && [[ ",$opts," == *,prjquota,* ]]; then
    ok "2 ensure: LV + ext4(quota,project) + fstab by UUID + prjquota mount + bare mount point 000"
else bad "2 ensure: under=$under opts=$opts feat=$feat"; fi
report="$(grep '^{' "$W/ensure.log" | tail -1)"
echo "     $report"

# 3 budgets sized from the usable space: ottertube reduced first, floor kept
read -r usable door meet ott free_full < <(printf '%s' "$report" | "$PY" -c '
import json,sys; d=json.load(sys.stdin); q=d["quota_mib"]
print(d["usable_mib"], q["door"], q["meetings"], q["ottertube"], d["free_at_full_mib"])')
if [ "$door" = 200 ] && [ "$meet" = 60 ] && [ "$ott" -lt 400 ] && [ "$free_full" -ge 64 ]; then
    ok "3 budgets: usable ${usable} MiB -> door 200, meetings 60, ottertube ${ott} (reduced from 400), ${free_full} MiB free at full"
else bad "3 budgets: $report"; fi

# 4 idempotent: a second ensure changes nothing
hs ensure >"$W/ensure2.log" 2>&1
if grep -q '"actions": \[\]' "$W/ensure2.log"; then ok "4 a second ensure has no actions"
else bad "4 second ensure acted: $(grep -o '"actions": \[[^]]*\]' "$W/ensure2.log")"; fi

# 5 the guard: refused WITH CAP_SYS_RESOURCE, accepted without it
g_cap="$(hs guard --writer door 2>&1)"; rc_cap=$?
g_no="$(nocap "$PY" "$SCRATCH/homehub_scratch.py" --env-file "$W/env" --root "$R" guard --writer door 2>&1)"; rc_no=$?
if [ "$rc_cap" = 1 ] && [[ "$g_cap" == *CAP_SYS_RESOURCE* ]] && [ "$rc_no" = 0 ]; then
    ok "5 guard: refuses a writer holding CAP_SYS_RESOURCE, passes one without it"
else bad "5 guard: cap rc=$rc_cap '$g_cap' / nocap rc=$rc_no '$g_no'"; fi
guard_nocap() { nocap "$PY" "$SCRATCH/homehub_scratch.py" --env-file "$W/env" --root "$R" guard --writer "${1:-door}" 2>&1; }

# 6 each writer hits ITS quota with EDQUOT; one full writer leaves the others writing
fill() {  # WRITER -> bytes written, error text
    # Lower-cased: glibc says "Disk quota exceeded", musl "Quota exceeded".
    nocap dd if=/dev/zero of="$MP/$1/fill.bin" bs=1M count=2000 conv=fsync 2>&1 | grep -iE 'quota|space|copied' | tr '\n' ' ' | tr 'A-Z' 'a-z'
}
d_out="$(fill door)"; d_mib=$(( $(stat -c %s "$MP/door/fill.bin") / 1048576 ))
m_out="$(fill meetings)"; m_mib=$(( $(stat -c %s "$MP/meetings/fill.bin") / 1048576 ))
if [[ "$d_out" == *"quota exceeded"* ]] && [ "$d_mib" -ge 198 ] && [ "$d_mib" -le 200 ] \
   && [[ "$m_out" == *"quota exceeded"* ]] && [ "$m_mib" -ge 58 ] && [ "$m_mib" -le 60 ]; then
    ok "6 door stopped at ${d_mib} MiB (EDQUOT), meetings still wrote, stopped at ${m_mib} MiB (EDQUOT)"
else bad "6 door=${d_mib} '$d_out' meetings=${m_mib} '$m_out'"; fi

# 7 all four full: the floor is still free
o_out="$(fill ottertube)"
c_out="$(fill clips)"; c_mib=$(( $(stat -c %s "$MP/clips/fill.bin") / 1048576 ))
free_mib="$("$PY" -c 'import os,sys; s=os.statvfs(sys.argv[1]); print(s.f_bavail*s.f_frsize//1048576)' "$MP")"
if [[ "$o_out" == *"quota exceeded"* ]] && [[ "$c_out" == *"quota exceeded"* ]] && [ "$c_mib" -ge 38 ]    && [ "$c_mib" -le 40 ] && [ "$free_mib" -ge 64 ]; then
    ok "7 all four writers at their quotas (clips stopped at ${c_mib} MiB): ${free_mib} MiB still free (floor 64)"
else bad "7 ottertube '$o_out' clips=${c_mib} '$c_out' free=${free_mib} MiB"; fi
# A 1 MiB dd stops at the first block that does not fit, so up to 1 MiB of door's quota can
# still be free, and the guard's 4 KiB probe then rightly passes (seen on the hub's kernel 147).
# Top door off in 4 KiB blocks so it is full to the guard's own granularity first.
nocap dd if=/dev/zero of="$MP/door/top.bin" bs=4k count=262144 conv=fsync >/dev/null 2>&1 || true
g_full="$(guard_nocap door)"; [[ "$g_full" == *"not writable (EDQUOT)"* ]] && ok "7b the guard reports a full writer: $g_full" || bad "7b guard on a full door/: $g_full"

# 8 WHY the units drop the capability: with CAP_SYS_RESOURCE root writes THROUGH the quota
dd if=/dev/zero of="$MP/door/bypass.bin" bs=1M count=8 conv=fsync >/dev/null 2>&1
by=$(( $(stat -c %s "$MP/door/bypass.bin" 2>/dev/null || echo 0) / 1048576 ))
[ "$by" = 8 ] && ok "8 a writer holding CAP_SYS_RESOURCE wrote 8 MiB past door's full quota (the bypass the unit closes)" \
    || bad "8 bypass expected 8 MiB, wrote $by"
rm -f "$MP"/*/fill.bin "$MP/door/top.bin" "$MP/door/bypass.bin"

# 9 read-only -> refused; rw again -> accepted
mount -o remount,ro "$MP"; g_ro="$(guard_nocap)"; mount -o remount,rw "$MP"
[[ "$g_ro" == *read-only* ]] && ok "9 read-only mount refused" || bad "9 read-only: $g_ro"

# 10 wrong device mounted at the mount point -> refused
truncate -s 64M "$W/other.img"; OL="$(losetup -f --show "$W/other.img")"; LOOPS+=("$OL")
mkfs.ext4 -q -O quota,project -E quotatype=prjquota -L hub-scratch "$OL"
link_uuid "$OL" >/dev/null
umount "$MP"; mount -o prjquota "$OL" "$MP"; mkdir -p "$MP/door"
g_wrong="$(guard_nocap)"; umount "$MP"
[[ "$g_wrong" == *"wrong device"* ]] && ok "10 another ext4 (same label) at the mount point refused: wrong device" || bad "10 wrong device: $g_wrong"

# 11 not mounted -> refused, and the bare mount point is still 000
g_none="$(guard_nocap)"
[[ "$g_none" == *"not a mount point"* ]] && [ "$(stat -c %a "$MP")" = 0 ] && ok "11 nothing mounted: refused" || bad "11 unmounted: $g_none"
mount -T "$R/etc/fstab" "$MP"

# 12 unmount mid-run: the recorder stops before its next file, and writes nothing under the bare mount point
cat >"$W/rec.py" <<'PY'
import os, sys, threading, time
sys.path[:0] = [os.environ["DOOR"], os.path.join(os.environ["DOOR"], "tests"), os.environ["SCRATCH"],
                os.path.join(os.environ["SCRATCH"], "..", "monitoring")]
import door_recorder as dr, homehub_scratch as hs, ts_fixture as tf
import monitor_common as mc
knobs = mc.read_knobs(os.environ["ENVF"])
paths = hs.Paths(os.environ["ROOT"])
rec = dr.Recorder("/nonexistent", os.path.join(paths.mountpoint, "door"),
                  dr.RecorderSettings({"DOOR_RECORDER_SEGMENT_SEC": "10", "DOOR_RECORDER_CAP_MIB": os.environ.get("CAP", "7680")}),
                  hs.Settings(knobs), paths, alert=lambda *a: print("ALERT", a[0]))
r, w = os.pipe()
clock = {"t": 1000.0}
rec.clock = lambda: clock["t"]
rec.recover_parts()
def feed():
    gop = b"".join(tf.stream(10, bodies=int(os.environ.get("BODIES", "2"))))
    for i in range(int(os.environ.get("GOPS", "6"))):
        os.write(w, gop)
        clock["t"] += 5.0
        time.sleep(float(os.environ.get("PAUSE", "0.5")))
        if i == int(os.environ.get("UNMOUNT_AT", "-1")):
            os.system("umount -l " + paths.mountpoint)
    os.close(w)
threading.Thread(target=feed, daemon=True).start()
try:
    code = rec.consume(os.fdopen(r, "rb"))
except SystemExit as e:
    code = e.code
try:
    rec.finalise()
except OSError:
    pass
print("EXIT", code)
PY
run_rec() { DOOR="$DOOR" SCRATCH="$SCRATCH" ENVF="$W/env" ROOT="$R" nocap "$PY" "$W/rec.py" "$@"; }
out12="$(UNMOUNT_AT=1 GOPS=6 run_rec 2>&1)"
leaked="$(find "$MP" -mindepth 1 2>/dev/null | head -3)"
if [[ "$out12" == *"EXIT 76"* ]] && [[ "$out12" == *"ALERT door-recorder-guard"* ]] && [ -z "$leaked" ]; then
    ok "12 unmounted mid-run: the recorder stopped before its next segment (exit 76, alert), nothing written to the bare mount point"
else bad "12 unmount mid-run: $(echo "$out12" | tail -3) leaked=[$leaked]"; fi
mount -T "$R/etc/fstab" "$MP"

# 13 the recorder hitting door's quota: exit 75, alert, nothing anywhere else
"$PY" -c "
import sys; sys.path[:0]=['$SCRATCH', '$SCRATCH/../monitoring']
import homehub_scratch as hs; hs.set_project_quota('$DEV', 7101, 4)"
out13="$(GOPS=40 PAUSE=0.02 BODIES=60 CAP=7680 run_rec 2>&1)"
others="$(find "$MP/meetings" "$MP/ottertube" -type f | wc -l)"
if [[ "$out13" == *"EXIT 75"* ]] && [[ "$out13" == *"ALERT door-recorder-quota"* ]] && [ "$others" = 0 ]; then
    ok "13 the recorder at a 4 MiB door quota: EDQUOT -> exit 75, alert, no file outside door/"
else bad "13 recorder quota: $(echo "$out13" | tail -3) others=$others"; fi
"$PY" -c "
import sys; sys.path[:0]=['$SCRATCH', '$SCRATCH/../monitoring']
import homehub_scratch as hs; hs.set_project_quota('$DEV', 7101, 200)"

# 14 status reads every budget back through quotactl
st="$(hs status)"
echo "$st" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); w=d["writers"]; sys.exit(0 if d["identity_ok"] and d["prjquota"] and w["door"]["limit_mib"]==200 and w["meetings"]["limit_mib"]==60 else 1)' \
    && ok "14 status: identity ok, prjquota, limits read back (door 200, meetings 60 MiB)" || bad "14 status: $st"

echo "scratch-fs.root-test: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
