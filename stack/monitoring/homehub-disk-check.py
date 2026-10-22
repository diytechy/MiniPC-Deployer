#!/usr/bin/env python3
"""homehub-disk-check - the hub's low-disk alarm: alert once when a filesystem
crosses its floor, and once when it recovers.

WHY THIS EXISTS (SN-048). A full root on the box that serves this LAN's DNS does
not degrade, it fails: journald stops, sshd cannot write, and recovery needs the
console (firstboot.sh step 1b). Docker has its own LV for that reason, but
nothing said when either was filling. The panel has had its alarm since
2026-10-18 (SR-083, wall-disk-check.py); this is the hub's, with an alert route.

WHAT IT MEASURES. The mount points in HOMEHUB_DISK_MOUNTS (default
`/ /var/lib/docker /boot`), each optionally `PATH=FLOOR` where FLOOR is `N%` of
the filesystem or `NG` gigabytes (10^9). Without a floor a mount is LOW when its
available bytes are below the SMALLER of 10 % of its size and 5 GB - the panel's
rule (SR-083), so a big volume does not alarm with tens of GB free and a small
one (/boot, 2 GB) does not alarm for lack of 5 GB it never had. The scratch
volume joins by config (`/srv/scratch`) when it exists; no code change.

IT NEVER WAKES A DISK. A configured path must be a mount point in
/proc/self/mountinfo AND live on the NVMe (monitor_common.is_nvme_backed) before
it is statfs'd. A path on a USB drive is reported "skipped" and never touched;
the backup drive's free space belongs to the backup run's own record. A
configured path that is NOT mounted alerts too: a recorder writing into an
unmounted mount point fills the root instead.

Each mount alerts once per crossing (high) and once on recovery (default); the
alert helper rate-limits per key and journals every attempt. One journal line
per mount per run (`disk: LOW|ok|absent|skipped ...`) and a status file,
/var/lib/homehub-monitor/disk-status.json, whose `ts` is what freshness checks read.

Exit: 0 whenever it ran (a low disk is a finding, not a failure of the check);
1 when the mount table could not be read.

Implements: SR-087, LLR-1154, LLR-1155
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import monitor_common as mc  # noqa: E402

DEFAULT_MOUNTS = "/ /var/lib/docker /boot"
DEFAULT_PERCENT = 10.0
DEFAULT_FLOOR_BYTES = 5 * 10**9
# Every alert of one run must have finished this long after the run started;
# with monitor_common.SAVE_RESERVE_SEC it fits the unit's TimeoutStartSec=60.
ALERT_DEADLINE_SEC = 45


def parse_mount_spec(word: str) -> Optional[Tuple[str, Optional[Tuple[str, float]]]]:
    """`PATH` or `PATH=N%` / `PATH=NG` -> (path, None | ("pct"|"bytes", value)).

    None for a malformed entry (a relative path, an unknown unit), which the
    caller reports and skips rather than guessing.
    """
    path, sep, floor = word.partition("=")
    if not path.startswith("/") or ".." in path.split("/"):
        return None
    path = path.rstrip("/") or "/"
    if not sep:
        return path, None
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(%|G)", floor)
    if not m:
        return None
    value = float(m.group(1))
    if m.group(2) == "%":
        return (path, ("pct", value)) if 0 < value < 100 else None
    return path, ("bytes", value * 10**9)


def threshold_bytes(total: int, floor: Optional[Tuple[str, float]]) -> int:
    """The available-bytes floor below which `total` is LOW (see the module header)."""
    if floor is None:
        return int(min(total * DEFAULT_PERCENT / 100.0, DEFAULT_FLOOR_BYTES))
    kind, value = floor
    return int(total * value / 100.0) if kind == "pct" else int(value)


def label_for(path: str) -> str:
    """A stable alert-key fragment: `/` -> root, `/var/lib/docker` -> var-lib-docker."""
    return (
        "root" if path == "/" else re.sub(r"[^A-Za-z0-9_.-]", "-", path.strip("/"))[:48]
    )


def evaluate(specs, mounts: List[Dict[str, str]], nvme_check, statfs) -> List[dict]:
    """One result per configured mount: state in {low, ok, absent, skipped, error}.

    Contract:
      Inputs:  specs: [(path, floor)] from parse_mount_spec; mounts: parse_mountinfo
               output; nvme_check(majmin) -> bool; statfs(path) -> (total, avail).
      Outputs: [{"mount","label","state","totalBytes"?,"freeBytes"?,"floorBytes"?,"why"?}]
      statfs is called ONLY for a mount point on the NVMe (SN-048: never wake a disk).
    Implements: LLR-1154
    """
    out = []
    for path, floor in specs:
        result = {"mount": path, "label": label_for(path)}
        entry = mc.mount_entry(mounts, path)
        if entry is None:
            result.update(state="absent", why="not a mount point")
        elif not nvme_check(entry["majmin"]):
            result.update(
                state="skipped",
                why="not on the NVMe (%s); never statfs'd" % entry["source"],
            )
        else:
            try:
                total, avail = statfs(path)
            except OSError as exc:
                result.update(state="error", why="statfs failed: %s" % exc)
            else:
                limit = threshold_bytes(total, floor)
                result.update(
                    state="low" if avail < limit else "ok",
                    totalBytes=total, freeBytes=avail, floorBytes=limit,
                )  # fmt: skip
        out.append(result)
    return out


def _gb(n) -> str:
    return "%.2f GB" % (n / 1e9)


def journal_line(r: dict) -> str:
    if r["state"] in ("low", "ok"):
        return "disk: %s %s free %s of %s (floor %s)" % (
            "LOW" if r["state"] == "low" else "ok", r["mount"], _gb(r["freeBytes"]),
            _gb(r["totalBytes"]), _gb(r["floorBytes"]))  # fmt: skip
    return "disk: %s %s (%s)" % (r["state"], r["mount"], r.get("why", ""))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="HomeHub low-disk alarm (SN-048)")
    p.add_argument(
        "--state-dir",
        default=os.environ.get("HOMEHUB_MONITOR_STATE_DIR", mc.DEFAULT_STATE_DIR),
    )
    p.add_argument(
        "--env-file",
        default=os.environ.get("HOMEHUB_MONITOR_ENV_FILE", mc.DEFAULT_ENV_FILE),
    )
    p.add_argument(
        "--alert-helper",
        default=os.environ.get("HOMEHUB_ALERT_HELPER", mc.DEFAULT_ALERT_HELPER),
    )
    p.add_argument(
        "--sys-root", default=os.environ.get("HOMEHUB_MONITOR_SYS_ROOT", "/sys")
    )
    p.add_argument(
        "--proc-root", default=os.environ.get("HOMEHUB_MONITOR_PROC_ROOT", "/proc")
    )
    args = p.parse_args(argv)
    budget = mc.AlertBudget(ALERT_DEADLINE_SEC)
    env = mc.read_knobs(args.env_file)

    try:
        with open(
            os.path.join(args.proc_root, "self", "mountinfo"),
            encoding="utf-8",
            errors="replace",
        ) as h:
            mounts = mc.parse_mountinfo(h.read())
    except OSError as exc:
        mc.log("disk: cannot read the mount table (%s) - nothing measured" % exc, True)
        return 1
    specs = []
    for word in mc.knob_words(env, "HOMEHUB_DISK_MOUNTS", DEFAULT_MOUNTS):
        spec = parse_mount_spec(word)
        if spec is None:
            mc.log(
                "disk: ignoring malformed HOMEHUB_DISK_MOUNTS entry %r (PATH, PATH=N%% or PATH=NG)"
                % word,
                True,
            )
        else:
            specs.append(spec)

    def statfs(path):
        st = os.statvfs(path)
        return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize

    results = evaluate(
        specs, mounts, lambda mm: mc.is_nvme_backed(mm, args.sys_root), statfs
    )
    state_path = os.path.join(args.state_dir, "disk-state.json")
    state = mc.load_json(state_path, {})
    episodes = state.get("episodes", {})
    repeat = mc.knob_int(env, "HOMEHUB_ALERT_REPEAT_SEC", 1800, 0, 86400)

    def sender(key, prio, title, body):
        return mc.send_alert(key, prio, title, body, args.env_file, args.state_dir, helper=args.alert_helper,
                             repeat_sec=repeat, log_tag="homehub-disk-check", budget=budget)  # fmt: skip

    pending = []
    for r in results:
        mc.log(journal_line(r), warning=r["state"] in ("low", "absent", "error"))
        if r["state"] in ("low", "ok"):
            pending.append(mc.plan_episode(
                episodes, "disk-low-" + r["label"], r["state"] == "low",
                ("high", "HomeHub: %s is low on space (%s free)" % (r["mount"], _gb(r["freeBytes"])),
                 "%s has %s free of %s; the alarm floor is %s. Look at: du -xh --max-depth=2 %s | sort -h | tail"
                 % (r["mount"], _gb(r["freeBytes"]), _gb(r["totalBytes"]), _gb(r["floorBytes"]), r["mount"])),
                ("HomeHub: %s has space again (%s free)" % (r["mount"], _gb(r["freeBytes"])),
                 "%s is back above its floor of %s." % (r["mount"], _gb(r["floorBytes"]))),
            ))  # fmt: skip
        if r["state"] in ("absent", "ok", "low"):
            pending.append(mc.plan_episode(
                episodes, "disk-absent-" + r["label"], r["state"] == "absent",
                ("high", "HomeHub: %s is not mounted" % r["mount"],
                 "%s is configured in HOMEHUB_DISK_MOUNTS but is not a mount point; anything "
                 "writing there is filling its parent filesystem instead." % r["mount"]),
                ("HomeHub: %s is mounted again" % r["mount"], "%s is a mount point again." % r["mount"]),
            ))  # fmt: skip
    mc.drop_stale_episodes(
        episodes,
        {k + r["label"] for r in results for k in ("disk-low-", "disk-absent-")},
    )
    state["episodes"] = episodes
    # State (every owed alert pending) and status are written before any alert
    # is attempted, and the state again after each attempt: a slow ntfy can cost
    # this run its alerts, never its record.
    saved = {"ok": mc.save_json_atomic(state_path, state)}
    status = {"ts": int(time.time()), "mounts": results}
    if not mc.save_json_atomic(
        os.path.join(args.state_dir, "disk-status.json"), status
    ):
        mc.log("disk: WARN could not write disk-status.json", True)

    def checkpoint():
        saved["ok"] = mc.save_json_atomic(state_path, state)

    mc.deliver_pending(episodes, pending, sender, checkpoint)
    if not saved["ok"]:
        mc.log("disk: WARN could not save %s" % state_path, True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
