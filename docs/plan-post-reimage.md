# Plan — finish Option E, and write the burn-in that never existed

Two items were left open when the hub was reimaged on 2026-08-28. Both are
self-contained and can start cold. Read the Current State header of
[status.md](status.md) first for the machine state; read
[../REMOTE_MANAGEMENT.md](../REMOTE_MANAGEMENT.md) Part 2 Option E and
[../remote-reimage/README.md](../remote-reimage/README.md) for how the reimage
path works.

---

## Task 1 — `Complete-RemoteReimage.ps1`: delete the tree once the install is confirmed

**The Owner's ruling (2026-08-28):** the ISO tree served over SMB should be
**deleted as soon as the install is confirmed complete**. The share itself stays
— it is standing infrastructure. The credential is explicitly *not* a concern
(private machine, private network); the tree is, for two reasons: a full
production image sitting on an exposed share, and staleness — a tree that
lingers is what a future reimage would silently install, with nothing checking
it is current.

**The constraint that makes this delicate.** The share is the **live root for
the entire install** — casper loop-mounts the squashfs stack off it rather than
copying to RAM (measured: four open handles on
`casper/ubuntu-server-minimal*.squashfs` mid-install). Deleting it early does not
merely lose the ISO, it kills the running installer, which is the one path that
leaves a half-wiped disk. So "confirmed" has to mean confirmed.

**Two tempting completion signals are WRONG, both measured on 2026-08-28:**

| signal | why it lies |
|---|---|
| `ping` answers | the initramfs answers ping the whole time it is failing |
| **tcp/22 is open** | **the live installer runs its own sshd.** A bare TCP check reported "install finished" about two minutes after the jump, while the installer was still running. This one actually fooled a session. |

The only trustworthy signal is **operator key authentication** — only the
installed image carries the key — *and* evidence the image is NEW, because a
failed netboot self-recovers into the OLD image, which also answers key auth.
That combination fooled a session too: ports opened, then the box turned out to
be the previous install.

**Algorithm.**

1. Record the trigger time before the jump.
2. Poll `ssh hub@<ip> true` until it exits 0 (bounded, ~45 min).
3. Assert the image is new, not the self-recovered old one:
   - `/var/log/installer/autoinstall-user-data` mtime is **after the trigger time**
   - `grep -c no-block /opt/homehub/stack/remote-ui/setup-remote-ui.sh` > 0
   - `which kexec` succeeds (proves the new packages.list landed)
4. Wait for `homehub-firstboot.service` to leave `activating`, then wait for
   `systemctl list-jobs` to drain — the `--no-block` desktop session start lands
   *after* firstboot's finish line, and asserting before the queue empties is
   what made the gate red on 2026-08-28. See `Wait-GuestBootSettled` in HomeHub's
   `scripts/launchers/Start-VirtualHomeHub.ps1` for the same fix.
5. Run `assert-installed.sh --target hub`. **Require ALL CHECKS PASSED.**
6. **Only then** empty the tree. Leave the share and its directory in place.

**Acceptance:** deletion is conditional on step 5, not on the install merely
finishing. A run that reaches step 4 and fails step 5 must leave the tree alone
and say so — a box that installed but did not come up green is exactly when
someone needs to reimage again immediately.

**Where it goes:** `remote-reimage/Complete-RemoteReimage.ps1`, referenced from
that directory's README beside the trigger.

---

## Task 2 — `HUB-BURN-IN.md`: C2 points at a checklist that does not exist

C2 in HomeHub's `open-items.md` requires "a 48-hour burn-in" and points at a
checklist. **There is no hub burn-in document.** The only one in the repo is
`stack/autoinstall/wall/WALL-BURN-IN.md` (280 lines), which is for the panel.
The reference is dangling and has been since the row was written.

Model the hub one on the wall one — its subtitle, *"what only the real panel can
settle"*, is the right frame. The gate already tells you the scope by listing
what it cannot prove:

> NOT proven: Wi-Fi, the real drives, thermals, SMART, VA-API, suspend, the AWOW firmware.

Sections worth having, at minimum:

- **The real drives.** Do the USB enclosures honour `hdparm -y` standby, and does
  `-C` report it? Does SMART read through the bridges? This overlaps C3, which is
  still open — fold it in rather than duplicating it.
- **A real backup run, end to end.** Including the Wake-on-LAN of Mini-serv,
  which has never run outside a sim, and the `BACKUP_SOURCES` paths that were
  missing on 2026-08-26 (`run_20260826_194055` failed with "source directory
  missing" for eight sets).
- **Thermals** under sustained load, in the location it will actually live.
- **Power-loss recovery** — does it come back unattended after a mains blip?
- **48 hours of the stack**: cert renewal, DDNS, unattended-upgrades, no leaks.
- **IceDrive still authenticated after a reboot.** The stored token surviving is
  the entire point of that lane, and it is per-reimage, not per-reboot.
- **`finance-auditor`**, which is in a restart loop and was in one on the old
  image too — so not a regression, but the one thing on the box that is not green.

**Ordering:** the box has not been physically moved. Burning in on both sides of
a move is work done twice — move it first.

---

## Things that will waste a day if you do not know them

All measured 2026-08-28.

1. **casper has no `nfsopts=` parameter.** It ignores CIFS credentials from the
   kernel command line and uses `-ouser=root,password=`, which Windows refuses
   with `STATUS_LOGON_FAILURE`. A **stock initrd will never mount an
   authenticated share.** The fix is the prepended `/conf/param.conf` segment
   built by `remote-reimage/build-initrd-e.sh`.
2. **`kexec_file_load` faults on 6.8.0-138** in `ima_add_kexec_buffer` once the
   IMA measurement list grows — cumulative, not random. It succeeded on a
   1-minute-old boot and failed on the very next call. **Reboot, then jump once.**
   It presents as a bare `Killed`; only `dmesg` says why.
3. **The console dies at the jump and `nomodeset` does not recover it.** A failed
   netboot leaves no evidence on headless hardware — the initramfs writes
   `casper.log` and never persists it. Reproduce in the lab instead: the watcher
   screenshots the VM console (`Get-LabConsoleShot`), and that is what found (1).
4. **A leftover lab VM holds the hub's DHCP reservation**, not just its ISO.
   `-KeepVms` costs both, and forgetting it once nearly put two machines on the
   hub's address on the live LAN.
5. **Size cannot distinguish a good ISO from a bad one** — ISO9660 block padding
   absorbed a 26-line edit, so the fixed image was the same byte length as the
   deadlocking one. Go by timestamp, or extract the file and diff it.
6. **Measure traffic on the right adapter.** The LAN lives on
   `vEthernet (HomeHub-Lab-External)`; the bare Realtek is bound to the vSwitch
   and carries nothing. Sampling the wrong one produced numbers that looked like
   data for an entire debugging session.
