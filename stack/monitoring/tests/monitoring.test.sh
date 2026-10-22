#!/usr/bin/env bash
# monitoring.test.sh - the hub monitors' Linux-only halves, run for real (SN-048).
#
# tests/test_hub_monitoring.py covers the decisions on any OS. What only Linux
# can show is here, against the REAL scripts and real kernel behaviour:
#   * the NVMe test reading real sysfs symlinks, through a device-mapper
#     device's slaves, and the disk check statfs'ing ONLY those (TC-1306);
#   * a REAL ENOSPC: the telemetry day file pointed at /dev/full (TC-1307);
#   * the alert helper bounded against a curl that hangs (TC-1308);
#   * a docker that blocks for ever, and a journalctl that floods, killed with
#     their whole process group at the evidence item's timeout and cap
#     (TC-1309, TC-1310), and a descendant that escapes the group (TC-1312).
# Nothing here needs root, a network, docker or a real disk.
#
# Run by stack/run-hermetic-tests.sh.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MON="$HERE/.."
PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$*"; }

command -v python3 >/dev/null 2>&1 || { echo "SKIP: no python3"; printf '0 PASS  0 FAIL\n'; exit 0; }
TMP="$(mktemp -d)"
# A sleep length unique to THIS run, so a leftover-process check can never see
# another suite's (or a concurrent run's) sleeper.
NAP=$((7000000 + $$))
trap 'pkill -f "sleep $NAP" 2>/dev/null; rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
export HOMEHUB_MONITOR_OVERRIDE_FILE="$TMP/no-override"

# ── a fake sysfs with real links: nvme0n1p2, dm-0 over nvme0n1p3, sdb1, dm-1 over sda1
S="$TMP/sys"
mkdir -p "$S/dev/block" "$S/devices/pci/nvme/nvme0/nvme0n1/nvme0n1p2" \
         "$S/devices/pci/nvme/nvme0/nvme0n1/nvme0n1p3" "$S/devices/virtual/block/dm-0/slaves" \
         "$S/devices/usb/host/sdb/sdb1" "$S/devices/usb/host/sda/sda1" "$S/devices/virtual/block/dm-1/slaves"
echo 259:3 >"$S/devices/pci/nvme/nvme0/nvme0n1/nvme0n1p3/dev"
echo 8:1 >"$S/devices/usb/host/sda/sda1/dev"
ln -s ../../devices/pci/nvme/nvme0/nvme0n1/nvme0n1p2 "$S/dev/block/259:2"
ln -s ../../devices/pci/nvme/nvme0/nvme0n1/nvme0n1p3 "$S/dev/block/259:3"
ln -s ../../devices/virtual/block/dm-0 "$S/dev/block/253:0"
ln -s ../../../../pci/nvme/nvme0/nvme0n1/nvme0n1p3 "$S/devices/virtual/block/dm-0/slaves/nvme0n1p3"
ln -s ../../devices/usb/host/sdb/sdb1 "$S/dev/block/8:17"
ln -s ../../devices/usb/host/sda/sda1 "$S/dev/block/8:1"
ln -s ../../devices/virtual/block/dm-1 "$S/dev/block/253:1"
ln -s ../../../../usb/host/sda/sda1 "$S/devices/virtual/block/dm-1/slaves/sda1"

got="$(cd "$MON" && python3 -c '
import sys, monitor_common as mc
print(" ".join("%s=%s" % (m, mc.is_nvme_backed(m, sys.argv[1])) for m in ("259:2", "253:0", "8:17", "253:1", "0:27", "9:9")))
' "$S")"
if [ "$got" = "259:2=True 253:0=True 8:17=False 253:1=False 0:27=False 9:9=False" ]; then
    pass "TC-1306 NVMe partition and LVM-on-NVMe are NVMe; USB and LVM-on-USB and virtual are not"
else
    fail "TC-1306 is_nvme_backed: $got"
fi

# The disk check: three mount points; only the NVMe ones may be statfs'd. The
# USB one is a path that does not exist, so a statfs on it would be an error
# state, not "skipped".
mkdir -p "$TMP/proc/self" "$TMP/mnt/root" "$TMP/mnt/boot"
cat >"$TMP/proc/self/mountinfo" <<EOF
29 1 253:0 / $TMP/mnt/root rw - ext4 /dev/mapper/vg-root rw
31 29 259:2 / $TMP/mnt/boot rw - ext4 /dev/nvme0n1p2 rw
40 29 8:17 / $TMP/mnt/never-touch rw - ext4 /dev/sdb1 rw
EOF
printf 'HOMEHUB_DISK_MOUNTS="%s %s %s=99%%"\n' "$TMP/mnt/root" "$TMP/mnt/never-touch" "$TMP/mnt/boot" >"$TMP/env"
cat >"$TMP/alert.sh" <<'EOF'
#!/usr/bin/env bash
k=""; while [ $# -gt 0 ]; do [ "$1" = --key ] && k="$2"; shift; done
echo "$k" >>"$ALERTS"; echo "stub alert $k"; exit 0
EOF
export ALERTS="$TMP/alerts.log"
out="$(python3 "$MON/homehub-disk-check.py" --state-dir "$TMP/st" --env-file "$TMP/env" --alert-helper "$TMP/alert.sh" \
        --sys-root "$S" --proc-root "$TMP/proc" 2>&1)"; rc=$?
states="$(python3 -c 'import json,sys; print(" ".join(m["state"] for m in json.load(open(sys.argv[1]))["mounts"]))' "$TMP/st/disk-status.json")"
if [ "$rc" = 0 ] && [ "$states" = "ok skipped low" ] && grep -qx 'disk-low-'"$(printf '%s' "$TMP/mnt/boot" | sed 's#^/##; s#[^A-Za-z0-9_.-]#-#g' | cut -c1-48)" "$ALERTS"; then
    pass "TC-1306 disk check: NVMe measured, USB skipped untouched, a 99% floor fires"
else
    fail "TC-1306 disk check rc=$rc states=$states alerts=$(tr '\n' ' ' <"$ALERTS" 2>/dev/null) out=$out"
fi

# ── a real ENOSPC: today's telemetry file is /dev/full ─────────────────────
if [ -w /dev/full ]; then
    mkdir -p "$TMP/tel" "$TMP/fake/proc/sys/kernel/random"
    echo bootid >"$TMP/fake/proc/sys/kernel/random/boot_id"
    printf 'cpu  1 0 1 8 0 0 0 0 0 0\n' >"$TMP/fake/proc/stat"
    ln -s /dev/full "$TMP/tel/telemetry-$(date -u +%Y%m%d).jsonl"
    out="$(python3 "$MON/homehub-telemetry.py" --data-dir "$TMP/tel" --state-dir "$TMP/st" --env-file "$TMP/env" \
            --alert-helper "$TMP/alert.sh" --sys-root "$TMP/fake/sys" --proc-root "$TMP/fake/proc" sample 2>&1)"; rc=$?
    if [ "$rc" = 1 ] && printf '%s' "$out" | grep -q 'No space left on device' && [ -f "$TMP/st/telemetry-state.json" ]; then
        pass "TC-1307 telemetry on a full disk exits 1, says ENOSPC, still saves its rules state"
    else
        fail "TC-1307 rc=$rc out=$out"
    fi
else
    echo "SKIP  TC-1307 (no writable /dev/full)"
fi

# ── the alert helper against a curl that never returns ─────────────────────
printf '#!/usr/bin/env bash\nsleep %s\n' "$NAP" >"$TMP/bin/curl"
chmod +x "$TMP/bin/curl"
printf 'LAN_IP=192.0.2.10\nNTFY_PORT=8081\n' >"$TMP/ntfy.env"
start=$(date +%s)
out="$(PATH="$TMP/bin:$PATH" bash "$MON/homehub-alert.sh" --key hang --priority high --title t --body b \
        --env-file "$TMP/ntfy.env" --state-dir "$TMP/as" --timeout 2 2>&1)"; rc=$?
took=$(( $(date +%s) - start ))
if [ "$rc" = 4 ] && [ "$took" -le 8 ] && printf '%s' "$out" | grep -q 'ALERT NOT DELIVERED' && [ ! -e "$TMP/as/alerted-hang" ]; then
    pass "TC-1308 a hung ntfy costs the helper its timeout (${took}s), is journaled and not stamped"
else
    fail "TC-1308 rc=$rc took=${took}s out=$out"
fi
pkill -f "sleep $NAP" 2>/dev/null

# ── evidence: a docker that blocks for ever, a journal that floods ─────────
printf '#!/usr/bin/env bash\nsleep %s &\nwait\n' "$NAP" >"$TMP/bin/docker"
cat >"$TMP/bin/journalctl" <<'EOF'
#!/usr/bin/env bash
yes "Oct 22 flood[1]: a line that repeats for ever ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
EOF
chmod +x "$TMP/bin/docker" "$TMP/bin/journalctl"
out="$(cd "$MON" && PATH="$TMP/bin:$PATH" python3 - "$TMP/ev" <<'EOF'
import json, sys, time
import homehub_evidence as ev
plan = ev.capture_plan("/proc", ["docker.service"], ["kernel"])
for item in plan:
    if "argv" in item and item["argv"][0] == "docker":
        item["timeout"] = 2
    if "env" in item:
        item["env"] = dict(item["env"], PATH=__import__("os").environ["PATH"])
t0 = time.monotonic()
m = ev.capture(sys.argv[1], 1, plan, {"why": "hermetic"}, deadline_s=60)
print(json.dumps({"took": round(time.monotonic() - t0, 1),
                  "status": {i["name"]: i["status"] for i in m["items"]},
                  "bytes": {i["name"]: i["bytes"] for i in m["items"]},
                  "red": {i["name"]: i["redactions"] for i in m["items"]}, "path": m["path"]}))
EOF
)"
leftover="$(pgrep -f "sleep $NAP" | wc -l)"
check() { python3 -c "import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if ($2) else 1)" "$out"; }
if check x 'd["status"]["docker-ps.txt"].startswith("timeout") and d["status"]["docker-stats.txt"].startswith("timeout") and d["took"] < 30' \
   && [ "$leftover" = 0 ]; then
    pass "TC-1309 a blocked docker times out per item and its whole process group is killed"
else
    fail "TC-1309 leftover=$leftover out=$out"
fi
if check x 'd["status"]["journal-units.txt"] == "truncated" and d["bytes"]["journal-units.txt"] <= 512*1024 and d["red"]["journal-units.txt"] > 0' \
   && ! grep -q 'ghp_A1b2' "$(check x 'True' && python3 -c 'import json,sys; print(json.loads(sys.argv[1])["path"])' "$out")/journal-units.txt"; then
    pass "TC-1310 a journal flood is cut at the file cap, and the planted token is redacted"
else
    fail "TC-1310 out=$out"
fi
# ── a descendant that escapes the process group cannot hang the runner ─────
# It keeps the pipe open after the group is killed; the runner must still
# return at its timeout rather than wait on the pipe for ever.
if command -v setsid >/dev/null 2>&1; then
    printf '#!/usr/bin/env bash\nsetsid sleep %s &\nexec sleep %s\n' "$NAP" "$NAP" >"$TMP/escapee.sh"
    start=$(date +%s)
    got="$(cd "$MON" && timeout 60 python3 -c '
import sys, monitor_common as mc
r = mc.run_bounded(["bash", sys.argv[1]], 1.0, 4096)
print("timed_out=%s" % r.timed_out)
' "$TMP/escapee.sh" 2>&1)"; rc=$?
    took=$(( $(date +%s) - start ))
    pkill -f "sleep $NAP" 2>/dev/null
    if [ "$rc" = 0 ] && [ "$got" = "timed_out=True" ] && [ "$took" -le 10 ]; then
        pass "TC-1312 a setsid'd descendant holding the pipe does not hang the runner (${took}s)"
    else
        fail "TC-1312 rc=$rc took=${took}s got=$got"
    fi
else
    echo "SKIP  TC-1312 (no setsid)"
fi

mode="$(stat -c %a "$TMP/ev")"
if [ "$mode" = 700 ]; then pass "TC-1310 the evidence root is 0700"; else fail "TC-1310 evidence root mode $mode"; fi

printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
