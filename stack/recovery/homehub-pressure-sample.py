#!/usr/bin/env python3
"""homehub-pressure-sample — C1: one memory-pressure sample, as one journal line.

ALWAYS ON (Owner D1), record-only, every 30 s from homehub-pressure-sample.timer,
journal tag `hub-pressure`. It exists because the 10-04 wedge left NO pressure
record behind, so the 40 % threshold the supervisor uses is a guess until a week
of these says otherwise (D3).

One line of JSON per run:
  psi        memory/io/cpu `some`/`full` avg10 and avg60
  mem        MemTotal, MemAvailable, SwapTotal, SwapFree (kB)
  containers every container scope's memory.current, largest first, by name
  protected  EVERY protected item in full, every sample (§6): the containers in
             HUB_WD_PROTECT, and the host services kept at OOMScoreAdjust=-1000
             (their cgroup memory.current and summed RSS). A pass must not
             become a blind spot.
  top        the five largest host processes by RSS
  dstate     tasks in uninterruptible sleep (D), which is what a storage stall
             looks like from here

It reads procfs, cgroupfs and Docker's own metadata files — never the Docker
daemon, which is exactly what is unresponsive when this record matters.

Implements: SR-049, LLR-1003
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import recovery_common as rc  # noqa: E402

PROTECTED_HOST_UNITS = ("ssh.service", "tailscaled.service", "homehub-recoveryd.service")
TOP_N = 5
DSTATE_MAX = 20


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def read_psi(proc_dir: str) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for resource in ("memory", "io", "cpu"):
        text = _read(os.path.join(proc_dir, "pressure", resource)) or ""
        values: Dict[str, float] = {}
        for line in text.splitlines():
            parts = line.split()
            if not parts:
                continue
            for field in parts[1:]:
                key, _, value = field.partition("=")
                if key in ("avg10", "avg60"):
                    values["{}_{}".format(parts[0], key)] = float(value)
        out[resource] = values
    return out


def _status_fields(proc_dir: str, pid: str) -> Optional[Dict[str, str]]:
    text = _read(os.path.join(proc_dir, pid, "status"))
    if text is None:
        return None
    fields = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        fields[key] = value.strip()
    return fields


def processes(proc_dir: str) -> List[dict]:
    """Every process: pid, comm, rss_kb, state letter."""
    out = []
    try:
        pids = [p for p in os.listdir(proc_dir) if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        fields = _status_fields(proc_dir, pid)
        if not fields:
            continue
        rss = fields.get("VmRSS", "0 kB").split()[0]
        out.append({"pid": int(pid), "comm": fields.get("Name", "?"),
                    "rss_kb": int(rss) if rss.isdigit() else 0,
                    "state": (fields.get("State") or "?")[:1]})
    return out


def host_unit(cgroup_root: str, proc_dir: str, unit: str, by_pid: Dict[int, dict]) -> Optional[dict]:
    base = os.path.join(cgroup_root, "system.slice", unit)
    current = rc._read_int(os.path.join(base, "memory.current"))
    procs = _read(os.path.join(base, "cgroup.procs"))
    if current is None and procs is None:
        return None
    rss = 0
    for line in (procs or "").split():
        if line.isdigit() and int(line) in by_pid:
            rss += by_pid[int(line)]["rss_kb"]
    return {"unit": unit, "memory_current": current, "rss_kb": rss}


def sample(paths: rc.Paths, protect) -> dict:
    """Build one record. Pure of side effects; every path comes from `paths`.

    Implements: SR-049, LLR-1003
    """
    mem = rc.read_meminfo(_read(os.path.join(paths.proc_dir, "meminfo")) or "")
    inventory = rc.read_inventory(paths.docker_containers_dir)
    scopes = sorted(rc.scan_container_scopes(paths.cgroup_root), key=lambda s: s.current, reverse=True)
    containers = []
    protected_containers = []
    for scope in scopes:
        info = inventory.get(scope.container_id)
        name = info.name if info else scope.container_id[:12]
        entry = {"name": name, "memory_current": scope.current}
        if rc.FILEBACKUP_SLICE_REL in scope.path.replace(os.sep, "/"):
            entry["slice"] = "homehub-filebackup"
        containers.append(entry)
        if info is not None and rc.is_protected(info, protect):
            protected_containers.append(entry)
    procs = processes(paths.proc_dir)
    by_pid = {p["pid"]: p for p in procs}
    protected_units = [u for u in (host_unit(paths.cgroup_root, paths.proc_dir, unit, by_pid)
                                   for unit in PROTECTED_HOST_UNITS) if u]
    top = sorted(procs, key=lambda p: p["rss_kb"], reverse=True)[:TOP_N]
    dstate = [{"pid": p["pid"], "comm": p["comm"]} for p in procs if p["state"] == "D"]
    return {
        "psi": read_psi(paths.proc_dir),
        "mem": {k: mem.get(k) for k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")},
        "containers": containers,
        "protected": {"containers": protected_containers, "host_units": protected_units,
                      "names": list(protect)},
        "top": [{k: p[k] for k in ("pid", "comm", "rss_kb")} for p in top],
        "dstate_count": len(dstate),
        "dstate": dstate[:DSTATE_MAX],
    }


def main(argv=None) -> int:
    paths = rc.Paths(os.environ.get("HUB_PRESSURE_ROOT", "/"))
    try:
        protect = rc.load_config(paths, os.environ).protect
    except rc.ConfigError:
        protect = tuple(rc.KNOB_DEFAULTS["HUB_WD_PROTECT"].split(","))
    print(json.dumps(sample(paths, protect), sort_keys=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
