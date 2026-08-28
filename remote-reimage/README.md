# remote-reimage — Option E: reimage the hub over Ethernet, no media

Triggered from an SSH session on the running hub. The installer's kernel and
initrd are loaded straight into RAM (`kexec`) and jumped into, so **nothing is
staged on the disk being wiped** and no partition has to be reserved for it.
Rationale, the full ladder, and the measured caveats live in
[../REMOTE_MANAGEMENT.md](../REMOTE_MANAGEMENT.md) Part 2, Option E.

**Proven on the AK41 2026-08-28:** jump 23:06:46 → installed and answering on the
operator key 23:24:07, `assert-installed.sh` 5c ALL CHECKS PASSED.

## Why the initrd needs patching at all

`casper` parses `nfsroot=` and `netboot=` off the kernel command line but has
**no `nfsopts=` case**, so `do_cifsmount` always uses its hardcoded fallback
`-ouser=root,password=` — which a Windows share refuses with
`STATUS_LOGON_FAILURE`. There is no supported way to hand casper CIFS
credentials.

`build-initrd-e.sh` prepends a 2 KB cpio segment carrying `/conf/param.conf`.
`run_scripts()` *sources* the hook directory's `ORDER`, which sources
`/conf/param.conf` after each hook — casper's own channel for setting variables
in its scope, and it runs before the netboot block. That file copies `cifsopts=`
off the command line into `NFSOPTS`. **No secret is baked into the initrd**; the
credential rides the cmdline exactly as it did before.

## Use

```sh
# 1. extract the production ISO to a tree and share it read-only over SMB
#    (Windows: New-SmbShare -Name HubISO -Path Z:\hub-isotree -ReadAccess <user>)
xorriso -osirrox on -indev repacked.iso -extract / /mnt/z/hub-isotree

# 2. build the patched initrd next to the stock one on that tree
TREE=/mnt/z/hub-isotree bash build-initrd-e.sh
cp /tmp/initrd-e/ebuild/initrd-e /mnt/z/hub-isotree/casper/initrd-e

# 3. on the hub: rehearse (mounts the share, checks the tree, loads the kernel,
#    then UNLOADS and stops — nothing destructive)
sudo CIFSPASS=... bash kexec-reimage.sh

# 4. commit
sudo CIFSPASS=... bash kexec-reimage.sh --go
```

## Read these before you run it

- **The share is the live root for the whole install.** casper loop-mounts the
  squashfs stack off it rather than copying to RAM. The serving machine must stay
  awake; losing it after partitioning starts is the one path to a half-wiped disk.
- **Reboot, then jump once.** `kexec_file_load` faults in `ima_add_kexec_buffer`
  on 6.8.0-138 once the IMA measurement list grows — cumulative, not random. It
  presents as a bare `Killed`; the reason is only in `dmesg`.
- **The console goes dark after the jump and `nomodeset` does not recover it.**
  A failed netboot leaves no evidence on headless hardware. Reproduce in the lab,
  whose watcher screenshots the VM console, rather than debugging on the box.
- **Keep a written USB stick.** E needs a booting, reachable OS; nothing here
  recovers a dead GRUB or an already-wiped disk.
