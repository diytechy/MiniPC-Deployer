#!/usr/bin/env bash
# run-root-suite-in-qemu.sh - run scratch-fs.root-test.sh under a GIVEN Linux
# kernel (the hub's own, copied from it) in QEMU, so the quota cases are proven
# on the kernel that will enforce them rather than on the dev PC's WSL2 kernel,
# which has no ext4 quota support (CONFIG_QFMT_V2 unset).
#
#   bash run-root-suite-in-qemu.sh KERNEL MODULES_DIR
#     KERNEL       a bzImage, e.g. the hub's /boot/vmlinuz-6.8.0-139-generic
#     MODULES_DIR  that kernel's quota_tree.ko and quota_v2.ko (.ko or .ko.zst)
#                  from /lib/modules/<release>/kernel/fs/quota/
#
# Needs root and docker (WSL Ubuntu is fine). Everything happens inside one
# throwaway Alpine container: QEMU (TCG, no KVM needed), an initramfs built
# from Alpine packages plus stack/scratch, stack/door and stack/monitoring, and
# the suite as /init's payload. Nothing is installed on the host. A few minutes.
# Exit status: the suite's.
set -euo pipefail
KERNEL="$(readlink -f "${1:?KERNEL}")"
MODS="$(readlink -f "${2:?MODULES_DIR}")"
STACK="$(cd "$(dirname "$0")/../.." && pwd)"
# Alpine 3.20, by digest (the harness's only image).
ALPINE="alpine:3.20@sha256:${ALPINE_DIGEST:-d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc}"

docker run --rm -i -v "$KERNEL:/in/vmlinuz:ro" -v "$MODS:/in/mods:ro" -v "$STACK:/in/stack:ro" \
    "$ALPINE" sh -s <<'EOF'
set -e
apk add -q --no-cache qemu-system-x86_64 cpio zstd >/dev/null
R=/rootfs
mkdir -p $R/etc/apk && cp -r /etc/apk/keys $R/etc/apk/ && cp /etc/apk/repositories $R/etc/apk/
apk add -q --root $R --initdb --no-cache alpine-base bash python3 e2fsprogs e2fsprogs-extra lvm2 \
    util-linux coreutils findutils grep sed gawk kmod >/dev/null
mkdir -p $R/w4/stack $R/w4/mods
cp -r /in/stack/scratch /in/stack/door /in/stack/monitoring $R/w4/stack/
for m in quota_tree quota_v2; do
    if [ -f /in/mods/$m.ko.zst ]; then zstd -q -d -o $R/w4/mods/$m.ko /in/mods/$m.ko.zst; else cp /in/mods/$m.ko $R/w4/mods/; fi
done
cat >$R/init <<'INIT'
#!/bin/sh
# The kernel hands /init no PATH; python's subprocess would then search /bin:/usr/bin only.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
mount -t proc proc /proc; mount -t sysfs sys /sys; mount -t devtmpfs dev /dev
mkdir -p /tmp /run/lvm /dev/disk/by-uuid; mount -t tmpfs tmp /tmp
ln -s /proc/self/fd /dev/fd 2>/dev/null
for c in pvs vgs lvs pvcreate vgcreate lvcreate vgchange vgremove; do [ -e /sbin/$c ] || ln -s lvm /sbin/$c; done
insmod /w4/mods/quota_tree.ko; insmod /w4/mods/quota_v2.ko
echo "KERNEL $(uname -r)"
bash /w4/stack/scratch/tests/scratch-fs.root-test.sh; echo "SUITE-EXIT $?"
poweroff -f
INIT
chmod 0755 $R/init
(cd $R && find . | cpio -o -H newc 2>/dev/null | gzip -1) >/initrd.gz
timeout 1500 qemu-system-x86_64 -m 2560 -smp 2 -nographic -no-reboot -kernel /in/vmlinuz -initrd /initrd.gz \
    -append "console=ttyS0 quiet panic=-1 rdinit=/init" | tr -d '\r' | tee /console.log | grep -E '^(KERNEL|PASS|FAIL|SKIP|SUITE-EXIT|scratch-fs|     )'
grep -q '^SUITE-EXIT 0' /console.log || { echo '--- console tail ---'; tail -60 /console.log; exit 1; }
EOF
