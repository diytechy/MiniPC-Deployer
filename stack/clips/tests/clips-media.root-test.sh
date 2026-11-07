#!/usr/bin/env bash
# clips-media.root-test.sh - motion clips on a REAL ext4 with project quotas on a
# REAL disk that really goes away (SR-148, SR-149; TC-1695).
#
# A LABELLED ROOT SUITE, NOT A HERMETIC ONE, AND ONLY FOR A THROWAWAY GUEST. It uses
# the hub's real paths (/srv/media, /etc/homehub-samba, /srv/scratch/clips), so it
# refuses to run anywhere but the QEMU guest of run-root-suite-in-qemu.sh, which
# gives it a spare virtio-scsi disk:
#   bash stack/scratch/tests/run-root-suite-in-qemu.sh <vmlinuz> <quota modules> clips/tests/clips-media.root-test.sh
# The WSL2 kernel has no ext4 quota support (no CONFIG_QFMT_V2), so WSL cannot run it.
#
# Scale: CLIPS_CAP_MIB 64 (the hub: 95367), so the derived hard limit is 76 MiB and
# filling it takes seconds. The drop at the end is the real thing: the SCSI layer
# deletes the disk under the mount (del_gendisk, what a USB disconnect does) and
# brings it back under a new name.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
CLIPS="$(cd "$HERE/.." && pwd)"
SAMBA="$(cd "$HERE/../../samba" && pwd)"
MON="$(cd "$HERE/../../monitoring" && pwd)"
PY="${PYTHON:-python3}"
PASS=0 FAIL=0
ok()   { PASS=$((PASS + 1)); echo "PASS $*"; }
bad()  { FAIL=$((FAIL + 1)); echo "FAIL $*"; }
skip() { echo "SKIP $*"; exit 0; }

[ "$(id -u)" = 0 ] || skip "needs root"
grep -q 'rdinit=/init' /proc/cmdline || skip "only inside run-root-suite-in-qemu.sh's guest: it uses the hub's real paths"
DISK="$(ls /sys/block | grep '^sd' | head -n1)"
[ -n "$DISK" ] || skip "no spare SCSI disk in this guest"
for t in sfdisk mkfs.ext4 losetup setfacl getfacl setpriv useradd groupadd "$PY"; do
    command -v "$t" >/dev/null 2>&1 || skip "needs $t"
done
export HOMEHUB_MONITOR_OVERRIDE_FILE=/nonexistent
W="$(mktemp -d /tmp/clips-root-test.XXXXXX)"
mkdir -p "$W/state" /etc/homehub-samba /dev/disk/by-id /srv/scratch/clips
install -d -m 000 /srv/media
MOUNT_OPTS=noatime,prjquota,errors=remount-ro
BY_ID=usb-Fake_MediaDrive_0-0:0

cat >"$W/env" <<EOF
CLIPS_ENABLED=true
CLIPS_ROOT=/srv/media/Clips
CLIPS_CAP_MIB=64
CLIPS_QUOTA_HIGH_PCT=90
HOMEHUB_DISK_MOUNTS=/srv/media
EOF
hc()    { "$PY" "$CLIPS/homehub_clips.py" --env-file "$W/env" --state-dir "$W/state" "$@"; }
nocap() { setpriv --bounding-set -sys_resource --inh-caps -sys_resource "$@"; }
# As the cutter's ffmpeg: uid 0, every capability dropped, docker's umask.
jailed() { setpriv --bounding-set -all --inh-caps -all --no-new-privs "$@"; }
as_uid() { local u="$1"; shift; setpriv --reuid "$u" --regid "$u" --clear-groups "$@"; }
identity_row() { printf '/srv/library\tusb-Other_0-0:0\tLibrary\n'; [ "${1:-}" = none ] || printf '/srv/media\t%s\tMediaDrive\n' "$BY_ID"; }

cat >"$W/h.py" <<'PY'
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
import homehub_clips as hc
import monitor_common as mc
s = hc.ClipsSettings(mc.read_knobs(sys.argv[2]))
verb, args = sys.argv[3], sys.argv[4:]
if verb == "guard":
    print(hc.guard(s)[1])
elif verb == "project":
    print(*hc.scratch.get_project(hc.MEDIA_ROOT))
elif verb == "quota":
    used, limit = hc.clips_quota(s)
    print(used // hc.MIB, limit // hc.MIB)
elif verb == "clips":                     # N clips of M MiB, the oldest first, minutes apart
    n, mib, root = int(args[0]), int(args[1]), (args[2] if len(args) > 2 else s.root)
    base = time.time() - 86400 + (float(args[3]) if len(args) > 3 else 0)
    for i in range(n):
        at = base + i * 60
        day, stem = hc.clip_stem("door", int(at * 1000), "approach", 11)
        folder = os.path.join(root, "door", day)
        os.makedirs(folder, mode=0o750, exist_ok=True)
        with open(os.path.join(folder, stem + ".mp4"), "wb") as handle:
            handle.write(os.urandom(1024) * (mib * 1024))
        ms = int(at * 1000)
        hc.write_json_atomic(os.path.join(folder, stem + ".json"), {
            "v": 1, "camera": "door", "clip": stem + ".mp4", "cause": "approach", "coverage": "full",
            "actual": {"start_utc_ms": ms, "end_utc_ms": ms + 11000}, "events": [{"utc_ms": ms}]})
elif verb == "list":
    clips = hc.list_clips(args[0] if args else s.root)
    print(len(clips), sum(c[3] for c in clips) // hc.MIB)
elif verb == "room":
    need = int(args[0]) * hc.MIB
    print(hc.make_room(s, "", int(time.time() * 1000), need, lambda: hc.clips_quota(s)) // hc.MIB)
PY
h() { "$PY" "$W/h.py" "$CLIPS" "$W/env" "$@"; }

# ── the disk, as the plan's section 3.2 formats it ───────────────────────────
echo ',,83' | sfdisk -q "/dev/$DISK" >/dev/null 2>&1
blockdev --rereadpt "/dev/$DISK" 2>/dev/null
PART="/dev/${DISK}1"
for _ in 1 2 3 4 5; do [ -b "$PART" ] && break; sleep 1; done
mkfs.ext4 -q -L MediaDrive -T largefile -m 0 -O quota,project \
    -E quotatype=prjquota,lazy_itable_init=0,lazy_journal_init=0 "$PART" || { bad "mkfs failed"; exit 1; }
ln -sf "/dev/$DISK" "/dev/disk/by-id/$BY_ID"
identity_row >/etc/homehub-samba/drive-identity.conf

# 1 not mounted: refused, and nothing lands on the root filesystem
hc ensure >"$W/1.log" 2>&1; rc=$?
if [ "$rc" = 4 ] && grep -q 'not a mount point' "$W/1.log" && [ -z "$(ls -A /srv/media)" ] && ! grep -q homehub-clips /etc/passwd; then
    ok "1 unmounted: ensure exit 4, nothing under the bare mount point, no account"
else bad "1 unmounted: rc=$rc $(tail -1 "$W/1.log")"; fi

# 2 a stand-in with the right label and options, on another disk: refused
truncate -s 64M "$W/standin.img"
mkfs.ext4 -q -L MediaDrive -O quota,project -E quotatype=prjquota "$W/standin.img"
STANDIN="$(losetup -f --show "$W/standin.img")"
mount -o "$MOUNT_OPTS" "$STANDIN" /srv/media
hc ensure >"$W/2.log" 2>&1; rc=$?
if [ "$rc" = 4 ] && grep -q 'not the disk drive-identity.conf names' "$W/2.log" && [ ! -e /srv/media/Clips ]; then
    ok "2 stand-in: the right label on the wrong disk is refused, no folder made"
else bad "2 stand-in: rc=$rc $(tail -1 "$W/2.log")"; fi
umount /srv/media; losetup -d "$STANDIN"

# 3 the real disk, but nothing says it is
mount -o "$MOUNT_OPTS" "$PART" /srv/media
identity_row none >/etc/homehub-samba/drive-identity.conf
hc ensure >"$W/3.log" 2>&1; rc=$?
identity_row >/etc/homehub-samba/drive-identity.conf
if [ "$rc" = 4 ] && grep -q 'has no /srv/media row' "$W/3.log" && [ ! -e /srv/media/Clips ]; then
    ok "3 no identity row: refused rather than assumed"
else bad "3 no identity row: rc=$rc $(tail -1 "$W/3.log")"; fi

# 4 read-only
mount -o remount,ro /srv/media
hc ensure >"$W/4.log" 2>&1; rc=$?
mount -o remount,rw /srv/media
if [ "$rc" = 4 ] && grep -q 'read-only' "$W/4.log"; then ok "4 read-only: refused"
else bad "4 read-only: rc=$rc $(tail -1 "$W/4.log")"; fi

# the volume root as provision-mounts.sh leaves it (storage-map 1a)
chown 0:3000 /srv/media; chmod 2775 /srv/media
setfacl -m d:u::rwx,d:g::rwx,d:o::rx /srv/media

# 5 the plan changes nothing
hc ensure --plan >"$W/5.log" 2>&1; rc=$?
if [ "$rc" = 0 ] && grep -q 'would create /srv/media/Clips' "$W/5.log" && [ ! -e /srv/media/Clips ] && ! grep -q homehub-clips /etc/passwd; then
    ok "5 plan: says what it would do, makes nothing"
else bad "5 plan: rc=$rc $(tail -2 "$W/5.log")"; fi

# 6 ensure: account, folder, no inherited ACL, project, the derived hard limit
hc ensure >"$W/6.log" 2>&1; rc=$?
own="$(stat -c '%u %g %a' /srv/media/Clips)"
defaults="$(getfacl -p /srv/media/Clips 2>/dev/null | grep -c '^default:')"
account="$(grep '^homehub-clips:' /etc/passwd | cut -d: -f3,4,7)"
if [ "$rc" = 0 ] && [ "$own" = "0 3100 2750" ] && [ "$defaults" = 0 ] && [ "$account" = "3100:3100:/usr/sbin/nologin" ] \
   && [ "$(h project)" = "7105 True" ] && [ "$(h quota)" = "0 76" ]; then
    ok "6 ensure: root:3100 2750, no default ACL, account 3100, project 7105 inherit, limit 76 MiB for a 64 MiB cap"
else bad "6 ensure: rc=$rc own=$own defaults=$defaults account=$account project=$(h project) quota=$(h quota) $(tail -2 "$W/6.log")"; fi

# 7 idempotent
hc ensure >"$W/7.log" 2>&1; rc=$?
if [ "$rc" = 0 ] && grep -q '"actions": \[\]' "$W/7.log"; then ok "7 a second ensure has no actions"
else bad "7 second ensure: rc=$rc $(tail -1 "$W/7.log")"; fi

# 8 the guard, as a writer and as a root shell
g_cap="$(h guard)"; g_no="$(nocap "$PY" "$W/h.py" "$CLIPS" "$W/env" guard)"
if [[ "$g_cap" == *CAP_SYS_RESOURCE* ]] && [ "$g_no" = ok ]; then ok "8 guard: refuses CAP_SYS_RESOURCE, passes the writer"
else bad "8 guard: cap='$g_cap' nocap='$g_no'"; fi

# 9 what clips really land with, and who can read them
day=/srv/media/Clips/door/2026-11-06
(umask 0027; nocap "$PY" -c "import os; os.makedirs('$day', mode=0o750)")
jailed sh -c "umask 022; dd if=/dev/zero of=$day/c.mp4.part bs=1M count=1 2>/dev/null && mv $day/c.mp4.part $day/c.mp4"; rc_jail=$?
(umask 0027; nocap "$PY" -c "open('$day/c.json', 'w').write('{}')")
dir="$(stat -c '%u %g %a' "$day")"; mp4="$(stat -c '%u %g %a' "$day/c.mp4")"; side="$(stat -c '%u %g %a' "$day/c.json")"
as_uid 3100 cat "$day/c.mp4" >/dev/null 2>&1; rc_read=$?
as_uid 3100 sh -c "echo x >>$day/c.mp4" 2>/dev/null; rc_write=$?
as_uid 3100 touch "$day/n" 2>/dev/null; rc_new=$?
as_uid 3999 cat "$day/c.mp4" >/dev/null 2>&1; rc_other=$?
if [ "$rc_jail" = 0 ] && [ "$dir" = "0 3100 2750" ] && [ "$mp4" = "0 3100 644" ] && [ "$side" = "0 3100 640" ] \
   && [ "$rc_read" = 0 ] && [ "$rc_write" != 0 ] && [ "$rc_new" != 0 ] && [ "$rc_other" != 0 ] && [ "$(h quota | cut -d' ' -f1)" = 1 ]; then
    ok "9 a capability-free uid 0 writes; day 2750, clip 644, sidecar 640, all group 3100, counted; homehub-clips reads and cannot write; a stranger cannot read"
else bad "9 shapes: jail=$rc_jail dir=$dir mp4=$mp4 side=$side read=$rc_read write=$rc_write new=$rc_new other=$rc_other quota=$(h quota)"; fi
rm -rf /srv/media/Clips/door

# 10 the cap acts, not the quota: 68 MiB of clips, a 64 MiB cap, a rule that starts at 68
nocap "$PY" "$W/h.py" "$CLIPS" "$W/env" clips 17 4
sync
before="$(h quota)"
nocap "$PY" "$CLIPS/homehub_clips.py" --env-file "$W/env" --state-dir "$W/state" retention >"$W/10.log" 2>&1; rc=$?
read -r n mib < <(h list)
if [ "$rc" = 0 ] && [ "$n" = 15 ] && [ "$mib" -le 64 ] && [ "${before%% *}" -le 68 ]; then
    ok "10 cap first: used $before before, $n clips ($mib MiB) kept, quota rule never reached"
else bad "10 cap first: rc=$rc before=$before kept=$n/$mib $(tail -1 "$W/10.log")"; fi

# 11 make_room reads the real quota on the drive, past the rule's start
nocap "$PY" "$W/h.py" "$CLIPS" "$W/env" clips 3 4 /srv/media/Clips 3600
sync
freed="$(nocap "$PY" "$W/h.py" "$CLIPS" "$W/env" room 8 | tail -n1)"
read -r used limit < <(h quota)
if [ "${freed:-0}" -ge 4 ] && [ $((used + 8)) -le 68 ]; then ok "11 make_room: freed $freed MiB so 8 MiB fits under 90 % of $limit"
else bad "11 make_room: freed=$freed used=$used limit=$limit"; fi

# 12 the wall holds a runaway, a full quota is no refusal, and retention still clears it
nocap dd if=/dev/zero of=/srv/media/Clips/fill.bin bs=1M count=200 conv=fsync >"$W/12.log" 2>&1; rc=$?
g_full="$(nocap "$PY" "$W/h.py" "$CLIPS" "$W/env" guard)"
read -r used limit < <(h quota)
nocap "$PY" "$CLIPS/homehub_clips.py" --env-file "$W/env" --state-dir "$W/state" retention >>"$W/12.log" 2>&1; rc_ret=$?
if [ "$rc" != 0 ] && grep -qiE 'quota exceeded' "$W/12.log" && [ "$used" -ge 75 ] && [ "$used" -le 76 ] && [ "$g_full" = ok ] && [ "$rc_ret" = 0 ]; then
    ok "12 wall: EDQUOT at $used of $limit MiB; the guard still passes and retention still runs"
else bad "12 wall: dd=$rc used=$used limit=$limit guard=$g_full retention=$rc_ret"; fi
rm -f /srv/media/Clips/fill.bin; rm -rf /srv/media/Clips/door

# 13 the move off scratch
nocap "$PY" "$W/h.py" "$CLIPS" "$W/env" clips 3 1 /srv/scratch/clips
printf '{"clip":"old"}\n' >/srv/scratch/clips/door/index.jsonl
sums="$(cd /srv/scratch/clips/door && sha256sum */*.mp4 | sort)"
nocap "$PY" "$CLIPS/homehub_clips.py" --env-file "$W/env" --state-dir "$W/state" move-from-scratch >"$W/13a.log" 2>&1
left="$(h list /srv/scratch/clips)"
nocap "$PY" "$CLIPS/homehub_clips.py" --env-file "$W/env" --state-dir "$W/state" move-from-scratch --apply >"$W/13.log" 2>&1; rc=$?
moved_sums="$(cd /srv/media/Clips/door && sha256sum */*.mp4 | sort)"
f="$(ls /srv/media/Clips/door/*/*.mp4 | head -n1)"
if [ "$rc" = 0 ] && [ "${left%% *}" = 3 ] && [ "$sums" = "$moved_sums" ] && [ ! -e /srv/scratch/clips/door ] \
   && [ "$(wc -l </srv/media/Clips/door/index.jsonl)" = 3 ] && [ "$(stat -c '%g %a' "$f")" = "3100 640" ] \
   && [ "$(stat -c '%g %a' "$(dirname "$f")")" = "3100 2750" ] && [ "$(h quota | cut -d' ' -f1)" = 3 ]; then
    ok "13 move: 3 clips across byte for byte, 3100 640, counted, index rebuilt, the old folder gone"
else bad "13 move: rc=$rc left=$left $(tail -1 "$W/13.log") $(stat -c '%g %a' "$f")"; fi
sync

# 14 the drive drops under the mount (del_gendisk, as a USB disconnect)
SYSDEV="$(cat "/sys/block/$DISK/$(basename "$PART")/dev")"
echo 1 >"/sys/block/$DISK/device/delete"
sleep 1
line="$(grep -c ' /srv/media ' /proc/self/mountinfo)"
reason="$(nocap "$PY" "$W/h.py" "$CLIPS" "$W/env" guard)"
/bin/bash "$SAMBA/library-guard.sh" --check Clips --library /srv/media >"$W/14.log" 2>&1; rc_share=$?
"$PY" "$MON/homehub-disk-check.py" --state-dir "$W/mon" --env-file "$W/env" --alert-helper /bin/true >"$W/14d.log" 2>&1
disk_state="$("$PY" -c "import json; print(json.load(open('$W/mon/disk-status.json'))['mounts'][0]['state'])")"
hc ensure >"$W/14e.log" 2>&1; rc_ens=$?
if [ "$line" = 1 ] && [[ "$reason" == *"the drive dropped"* ]] && [ "$rc_share" = 1 ] && grep -q STALE "$W/14.log" \
   && [ "$disk_state" = absent ] && [ "$rc_ens" = 4 ]; then
    ok "14 dropped: mountinfo keeps its line ($SYSDEV); the guard, the share and ensure refuse; the disk check says absent"
else bad "14 dropped: line=$line reason='$reason' share=$rc_share disk=$disk_state ensure=$rc_ens $(tail -1 "$W/14.log")"; fi

# 15 it returns under a new name; --reattach puts it back through the mount unit
for host in /sys/class/scsi_host/*; do echo '- - -' >"$host/scan"; done
NEW=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    NEW="$(ls /sys/block | grep '^sd' | head -n1)"
    [ -n "$NEW" ] && [ -b "/dev/${NEW}1" ] && break
    sleep 1
done
ln -sf "/dev/$NEW" "/dev/disk/by-id/$BY_ID"
mkdir -p "$W/bin"
printf 'LABEL=MediaDrive /srv/media ext4 nofail,noatime,prjquota,errors=remount-ro 0 2\n' >"$W/fstab"
# No systemd in the guest: the mount unit's job, by the same fstab row.
cat >"$W/bin/systemctl" <<EOF
#!/bin/sh
[ "\$1 \$2" = "start srv-media.mount" ] && echo "\$*" >>"$W/systemctl.log" && exec mount -T "$W/fstab" /srv/media
exit 1
EOF
printf '#!/bin/sh\necho srv-media.mount\n' >"$W/bin/systemd-escape"
chmod +x "$W/bin/systemctl" "$W/bin/systemd-escape"
PATH="$W/bin:$PATH" FSTAB_FILE="$W/fstab" NO_REMOUNT_FILE="$W/no-such" /bin/bash "$SAMBA/library-guard.sh" \
    --reattach --library /srv/media --label media >"$W/15.log" 2>&1; rc=$?
g_back="$(nocap "$PY" "$W/h.py" "$CLIPS" "$W/env" guard)"
back_sums="$(cd /srv/media/Clips/door 2>/dev/null && sha256sum */*.mp4 | sort)"
/bin/bash "$SAMBA/library-guard.sh" --check Clips --library /srv/media >/dev/null 2>&1; rc_share=$?
if [ "$rc" = 0 ] && grep -q 'start srv-media.mount' "$W/systemctl.log" && [ "$g_back" = ok ] && [ "$back_sums" = "$moved_sums" ] && [ "$rc_share" = 0 ]; then
    ok "15 returned as $NEW: reattached through the unit, the guard and the share pass, the clips are intact"
else bad "15 reattach: rc=$rc guard='$g_back' share=$rc_share $(tail -3 "$W/15.log")"; fi

# 16 I/O errors on a live mount. errors=remount-ro: the kernel says "Remounting
# filesystem read-only" and shuts ext4 down WITHOUT setting the flag, so mountinfo
# still says rw. Only a write can tell. The share's guard reads mountinfo, so it
# still passes: reads then fail on their own. That limit is asserted, not hidden.
echo offline >"/sys/block/$NEW/device/state"
dd if=/dev/zero of=/srv/media/Clips/door/x bs=1M count=4 conv=fsync >/dev/null 2>&1
sync; sleep 1
echo running >"/sys/block/$NEW/device/state"
sb="$(grep ' /srv/media ' /proc/self/mountinfo | sed 's/.* - //')"
reason="$(nocap "$PY" "$W/h.py" "$CLIPS" "$W/env" guard)"
/bin/bash "$SAMBA/library-guard.sh" --check Clips --library /srv/media >"$W/16.log" 2>&1; rc_share=$?
if [[ "$sb" == *" rw,"* ]] && [[ "$reason" == *"not writable ("* ]] && [ "$rc_share" = 0 ]; then
    ok "16 I/O error: mountinfo still says rw ($sb); the write probe refuses: $reason"
else bad "16 I/O error: sb='$sb' reason='$reason' share=$rc_share"; fi

# 17 and a stopped filesystem needs a remount by hand: the device is fine, so nothing re-attaches it
umount /srv/media; mount -T "$W/fstab" /srv/media
g_back="$(nocap "$PY" "$W/h.py" "$CLIPS" "$W/env" guard)"
back_sums="$(cd /srv/media/Clips/door 2>/dev/null && sha256sum */*.mp4 | sort)"
if [ "$g_back" = ok ] && [ "$back_sums" = "$moved_sums" ]; then ok "17 umount and mount: the guard passes, the clips are intact"
else bad "17 remount: guard='$g_back'"; fi

echo "clips-media: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
