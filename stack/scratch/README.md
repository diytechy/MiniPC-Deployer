# scratch — the hub's NVMe scratch volume (SN-050)

Implements **SR-102** (the volume), **SR-103** (the mount guard and the writer
unit pattern) and **SR-104** (enforced budgets). Interface for writers: **IF-071**.

An 18 GiB LV `hub-scratch` in the root disk's volume group, ext4 with project
quotas, mounted at `/srv/scratch`, for recording and staging until a new drive
is installed (the Owner, 2026-10-22). It is **scratch**: never backed up, never
shared (HomeHub `scripts/deploy/storage-map.md` sections 1 and 3c), and a
reimage recreates it empty.

| Directory | Writer | Quota project | Budget (knob) |
|---|---|---|---|
| `door/` | the door recorder (`stack/door`) | 7101 | 8 GiB (`HOMEHUB_SCRATCH_QUOTA_DOOR_MIB`) |
| `meetings/` | meeting recording (plan W6) | 7102 | 2 GiB (`..._MEETINGS_MIB`) |
| `ottertube/` | OtterTube downloads, publication, staging, temp, logs (plan W8) | 7103 | 6 GiB, reduced first (`..._OTTERTUBE_MIB`) |

## `homehub_scratch.py`

| Verb | What |
|---|---|
| `plan` | read-only: the capacity arithmetic, the quota sizes and every action `ensure` would take. Exit 0 feasible / nothing to do, **3 STOP**, **4 REFUSE**. |
| `ensure` | do it, non-destructively and idempotently (below). Prints a JSON report. |
| `status` | read-only JSON: identity, prjquota, usable/free space, each writer's usage and limit (read back through quotactl). |
| `guard --writer NAME` | the mount guard. Exit 0 = write, 1 = refuse, one journal line naming why. |

**What `ensure` will never do**: touch an LV other than its own, resize its own,
format a device on which `blkid -p` finds any signature (other than its own
labelled ext4), accept its own label with a UUID other than
`HOMEHUB_SCRATCH_UUID`, edit an fstab line it did not write, or mount over a
non-empty bare mount point (which would mean something wrote to `/` while the
volume was absent). Each is exit 4 with the reason. When the VG would keep less
than `HOMEHUB_SCRATCH_VG_RESERVE_MIB` (4 GiB) unallocated it STOPS (exit 3)
having changed nothing. Shrinking an LV to make room is the Owner's call.

The filesystem: `mkfs.ext4 -O quota,project -E quotatype=prjquota -m 0 -i 65536
-L hub-scratch [-U $HOMEHUB_SCRATCH_UUID]`. `-m 0` because reserved blocks only
keep non-root writers out and every writer here is uid 0; the floor is held by
the quotas' sum instead. Mounted by an fstab line by UUID with
`nofail,noatime,prjquota` (prjquota is what turns enforcement on); the bare
mount point underneath is mode 000.

**The budgets** are sized after mkfs from the filesystem's usable space —
`f_blocks` less what ext4 holds back from every writer (`f_bfree - f_bavail`,
its reserved clusters: measured, 16 MiB) — so that all three writers at their
quotas still leave `HOMEHUB_SCRATCH_FREE_FLOOR_MIB` (2 GiB) free; ottertube is
reduced first, then meetings, then door, none below 512 MiB. The report prints
the numbers; record them.

### Reimage: recreated, deliberately

Firstboot step 1b-scratch runs `ensure` after the docker LV step whenever
`HOMEHUB_SCRATCH_ENABLED=true`. The tracked config describes a hub with the
volume and its writers refuse to start without it, so a reflash that left it out
would come up with the recorder down. The data does not survive (scratch); the
identity does: with `HOMEHUB_SCRATCH_UUID` set the filesystem is made with that
UUID, so the guard knob stays true. On a disk too small (the lab VHDX) the step
STOPs, logs it, and the writers stay down. It never fails firstboot.

## The mount guard and the writer pattern (IF-071)

The guard passes only when **all** hold: `/srv/scratch` is a mount point;
`HOMEHUB_SCRATCH_UUID` is set; `/dev/disk/by-uuid/<it>` is the mounted device;
ext4 mounted with prjquota; not read-only; the caller does **not** hold
`CAP_SYS_RESOURCE`; `/srv/scratch/<writer>` is a real directory on that
filesystem in the writer's quota project (inheriting); and one 4 KiB block can
be written and synced there (an empty probe file succeeds even in a full quota).

A writer:

* runs the guard before it starts (`ExecStartPre=`) **and before every new
  output file**, stopping on a refusal rather than opening the next path;
* is a unit with `RequiresMountsFor=/srv/scratch`, `BindsTo=` and
  `After=srv-scratch.mount` (it stops when the mount goes);
* has `CapabilityBoundingSet=~CAP_SYS_RESOURCE`. **Without it the quota is not a
  wall**: the kernel ignores hard limits for a holder of that capability
  (`fs/quota/dquot.c ignore_hardlimit`) — measured, a root writer wrote 8 MiB
  past a full quota;
* stops cleanly on EDQUOT/ENOSPC and alerts through
  [`../monitoring/homehub-alert.sh`](../monitoring/README.md), never writing
  elsewhere, and is not restarted in a loop for it;
* if it starts a container: `restart: "no"` (its unit starts it) and long-syntax
  bind mounts with `create_host_path: false`.

## Tests

* `tests/test_hub_scratch.py` (pytest, dev PC): capacity, budgets, the
  provisioner's refusals and plan, every guard verdict (TC-1370..TC-1375).
* `tests/scratch-fs.root-test.sh` (root; TC-1376): a real loop-backed VG and
  ext4 with project quotas at 1/28 scale — 14 checks: STOP, ensure, budgets,
  idempotence, the capability rule, each writer at EDQUOT with the others still
  writing, all three full keeping the floor, the bypass, read-only, wrong
  device, unmounted, unmount mid-run, the recorder at its quota, status. It
  SKIPS on a kernel without ext4 quota support (WSL2 has no `CONFIG_QFMT_V2`);
  `tests/run-root-suite-in-qemu.sh <vmlinuz> <quota-modules-dir>` runs it under
  the hub's own kernel in QEMU inside a throwaway container.
