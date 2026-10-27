#!/usr/bin/env python3
"""homehub_scratch.py - the hub's NVMe scratch volume: provision it, budget it,
and guard every write onto it (SN-050, SR-102..SR-104).

ONE FILE, FOUR VERBS, because the facts they share are one set: the mount point,
the label, the writers' directories and their quota projects, the expected
filesystem identity. A writer that guarded against a different list than the
provisioner created would guard nothing.

    plan     read-only. What `ensure` would do, the capacity arithmetic, and the
             quota sizes. Exit 0 = nothing to do or feasible, 3 = STOP (the VG
             cannot give the volume and keep its reserve), 4 = REFUSE (the
             machine holds something this script did not make).
    ensure   do it, non-destructively and idempotently. Every step asks its END
             STATE first (firstboot's rule: never ask "did I already do this?"
             of your own output), so a run killed half-way resumes - except
             between creating the LV and formatting it: it NEVER touches an
             existing LV other than its own, NEVER formats an LV it did not
             create in the same run (an existing one with no signature is
             refused, not formatted), NEVER formats a device on which blkid
             finds any signature, and NEVER edits an fstab line it did not
             write. Anything unexpected is exit 4 with the reason.
    status   read-only: identity, mount state, per-writer usage against quota.
    guard    the mount guard (SR-103). `guard --writer door` exits 0 only when
             /srv/scratch is mounted, is the filesystem whose UUID is the
             HOMEHUB_SCRATCH_UUID knob, is mounted with project quotas enforced,
             is writable, and the writer's directory is a real directory on that
             filesystem inside its own quota project. Every writer calls it
             before it starts (ExecStartPre) and before every new output file.

WHY THE GUARD EXISTS. /srv/scratch is a directory on / when the volume is not
mounted. A writer that opened /srv/scratch/door/... then would fill the root
filesystem of the box that serves the LAN's DNS - the failure firstboot step 1b
moved docker off / to prevent. systemd's RequiresMountsFor= covers the start;
the guard covers the identity (a wrong device mounted there) and the run (an
unmount between two files).

THE QUOTA BYPASS (measured from the kernel source, fs/quota/dquot.c
ignore_hardlimit): a process holding CAP_SYS_RESOURCE ignores ext4 hard quota
limits. Every writer unit therefore drops that capability
(CapabilityBoundingSet=~CAP_SYS_RESOURCE); the guard checks the calling process
does not hold it, because a writer that kept it would write straight through its
budget and the "one writer cannot fill the volume" promise would be false.

Python 3.8-compatible: the dev PC's test runner imports this module.
Implements: SR-102, SR-103, SR-104, LLR-1215..LLR-1222.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
import json
import os
import re
import stat
import struct
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "monitoring"))
import monitor_common as mc  # noqa: E402  (the hub's shared knob reader and logger)

GIB = 1024 ** 3
MIB = 1024 ** 2

MOUNTPOINT = "/srv/scratch"
LABEL = "hub-scratch"
LV_NAME = "hub-scratch"
FSTYPE = "ext4"
# The mount options firstboot and the coordinator write into fstab. prjquota is
# what turns ENFORCEMENT on: the `quota` feature alone only accounts (ext4
# super.c ext4_enable_quotas: DQUOT_LIMITS_ENABLED only with the mount option).
# nofail: a missing volume must never stop the hub booting; the writers are the
# ones that stay down, through BindsTo=srv-scratch.mount.
FSTAB_OPTIONS = "defaults,nofail,noatime,prjquota,x-systemd.device-timeout=15s"
# ext4 with project quotas; prjquota-only quota inode (no user/group quotas are
# used). -m 0: reserved blocks only keep NON-root writers out, every writer here
# runs as uid 0 (and so may use them anyway), and the free floor is enforced by
# the quotas' sum instead - reserving blocks would only shrink the usable space
# the floor is computed from. -i 65536: one inode per 64 KiB, ~290k on 18 GiB,
# far above what 10-minute segments and downloads need, and ~220 MiB less inode
# table than the default 16 KiB ratio.
MKFS_ARGS = ["-q", "-t", "ext4", "-O", "quota,project", "-E", "quotatype=prjquota",
             "-m", "0", "-i", "65536", "-L", LABEL]

# The writers, their quota project ids and default budgets. ORDER IS THE
# REDUCTION ORDER when the budgets do not fit: last first, i.e. ottertube is
# reduced before clips, clips before meetings, meetings before door (the plan:
# "reduce ottertube first"). Project ids are arbitrary but fixed: a reimage that
# recreates the filesystem must give each directory the same project it had.
#
# clips/ (2026-10-27, SR-131): the motion clips' small, capped area until the
# media drive exists (the Owner, 2026-10-26; HomeHub open-items C111). It sits
# before ottertube, so its room comes out of ottertube's ceiling (about 4800 MiB
# granted on the volume grown to 22 GiB, with clips at 5 GiB; ottertube uses under
# 1 GiB of a 3 GiB list budget), not out of the door recording the clips are cut from.
WRITERS: Tuple[Tuple[str, int, str, int], ...] = (
    ("door", 7101, "HOMEHUB_SCRATCH_QUOTA_DOOR_MIB", 8 * 1024),
    ("meetings", 7102, "HOMEHUB_SCRATCH_QUOTA_MEETINGS_MIB", 2 * 1024),
    ("clips", 7104, "HOMEHUB_SCRATCH_QUOTA_CLIPS_MIB", 5 * 1024),
    ("ottertube", 7103, "HOMEHUB_SCRATCH_QUOTA_OTTERTUBE_MIB", 6 * 1024),
)
WRITER_NAMES = tuple(w[0] for w in WRITERS)
PROJECT_ID = {w[0]: w[1] for w in WRITERS}

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

EXIT_OK, EXIT_REFUSE_GUARD, EXIT_STOP, EXIT_REFUSE = 0, 1, 3, 4


class Refused(Exception):
    """The machine holds something this script did not make; touch nothing."""


class Stop(Exception):
    """The plan's capacity condition does not hold; nothing was changed."""


# ── knobs ────────────────────────────────────────────────────────────────────


class Settings:
    """Every knob this volume reads, resolved once (stack .env + override file).

    Contract:
      Config:  HOMEHUB_SCRATCH_UUID (the expected filesystem UUID, lowercase; empty
               = not yet created), HOMEHUB_SCRATCH_SIZE_MIB (22528: grown online
               from 18432 when raised, never shrunk),
               HOMEHUB_SCRATCH_VG_RESERVE_MIB (1200: VG space left unallocated by a
               create or a grow; it was 4096 until the 2026-10-27 grow ruling),
               HOMEHUB_SCRATCH_FREE_FLOOR_MIB (2048: usable space left free when
               every writer is at its quota), HOMEHUB_SCRATCH_MIN_QUOTA_MIB (512:
               a writer reduced below this is a STOP, not a budget), and one
               HOMEHUB_SCRATCH_QUOTA_<WRITER>_MIB per writer.  [stack/.env]
    """

    def __init__(self, env: Dict[str, str]):
        uuid = env.get("HOMEHUB_SCRATCH_UUID", "").strip().lower()
        self.uuid = uuid if UUID_RE.fullmatch(uuid) else ""
        self.uuid_malformed = bool(uuid) and not self.uuid
        self.size_mib = mc.knob_int(env, "HOMEHUB_SCRATCH_SIZE_MIB", 22 * 1024, 64, 1 << 22)
        self.vg_reserve_mib = mc.knob_int(env, "HOMEHUB_SCRATCH_VG_RESERVE_MIB", 1200, 0, 1 << 22)
        self.free_floor_mib = mc.knob_int(env, "HOMEHUB_SCRATCH_FREE_FLOOR_MIB", 2 * 1024, 0, 1 << 22)
        self.min_quota_mib = mc.knob_int(env, "HOMEHUB_SCRATCH_MIN_QUOTA_MIB", 512, 1, 1 << 22)
        self.quota_mib = {name: mc.knob_int(env, knob, default, 0, 1 << 22)
                          for name, _pid, knob, default in WRITERS}


# ── pure core: capacity, budgets, verdicts ───────────────────────────────────


def capacity_plan(free_extents: int, extent_bytes: int, size_mib: int, reserve_mib: int) -> Dict[str, int]:
    """How many extents the LV takes and what the VG keeps; raise Stop if the
    reserve would be broken.

    Contract:
      Inputs:  free_extents, extent_bytes: `vgs -o vg_free_count,vg_extent_size`.
               size_mib: the LV size wanted; reserve_mib: VG space that must stay
               unallocated afterwards (SR-102).
      Outputs: {lv_extents, leftover_extents, leftover_bytes}
      Raises:  Stop with the exact numbers when free - lv < reserve. Nothing is
               ever shrunk to make room: that is the Owner's call (plan W4a.1).
    """
    if extent_bytes <= 0:
        raise Stop("the volume group reports an extent size of %d bytes" % extent_bytes)
    lv_extents = -(-size_mib * MIB // extent_bytes)  # ceiling
    reserve_extents = -(-reserve_mib * MIB // extent_bytes)
    leftover = free_extents - lv_extents
    if leftover < reserve_extents:
        raise Stop(
            "the VG has %d free extents of %d bytes (%.2f GiB); an LV of %d MiB takes %d and "
            "would leave %d (%.2f GiB), below the %d MiB reserve. Nothing was changed; "
            "do not shrink an existing LV to make room."
            % (free_extents, extent_bytes, free_extents * extent_bytes / GIB, size_mib,
               lv_extents, max(leftover, 0), max(leftover, 0) * extent_bytes / GIB, reserve_mib))
    return {"lv_extents": lv_extents, "leftover_extents": leftover,
            "leftover_bytes": leftover * extent_bytes}


def grow_plan(lv_bytes: int, want_mib: int, free_extents: int, extent_bytes: int,
              reserve_mib: int) -> Optional[Dict[str, int]]:
    """How far the existing LV grows, or None when it is already at least the
    size wanted - it is NEVER shrunk (pure).

    Contract:
      Inputs:  lv_bytes: the LV's size now; want_mib: HOMEHUB_SCRATCH_SIZE_MIB;
               free_extents, extent_bytes: the VG's; reserve_mib: VG space that
               must stay unallocated afterwards.
      Outputs: {extents, new_mib, leftover_extents, leftover_bytes}.
      Raises:  Stop with the exact numbers when the VG cannot give the extents
               and keep its reserve. Nothing was changed.
    Implements: SR-102, SR-131, LLR-1380.
    """
    if extent_bytes <= 0:
        raise Stop("the volume group reports an extent size of %d bytes" % extent_bytes)
    want = want_mib * MIB
    if want <= lv_bytes:
        return None
    extents = -(-(want - lv_bytes) // extent_bytes)  # ceiling
    reserve_extents = -(-reserve_mib * MIB // extent_bytes)
    leftover = free_extents - extents
    if leftover < reserve_extents:
        raise Stop("growing the LV from %d to %d MiB takes %d extents of %d bytes; the VG has %d free and must keep "
                   "%d MiB (%d extents) unallocated. Nothing was changed; lower HOMEHUB_SCRATCH_SIZE_MIB or free VG "
                   "space first" % (lv_bytes // MIB, want_mib, extents, extent_bytes, free_extents, reserve_mib,
                                    reserve_extents))
    return {"extents": extents, "new_mib": (lv_bytes + extents * extent_bytes) // MIB,
            "leftover_extents": leftover, "leftover_bytes": leftover * extent_bytes}


def size_quotas(usable_bytes: int, requested_mib: Dict[str, int], floor_mib: int, min_mib: int) -> Dict[str, int]:
    """The hard quota per writer, in MiB, so that every writer at its quota still
    leaves `floor_mib` of the usable space free (SR-104).

    Contract:
      Inputs:  usable_bytes: usable_bytes() of the MOUNTED filesystem (after
               metadata, the journal and ext4's reserved clusters).
               requested_mib: {writer: wanted MiB}, the knob values.
      Outputs: {writer: granted MiB}. Writers are reduced in REVERSE WRITERS
               order (ottertube first), each no lower than min_mib, until the
               sum fits.
      Raises:  Stop when even every writer at min_mib does not fit.
    """
    budget = usable_bytes // MIB - floor_mib
    if budget <= 0:
        raise Stop("the filesystem has %d MiB usable, not more than the %d MiB free floor"
                   % (usable_bytes // MIB, floor_mib))
    # Every writer, always: a writer left out would get no project limit at all,
    # and sizing a subset against the whole budget would hand out the others' room.
    missing = [name for name in WRITER_NAMES if name not in requested_mib]
    if missing:
        raise Stop("size_quotas needs every writer; missing: %s" % ", ".join(missing))
    granted = {name: requested_mib[name] for name in WRITER_NAMES}
    excess = sum(granted.values()) - budget
    for name in reversed(WRITER_NAMES):
        if excess <= 0:
            break
        # Each writer gives up space down to the minimum, never below it: a
        # budget too small to hold one working set is not a budget.
        spare = max(granted[name] - min_mib, 0)
        cut = min(excess, spare)
        granted[name] -= cut
        excess -= cut
    if excess > 0:
        raise Stop("%d MiB usable cannot hold the %d MiB free floor plus every writer at its %d MiB "
                   "minimum (still %d MiB over after reducing ottertube, then clips, then meetings, then door)"
                   % (usable_bytes // MIB, floor_mib, min_mib, excess))
    return granted


def judge_guard(facts: Dict[str, object]) -> Tuple[bool, str]:
    """The mount guard's verdict from gathered facts; first failure wins.

    Contract:
      Inputs:  facts (all from gather_guard_facts): writer, known_writer,
               expected_uuid, uuid_malformed, mounted, mount_majmin, fstype,
               superopts, uuid_majmin (the device /dev/disk/by-uuid/<uuid> names,
               or None), read_only, dir_kind ("dir"|"missing"|"symlink"|"other"),
               dir_dev_ok, project_id, project_inherit, has_cap_sys_resource,
               probe_error (None or an errno name).
      Outputs: (ok, reason). reason is one short sentence naming the failure,
               or "ok" - it is the journal line the writer logs.
    Implements: SR-103, LLR-1218.
    """
    w = facts.get("writer")
    if not facts.get("known_writer"):
        return False, "unknown writer %r (known: %s)" % (w, " ".join(WRITER_NAMES))
    if facts.get("uuid_malformed"):
        return False, "HOMEHUB_SCRATCH_UUID is not a filesystem UUID; refusing to guess the volume"
    if not facts.get("expected_uuid"):
        return False, "HOMEHUB_SCRATCH_UUID is unset; without the expected identity any mount would pass"
    if not facts.get("mounted"):
        return False, "%s is not a mount point (the scratch volume is not mounted)" % MOUNTPOINT
    if facts.get("uuid_majmin") is None:
        return False, "no device has UUID %s (the expected scratch filesystem is absent)" % facts["expected_uuid"]
    if facts.get("uuid_majmin") != facts.get("mount_majmin"):
        return False, ("%s is mounted from device %s, but UUID %s is device %s (wrong device)"
                       % (MOUNTPOINT, facts.get("mount_majmin"), facts["expected_uuid"], facts.get("uuid_majmin")))
    if facts.get("fstype") != FSTYPE:
        return False, "%s is %s, not %s" % (MOUNTPOINT, facts.get("fstype"), FSTYPE)
    if "prjquota" not in str(facts.get("superopts", "")).split(","):
        return False, "%s is mounted without prjquota: quotas are not enforced" % MOUNTPOINT
    if facts.get("read_only"):
        return False, "%s is mounted read-only" % MOUNTPOINT
    if facts.get("has_cap_sys_resource"):
        return False, ("this process holds CAP_SYS_RESOURCE, which ignores quota hard limits; "
                       "the writer unit must drop it (CapabilityBoundingSet=~CAP_SYS_RESOURCE)")
    kind = facts.get("dir_kind")
    if kind != "dir":
        return False, "%s/%s is %s, not a directory" % (MOUNTPOINT, w, kind)
    if not facts.get("dir_dev_ok"):
        return False, "%s/%s is not on the scratch filesystem" % (MOUNTPOINT, w)
    if facts.get("project_id") != PROJECT_ID[str(w)] or not facts.get("project_inherit"):
        return False, ("%s/%s is in quota project %s (inherit %s), not %d: it would write outside its budget"
                       % (MOUNTPOINT, w, facts.get("project_id"), facts.get("project_inherit"), PROJECT_ID[str(w)]))
    if facts.get("probe_error"):
        return False, "%s/%s is not writable (%s)" % (MOUNTPOINT, w, facts["probe_error"])
    return True, "ok"


# ── small I/O helpers (each one thing, each mockable) ───────────────────────


def parse_mountinfo(text: str) -> List[Dict[str, str]]:
    """proc(5) mountinfo -> [{mount, majmin, fstype, source, superopts}].

    Not monitor_common.parse_mountinfo: that one deliberately keeps four fields
    (its test pins the shape) and the guard needs the super options, which is
    where `prjquota` is reported.
    """
    out = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if not sep or len(fields) < 5 or len(tail) < 2:
            continue
        unescape = lambda f: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), f)  # noqa: E731
        out.append({"mount": unescape(fields[4]), "majmin": fields[2], "fstype": tail[0],
                    "source": unescape(tail[1]), "superopts": tail[2] if len(tail) > 2 else ""})
    return out


def mount_of(mounts: List[Dict[str, str]], path: str) -> Optional[Dict[str, str]]:
    """The entry whose mount point IS path; the last wins, as the kernel stacks them."""
    found = None
    for entry in mounts:
        if entry["mount"] == path:
            found = entry
    return found


def majmin_of_rdev(path: str) -> Optional[str]:
    """'MAJ:MIN' of the block device a path (e.g. a by-uuid symlink) resolves to."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISBLK(st.st_mode):
        return None
    return "%d:%d" % (os.major(st.st_rdev), os.minor(st.st_rdev))


# FS_IOC_FSGETXATTR / FS_IOC_FSSETXATTR (linux/fs.h): struct fsxattr is
# u32 xflags, u32 extsize, u32 nextents, u32 projid, u32 cowextsize, 8 pad.
_FSXATTR = struct.Struct("=IIIII8x")
FS_IOC_FSGETXATTR = 0x801C581F
FS_IOC_FSSETXATTR = 0x401C5820
FS_XFLAG_PROJINHERIT = 0x00000200


def get_project(path: str) -> Tuple[Optional[int], bool]:
    """(project id, inherit flag) of a directory, or (None, False) if unreadable."""
    import fcntl  # local: the dev PC's test runner (Windows) has no fcntl

    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None, False
    try:
        buf = fcntl.ioctl(fd, FS_IOC_FSGETXATTR, bytes(_FSXATTR.size))
        xflags, _ext, _n, projid, _cow = _FSXATTR.unpack(buf)
        return projid, bool(xflags & FS_XFLAG_PROJINHERIT)
    except OSError:
        return None, False
    finally:
        os.close(fd)


def set_project(path: str, project_id: int) -> None:
    """Give a directory a project id with inheritance (`chattr -p ID +P`)."""
    import fcntl  # local, as in get_project

    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        buf = fcntl.ioctl(fd, FS_IOC_FSGETXATTR, bytes(_FSXATTR.size))
        xflags, ext, n, _old, cow = _FSXATTR.unpack(buf)
        fcntl.ioctl(fd, FS_IOC_FSSETXATTR, _FSXATTR.pack(xflags | FS_XFLAG_PROJINHERIT, ext, n, project_id, cow))
    finally:
        os.close(fd)


def holds_cap_sys_resource(status_path: str = "/proc/self/status") -> bool:
    """True when this process's EFFECTIVE set holds CAP_SYS_RESOURCE (bit 24)."""
    try:
        with open(status_path, encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("CapEff:"):
                    return bool(int(line.split()[1], 16) & (1 << 24))
    except (OSError, ValueError, IndexError):
        pass
    return False


# quotactl(2) for ext4 project quotas through libc: no quota-tools package is
# needed on the hub. if_dqblk limits are in 1 KiB blocks (QIF_DQBLKSIZE).
_Q_GETQUOTA, _Q_SETQUOTA, _PRJQUOTA, _QIF_BLIMITS = 0x800007, 0x800008, 2, 1


class _IfDqblk(ctypes.Structure):
    _fields_ = [("bhardlimit", ctypes.c_uint64), ("bsoftlimit", ctypes.c_uint64),
                ("curspace", ctypes.c_uint64), ("ihardlimit", ctypes.c_uint64),
                ("isoftlimit", ctypes.c_uint64), ("curinodes", ctypes.c_uint64),
                ("btime", ctypes.c_uint64), ("itime", ctypes.c_uint64), ("valid", ctypes.c_uint32)]


def _quotactl(cmd: int, device: str, qid: int, dq: _IfDqblk) -> None:
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    full = (cmd << 8) | _PRJQUOTA
    full = full - (1 << 32) if full >= (1 << 31) else full
    if libc.quotactl(ctypes.c_int(full), device.encode(), ctypes.c_int(qid), ctypes.byref(dq)) != 0:
        code = ctypes.get_errno()
        raise OSError(code, "quotactl(%s, project %d): %s" % (device, qid, os.strerror(code)))


def get_project_quota(device: str, project_id: int) -> Tuple[int, int]:
    """(used bytes, hard limit bytes; 0 = none) for one project."""
    dq = _IfDqblk()
    _quotactl(_Q_GETQUOTA, device, project_id, dq)
    return int(dq.curspace), int(dq.bhardlimit) * 1024


def set_project_quota(device: str, project_id: int, hard_mib: int) -> None:
    """Set one project's block hard limit (soft = hard: no grace window)."""
    dq = _IfDqblk()
    dq.bhardlimit = dq.bsoftlimit = hard_mib * 1024
    dq.valid = _QIF_BLIMITS
    _quotactl(_Q_SETQUOTA, device, project_id, dq)


# ── the guard (I/O shell around judge_guard) ────────────────────────────────


def write_probe(directory: str) -> Optional[str]:
    """None when one block can be written and synced in `directory`, else the
    errno name. A whole block, not an empty file: an empty file needs no block,
    so it is created happily in a writer whose quota is already full (measured
    on the hub's kernel), and the guard would pass a writer that cannot write."""
    probe = os.path.join(directory, ".scratch-guard-%d" % os.getpid())
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
    try:
        os.write(fd, b"\0" * 4096)
        os.fsync(fd)
        return None
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
    finally:
        os.close(fd)
        try:
            os.unlink(probe)
        except OSError:
            pass


class Paths:
    """Every host path the module reads or writes; tests point them elsewhere."""

    def __init__(self, root: str = "/"):
        def at(p: str) -> str:
            return os.path.join(root, p.lstrip("/"))
        self.mountpoint = at(MOUNTPOINT)
        self.mountpoint_name = MOUNTPOINT if root == "/" else self.mountpoint
        self.mountinfo = "/proc/self/mountinfo"
        self.by_uuid = "/dev/disk/by-uuid"
        self.fstab = at("/etc/fstab")
        self.status = "/proc/self/status"


def gather_guard_facts(writer: str, settings: Settings, paths: Paths) -> Dict[str, object]:
    """Read everything judge_guard needs. Touches nothing but a probe file."""
    facts: Dict[str, object] = {"writer": writer, "known_writer": writer in PROJECT_ID,
                                "expected_uuid": settings.uuid, "uuid_malformed": settings.uuid_malformed}
    try:
        with open(paths.mountinfo, encoding="utf-8", errors="replace") as handle:
            entry = mount_of(parse_mountinfo(handle.read()), paths.mountpoint_name)
    except OSError:
        entry = None
    facts["mounted"] = entry is not None
    if entry:
        facts.update(mount_majmin=entry["majmin"], fstype=entry["fstype"], superopts=entry["superopts"])
    facts["uuid_majmin"] = majmin_of_rdev(os.path.join(paths.by_uuid, settings.uuid)) if settings.uuid else None
    if entry is None:
        return facts  # judge_guard refuses on `mounted` before it reads anything else
    try:
        facts["read_only"] = bool(os.statvfs(paths.mountpoint).f_flag & os.ST_RDONLY)
    except OSError:
        facts["read_only"] = True
    facts["has_cap_sys_resource"] = holds_cap_sys_resource(paths.status)
    if writer not in PROJECT_ID:
        return facts
    wdir = os.path.join(paths.mountpoint, writer)
    try:
        lst = os.lstat(wdir)
        facts["dir_kind"] = ("dir" if stat.S_ISDIR(lst.st_mode) else
                             "symlink" if stat.S_ISLNK(lst.st_mode) else "other")
    except FileNotFoundError:
        facts["dir_kind"] = "missing"
    except OSError as exc:
        facts["dir_kind"] = "unreadable (%s)" % errno.errorcode.get(exc.errno, exc.errno)
    if facts["dir_kind"] == "dir":
        maj, mnr = (int(x) for x in entry["majmin"].split(":"))
        facts["dir_dev_ok"] = lst.st_dev == os.makedev(maj, mnr)
        facts["project_id"], facts["project_inherit"] = get_project(wdir)
        facts["probe_error"] = write_probe(wdir)
    return facts


def guard(writer: str, settings: Settings, paths: Optional[Paths] = None) -> Tuple[bool, str]:
    """The mount guard (SR-103): (ok, reason). The one function every writer calls.

    Contract:
      Inputs:  writer: one of WRITER_NAMES. settings: Settings from the stack .env.
      Outputs: (True, "ok") only when every judge_guard condition holds.
      Raises:  nothing - any unreadable fact is a refusal, never an exception.
    Implements: SR-103, LLR-1218.
    """
    return judge_guard(gather_guard_facts(writer, settings, paths or Paths()))


# ── provisioning (ensure / plan) ─────────────────────────────────────────────


def run(argv: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    """One command, captured, never through a shell; a failure is loud."""
    done = subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, universal_newlines=True, timeout=300)
    if check and done.returncode != 0:
        raise Refused("%s exited %d: %s" % (" ".join(argv), done.returncode, done.stderr.strip()[-400:]))
    return done


def root_vg() -> str:
    """The VG holding / - the scratch LV is carved from the root disk's VG only."""
    src = run(["findmnt", "-no", "SOURCE", "/"]).stdout.strip()
    vg = run(["lvs", "--noheadings", "-o", "vg_name", src], check=False).stdout.strip()
    if not vg:
        raise Refused("/ (%s) is not on LVM; there is no volume group to carve the scratch LV from" % src)
    return vg


def vg_free(vg: str) -> Tuple[int, int]:
    """(free extents, extent size in bytes) of a VG, in exact units."""
    out = run(["vgs", "--noheadings", "--units", "b", "--nosuffix", "-o", "vg_free_count,vg_extent_size", vg]).stdout.split()
    return int(out[0]), int(out[1])


def lv_bytes(device: str) -> int:
    """An LV's size in bytes, exact units."""
    return int(run(["lvs", "--noheadings", "--units", "b", "--nosuffix", "-o", "lv_size", device]).stdout.split()[0])


def fs_bytes(device: str) -> Optional[int]:
    """The ext4 filesystem's size on a device (block count x block size), or None."""
    out = run(["dumpe2fs", "-h", device], check=False).stdout
    count = re.search(r"^Block count:\s+(\d+)", out, re.M)
    size = re.search(r"^Block size:\s+(\d+)", out, re.M)
    return int(count.group(1)) * int(size.group(1)) if count and size else None


def lv_path(vg: str) -> Optional[str]:
    done = run(["lvs", "--noheadings", "-o", "lv_path", "%s/%s" % (vg, LV_NAME)], check=False)
    path = done.stdout.strip()
    return path if done.returncode == 0 and path else None


def probe_signature(device: str) -> Dict[str, str]:
    """blkid's LOW-LEVEL probe (-p: no cache) of a device: {} when it finds nothing.

    blkid exits 2 when no signature is found; that, on an LV created in the
    same run, is the one state in which `ensure` may format. Anything else it
    reports (ext4 or not) is kept.
    """
    done = run(["blkid", "-p", "-o", "export", device], check=False)
    if done.returncode == 2:
        return {}
    if done.returncode != 0:
        raise Refused("blkid -p %s exited %d: %s" % (device, done.returncode, done.stderr.strip()))
    return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)


def fstab_line(uuid: str, mountpoint: str = MOUNTPOINT) -> str:
    """The one fstab line this module writes: by UUID, nofail, prjquota."""
    return "UUID=%s %s %s %s 0 2" % (uuid, mountpoint, FSTYPE, FSTAB_OPTIONS)


def fstab_state(text: str, uuid: str, mountpoint: str = MOUNTPOINT) -> str:
    """'absent' | 'ours' | 'foreign: <line>' for the fstab entry on MOUNTPOINT."""
    for raw in text.splitlines():
        fields = raw.split()
        if len(fields) >= 2 and not fields[0].startswith("#") and fields[1] == mountpoint:
            return "ours" if fields[:4] == fstab_line(uuid, mountpoint).split()[:4] else "foreign: " + raw.strip()
    return "absent"


def usable_bytes(path: str) -> int:
    """What writers can ever have: the filesystem's blocks less what ext4 keeps
    back from every writer whatever the usage (f_bfree - f_bavail: its reserved
    clusters, the smaller of 2 % and 4096 clusters, plus any -m reserve).
    Measured on the hub's kernel: f_blocks alone over-counted by 16 MiB, so
    three full writers left 51 MiB of a 64 MiB floor. Independent of current
    usage, so a re-run of `ensure` on a filled volume sizes the same budgets."""
    st = os.statvfs(path)
    return (st.f_blocks - (st.f_bfree - st.f_bavail)) * st.f_frsize


class Provisioner:
    """`plan` and `ensure` as one walk over the steps, each asking its end state.

    With apply=False nothing is executed and every action is described; with
    apply=True each action runs. The two cannot drift: it is one code path.
    Implements: SR-102, SR-104, LLR-1215, LLR-1216, LLR-1217.
    """

    def __init__(self, settings: Settings, apply: bool, paths: Optional[Paths] = None,
                 vg: Optional[str] = None, mount_with: str = "systemd"):
        self.s, self.apply, self.paths = settings, apply, paths or Paths()
        self.vg, self.mount_with = vg, mount_with
        self.lv_growing = False
        self.extent_bytes = 0
        self.actions: List[str] = []
        self.report: Dict[str, object] = {}

    def act(self, what: str, argv: Optional[Sequence[str]] = None) -> None:
        self.actions.append(what)
        mc.log(("scratch: " if self.apply else "scratch (plan): would ") + what)
        if self.apply and argv:
            run(argv)

    def walk(self) -> Dict[str, object]:
        vg = self.vg or root_vg()
        device, existed = self.step_lv(vg)
        uuid = self.step_filesystem(device, existed)
        self.step_fstab(uuid)
        self.step_mount(uuid)
        self.step_grow_fs(device)
        self.step_writers()
        self.report["actions"] = self.actions
        return self.report

    def step_lv(self, vg: str) -> Tuple[str, bool]:
        """(device path, whether the LV already existed)."""
        free, extent = vg_free(vg)
        self.report.update(vg=vg, vg_free_extents=free, vg_extent_bytes=extent,
                           vg_free_gib=round(free * extent / GIB, 2))
        self.extent_bytes = extent
        existing = lv_path(vg)
        if existing:
            self.report["lv"] = existing
            have = lv_bytes(existing)
            self.report["lv_mib"] = have // MIB
            grow = grow_plan(have, self.s.size_mib, free, extent, self.s.vg_reserve_mib)
            if grow is None:
                mc.log("scratch: %s already exists (%d MiB, %d wanted) - not recreated, never shrunk"
                       % (existing, have // MIB, self.s.size_mib))
                return existing, True
            self.report.update(lv_grow_extents=grow["extents"], lv_new_mib=grow["new_mib"],
                               vg_leftover_gib=round(grow["leftover_bytes"] / GIB, 2))
            # Online: lvextend grows the LV under the mounted filesystem, and
            # step_grow_fs then grows ext4 into it (resize2fs, online for ext4).
            self.act("grow LV %s by %d extents (%d -> %d MiB), leaving %d extents (%.2f GiB) free; never shrinks"
                     % (existing, grow["extents"], have // MIB, grow["new_mib"], grow["leftover_extents"],
                        grow["leftover_bytes"] / GIB),
                     ["lvextend", "-l", "+%d" % grow["extents"], existing])
            self.lv_growing = True
            return existing, True
        plan = capacity_plan(free, extent, self.s.size_mib, self.s.vg_reserve_mib)
        self.report.update(lv_extents=plan["lv_extents"], vg_leftover_gib=round(plan["leftover_bytes"] / GIB, 2))
        # -Z y zeroes the new LV's first 4 KiB (lvcreate's default, made explicit);
        # -W n: no signature-wipe prompt - probe_signature below decides instead,
        # and refuses on anything it finds.
        self.act("create LV %s/%s, %d extents (%d MiB), leaving %d extents (%.2f GiB) free"
                 % (vg, LV_NAME, plan["lv_extents"], self.s.size_mib, plan["leftover_extents"],
                    plan["leftover_bytes"] / GIB),
                 ["lvcreate", "-W", "n", "-Z", "y", "-n", LV_NAME, "-l", str(plan["lv_extents"]), vg])
        created = lv_path(vg) if self.apply else None
        if self.apply and not created:
            raise Refused("lvcreate reported success but %s/%s is not there" % (vg, LV_NAME))
        return created or "/dev/%s/%s" % (vg, LV_NAME), False

    def step_filesystem(self, device: str, existed: bool) -> str:
        # A planned LV that does not exist yet has nothing to probe; an existing
        # one (or one just created) is ALWAYS probed before anything is decided.
        sig = probe_signature(device) if (self.apply or existed) else {}
        if sig:
            if sig.get("TYPE") != FSTYPE or sig.get("LABEL") != LABEL:
                raise Refused("%s carries a %s signature labelled %r, which this script did not make; "
                              "it is never formatted" % (device, sig.get("TYPE"), sig.get("LABEL")))
            uuid = sig.get("UUID", "").lower()
            if self.s.uuid and uuid != self.s.uuid:
                raise Refused("%s is %s labelled %s but its UUID %s is not HOMEHUB_SCRATCH_UUID %s"
                              % (device, FSTYPE, LABEL, uuid, self.s.uuid))
            features = run(["dumpe2fs", "-h", device]).stdout
            feat_line = next((l for l in features.splitlines() if l.startswith("Filesystem features:")), "")
            if " quota" not in feat_line or " project" not in feat_line:
                raise Refused("%s is the scratch filesystem but lacks the quota/project features (%s)"
                              % (device, feat_line.strip()))
            mc.log("scratch: %s already holds the scratch filesystem (UUID %s) - not reformatted" % (device, uuid))
            self.report["uuid"] = uuid
            return uuid
        if existed:
            # Only an LV this same run created is ever formatted. An existing
            # one with no signature blkid can read may be a filesystem whose
            # primary superblock is damaged, or an interrupted earlier run: the
            # script cannot tell, so it never decides.
            raise Refused("%s already existed and blkid -p finds no signature on it; this script formats "
                          "only an LV it has just created. Inspect it (e.g. dumpe2fs -o superblock=32768 "
                          "-h %s for a backup superblock); if it holds nothing worth keeping, "
                          "`lvremove %s` and run `ensure` again" % (device, device, device))
        argv =["mkfs.ext4"] + MKFS_ARGS + (["-U", self.s.uuid] if self.s.uuid else []) + [device]
        self.act("make ext4 (quota,project; -m 0; label %s%s) on %s, which has no signature"
                 % (LABEL, "; UUID " + self.s.uuid if self.s.uuid else "", device), argv)
        uuid = probe_signature(device).get("UUID", "").lower() if self.apply else (self.s.uuid or "<new>")
        self.report["uuid"] = uuid
        return uuid

    def step_fstab(self, uuid: str) -> None:
        try:
            with open(self.paths.fstab, encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            text = ""
        mp_name = self.paths.mountpoint_name
        state = fstab_state(text, uuid, mp_name)
        if state == "ours":
            return
        if state != "absent":
            raise Refused("fstab already mounts %s with a line this script did not write: %s"
                          % (MOUNTPOINT, state[len("foreign: "):]))
        self.act("append to %s: %s" % (self.paths.fstab, fstab_line(uuid, mp_name)))
        if self.apply:
            with open(self.paths.fstab, "a", encoding="utf-8") as handle:
                handle.write(("" if text.endswith("\n") or not text else "\n")
                             + "# hub scratch volume (MiniPC-Deployer stack/scratch, SR-102)\n"
                             + fstab_line(uuid, mp_name) + "\n")

    def step_mount(self, uuid: str) -> None:
        with open(self.paths.mountinfo, encoding="utf-8", errors="replace") as handle:
            entry = mount_of(parse_mountinfo(handle.read()), self.paths.mountpoint_name)
        if entry:
            want = majmin_of_rdev(os.path.join(self.paths.by_uuid, uuid))
            if want != entry["majmin"]:
                raise Refused("%s is already mounted from %s (%s), not the scratch filesystem"
                              % (MOUNTPOINT, entry["source"], entry["majmin"]))
            if "prjquota" not in entry["superopts"].split(","):
                raise Refused("%s is mounted without prjquota; remount it from the fstab line" % MOUNTPOINT)
            return
        mp = self.paths.mountpoint
        if os.path.isdir(mp) and os.listdir(mp):
            raise Refused("the bare mount point %s is not empty: something wrote to / while the volume "
                          "was absent. Inspect it; nothing is mounted over data" % mp)
        self.act("create the bare mount point %s, mode 000 (a backstop for unprivileged writers)" % mp)
        if self.apply:
            os.makedirs(mp, exist_ok=True)
            os.chmod(mp, 0)
        if self.mount_with == "systemd":
            self.act("mount it (systemctl daemon-reload; systemctl start srv-scratch.mount)")
            if self.apply:
                run(["systemctl", "daemon-reload"])
                run(["systemctl", "start", "srv-scratch.mount"])
        else:
            # Through the same fstab line systemd would use (util-linux mount -T).
            self.act("mount it (mount -T %s %s)" % (self.paths.fstab, mp), ["mount", "-T", self.paths.fstab, mp])

    def step_grow_fs(self, device: str) -> None:
        """Grow ext4 ONLINE into the LV when the LV is larger than the
        filesystem by at least an extent: after a grow this run, or one an
        earlier run was stopped between. Only while mounted (resize2fs grows a
        mounted ext4 in place); never shrinks. Implements: LLR-1380."""
        if not self.apply:
            if self.lv_growing:
                self.act("grow the ext4 filesystem on %s online to fill the LV (resize2fs)" % device)
                return
        if not os.path.exists(device) and not self.apply:
            return
        try:
            with open(self.paths.mountinfo, encoding="utf-8", errors="replace") as handle:
                mounted = mount_of(parse_mountinfo(handle.read()), self.paths.mountpoint_name) is not None
        except OSError:
            mounted = False
        have_fs = fs_bytes(device)
        have_lv = lv_bytes(device) if self.apply or not self.lv_growing else None
        if have_fs is None or have_lv is None or have_lv - have_fs < max(self.extent_bytes, MIB):
            return
        if not mounted:
            mc.log("scratch: the filesystem (%d MiB) is smaller than its LV (%d MiB) but not mounted; it is grown "
                   "online at the next ensure with it mounted" % (have_fs // MIB, have_lv // MIB), warning=True)
            return
        self.act("grow the ext4 filesystem on %s online from %d to %d MiB (resize2fs)"
                 % (device, have_fs // MIB, have_lv // MIB), ["resize2fs", device])

    def step_writers(self) -> None:
        mp = self.paths.mountpoint
        if not self.apply and not os.path.ismount(mp):
            self.report["quota_mib"] = "computed after mkfs from the usable space"
            self.act("create %s with their quota projects and hard limits" % ", ".join(WRITER_NAMES))
            return
        usable = usable_bytes(mp)
        granted = size_quotas(usable, self.s.quota_mib, self.s.free_floor_mib, self.s.min_quota_mib)
        self.report.update(usable_mib=usable // MIB, quota_mib=granted,
                           free_at_full_mib=usable // MIB - sum(granted.values()))
        with open(self.paths.mountinfo, encoding="utf-8", errors="replace") as handle:
            source = mount_of(parse_mountinfo(handle.read()), self.paths.mountpoint_name)["source"]
        if self.apply:
            os.chmod(mp, 0o755)
        for name in WRITER_NAMES:
            wdir = os.path.join(mp, name)
            if os.path.islink(wdir) or (os.path.exists(wdir) and not os.path.isdir(wdir)):
                raise Refused("%s exists and is not a directory" % wdir)
            if not os.path.isdir(wdir):
                self.act("create %s (0750 root)" % wdir)
                if self.apply:
                    os.mkdir(wdir, 0o750)
            pid, inherit = get_project(wdir) if os.path.isdir(wdir) else (None, False)
            if (pid, inherit) != (PROJECT_ID[name], True):
                self.act("put %s in quota project %d (inherit)" % (wdir, PROJECT_ID[name]))
                if self.apply:
                    set_project(wdir, PROJECT_ID[name])
            used, limit = get_project_quota(source, PROJECT_ID[name]) if self.apply or os.path.isdir(wdir) else (0, 0)
            if limit != granted[name] * MIB:
                self.act("set %s's hard quota to %d MiB (was %d MiB; %d MiB used)"
                         % (name, granted[name], limit // MIB, used // MIB))
                if self.apply:
                    set_project_quota(source, PROJECT_ID[name], granted[name])


def status(settings: Settings, paths: Optional[Paths] = None) -> Dict[str, object]:
    """Read-only identity, mount and per-writer budget record (deployment-state)."""
    paths = paths or Paths()
    with open(paths.mountinfo, encoding="utf-8", errors="replace") as handle:
        entry = mount_of(parse_mountinfo(handle.read()), paths.mountpoint_name)
    out: Dict[str, object] = {"mountpoint": MOUNTPOINT, "expected_uuid": settings.uuid,
                              "mounted": entry is not None}
    if not entry:
        return out
    want = majmin_of_rdev(os.path.join(paths.by_uuid, settings.uuid)) if settings.uuid else None
    out.update(source=entry["source"], identity_ok=want == entry["majmin"],
               prjquota="prjquota" in entry["superopts"].split(","),
               usable_mib=usable_bytes(paths.mountpoint) // MIB,
               free_mib=os.statvfs(paths.mountpoint).f_bavail * os.statvfs(paths.mountpoint).f_frsize // MIB)
    writers = {}
    for name in WRITER_NAMES:
        try:
            used, limit = get_project_quota(entry["source"], PROJECT_ID[name])
            writers[name] = {"used_mib": used // MIB, "limit_mib": limit // MIB}
        except OSError as exc:
            writers[name] = {"error": exc.strerror}
    out["writers"] = writers
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", default=mc.DEFAULT_ENV_FILE)
    p.add_argument("--root", default="/", help="prefix for the mount point and fstab (tests only)")
    p.add_argument("--vg", help="the volume group (default: the one holding /)")
    p.add_argument("--mount-with", choices=("systemd", "mount"), default="systemd")
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("plan")
    sub.add_parser("ensure")
    sub.add_parser("status")
    g = sub.add_parser("guard")
    g.add_argument("--writer", required=True)
    args = p.parse_args(argv)
    settings = Settings(mc.read_knobs(args.env_file))
    paths = Paths(args.root)
    if args.verb == "guard":
        ok, reason = guard(args.writer, settings, paths)
        mc.log("scratch guard [%s]: %s" % (args.writer, reason if ok else "REFUSED - " + reason), warning=not ok)
        return EXIT_OK if ok else EXIT_REFUSE_GUARD
    if args.verb == "status":
        print(json.dumps(status(settings, paths), sort_keys=True))
        return EXIT_OK
    if args.verb not in ("plan", "ensure"):
        p.print_help()
        return 2
    try:
        report = Provisioner(settings, args.verb == "ensure", paths, args.vg, args.mount_with).walk()
    except Stop as exc:
        mc.log("scratch: STOP - %s" % exc, warning=True)
        return EXIT_STOP
    except Refused as exc:
        mc.log("scratch: REFUSED - %s" % exc, warning=True)
        return EXIT_REFUSE
    print(json.dumps(report, sort_keys=True))
    if args.verb == "ensure":
        # The guard as a WRITER would see it. The provisioner itself runs with
        # every capability, so that one fact is the writer unit's, not ours.
        facts = gather_guard_facts(WRITER_NAMES[0], settings, paths)
        facts["has_cap_sys_resource"] = False
        ok, reason = judge_guard(facts)
        mc.log("scratch: ensure finished; filesystem UUID %s%s" % (
            report.get("uuid"), "" if settings.uuid else
            " - set HOMEHUB_SCRATCH_UUID=%s in the stack .env and HomeHub config" % report.get("uuid")))
        if settings.uuid and not ok:
            mc.log("scratch: the guard still refuses after ensure: %s" % reason, warning=True)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
