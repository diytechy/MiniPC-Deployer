#!/usr/bin/env python3
"""panel-telemetry -- item S: bounded thermal/CPU/presentation collector.

Thin I/O shell around panel_telemetry_core (all decisions live there). Three
subcommands:
  run       -- foreground daemon loop (what the systemd unit execs)
  export    -- `panel-telemetry export --since ISO8601 > file` dumps JSONL
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
    "PANEL_TELEMETRY_PRESENTATION_PATH", "/run/wall-panel/presentation-snapshot.json"
)
INTERVAL_S = float(os.environ.get("PANEL_TELEMETRY_INTERVAL_S", "5"))
RETENTION_DAYS = int(os.environ.get("PANEL_TELEMETRY_RETENTION_DAYS", "7"))
CAP_BYTES = int(os.environ.get("PANEL_TELEMETRY_CAP_BYTES", str(50 * 1024 * 1024)))
KIOSK_PROCESS_NAMES = os.environ.get("PANEL_TELEMETRY_KIOSK_PROCS", "cage,electron,projectm").split(",")
STALE_THRESHOLD_S = 15.0

# Static, this-panel sensor inventory (see build/panel-telemetry-inventory-20260914
# for how this was derived). Kept in code, not autodiscovered, because sysfs
# device numbering is not guaranteed stable across kernels/boots and a wrong
# guess is worse than a documented list. `canonical=True` marks the one
# reading used for summaries; duplicates are still logged with their own
# labels so nothing is silently discarded.
SENSOR_INVENTORY = [
    {"source": "hwmon:coretemp:temp1", "label": "Package id 0", "path": "/sys/class/hwmon/hwmon3/temp1_input", "canonical": True},
    {"source": "hwmon:coretemp:temp2", "label": "Core 0", "path": "/sys/class/hwmon/hwmon3/temp2_input", "canonical": False},
    {"source": "hwmon:coretemp:temp3", "label": "Core 1", "path": "/sys/class/hwmon/hwmon3/temp3_input", "canonical": False},
    {"source": "thermal_zone:pch_skylake", "label": "PCH", "path": "/sys/class/thermal/thermal_zone1/temp", "canonical": False},
    {"source": "hwmon:ath10k_hwmon:temp1", "label": "WiFi radio", "path": "/sys/class/hwmon/hwmon4/temp1_input", "canonical": False},
]

GPU_PATHS = {
    "freq_cur_mhz": "/sys/class/drm/card1/gt_cur_freq_mhz",
    "freq_max_mhz": "/sys/class/drm/card1/gt_max_freq_mhz",
    "freq_act_mhz": "/sys/class/drm/card1/gt_act_freq_mhz",
    "rc6_residency_ms": "/sys/class/drm/card1/power/rc6_residency_ms",
}
# No throttle_reason_status file exists on this panel's kernel/driver (see
# inventory) -- marked unsupported rather than guessed.
GPU_THROTTLE_SUPPORTED = False

COOLING_GLOB = "/sys/class/thermal/cooling_device*"
CPUFREQ_GLOB = "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"


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


def read_sensors():
    out = []
    for entry in SENSOR_INVENTORY:
        raw = _read_int(entry["path"])
        deg_c = raw / 1000.0 if raw is not None else None
        out.append(core.build_sensor_record(entry["source"], entry["label"], deg_c, entry["canonical"]))
    return out


def read_gpu():
    values = {k: _read_int(p) for k, p in GPU_PATHS.items()}
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


def read_kiosk_processes(names):
    """Cheap /proc scan for CPU%/RSS of configured process names. CPU% here
    is a coarse instantaneous utime+stime/HZ-over-uptime style estimate --
    good enough for "is the kiosk expensive right now", not a substitute for
    /proc/stat's system-wide delta accounting."""
    totals = {}
    try:
        clk_tck = os.sysconf("SC_CLK_TCK")
    except (ValueError, AttributeError):
        clk_tck = 100
    try:
        with open("/proc/uptime") as fh:
            uptime_s = float(fh.read().split()[0])
    except OSError:
        uptime_s = None
    for pid_name in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not _PID_NAME_RE.match(pid_name):
            continue
        comm_path = "/proc/%s/comm" % pid_name
        comm = _read_str(comm_path)
        if not comm or comm not in names:
            continue
        stat_path = "/proc/%s/stat" % pid_name
        statm_path = "/proc/%s/statm" % pid_name
        try:
            with open(stat_path) as fh:
                stat_fields = fh.read().split(")", 1)[-1].split()
            utime = int(stat_fields[11])
            stime = int(stat_fields[12])
            starttime = int(stat_fields[19])
        except (OSError, IndexError, ValueError):
            continue
        rss_kb = None
        try:
            with open(statm_path) as fh:
                pages = int(fh.read().split()[1])
            rss_kb = pages * (os.sysconf("SC_PAGE_SIZE") // 1024)
        except (OSError, IndexError, ValueError, AttributeError):
            pass
        cpu_pct = None
        if uptime_s is not None:
            proc_uptime_s = uptime_s - (starttime / clk_tck)
            if proc_uptime_s > 0:
                cpu_pct = min(100.0, ((utime + stime) / clk_tck) / proc_uptime_s * 100.0)
        agg = totals.setdefault(comm, {"cpu_pct": 0.0, "rss_kb": 0, "count": 0})
        agg["count"] += 1
        agg["rss_kb"] += rss_kb or 0
        agg["cpu_pct"] += cpu_pct or 0.0
    return [
        {"name": name, "cpu_pct": round(v["cpu_pct"], 2), "rss_kb": v["rss_kb"], "process_count": v["count"]}
        for name, v in sorted(totals.items())
    ]


def read_presentation(path, now_mono_ms):
    """Read and validate the host-written presentation snapshot. Returns a
    dict always: {"status": "ok"|"stale"|"unknown"|"invalid", "payload": ... or None}
    Never raises."""
    try:
        with open(path, "r") as fh:
            raw = fh.read()
    except OSError:
        return {"status": "unknown", "payload": None, "errors": ["snapshot file not present"]}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return {"status": "invalid", "payload": None, "errors": ["json parse error: %s" % exc]}
    ok, errors = core.validate_presentation(payload)
    if not ok:
        return {"status": "invalid", "payload": None, "errors": errors}
    record_mono = payload.get("monotonicMs")
    if core.is_stale(record_mono, now_mono_ms, STALE_THRESHOLD_S):
        return {"status": "stale", "payload": payload, "errors": []}
    return {"status": "ok", "payload": payload, "errors": []}


def _state_path_for_day(state_dir, day_str):
    return os.path.join(state_dir, "telemetry-%s.jsonl" % day_str)


def _boot_id():
    return _read_str("/proc/sys/kernel/random/boot_id") or "unknown-boot"


def _rotate(state_dir):
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


def _append_record(state_dir, record):
    day_str = record["ts_utc"][:10]
    path = _state_path_for_day(state_dir, day_str)
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        return True
    except OSError:
        # Disk full / permission loss: never raise out of the loop.
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


def cmd_run(args):
    state_dir = args.state_dir
    session_id = str(uuid.uuid4())
    boot_id = _boot_id()
    prev_stat = None
    prev_mono = None
    last_rotate = 0.0
    last_revision = None
    next_full_sample = time.monotonic()
    while True:
        tick_start = time.monotonic()
        try:
            now_mono_ms = time.monotonic() * 1000.0
            presentation = read_presentation(args.presentation_path, now_mono_ms)
            payload = presentation.get("payload") or {}
            revision = payload.get("revision")
            if revision is not None and last_revision is not None and revision != last_revision:
                _write_mode_change_record(state_dir, boot_id, session_id, tick_start, presentation, last_revision)
            if revision is not None:
                last_revision = revision

            if tick_start >= next_full_sample:
                stat = read_stat_line()
                cpu_pct = core.cpu_percent(prev_stat, stat) if stat is not None else None
                prev_stat = stat if stat is not None else prev_stat

                gap = core.detect_suspend_gap(prev_mono, tick_start, INTERVAL_S)
                event = "suspend_gap" if gap is not None else None

                sensors = read_sensors()
                gpu = read_gpu()
                cooling = read_cooling()
                cpufreq = read_cpufreq()
                kiosk = read_kiosk_processes(KIOSK_PROCESS_NAMES)

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

                if tick_start - last_rotate > 3600:
                    _rotate(state_dir)
                    last_rotate = tick_start

                prev_mono = tick_start
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
