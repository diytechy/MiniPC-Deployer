#!/bin/bash
# Option E, one-shot: reimage this box by kexec-ing into the installer, with the
# ISO tree served over CIFS. Nothing is staged on the disk being wiped.
#
# TWO PHASES ON PURPOSE. Without --go it does everything EXCEPT jump: it mounts
# the share exactly as casper will, checks the tree, loads the kernel, and stops.
# A failure there costs nothing. --go adds the jump, and the jump is the point of
# no return: the next thing that touches this disk is an unattended installer.
set -u

SERVER="${SERVER:-192.168.117.243}"
SHARE="${SHARE:-HubISO}"
CIFSUSER="${CIFSUSER:-hubread}"
CIFSPASS="${CIFSPASS:?set CIFSPASS}"
SEED="${SEED:-/cdrom/nocloud/}"
MNT=/mnt/isopreflight
GO=0
[ "${1:-}" = "--go" ] && GO=1

say() { printf '  %s\n' "$*"; }
die() { printf 'ABORT: %s\n' "$*" >&2; umount "$MNT" 2>/dev/null; exit 1; }

echo "== 1. kexec available and unarmed =="
command -v kexec >/dev/null || die "kexec-tools is not installed"
say "kexec: $(command -v kexec)   kexec_loaded=$(cat /sys/kernel/kexec_loaded)"

echo "== 2. PRE-FLIGHT: mount the share exactly as casper will =="
CIFSOPTS="-ouser=$CIFSUSER,password=$CIFSPASS,ro,vers=3.0,sec=ntlmssp"
say "cifs opts: ${CIFSOPTS//$CIFSPASS/********}"
mkdir -p "$MNT"
umount "$MNT" 2>/dev/null
mount.cifs "//$SERVER/$SHARE" "$MNT" "$CIFSOPTS" \
  || die "the CIFS mount failed - casper would fail here too, and nothing has been touched"
say "mounted //$SERVER/$SHARE read-only"

echo "== 3. the tree is what casper needs =="
for p in casper/vmlinuz casper/initrd-e .disk/casper-uuid-generic nocloud/user-data deploy-payload; do
    [ -e "$MNT/$p" ] || die "missing on the share: $p"
    say "present: $p"
done
TREE_UUID=$(cat "$MNT/.disk/casper-uuid-generic")
say "tree UUID: $TREE_UUID"

echo "== 4. stage kernel + initrd locally (RAM-backed) =="
STAGE=$(mktemp -d /dev/shm/kx.XXXXXX) || die "no /dev/shm"
cp "$MNT/casper/vmlinuz" "$STAGE/vmlinuz" || die "copy failed"
cp "$MNT/casper/initrd-e" "$STAGE/initrd" || die "copy failed (patched initrd)"
say "staged in $STAGE ($(du -sh "$STAGE" | cut -f1))"
umount "$MNT" && say "share unmounted - the mount is proven, casper will redo it"

echo "== 5. load the installer kernel (NOT executing) =="
# nomodeset: kexec skips firmware POST, so the kernel often never re-inits the
# GPU and the console goes dark - which is exactly why the 2026-08-28 failure
# was undiagnosable. nomodeset forces the simple EFI framebuffer, whose address
# arrives in the boot params rather than being probed, so the screen usually
# paints after a kexec. loglevel=7 so what paints is worth reading.
# vers=3.0,sec=ntlmssp: the initramfs mount.cifs is a stripped environment and
# Windows 11 is strict about dialect and signing. Free negotiation worked from
# the full OS; that is not evidence it works from the initramfs.
CMDLINE="netboot=cifs nfsroot=//$SERVER/$SHARE cifsopts=$CIFSOPTS ip=dhcp nomodeset console=tty0 loglevel=7 autoinstall ds=nocloud;s=$SEED ---"
say "cmdline: ${CMDLINE//$CIFSPASS/********}"
kexec -s -l "$STAGE/vmlinuz" --initrd="$STAGE/initrd" --command-line="$CMDLINE" \
  || { echo "  kexec load FAILED. Last kernel words:"
       dmesg | tail -6 | sed 's/^/    /'
       if dmesg | tail -40 | grep -q ima_add_kexec_buffer; then
           die "kernel oops in ima_add_kexec_buffer (the IMA/kexec bug). NOT Secure Boot. Reboot to clear the IMA measurement list and retry promptly."
       fi
       die "kexec load failed - check the kernel words above; nothing executed"; }
say "LOADED. kexec_loaded=$(cat /sys/kernel/kexec_loaded)"

if [ "$GO" -ne 1 ]; then
    echo
    echo "DRY RUN COMPLETE - everything worked and NOTHING was destroyed."
    echo "Unloading so a stray reboot cannot jump into the installer."
    kexec -u
    echo "kexec_loaded=$(cat /sys/kernel/kexec_loaded).  Re-run with --go to commit."
    exit 0
fi

echo "== 6. COMMITTING - syncing and jumping into the installer =="
sync
systemctl stop docker.socket docker.service 2>/dev/null
sync
echo "  jumping now; this SSH session will die and the disk will be wiped."
kexec -e
