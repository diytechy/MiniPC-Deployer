#!/usr/bin/env bash
# ottertube-quota.root-test.sh - the OtterTube downloader on a REAL ext4 with
# project quotas: an oversized item stops at EDQUOT and the other writers keep
# writing (plan W8c task 10; SR-116, TC-1440).
#
# A LABELLED ROOT SUITE, NOT A HERMETIC ONE - the same harness as
# stack/scratch/tests/scratch-fs.root-test.sh: root, loop devices, LVM,
# mkfs.ext4, util-linux mount/setpriv, python3, and a kernel whose ext4 supports
# quotas. The WSL2 kernel does not (no CONFIG_QFMT_V2), so on WSL it SKIPS; run
# it under the hub's own kernel in QEMU:
#   bash stack/scratch/tests/run-root-suite-in-qemu.sh <vmlinuz> <quota-modules-dir> \
#        ottertube/tests/ottertube-quota.root-test.sh
# Nothing outside its own temp directory, loop devices and VG (`hhscratchtest`)
# is touched.
#
# WHAT IS REAL AND WHAT IS NOT. Real: the scratch volume as homehub_scratch.py
# ensure makes it (LV, ext4 quota+project, the three projects and hard limits),
# the guard, ottertube.py's run, and the kernel's EDQUOT on a writer without
# CAP_SYS_RESOURCE. Stood in: `docker` - a fake on PATH plays the ytdl-sub
# container, writing the item through the same host path the container's bind
# mount would, with the runner's own (capability-dropped) credentials, and
# printing yt-dlp's own error line when the write fails. The real ytdl-sub's
# reaction to a full filesystem is proven separately (README "What was
# measured": the pinned image on a full loop filesystem printed
# "[Errno 28] No space left on device").
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
OT="$(cd "$HERE/.." && pwd)"
SCRATCH="$(cd "$HERE/../../scratch" && pwd)"
PY="${PYTHON:-python3}"
PASS=0 FAIL=0
ok()  { PASS=$((PASS + 1)); echo "PASS $*"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL $*"; }
skip() { echo "SKIP $*"; exit 0; }

[ "$(id -u)" = 0 ] || skip "needs root"
for t in losetup mkfs.ext4 pvcreate vgcreate lvcreate blkid mount umount setpriv "$PY"; do
    command -v "$t" >/dev/null 2>&1 || skip "needs $t"
done
export DM_DISABLE_UDEV=1 HOMEHUB_MONITOR_OVERRIDE_FILE=/nonexistent
W="$(mktemp -d /tmp/ottertube-root-test.XXXXXX)"
R="$W/root"; MP="$R/srv/scratch"
mkdir -p "$R/etc" /dev/disk/by-uuid "$W/bin" "$W/stack/ottertube/config"
: >"$R/etc/fstab"
LOOPS=() MADE_LINKS=()
cleanup() {
    umount -l "$MP" 2>/dev/null
    vgchange -an hhscratchtest >/dev/null 2>&1; vgremove -ff -y hhscratchtest >/dev/null 2>&1
    for l in "${LOOPS[@]}"; do losetup -d "$l" 2>/dev/null; done
    for l in "${MADE_LINKS[@]}"; do rm -f "$l"; done
    rm -rf "$W"
}
trap cleanup EXIT

truncate -s 32M "$W/probe.img"
mkfs.ext4 -q -O quota,project -E quotatype=prjquota "$W/probe.img" >/dev/null 2>&1
mkdir -p "$W/probe"
mount -o loop,prjquota "$W/probe.img" "$W/probe" 2>/dev/null \
    || skip "this kernel cannot mount ext4 with project quotas (no CONFIG_QFMT_V2?) - $(uname -r)"
umount "$W/probe"

link_uuid() {
    local u; u="$(blkid -p -s UUID -o value "$1")"
    [ -e "/dev/disk/by-uuid/$u" ] || { ln -s "$(readlink -f "$1")" "/dev/disk/by-uuid/$u"; MADE_LINKS+=("/dev/disk/by-uuid/$u"); }
    printf '%s' "$u"
}

truncate -s 1G "$W/pv.img"
PV="$(losetup -f --show "$W/pv.img")"; LOOPS+=("$PV")
pvcreate -q "$PV" >/dev/null && vgcreate -q hhscratchtest "$PV" >/dev/null || { bad "could not make the test VG"; exit 1; }

# 1/28 of the real budgets, as the scratch suite: door 200, meetings 60,
# ottertube 120 MiB. The list's budget (80 MiB) sits inside ottertube's quota.
cat >"$W/env" <<'EOF'
HOMEHUB_SCRATCH_SIZE_MIB=640
HOMEHUB_SCRATCH_VG_RESERVE_MIB=100
HOMEHUB_SCRATCH_FREE_FLOOR_MIB=64
HOMEHUB_SCRATCH_MIN_QUOTA_MIB=16
HOMEHUB_SCRATCH_QUOTA_DOOR_MIB=200
HOMEHUB_SCRATCH_QUOTA_MEETINGS_MIB=60
HOMEHUB_SCRATCH_QUOTA_OTTERTUBE_MIB=120
OTTERTUBE_ENABLED=true
OTTERTUBE_BUDGET_MIB=80
OTTERTUBE_MAX_FILESIZE_MIB=60
OTTERTUBE_PAUSE_SEC=0
EOF
"$PY" "$SCRATCH/homehub_scratch.py" --env-file "$W/env" --root "$R" --vg hhscratchtest --mount-with mount ensure \
    >"$W/ensure.log" 2>&1 || { bad "ensure failed: $(tail -3 "$W/ensure.log")"; exit 1; }
DEV="$(lvs --noheadings -o lv_path hhscratchtest/hub-scratch | tr -d ' ')"
echo "HOMEHUB_SCRATCH_UUID=$(link_uuid "$DEV")" >>"$W/env"
nocap() { setpriv --bounding-set -sys_resource --inh-caps -sys_resource "$@"; }

# The list: a small item, then one whose source turns out far bigger than its
# estimate (the oversized item), then one more that must NOT be attempted.
cat >"$W/stack/ottertube/config/list.json" <<'EOF'
{"version": 1, "collections": [{"id": "test", "title": "Test", "bands": ["k2"], "items": [
 {"id": "small-item", "title": "Small", "url": "https://images-assets.nasa.gov/video/a/a~small.mp4", "estimateMiB": 5,
  "licence": "PD", "source": "test", "basis": "https://example.org/terms"},
 {"id": "huge-item", "title": "Huge", "url": "https://images-assets.nasa.gov/video/b/b~small.mp4", "estimateMiB": 50,
  "licence": "PD", "source": "test", "basis": "https://example.org/terms"},
 {"id": "after-item", "title": "After", "url": "https://images-assets.nasa.gov/video/c/c~small.mp4", "estimateMiB": 5,
  "licence": "PD", "source": "test", "basis": "https://example.org/terms"}]}]}
EOF

# The fake docker: `compose ... run ... ottertube-ytdl-sub ... sub /work/sub.json`
# writes the item as yt-dlp would (a .part in the working directory, renamed on
# completion), FAKE_SIZE_<stem> MiB of it; the ffprobe height check answers
# 180; the indexer run succeeds.
cat >"$W/bin/docker" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"$FAKE_LOG"
case " $* " in
  *" --entrypoint ffprobe "*) echo "180"; exit 0 ;;
  *" ottertube-ytdl-sub "*)
    read -r folder stem < <(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); s=list(list(d.values())[0].values())[0]["overrides"]; print(s["ottertube_folder"], s["ottertube_stem"])' "$OTW/work/sub.json")
    size_var="FAKE_SIZE_${stem//-/_}"; size="${!size_var:-1}"
    [ -n "${FAKE_UNMOUNT_AFTER:-}" ] && [ "$stem" = "$FAKE_UNMOUNT_AFTER" ] && trap 'umount -l "$FAKE_MP"' EXIT
    mkdir -p "$OTW/work/ytdl-sub-working" "$OTW/lib/video/$folder"
    part="$OTW/work/ytdl-sub-working/$stem.mp4.part"
    if ! err="$(dd if=/dev/zero of="$part" bs=1M count="$size" conv=fsync 2>&1)"; then
        case "$err" in *[Qq]uota*) echo "ERROR: unable to write data: [Errno 122] Disk quota exceeded" ;;
                       *) echo "ERROR: unable to write data: $err" ;; esac
        exit 1
    fi
    mv "$part" "$OTW/lib/video/$folder/$stem.mp4"
    printf '{"id": "%s~small", "title": "%s~small", "duration": 10}\n' "$stem" "$stem" >"$OTW/lib/video/$folder/$stem.info.json"
    echo "[ytdl-sub] Download Summary: +1"; exit 0 ;;
  *" ottertube-indexer "*) echo "indexed"; exit 0 ;;
  *" rm "*) exit 0 ;;
esac
exit 0
EOF
chmod 0755 "$W/bin/docker"
export FAKE_LOG="$W/docker.log" OTW="$MP/ottertube" FAKE_MP="$MP" FAKE_SIZE_small_item=5 FAKE_SIZE_huge_item=300 FAKE_SIZE_after_item=5
otrun() { PATH="$W/bin:$PATH" nocap "$PY" "$OT/ottertube.py" --env-file "$W/env" --root "$R" --stack "$W/stack" run 2>&1; }
probe_write() {  # WRITER MiB -> 0 when the writer can still write MiB
    nocap dd if=/dev/zero of="$MP/$1/probe.bin" bs=1M count="$2" conv=fsync >/dev/null 2>&1; local rc=$?
    rm -f "$MP/$1/probe.bin"; return $rc
}

# 1 the oversized item: EDQUOT stops the run (exit 75), its partial is gone, the
#   next item is never attempted, and door/ and meetings/ keep writing
: >"$FAKE_LOG"
out1="$(otrun)"; rc1=$?
last="$(cat "$MP/ottertube/logs/last-run.json" 2>/dev/null)"
work_left="$(find "$MP/ottertube/work" -mindepth 1 2>/dev/null | wc -l)"
attempted_after="$(grep -c 'after-item' "$FAKE_LOG")"
if [ "$rc1" = 75 ] && [[ "$out1" == *"STOPPED - EDQUOT while downloading huge-item"* ]] && [[ "$last" == *'"result": "quota"'* ]] \
   && [ "$work_left" = 0 ] && [ -f "$MP/ottertube/lib/video/test/small-item.mp4" ] && [ "$attempted_after" = 0 ]; then
    ok "1 oversized item: EDQUOT -> exit 75, last-run 'quota', partial removed, the next item not attempted"
else bad "1 oversized: rc=$rc1 work_left=$work_left after=$attempted_after last=$last $(echo "$out1" | tail -4)"; fi
if probe_write door 50 && probe_write meetings 20; then
    ok "1b with ottertube stopped at its quota, door/ took 50 MiB and meetings/ 20 MiB"
else bad "1b another writer could not write after the ottertube stop"; fi
if probe_write ottertube 20; then ok "1c ottertube itself is writable again (its partial was freed)"
else bad "1c ottertube still full after the stop"; fi
[[ "$out1" == *"ALERT"*"ottertube-quota"* || "$out1" == *"OtterTube downloads stopped (budget full)"* ]] \
    && ok "1d the stop raised the ottertube-quota alert" || bad "1d no alert line: $(echo "$out1" | grep -i alert | head -2)"

# 2 a run with the volume unmounted: refused before anything (exit 76), no
#   container started, nothing written under the bare mount point
umount "$MP"; : >"$FAKE_LOG"
out2="$(otrun)"; rc2=$?
leaked="$(find "$MP" -mindepth 1 2>/dev/null | head -3)"
if [ "$rc2" = 76 ] && ! grep -q ' run ' "$FAKE_LOG" && [ -z "$leaked" ]; then
    ok "2 unmounted: exit 76 before any container, nothing under the bare mount point"
else bad "2 unmounted: rc=$rc2 leaked=[$leaked] $(echo "$out2" | tail -2)"; fi
mount -T "$R/etc/fstab" "$MP"

# 3 the volume goes away while an item's container runs: the next guard (before
#   that item's metadata, or - when the lazily unmounted item is no longer
#   visible - before the next item) refuses with exit 76, the next item's
#   container never starts, and nothing is written under the bare mount point
rm -f "$MP/ottertube/lib/video/test/"*; : >"$FAKE_LOG"
export FAKE_SIZE_huge_item=5 FAKE_UNMOUNT_AFTER=small-item
out3="$(otrun)"; rc3=$?
leaked="$(find "$MP" -mindepth 1 2>/dev/null | head -3)"
if [ "$rc3" = 76 ] && [[ "$out3" == *"before item small-item metadata"* || "$out3" == *"before item huge-item:"* ]] && ! grep -q 'huge-item' "$FAKE_LOG" && [ -z "$leaked" ]; then
    ok "3 unmounted mid-run: refused at the next guard (exit 76: $(echo "$out3" | grep -o 'before item [a-z-]*\( metadata\)\?' | head -1)), the next item never started, nothing under the bare mount point"
else bad "3 mid-run: rc=$rc3 leaked=[$leaked] $(echo "$out3" | tail -3)"; fi
unset FAKE_UNMOUNT_AFTER
mount -T "$R/etc/fstab" "$MP"

# 4 everything fits: all three items, the indexer run with --require-hardlink, result ok
rm -rf "$MP/ottertube/lib/video/test"; : >"$FAKE_LOG"
out4="$(otrun)"; rc4=$?
if [ "$rc4" = 0 ] && grep -q -- '--require-hardlink' "$FAKE_LOG" && [ "$(grep -c ' sub /work/sub.json' "$FAKE_LOG")" = 3 ] \
   && grep -q '"title": "Huge"' "$MP/ottertube/lib/video/test/huge-item.info.json"; then
    ok "4 a list that fits: 3 items, titles from the list, indexer with --require-hardlink, exit 0"
else bad "4 fits: rc=$rc4 $(echo "$out4" | tail -3)"; fi

# 5 the next run fetches nothing: every item is present
: >"$FAKE_LOG"
otrun >/dev/null; rc5=$?
[ "$rc5" = 0 ] && [ "$(grep -c ' ottertube-ytdl-sub ' "$FAKE_LOG")" = 0 ] \
    && ok "5 an unchanged list: no container started for a present item" || bad "5 rc=$rc5 $(cat "$FAKE_LOG")"

echo "ottertube-quota.root-test: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
