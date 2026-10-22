#!/usr/bin/env python3
"""homehub-telemetry - the hub's thermal, frequency and load record, and its alarms.

WHY THIS EXISTS (SN-048). The hub records memory pressure every 30 s (SN-046)
but nothing read its temperatures, its CPU frequency or the kernel's throttle
counters, so a cooling fault or a box quietly running at its floor clock would
leave no trace. The panel has had the equivalent since 2026-09-14 (WSN-036,
panel-telemetry.py); this is the hub's, and it keeps the same retention.

ONE SAMPLE PER RUN. homehub-telemetry.timer starts `sample` every 30 s. A sample
is one JSON line appended to /var/lib/homehub-telemetry/telemetry-YYYYMMDD.jsonl
(UTC day): every thermal zone, every hwmon temperature, each core's current
frequency, the kernel's core and package throttle counters, CPU busy since the
previous sample, the load average, PSI, and the usage of every filesystem that
lives on the NVMe. Persistent storage, so the record survives a reboot.

THE ALARMS, evaluated on every sample against the previous ones (state in
/var/lib/homehub-monitor/telemetry-state.json):
  cpu-temp   the hottest CPU sensor above HOMEHUB_THERMAL_CPU_MAX_C for
             HOMEHUB_THERMAL_SAMPLES consecutive samples; clears after as many
             samples 5 C below it.
  nvme-temp  the same for the NVMe's own sensor (HOMEHUB_THERMAL_NVME_MAX_C).
  throttle   the core or package throttle counters increased since the last
             sample; clears after HOMEHUB_THROTTLE_CLEAR_SAMPLES quiet samples.
  freq-floor every core below HOMEHUB_FREQ_FLOOR_MHZ while the CPU was at least
             HOMEHUB_FREQ_BUSY_PCT busy, for HOMEHUB_FREQ_SAMPLES samples.
Each alerts once per episode and once when it clears (monitor_common episodes).
"Consecutive" is counted on the monotonic clock and the boot id, never the wall
clock: a clock jump cannot fake a run of samples, and a gap of more than four
periods or a reboot starts the count again. A sensor that is missing is
recorded as null and its rule simply does not advance.

RETENTION, like the panel's: the newest 7 day files, and at most 50 MiB in all,
oldest first. The file being written is never deleted, and a day file that has
reached the cap is not appended to, so the record cannot fill the disk.
Retention counts day FILES, not wall-clock age, so a clock that jumps forward
cannot delete a week of real records in one run.

NEVER WAKES A DISK. The filesystem list comes from /proc/self/mountinfo, and
only filesystems monitor_common.is_nvme_backed() accepts are statfs'd; the USB
backup and library drives are never touched.

Subcommands: sample (the unit), export [--since ISO] [--until ISO] (JSONL to
stdout), summary [--hours N] (a short digest).

Exit: 0 when the sample was recorded (alarms or not); 1 when it could not be
written (a full disk) - the unit goes red, which is true.

Implements: SR-086, LLR-1150, LLR-1151, LLR-1152, LLR-1153
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import monitor_common as mc  # noqa: E402

DEFAULT_DATA_DIR = "/var/lib/homehub-telemetry"
RETENTION_FILES = 7
CAP_BYTES = 50 * 1024 * 1024
INTERVAL_S = 30
GAP_RESET_S = 4 * INTERVAL_S
HYSTERESIS_C = 5.0
FILE_RE = re.compile(r"^telemetry-([0-9]{8})\.jsonl$")
# The sensors that measure the CPU package and cores on this class of box
# (x86_pkg_temp and coretemp; TCPU is the ACPI zone some Intel boards expose).
CPU_ZONE_TYPES = ("x86_pkg_temp", "TCPU")
CPU_HWMON_NAMES = ("coretemp", "k10temp", "zenpower")
NVME_HWMON_NAMES = ("nvme",)


# ── reading the machine (thin; every root is an argument) ───────────────────


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    text = _read(path)
    if text is None or not re.fullmatch(r"-?[0-9]+", text):
        return None
    return int(text)


def _milli_c(value: Optional[int]) -> Optional[float]:
    return None if value is None else round(value / 1000.0, 1)


def read_thermal_zones(sys_root: str) -> List[dict]:
    out = []
    for zone in sorted(
        glob.glob(os.path.join(sys_root, "class", "thermal", "thermal_zone*"))
    ):
        out.append({
            "zone": os.path.basename(zone),
            "type": _read(os.path.join(zone, "type")) or "unknown",
            "temp_c": _milli_c(_read_int(os.path.join(zone, "temp"))),
        })  # fmt: skip
    return out


def read_hwmon(sys_root: str) -> List[dict]:
    out = []
    for hw in sorted(glob.glob(os.path.join(sys_root, "class", "hwmon", "hwmon*"))):
        name = _read(os.path.join(hw, "name")) or "unknown"
        for inp in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            index = os.path.basename(inp)[: -len("_input")]
            out.append({
                "name": name,
                "label": _read(os.path.join(hw, index + "_label")) or index,
                "temp_c": _milli_c(_read_int(inp)),
            })  # fmt: skip
    return out


def _cpu_dirs(sys_root: str) -> List[str]:
    dirs = glob.glob(os.path.join(sys_root, "devices", "system", "cpu", "cpu[0-9]*"))
    return sorted(dirs, key=lambda d: int(re.sub(r"\D", "", os.path.basename(d)) or 0))


def read_cpu_mhz(sys_root: str) -> List[Optional[int]]:
    out = []
    for cpu in _cpu_dirs(sys_root):
        khz = _read_int(os.path.join(cpu, "cpufreq", "scaling_cur_freq"))
        out.append(None if khz is None else int(round(khz / 1000.0)))
    return out


def read_throttle(sys_root: str) -> Optional[Dict[str, int]]:
    """Summed core throttle events, and the package count (the same counter is
    repeated on every CPU of a package, so the maximum is the package's)."""
    core, package, seen = 0, 0, False
    for cpu in _cpu_dirs(sys_root):
        base = os.path.join(cpu, "thermal_throttle")
        c = _read_int(os.path.join(base, "core_throttle_count"))
        p = _read_int(os.path.join(base, "package_throttle_count"))
        if c is not None:
            core += c
            seen = True
        if p is not None:
            package = max(package, p)
            seen = True
    return {"core_count": core, "package_count": package} if seen else None


def read_stat_ticks(proc_root: str) -> Optional[Tuple[int, int]]:
    """(total, idle+iowait) jiffies from the aggregate `cpu` line of /proc/stat."""
    text = _read(os.path.join(proc_root, "stat"))
    if not text:
        return None
    first = text.splitlines()[0].split()
    if not first or first[0] != "cpu":
        return None
    values = [int(v) for v in first[1:] if v.isdigit()]
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values[:8]), idle


def read_load(proc_root: str) -> Optional[List[float]]:
    text = _read(os.path.join(proc_root, "loadavg"))
    try:
        return [float(v) for v in (text or "").split()[:3]] or None
    except ValueError:
        return None


def read_psi(proc_root: str) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for resource in ("cpu", "memory", "io"):
        values: Dict[str, float] = {}
        for line in (
            _read(os.path.join(proc_root, "pressure", resource)) or ""
        ).splitlines():
            parts = line.split()
            for field in parts[1:]:
                key, _, value = field.partition("=")
                if key in ("avg10", "avg60"):
                    try:
                        values["%s_%s" % (parts[0], key)] = float(value)
                    except ValueError:
                        pass
        out[resource] = values
    return out


REAL_FS = ("ext4", "ext3", "xfs", "btrfs", "vfat", "f2fs")


def read_nvme_filesystems(proc_root: str, nvme_check, statfs) -> List[dict]:
    """Usage of every real filesystem on the NVMe, from mountinfo; nothing else."""
    text = _read(os.path.join(proc_root, "self", "mountinfo")) or ""
    out, seen = [], set()
    for entry in mc.parse_mountinfo(text):
        if entry["fstype"] not in REAL_FS or entry["mount"] in seen:
            continue
        if not nvme_check(entry["majmin"]):
            continue
        seen.add(entry["mount"])
        try:
            total, avail = statfs(entry["mount"])
        except OSError:
            out.append({"mount": entry["mount"], "error": "statfs failed"})
            continue
        used_pct = round(100.0 * (total - avail) / total, 1) if total else None
        out.append(
            {
                "mount": entry["mount"],
                "total_bytes": total,
                "avail_bytes": avail,
                "used_pct": used_pct,
            }
        )
    return out


def os_statfs(path: str) -> Tuple[int, int]:
    """(size, available-to-unprivileged) bytes of the filesystem at `path`."""
    st = os.statvfs(path)
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


def boot_id(proc_root: str) -> str:
    return mc.read_boot_id(proc_root)


# ── the pure core: the rules ─────────────────────────────────────────────────


def cpu_temp(record: dict) -> Optional[float]:
    temps = [
        z["temp_c"]
        for z in record.get("thermal_zones", [])
        if z["type"] in CPU_ZONE_TYPES
    ]
    temps += [
        h["temp_c"] for h in record.get("hwmon", []) if h["name"] in CPU_HWMON_NAMES
    ]
    temps = [t for t in temps if t is not None]
    return max(temps) if temps else None


def nvme_temp(record: dict) -> Optional[float]:
    temps = [
        h["temp_c"] for h in record.get("hwmon", []) if h["name"] in NVME_HWMON_NAMES
    ]
    temps = [t for t in temps if t is not None]
    return max(temps) if temps else None


def config_from_env(env: Dict[str, str]) -> dict:
    return {
        "cpu_max_c": mc.knob_int(env, "HOMEHUB_THERMAL_CPU_MAX_C", 90, 30, 125),
        "nvme_max_c": mc.knob_int(env, "HOMEHUB_THERMAL_NVME_MAX_C", 75, 30, 100),
        "temp_samples": mc.knob_int(env, "HOMEHUB_THERMAL_SAMPLES", 4, 1, 120),
        "throttle_clear": mc.knob_int(
            env, "HOMEHUB_THROTTLE_CLEAR_SAMPLES", 10, 1, 240
        ),
        "freq_floor_mhz": mc.knob_int(env, "HOMEHUB_FREQ_FLOOR_MHZ", 800, 100, 10000),
        "freq_busy_pct": mc.knob_int(env, "HOMEHUB_FREQ_BUSY_PCT", 75, 1, 100),
        "freq_samples": mc.knob_int(env, "HOMEHUB_FREQ_SAMPLES", 6, 1, 240),
    }


def _hysteresis_rule(
    counters: dict, bad_now: bool, value: Optional[float], limit: float, samples: int
) -> bool:
    """A level rule with hysteresis: bad after `samples` readings above `limit`,
    good again after `samples` readings HYSTERESIS_C below it. Returns the verdict
    (the previous one when the sensor is missing)."""
    if value is None:
        return bad_now
    if value > limit:
        counters["over"] = counters.get("over", 0) + 1
        counters["under"] = 0
    elif value < limit - HYSTERESIS_C:
        counters["under"] = counters.get("under", 0) + 1
        counters["over"] = 0
    else:
        counters["over"] = 0
        counters["under"] = 0
    if not bad_now and counters["over"] >= samples:
        return True
    if bad_now and counters["under"] >= samples:
        return False
    return bad_now


def evaluate(
    state: dict, record: dict, cfg: dict, mono: float
) -> Tuple[dict, Dict[str, bool], List[str]]:
    """Advance the rule counters with one record.

    Contract:
      Inputs:  state: the previous rules state ({} the first time); record: this
               sample; cfg: config_from_env(); mono: monotonic seconds now.
      Outputs: (new_state, verdicts {rule: bad}, notes) - notes say why counters
               were reset. A reboot (boot id changed) or a gap of more than
               GAP_RESET_S restarts every consecutive count; verdicts carry over.
    Implements: LLR-1151
    """
    new = json.loads(json.dumps(state or {}))
    notes = []
    counters = new.setdefault("counters", {})
    verdicts = new.setdefault("verdicts", {})
    same_boot = new.get("boot_id") == record.get("boot_id")
    gap = None if new.get("mono") is None else mono - new["mono"]
    if not same_boot or gap is None or gap < 0 or gap > GAP_RESET_S:
        if new.get("boot_id"):
            notes.append(
                "consecutive counts restarted (%s)"
                % ("a new boot" if not same_boot else "gap of %ss" % gap)
            )
        counters.clear()
        new.pop("throttle", None)
    new["boot_id"] = record.get("boot_id")
    new["mono"] = mono

    verdicts["cpu-temp"] = _hysteresis_rule(
        counters.setdefault("cpu-temp", {}), verdicts.get("cpu-temp", False),
        cpu_temp(record), cfg["cpu_max_c"], cfg["temp_samples"],
    )  # fmt: skip
    verdicts["nvme-temp"] = _hysteresis_rule(
        counters.setdefault("nvme-temp", {}), verdicts.get("nvme-temp", False),
        nvme_temp(record), cfg["nvme_max_c"], cfg["temp_samples"],
    )  # fmt: skip

    throttle = record.get("throttle")
    tc = counters.setdefault("throttle", {})
    if throttle is not None:
        total = throttle["core_count"] + throttle["package_count"]
        prev = new.get("throttle")
        if prev is not None and total > prev:
            verdicts["throttle"] = True
            tc["quiet"] = 0
            tc["increase"] = total - prev
        else:
            # A counter that went DOWN was reset (a reboot the boot id missed, a
            # module reload): a new baseline, never an episode.
            tc["quiet"] = tc.get("quiet", 0) + 1
            if verdicts.get("throttle") and tc["quiet"] >= cfg["throttle_clear"]:
                verdicts["throttle"] = False
        new["throttle"] = total
    verdicts.setdefault("throttle", False)

    fc = counters.setdefault("freq-floor", {})
    busy, mhz = (
        record.get("cpu_busy_pct"),
        [m for m in record.get("cpu_mhz", []) if m is not None],
    )
    if busy is not None and mhz:
        low = busy >= cfg["freq_busy_pct"] and max(mhz) < cfg["freq_floor_mhz"]
        fc["low"] = fc.get("low", 0) + 1 if low else 0
        fc["ok"] = 0 if low else fc.get("ok", 0) + 1
        if not verdicts.get("freq-floor") and fc["low"] >= cfg["freq_samples"]:
            verdicts["freq-floor"] = True
        elif verdicts.get("freq-floor") and fc["ok"] >= cfg["freq_samples"]:
            verdicts["freq-floor"] = False
    verdicts.setdefault("freq-floor", False)
    return new, dict(verdicts), notes


def alert_texts(
    rule: str, record: dict, cfg: dict
) -> Tuple[Tuple[str, str, str], Tuple[str, str]]:
    """((priority, title, body), (clear title, clear body)) for one rule."""
    host = "the hub"
    if rule == "cpu-temp":
        t = cpu_temp(record)
        fire = (
            "high",
            "HomeHub: CPU temperature %s C (limit %d C)" % (t, cfg["cpu_max_c"]),
            "%s has run above %d C for %d samples. Record: homehub-telemetry summary"
            % (host, cfg["cpu_max_c"], cfg["temp_samples"]),
        )
        clear = (
            "HomeHub: CPU temperature back to %s C" % t,
            "%s is below %d C again." % (host, cfg["cpu_max_c"] - HYSTERESIS_C),
        )
    elif rule == "nvme-temp":
        t = nvme_temp(record)
        fire = (
            "high",
            "HomeHub: NVMe temperature %s C (limit %d C)" % (t, cfg["nvme_max_c"]),
            "%s's system disk has run above %d C for %d samples. Record: homehub-telemetry summary"
            % (host, cfg["nvme_max_c"], cfg["temp_samples"]),
        )
        clear = (
            "HomeHub: NVMe temperature back to %s C" % t,
            "%s's system disk is below %d C again."
            % (host, cfg["nvme_max_c"] - HYSTERESIS_C),
        )
    elif rule == "throttle":
        fire = (
            "high",
            "HomeHub: the CPU is thermally throttling",
            "%s's kernel throttle counters increased (now %s). Record: homehub-telemetry summary"
            % (host, record.get("throttle")),
        )
        clear = (
            "HomeHub: CPU throttling has stopped",
            "No throttle events for %d samples." % cfg["throttle_clear"],
        )
    else:
        fire = (
            "high",
            "HomeHub: CPU held below %d MHz under load" % cfg["freq_floor_mhz"],
            "%s ran %s%% busy with every core below %d MHz for %d samples (cores: %s)."
            % (
                host,
                record.get("cpu_busy_pct"),
                cfg["freq_floor_mhz"],
                cfg["freq_samples"],
                record.get("cpu_mhz"),
            ),
        )
        clear = (
            "HomeHub: CPU frequency is normal again",
            "The frequency floor rule has cleared.",
        )
    return fire, clear  # fmt: skip


# ── the record on disk: rotation and append (pure planner, thin writer) ─────


def plan_rotation(
    files: List[Tuple[str, int]], active: str, keep_files: int, cap_bytes: int
) -> List[str]:
    """Which day files to delete: all but the newest `keep_files`, then the oldest
    until the total fits `cap_bytes`. The active file is never chosen.

    Implements: LLR-1152
    """
    ours = sorted((n, s) for n, s in files if FILE_RE.match(n))
    newest = {n for n, _ in ours[-keep_files:]} | {active}
    delete = [n for n, _ in ours if n not in newest]
    remaining = [(n, s) for n, s in ours if n not in delete]
    total = sum(s for _, s in remaining)
    for name, size in remaining:
        if total <= cap_bytes:
            break
        if name == active:
            continue
        delete.append(name)
        total -= size
    return delete


def day_file(epoch: float) -> str:
    return "telemetry-%s.jsonl" % _dt.datetime.fromtimestamp(
        epoch, _dt.timezone.utc
    ).strftime("%Y%m%d")


def append_record(
    data_dir: str, record: dict, cap_bytes: int = CAP_BYTES
) -> Tuple[bool, str]:
    """Append one line to today's file, then enforce retention. (ok, why)."""
    name = day_file(record["epoch"])
    path = os.path.join(data_dir, name)
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        os.makedirs(data_dir, mode=0o750, exist_ok=True)
        current = os.path.getsize(path) if os.path.exists(path) else 0
        if current + len(line) > cap_bytes:
            return (
                False,
                "today's file has reached the %d-byte cap; not appending" % cap_bytes,
            )
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError as exc:
        return False, "could not append to %s: %s" % (path, exc)
    try:
        files = [
            (n, os.path.getsize(os.path.join(data_dir, n)))
            for n in os.listdir(data_dir)
        ]
        for victim in plan_rotation(files, name, RETENTION_FILES, cap_bytes):
            os.unlink(os.path.join(data_dir, victim))
    except OSError as exc:
        return True, "recorded, but retention failed: %s" % exc
    return True, "recorded"


def iter_records(
    data_dir: str, since: Optional[float] = None, until: Optional[float] = None
):
    """Every well-formed record in file order; torn lines are skipped."""
    for path in sorted(glob.glob(os.path.join(data_dir, "telemetry-*.jsonl"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    epoch = rec.get("epoch") if isinstance(rec, dict) else None
                    if not isinstance(epoch, (int, float)):
                        continue
                    if (since is None or epoch >= since) and (
                        until is None or epoch <= until
                    ):
                        yield rec
        except OSError:
            continue


def summarise(records: List[dict]) -> List[str]:
    """A short human digest of `records` (pure)."""
    if not records:
        return ["no records"]

    def _span(values):
        values = [v for v in values if v is not None]
        if not values:
            return "n/a"
        return "min %s / mean %.1f / max %s" % (
            min(values),
            sum(values) / len(values),
            max(values),
        )

    first, last = records[0], records[-1]
    throttle = [r.get("throttle") for r in records if r.get("throttle")]
    boots = len({r.get("boot_id") for r in records})
    lines = [
        "%d records, %s .. %s, %d boot(s)"
        % (len(records), first.get("ts"), last.get("ts"), boots),
        "cpu temp C:  " + _span([cpu_temp(r) for r in records]),
        "nvme temp C: " + _span([nvme_temp(r) for r in records]),
        "cpu MHz:     " + _span([m for r in records for m in r.get("cpu_mhz", [])]),
        "cpu busy %:  " + _span([r.get("cpu_busy_pct") for r in records]),
        "load1:       " + _span([(r.get("load") or [None])[0] for r in records]),
        "throttle:    first %s, last %s"
        % (throttle[0] if throttle else None, throttle[-1] if throttle else None),
    ]
    for fs in last.get("fs", []):
        lines.append("fs %-18s %s%% used" % (fs.get("mount"), fs.get("used_pct")))
    return lines


# ── the shell ────────────────────────────────────────────────────────────────


def collect(
    sys_root: str, proc_root: str, prev_ticks, nvme_check, statfs, now: float
) -> Tuple[dict, Optional[list]]:
    """One record (and this sample's /proc/stat ticks, for the next busy figure)."""
    ticks = read_stat_ticks(proc_root)
    busy = None
    if ticks and prev_ticks and ticks[0] > prev_ticks[0]:
        dt, di = ticks[0] - prev_ticks[0], ticks[1] - prev_ticks[1]
        busy = round(100.0 * (1 - di / float(dt)), 1)
    record = {
        "v": 1,
        "ts": _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "epoch": round(now, 3),
        "boot_id": boot_id(proc_root),
        "thermal_zones": read_thermal_zones(sys_root),
        "hwmon": read_hwmon(sys_root),
        "cpu_mhz": read_cpu_mhz(sys_root),
        "cpu_busy_pct": busy,
        "throttle": read_throttle(sys_root),
        "load": read_load(proc_root),
        "psi": read_psi(proc_root),
        "fs": read_nvme_filesystems(proc_root, nvme_check, statfs),
    }
    return record, (list(ticks) if ticks else None)


def cmd_sample(args) -> int:
    env = mc.read_knobs(args.env_file)
    cfg = config_from_env(env)
    state_path = os.path.join(args.state_dir, "telemetry-state.json")
    state = mc.load_json(state_path, {})
    now, mono = time.time(), time.monotonic()
    prev_ticks = (
        state.get("ticks") if state.get("boot_id") == boot_id(args.proc_root) else None
    )
    record, ticks = collect(
        args.sys_root, args.proc_root, prev_ticks,
        lambda mm: mc.is_nvme_backed(mm, args.sys_root), os_statfs, now,
    )  # fmt: skip
    rules, verdicts, notes = evaluate(state.get("rules", {}), record, cfg, mono)
    for note in notes:
        mc.log("telemetry: " + note)
    episodes = state.get("episodes", {})

    def sender(key, prio, title, body):
        return mc.send_alert(key, prio, title, body, args.env_file, args.state_dir, helper=args.alert_helper,
                             repeat_sec=mc.knob_int(env, "HOMEHUB_ALERT_REPEAT_SEC", 1800, 0, 86400),
                             log_tag="homehub-telemetry")  # fmt: skip

    for rule in ("cpu-temp", "nvme-temp", "throttle", "freq-floor"):
        fire, clear = alert_texts(rule, record, cfg)
        mc.run_episode(
            episodes, "telemetry-" + rule, verdicts[rule], fire, clear, sender
        )
    record["alarms"] = sorted(r for r, bad in verdicts.items() if bad)
    ok, why = append_record(args.data_dir, record)
    mc.log("telemetry: %s (cpu %s C, nvme %s C, busy %s%%, alarms %s)"
           % (why, cpu_temp(record), nvme_temp(record), record["cpu_busy_pct"], record["alarms"] or "none"),
           warning=not ok)  # fmt: skip
    state.update(
        {
            "rules": rules,
            "episodes": episodes,
            "ticks": ticks,
            "boot_id": record["boot_id"],
        }
    )
    if not mc.save_json_atomic(state_path, state):
        mc.log(
            "telemetry: WARN could not save %s; the next sample starts its counts again"
            % state_path,
            True,
        )
    return 0 if ok else 1


def _parse_iso(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    text = text.strip().replace("Z", "+00:00")
    parsed = _dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def cmd_export(args) -> int:
    for rec in iter_records(
        args.data_dir, _parse_iso(args.since), _parse_iso(args.until)
    ):
        sys.stdout.write(json.dumps(rec, sort_keys=True) + "\n")
    return 0


def cmd_summary(args) -> int:
    since = time.time() - args.hours * 3600
    for line in summarise(list(iter_records(args.data_dir, since))):
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="HomeHub thermal/frequency/load record (SN-048)"
    )
    p.add_argument(
        "--data-dir", default=os.environ.get("HOMEHUB_TELEMETRY_DIR", DEFAULT_DATA_DIR)
    )
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
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("sample", help="take one sample (what the timer runs)")
    e = sub.add_parser("export", help="print records as JSONL")
    e.add_argument("--since")
    e.add_argument("--until")
    s = sub.add_parser("summary", help="a short digest of recent records")
    s.add_argument("--hours", type=float, default=24.0)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "export":
        return cmd_export(args)
    if args.cmd == "summary":
        return cmd_summary(args)
    return cmd_sample(args)


if __name__ == "__main__":
    sys.exit(main())
