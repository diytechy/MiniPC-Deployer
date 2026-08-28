#!/bin/bash
# Option E, one-shot: reimage this box by kexec-ing into the installer, with the
# ISO tree served over CIFS. Nothing is staged on the disk being wiped.
#
# TWO PHASES ON PURPOSE. Without --go it does everything EXCEPT jump: it mounts
# the share exactly as casper will, checks the tree, loads the kernel, and stops.
# A failure there costs nothing. --go adds the jump, and the jump is the point of
# no return: the next thing that touches this disk is an unattended installer.
#
# THE SAFETY PROPERTY THAT MATTERS: this script must never leave the machine with
# a kexec image ARMED unless it is about to execute it itself. An armed image is a
# disk wipe that fires on the next ordinary reboot, whenever that happens to be.
# Everything below about traps, unload checks and the initial state check exists
# for that one sentence. (Adversarial review, 2026-08-28.)
set -u

SERVER="${SERVER:-192.168.117.243}"
SHARE="${SHARE:-HubISO}"
CIFSUSER="${CIFSUSER:-hubread}"
CIFSPASS="${CIFSPASS:?set CIFSPASS}"
SEED="${SEED:-/cdrom/nocloud/}"
MNT="${MNT:-/run/kexec-reimage-preflight}"   # under /run: private, tmpfs, ours
GO=0
[ "${1:-}" = "--go" ] && GO=1

WE_MOUNTED=0
STAGE=""
COMMITTING=0

say() { printf '  %s\n' "$*"; }

cleanup() {
    rc=$?
    # Committing means kexec -e is running or has run; do NOT disarm it.
    if [ "$COMMITTING" -eq 0 ]; then
        if [ "$(cat /sys/kernel/kexec_loaded 2>/dev/null)" = "1" ]; then
            if kexec -u 2>/dev/null && [ "$(cat /sys/kernel/kexec_loaded)" = "0" ]; then
                say "cleanup: kexec image unloaded"
            else
                # Loud, because the box is now armed and a reboot wipes it.
                echo "  !! COULD NOT UNLOAD THE KEXEC IMAGE - THIS BOX IS ARMED." >&2
                echo "  !! A reboot will start an unattended install. Run: kexec -u" >&2
                rc=2
            fi
        fi
    fi
    [ "$WE_MOUNTED" -eq 1 ] && { umount "$MNT" 2>/dev/null && say "cleanup: share unmounted" \
        || echo "  !! the share is still mounted at $MNT" >&2; }
    [ -n "$STAGE" ] && rm -rf "$STAGE" 2>/dev/null
    exit $rc
}
# HUP matters most: this is normally run over SSH, and a dropped session between
# the load and the unload would otherwise leave the machine armed.
trap cleanup EXIT INT TERM HUP

die() { printf 'ABORT: %s\n' "$*" >&2; exit 1; }

echo "== 0. the credential must survive a kernel command line =="
# The cmdline is split on whitespace, and casper's param.conf splits it again;
# mount.cifs then splits -o options on commas. A password containing either
# cannot arrive intact, and the failure would look like a wrong password.
case "$CIFSPASS" in
    *[[:space:]]*) die "CIFSPASS contains whitespace - it cannot survive the kernel cmdline" ;;
    *,*)           die "CIFSPASS contains a comma - it would terminate the mount.cifs -o list" ;;
esac
say "credential is cmdline-safe"

echo "== 1. kexec available, and NOT already armed =="
command -v kexec >/dev/null || die "kexec-tools is not installed"
armed=$(cat /sys/kernel/kexec_loaded)
[ "$armed" = "0" ] || die "kexec_loaded=$armed - this box is ALREADY armed. Refusing to
       add to that. Inspect it, then clear with: kexec -u"
say "kexec: $(command -v kexec)   kexec_loaded=0"

echo "== 2. PRE-FLIGHT: mount the share exactly as casper will =="
CIFSOPTS="-ouser=$CIFSUSER,password=$CIFSPASS,ro,vers=3.0,sec=ntlmssp"
say "cifs opts: ${CIFSOPTS//$CIFSPASS/********}"
mkdir -p "$MNT"
mountpoint -q "$MNT" && die "$MNT is already a mountpoint - refusing to touch it"
mount.cifs "//$SERVER/$SHARE" "$MNT" "$CIFSOPTS" \
  || die "the CIFS mount failed - casper would fail here too, and nothing has been touched"
WE_MOUNTED=1
say "mounted //$SERVER/$SHARE read-only"

echo "== 3. the tree is what casper needs =="
for p in casper/vmlinuz casper/initrd-e .disk/casper-uuid-generic nocloud/user-data deploy-payload; do
    [ -e "$MNT/$p" ] || die "missing on the share: $p"
    say "present: $p"
done
say "tree UUID: $(cat "$MNT/.disk/casper-uuid-generic")"

echo "== 4. stage kernel + initrd locally (RAM-backed) =="
STAGE=$(mktemp -d /dev/shm/kx.XXXXXX) || die "no /dev/shm"
cp "$MNT/casper/vmlinuz"  "$STAGE/vmlinuz" || die "copy failed (vmlinuz)"
cp "$MNT/casper/initrd-e" "$STAGE/initrd"  || die "copy failed (patched initrd)"
say "staged in $STAGE ($(du -sh "$STAGE" | cut -f1))"
umount "$MNT" || die "could not unmount the preflight share - refusing to continue"
WE_MOUNTED=0
say "share unmounted - the mount is proven, casper will redo it"

echo "== 5. load the installer kernel (NOT executing) =="
# casper has no nfsopts= parameter; the patched initrd's /conf/param.conf reads
# cifsopts= instead. See ../REMOTE_MANAGEMENT.md Option E.
CMDLINE="netboot=cifs nfsroot=//$SERVER/$SHARE cifsopts=$CIFSOPTS ip=dhcp nomodeset console=tty0 loglevel=7 autoinstall ds=nocloud;s=$SEED ---"
say "cmdline: ${CMDLINE//$CIFSPASS/********}"
kexec -s -l "$STAGE/vmlinuz" --initrd="$STAGE/initrd" --command-line="$CMDLINE" || {
    echo "  kexec load FAILED. Last kernel words:"
    dmesg | tail -6 | sed 's/^/    /'
    if dmesg | tail -40 | grep -q ima_add_kexec_buffer; then
        die "kernel oops in ima_add_kexec_buffer (the IMA/kexec bug). NOT Secure Boot.
       Reboot to clear the IMA measurement list, then jump once, promptly."
    fi
    die "kexec load failed - see the kernel words above; nothing executed"
}
[ "$(cat /sys/kernel/kexec_loaded)" = "1" ] || die "kexec reported success but kexec_loaded=0"
say "LOADED. kexec_loaded=1"

if [ "$GO" -ne 1 ]; then
    echo
    echo "DRY RUN COMPLETE - everything worked and NOTHING was destroyed."
    echo "The trap now unloads the image; re-run with --go to commit."
    exit 0    # cleanup() unloads, unmounts and removes the staging dir
fi

echo "== 6. COMMITTING - syncing and jumping into the installer =="
sync
systemctl stop docker.socket docker.service 2>/dev/null
sync
COMMITTING=1          # from here the trap must NOT disarm the image
say "jumping now; this session will die and the disk will be wiped."
kexec -e
COMMITTING=0          # only reached if kexec -e itself failed
die "kexec -e returned - the jump did not happen; the image is still loaded"
