#!/usr/bin/env python3
"""panel-telemetry -- item S: bounded thermal/CPU/presentation collector.

Thin I/O shell around panel_telemetry_core (all decisions live there). Three
subcommands:
  run       -- foreground daemon loop (what the systemd unit execs)
  export    -- `python3 panel-telemetry.py export --since ISO8601 > file` dumps JSONL
  summary   -- small human-readable digest of recent records

Failure isolation: every I/O call that can fail (a missing sensor file, a
full disk, a torn presentation snapshot) is caught locally and turned into an
"unavailable"/"unknown" field or a skipped write -- this process must never
raise past its own loop and must never block or delay anything else on the
panel (it does not touch input, audio or the renderer's own process).

State dir (single writable directory, matches the unit's ProtectSystem=strict
+ ReadWritePaths): $PANEL_TELEMETRY_STATE_DIR, default
/var/lib/wall-panel/telemetry. Files: telemetry-YYYY-MM-DD.jsonl (one line
per record), rotated by calendar day, retained per panel_telemetry_core
defaults (7 days / 50 MiB total, oldest-first).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import panel_telemetry_core as core  # noqa: E402

DEFAULT_STATE_DIR = os.environ.get("PANEL_TELEMETRY_STATE_DIR", "/var/lib/wall-panel/telemetry")
DEFAULT_PRESENTATION_PATH = os.environ.get(
    "PANEL_TELEMETRY_PRESENTATION_PATH", "/run/wall-panel-renderer/presentation-snapshot.json"
)
INTERVAL_S = float(os.environ.get("PANEL_TELEMETRY_INTERVAL_S", "5"))
RETENTION_DAYS = int(os.environ.get("PANEL_TELEMETRY_RETENTION_DAYS", "7"))
CAP_BYTES = int(os.environ.get("PANEL_TELEMETRY_CAP_BYTES", str(50 * 1024 * 1024)))
KIOSK_PROCESS_NAMES = os.environ.get("PANEL_TELEMETRY_KIOSK_PROCS", "cage,electron,projectm").split(",")
STALE_THRESHOLD_S = 15.0

# This-panel sensor inventory (see build/panel-telemetry-inventory-20260914 for
# how it was derived): which hwmon *names* and thermal_zone *types* exist, and
# which temp*_label a given reading has. The hwmonN/thermal_zoneN NUMBERS are
# NOT trusted directly -- they are not guaranteed stable across a kernel
# update or a probe-order change -- so they are re-resolved from these stable
# names/labels at startup and once per rotation cycle (see _resolve_sensors).
# `canonical=True` marks the one reading used for summaries; duplicates are
# still logged with their own labels so nothing is silently discarded.
SENSOR_TARGETS = [
    {"hwmon_name": "coretemp", "temp_label": "Package id 0", "source": "hwmon:coretemp:package", "canonical": True},
    {"hwmon_name": "coretemp", "temp_label": "Core 0", "source": "hwmon:coretemp:core0", "canonical": False},
    {"hwmon_name": "coretemp", "temp_label": "Core 1", "source": "hwmon:coretemp:core1", "canonical": False},
    {"thermal_zone_type": "pch_skylake", "source": "thermal_zone:pch_skylake", "canonical": False},
    {"hwmon_name": "ath10k_hwmon", "temp_label": None, "source": "hwmon:ath10k_hwmon:temp1", "canonical": False},
]

GPU_SYSFS_FIELDS = ("gt_cur_freq_mhz", "gt_max_freq_mhz", "gt_act_freq_mhz")
GPU_RC6_RELATIVE = "power/rc6_residency_ms"
GPU_CARD_GLOB = "/sys/class/drm/card[0-9]*"
# No throttle_reason_status file exists on this panel's kernel/driver (see
# inventory) -- marked unsupported rather than guessed.
GPU_THROTTLE_SUPPORTED = False

COOLING_GLOB = "/sys/class/thermal/cooling_device*"
CPUFREQ_GLOB = "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"
HWMON_GLOB = "/sys/class/hwmon/hwmon*"
THERMAL_ZONE_GLOB = "/sys/class/thermal/thermal_zone*"


def _resolve_sensors(targets=SENSOR_TARGETS):
    """Resolve each configured sensor target to a concrete sysfs path by
    stable name/label, NOT by hwmonN/thermal_zoneN number. Call again if a
    target goes missing (numbering can change across a kernel/module reload).
    Returns a list of {source, label, path, canonical} with path=None when a
    target cannot be found this call (read_sensors() then reports it as
    unavailable, not zero)."""
    hwmon_dirs = {}
    for hwmon_dir in glob.glob(HWMON_GLOB):
        name = _read_str(os.path.join(hwmon_dir, "name"))
        if name:
            hwmon_dirs.setdefault(name, []).append(hwmon_dir)
    zone_dirs = {}
    for zone_dir in glob.glob(THERMAL_ZONE_GLOB):
        typ = _read_str(os.path.join(zone_dir, "type"))
        if typ:
            zone_dirs.setdefault(typ, []).append(zone_dir)

    resolved = []
    for target in targets:
        path = None
        label = target.get("temp_label")
        if "hwmon_name" in target:
            for hwmon_dir in hwmon_dirs.get(target["hwmon_name"], []):
                if target.get("temp_label") is None:
                    candidate = os.path.join(hwmon_dir, "temp1_input")
                    if os.path.exists(candidate):
                        path = candidate
                        break
                    continue
                for label_file in sorted(glob.glob(os.path.join(hwmon_dir, "temp*_label"))):
                    if _read_str(label_file) == target["temp_label"]:
                        path = label_file[: -len("_label")] + "_input"
                        break
                if path:
                    break
        elif "thermal_zone_type" in target:
            label = target["thermal_zone_type"]
            for zone_dir in zone_dirs.get(target["thermal_zone_type"], []):
                candidate = os.path.join(zone_dir, "temp")
                if os.path.exists(candidate):
                    path = candidate
                    break
        resolved.append({
            "source": target["source"],
            "label": label,
            "path": path,
            "canonical": target.get("canonical", False),
        })
    return resolved


def _resolve_gpu_card():
    """First DRM card exposing gt_cur_freq_mhz (this panel has exactly one
    real GPU; a future multi-GPU panel would need a configured preference,
    not this heuristic)."""
    for card_dir in sorted(glob.glob(GPU_CARD_GLOB)):
        if os.path.exists(os.path.join(card_dir, "gt_cur_freq_mhz")):
            return card_dir
    return None


def _read_int(path):
    try:
        with open(path, "r") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _read_str(path):
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except OSError:
        return None


def read_stat_line():
    try:
        with open("/proc/stat", "r") as fh:
            first = fh.readline()
        return core.parse_stat_line(first)
    except (OSError, ValueError):
        return None


def read_sensors(resolved):
    out = []
    for entry in resolved:
        raw = _read_int(entry["path"]) if entry["path"] else None
        deg_c = raw / 1000.0 if raw is not None else None
        out.append(core.build_sensor_record(entry["source"], entry["label"], deg_c, entry["canonical"]))
    return out


def read_gpu(card_dir):
    values = {}
    for field in GPU_SYSFS_FIELDS:
        values[field] = _read_int(os.path.join(card_dir, field)) if card_dir else None
    values["rc6_residency_ms"] = _read_int(os.path.join(card_dir, GPU_RC6_RELATIVE)) if card_dir else None
    # "load" is deliberately absent: neither frequency nor the cumulative
    # rc6 residency counter is a utilization percentage, and this kernel/
    # driver exposes no busy-% file (see the inventory) -- reporting one
    # would be a guess, not a measurement.
    values["throttle"] = "unsupported" if not GPU_THROTTLE_SUPPORTED else None
    return values


def read_cooling():
    out = []
    for path in sorted(glob.glob(COOLING_GLOB)):
        typ = _read_str(os.path.join(path, "type"))
        cur = _read_int(os.path.join(path, "cur_state"))
        mx = _read_int(os.path.join(path, "max_state"))
        if typ is None:
            continue
        out.append({"type": typ, "cur_state": cur, "max_state": mx})
    return out


def read_cpufreq():
    out = []
    for path in sorted(glob.glob(CPUFREQ_GLOB)):
        out.append(_read_int(path))
    return out


_PID_NAME_RE = re.compile(r"^\d+$")


def _read_proc_cpu_ticks(pid):
    """(comm, utime+stime ticks) for one pid, or None if it could not be
    read (process gone, permission, malformed line)."""
    try:
        with open("/proc/%s/stat" % pid) as fh:
            raw = fh.read()
        # comm can contain spaces/parens; split on the LAST ')' as /proc/pid/stat
        # is documented to require (comm is everything between the first '('
        # and the last ')').
        _name_part, _, rest = raw.rpartition(")")
        comm = raw.split("(", 1)[1].rsplit(")", 1)[0]
        fields = rest.split()
        utime = int(fields[11])
        stime = int(fields[12])
        return comm, utime + stime
    except (OSError, IndexError, ValueError):
        return None


def _read_rss_kb(pid):
    try:
        with open("/proc/%s/statm" % pid) as fh:
            pages = int(fh.read().split()[1])
        return pages * (os.sysconf("SC_PAGE_SIZE") // 1024)
    except (OSError, IndexError, ValueError, AttributeError):
        return None


def sample_kiosk_ticks(names):
    """One flat {pid: (comm, ticks)} snapshot. Pure sampling, no CPU% math --
    that needs two of these plus the elapsed wall time between them, which is
    read_kiosk_processes()'s job (mirrors the system-wide delta pattern, and
    keeps "first sample is unknown" true for per-process CPU too)."""
    out = {}
    if not os.path.isdir("/proc"):
        return out
    for pid_name in os.listdir("/proc"):
        if not _PID_NAME_RE.match(pid_name):
            continue
        comm = _read_str("/proc/%s/comm" % pid_name)
        if not comm or comm not in names:
            continue
        ticks = _read_proc_cpu_ticks(pid_name)
        if ticks is None:
            continue
        out[pid_name] = ticks


    return out


def read_kiosk_processes(names, prev_ticks, prev_wall_s, cur_wall_s):
    """Delta-based per-process-name aggregate CPU% (like the system-wide
    figure: ticks-busy / ticks-of-wall-time-elapsed) plus current RSS.
    Returns (records, cur_ticks) -- caller keeps cur_ticks for the next call.
    A process with no matching entry in prev_ticks (new PID, or first sample
    ever) contributes RSS but no CPU -- never a fabricated 0%.
    """
    cur_ticks = sample_kiosk_ticks(names)
    try:
        clk_tck = os.sysconf("SC_CLK_TCK")
    except (ValueError, AttributeError):
        clk_tck = 100
    elapsed_s = (cur_wall_s - prev_wall_s) if (prev_wall_s is not None and prev_ticks is not None) else None

    totals = {}
    for pid, (comm, ticks) in cur_ticks.items():
        agg = totals.setdefault(comm, {"cpu_pct": 0.0, "cpu_known": 0, "rss_kb": 0, "count": 0})
        agg["count"] += 1
        rss = _read_rss_kb(pid)
        agg["rss_kb"] += rss or 0
        prev = prev_ticks.get(pid) if prev_ticks else None
        if prev is not None and elapsed_s and elapsed_s > 0:
            _prev_comm, prev_tick_count = prev
            tick_delta = ticks - prev_tick_count
            if tick_delta >= 0:
                pct = (tick_delta / clk_tck) / elapsed_s * 100.0
                agg["cpu_pct"] += min(100.0, max(0.0, pct))
                agg["cpu_known"] += 1

    records = []
    for name in names:
        v = totals.get(name)
        if v is None:
            records.append({"name": name, "cpu_pct": None, "rss_kb": None, "process_count": 0})
            continue
        records.append({
            "name": name,
            "cpu_pct": round(v["cpu_pct"], 2) if v["cpu_known"] else None,
            "rss_kb": v["rss_kb"],
            "process_count": v["count"],
        })
    return records, cur_ticks


def read_presentation(path, now_mono_ms):
    """Read and validate the host-written presentation snapshot. Returns a
    dict always: {"status": "ok"|"stale"|"unknown"|"invalid", "payload": ... or None}
    Never raises.

    `payload` is populated ONLY when status is "ok". A stale, invalid or
    missing snapshot's mode must never be retained as if it were current (S:
    "never log stale mode as current") -- a downstream reader that only looks
    at `presentation.payload` can never mistake an old mode for a live one;
    the raw last-seen values are still visible in `last_seen` for debugging,
    clearly namespaced away from the "current" field.
    """
    try:
        with open(path, "r") as fh:
            raw = fh.read()
    except OSError:
        return {"status": "unknown", "payload": None, "last_seen": None, "errors": ["snapshot file not present"]}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return {"status": "invalid", "payload": None, "last_seen": None, "errors": ["json parse error: %s" % exc]}
    ok, errors = core.validate_presentation(payload)
    if not ok:
        return {"status": "invalid", "payload": None, "last_seen": None, "errors": errors}
    record_mono = payload.get("monotonicMs")
    if core.is_stale(record_mono, now_mono_ms, STALE_THRESHOLD_S):
        return {"status": "stale", "payload": None, "last_seen": payload, "errors": []}
    return {"status": "ok", "payload": payload, "last_seen": None, "errors": []}


def _state_path_for_day(state_dir, day_str):
    return os.path.join(state_dir, "telemetry-%s.jsonl" % day_str)


def _boot_id():
    return _read_str("/proc/sys/kernel/random/boot_id") or "unknown-boot"


def _rotate(state_dir):
    """Enforce retention + the 50 MiB cap. Called after EVERY append (the
    directory holds at most a handful of files, so the stat/glob cost is
    negligible), not just hourly -- a burst of large records must not be able
    to sit over cap for up to an hour before anything notices."""
    now = time.time()
    files = []
    for path in glob.glob(os.path.join(state_dir, "telemetry-*.jsonl")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        files.append((os.path.basename(path), st.st_mtime, st.st_size))
    to_delete = set(core.files_to_delete_for_retention([(n, m) for n, m, _ in files], now, RETENTION_DAYS))
    to_delete |= set(core.files_to_delete_for_cap([f for f in files if f[0] not in to_delete], CAP_BYTES))
    for name in to_delete:
        try:
            os.remove(os.path.join(state_dir, name))
        except OSError:
            pass


# Rate-limit disk-full/permission diagnostics: a failing write must not turn
# into a fresh journal line every 1s tick, which is its own kind of pressure
# on a full disk.
_APPEND_FAILURE_LOG_EVERY = 60
_append_failure_count = 0


def _append_record(state_dir, record):
    global _append_failure_count
    # Bound a single record's own size so one oversized payload cannot blow
    # past the cap before rotation ever runs.
    line = json.dumps(record, sort_keys=True)
    if len(line) > 32768:
        line = json.dumps({"v": record.get("v"), "ts_utc": record.get("ts_utc"),
                            "event": "record_too_large", "dropped_bytes": len(line)})
    day_str = record["ts_utc"][:10]
    path = _state_path_for_day(state_dir, day_str)
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(line + "\n")
        _append_failure_count = 0
        _rotate(state_dir)
        return True
    except OSError as exc:
        # Disk full / permission loss: never raise out of the loop, and never
        # spam the journal on every 1s tick while it persists.
        _append_failure_count += 1
        if _append_failure_count == 1 or _append_failure_count % _APPEND_FAILURE_LOG_EVERY == 0:
            sys.stderr.write("panel-telemetry: write failed (%d consecutive): %s\n" % (_append_failure_count, exc))
        return False


# How often the presentation snapshot is polled for a revision change
# between full 5s samples, so a short mode flip is still attributable (S:
# "Include mode-change records so short transitions between five-second
# samples remain attributable"). Cheap: one file read + JSON parse, no sysfs.
MODE_POLL_S = 1.0


def _write_mode_change_record(state_dir, boot_id, session_id, mono_s, presentation, prev_revision):
    record = {
        "v": core.SCHEMA_VERSION,
        "ts_utc": core.now_utc_iso(),
        "mono_s": mono_s,
        "boot_id": boot_id,
        "session_id": session_id,
        "event": "mode_change",
        "presentation": presentation,
        "prev_revision": prev_revision,
    }
    _append_record(state_dir, record)


def _boottime_s():
    """A clock that keeps advancing through suspend, unlike time.monotonic()
    (CLOCK_MONOTONIC excludes suspended time on Linux -- see clock_gettime(2)
    -- so it cannot see a suspend gap at all: it wakes up having "moved"
    almost exactly as far as the CPU was actually awake). CLOCK_BOOTTIME
    includes suspended time, so the divergence between two BOOTTIME samples IS
    the wall-clock gap, suspend included. Falls back to time.time() (also
    suspend-inclusive) on a platform without CLOCK_BOOTTIME (this collector
    only ships on Linux, but the fallback keeps `--once` runnable anywhere for
    tests)."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.time()


def cmd_run(args):
    state_dir = args.state_dir
    session_id = str(uuid.uuid4())
    boot_id = _boot_id()
    prev_stat = None
    prev_boottime = None
    last_revision = None
    sensor_targets = _resolve_sensors()
    last_sensor_resolve = time.monotonic()
    gpu_card = _resolve_gpu_card()
    prev_kiosk_ticks = None
    prev_kiosk_wall_s = None
    next_full_sample = time.monotonic()
    while True:
        tick_start = time.monotonic()
        try:
            now_mono_ms = time.monotonic() * 1000.0
            presentation = read_presentation(args.presentation_path, now_mono_ms)
            revision = (presentation.get("payload") or {}).get("revision")
            if revision is not None and last_revision is not None and revision != last_revision:
                _write_mode_change_record(state_dir, boot_id, session_id, tick_start, presentation, last_revision)
            if revision is not None:
                last_revision = revision

            if tick_start >= next_full_sample:
                stat = read_stat_line()
                cpu_pct = core.cpu_percent(prev_stat, stat) if stat is not None else None
                prev_stat = stat if stat is not None else prev_stat

                cur_boottime = _boottime_s()
                gap = core.detect_suspend_gap(prev_boottime, cur_boottime, INTERVAL_S)
                event = "suspend_gap" if gap is not None else None

                # Re-resolve sensor/GPU paths periodically (module reload,
                # kernel update between boots) rather than trusting numbers
                # that were only valid at process start.
                if tick_start - last_sensor_resolve > 3600:
                    sensor_targets = _resolve_sensors()
                    gpu_card = _resolve_gpu_card()
                    last_sensor_resolve = tick_start

                sensors = read_sensors(sensor_targets)
                gpu = read_gpu(gpu_card)
                cooling = read_cooling()
                cpufreq = read_cpufreq()
                kiosk, prev_kiosk_ticks = read_kiosk_processes(
                    KIOSK_PROCESS_NAMES, prev_kiosk_ticks, prev_kiosk_wall_s, tick_start,
                )
                prev_kiosk_wall_s = tick_start

                record = core.build_record(
                    ts_utc=core.now_utc_iso(),
                    mono_s=tick_start,
                    boot_id=boot_id,
                    session_id=session_id,
                    cpu_pct=cpu_pct,
                    sensors=sensors,
                    gpu=gpu,
                    cooling=cooling,
                    cpufreq_mhz=cpufreq,
                    kiosk=kiosk,
                    presentation=presentation,
                    event=event,
                )
                if gap is not None:
                    record["suspend_gap_s"] = gap
                _append_record(state_dir, record)

                prev_boottime = cur_boottime
                next_full_sample = tick_start + INTERVAL_S
        except Exception as exc:  # noqa: BLE001 - failure isolation is the point
            sys.stderr.write("panel-telemetry: sample failed, continuing: %s\n" % exc)

        if args.once:
            return
        elapsed = time.monotonic() - tick_start
        time.sleep(max(0.0, MODE_POLL_S - elapsed))


def cmd_export(args):
    state_dir = args.state_dir
    since = args.since
    for path in sorted(glob.glob(os.path.join(state_dir, "telemetry-*.jsonl"))):
        day = os.path.basename(path)[len("telemetry-"):-len(".jsonl")]
        if since and day < since[:10]:
            continue
        with open(path, "r") as fh:
            for line in fh:
                if since:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("ts_utc", "") < since:
                        continue
                sys.stdout.write(line if line.endswith("\n") else line + "\n")


def cmd_summary(args):
    state_dir = args.state_dir
    files = sorted(glob.glob(os.path.join(state_dir, "telemetry-*.jsonl")))
    if not files:
        print("no telemetry files in", state_dir)
        return
    count = 0
    cpu_sum = 0.0
    cpu_n = 0
    max_temp = None
    gaps = 0
    last_path = files[-1]
    with open(last_path, "r") as fh:
        for line in fh:
            count += 1
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("cpu_pct") is not None:
                cpu_sum += rec["cpu_pct"]
                cpu_n += 1
            for s in rec.get("sensors", []):
                if s.get("canonical") and s.get("deg_c") is not None:
                    if max_temp is None or s["deg_c"] > max_temp:
                        max_temp = s["deg_c"]
            if rec.get("event") == "suspend_gap":
                gaps += 1
    print("file:", last_path)
    print("records:", count)
    print("mean cpu_pct:", round(cpu_sum / cpu_n, 1) if cpu_n else "unknown")
    print("max canonical temp c:", max_temp if max_temp is not None else "unknown")
    print("suspend gaps:", gaps)


def build_parser():
    p = argparse.ArgumentParser(prog="panel-telemetry")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    p.add_argument("--presentation-path", default=DEFAULT_PRESENTATION_PATH)
    sub = p.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="foreground sampling loop")
    run_p.add_argument("--once", action="store_true", help="sample once and exit (testing)")
    run_p.set_defaults(func=cmd_run)

    export_p = sub.add_parser("export", help="dump JSONL records to stdout")
    export_p.add_argument("--since", default=None, help="ISO8601 UTC timestamp lower bound")
    export_p.set_defaults(func=cmd_export)

    summary_p = sub.add_parser("summary", help="print a small digest of the latest file")
    summary_p.set_defaults(func=cmd_summary)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
