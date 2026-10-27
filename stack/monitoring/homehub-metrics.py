#!/usr/bin/env python3
"""homehub-metrics - the hub's metrics log: a 5-minute snapshot and a daily row.

WHY THIS EXISTS (SN-048; the Owner, 2026-10-26, HomeHub open-items A79). The hub
recorded its temperatures and memory for a week and its gauges only as the
tracker's current state: nothing kept the gauges AS THE WALL SAW THEM (the
computed `stale` flag and colour), service status over time, or one row per
night. The Owner asked for both, for trend charts and for debugging by the Owner
and agents. RECORD ONLY: nothing here alerts, starts, stops or changes anything.

TWO SUBCOMMANDS, TWO UNITS:

  snapshot  homehub-metrics-snapshot.timer, every 5 minutes. One JSON line in
            /var/lib/homehub-metrics/snapshot-YYYYMMDD.jsonl (UTC day):
              gauges     every gauge as the tracker's /api/today serves it, read
                         as HOMEHUB_METRICS_USER - the tracker's real user. A
                         request with any other X-Forwarded-User makes the
                         tracker mint a new user, so a blank knob reads NOTHING
                         rather than guessing an identity;
              host       the newest homehub-telemetry sample (temperatures,
                         throttle, busy, load, PSI, NVMe use), memory, uptime;
              containers state, health and restart count of every container;
              units      every homehub-*.service's last result, and every
                         failed unit on the box;
              monitors   the watch/disk/liveness status files and the meeting
                         pull's last run (the panel-reachable signal);
              panel      a short summary of the wall panel's telemetry, disk
                         check and freeze watchdog, pulled over the meeting
                         pull's own key and forced command (`status` verb);
              smart      the NVMe's health log, once an hour.
  daily     homehub-metrics-daily.timer, 10:00 and 14:05 America/Chicago,
            Persistent=true. One row per run in daily.jsonl for the night that
            ENDED this morning: every gauge with its observed_at and stale flag
            (so a night missing at 10:00 is recorded as missing), the heart
            feeder's night summary, the CPAP's usage minutes and sleep score,
            the overnight temperature range, and the backup units' results. The
            10:00 row is `initial`, the 14:05 row `revision`; both are kept, and
            a chart takes the latest. A run that finds a row of its kind already
            written for the night writes nothing, so a catch-up after a reboot
            cannot double a row.

RETENTION (the Owner, 2026-10-26): snapshots 365 day files, gzip'd after 7;
daily rows forever (one small file). Bounded like its neighbours: a day file
stops at 16 MiB, the whole snapshot record at 1 GiB, daily.jsonl at 64 MiB.
On the hub NOT in the config backups, and never under the tracker's data folder,
whose git mirror is pushed to GitHub; copied weekly into the Owner's Docs folder
on the library instead (`copy`, below; HomeHub scripts/deploy/storage-map.md).

NEVER WAKES A DISK. The host figures come from the telemetry record, which reads
the NVMe only; SMART is asked of /dev/nvme0 alone.

NETWORK: the tracker on loopback (refused anywhere else) and the panel over the
meeting pull's ssh. Every external call is bounded (monitor_common.run_bounded
or a urllib timeout) inside one run deadline that fits TimeoutStartSec.

  copy      homehub-metrics-copy.timer, weekly (Sunday 18:30 America/Chicago,
            clear of the 03:00 nightly library backup and its long hash-recalc
            nights). Copies the closed snapshot day files (gzip'd), daily.jsonl
            and the raw watch samples into HOMEHUB_METRICS_COPY_DIR, a folder
            in the Owner's private Docs tree on the library, so the library's
            own backup carries them (the Owner, 2026-10-26). Skipped, with the
            reason logged, unless the library is really mounted and carries
            its marker. Never deletes at the destination.

Subcommands: snapshot, daily, copy, export [--daily] [--since ISO] [--until ISO],
summary [--hours N].
Exit: 0 when the record was written (whatever it says); 1 when it could not be.

Implements: SR-122, SR-123, SR-124, LLR-1321, LLR-1322, LLR-1323, LLR-1324,
            LLR-1325, LLR-1326, LLR-1327, LLR-1328, LLR-1329, LLR-1332
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import monitor_common as mc  # noqa: E402

DEFAULT_DATA_DIR = "/var/lib/homehub-metrics"
# The heart feeder's raw watch record (stack/weight, its second StateDirectory).
DEFAULT_RAW_DIR = "/var/lib/homehub-health-raw"
SNAPSHOT_PREFIX = "snapshot"
DAILY_FILE = "daily.jsonl"
STATE_FILE = "metrics-state.json"
KEEP_DAYS = 365
COMPRESS_AFTER_DAYS = 7
DAY_CAP_BYTES = 16 * 1024 * 1024
TOTAL_CAP_BYTES = 1024 * 1024 * 1024
DAILY_CAP_BYTES = 64 * 1024 * 1024

# The whole run must finish inside the unit's TimeoutStartSec (asserted by the
# tests against the unit files): every external call is cut to what is left.
SNAPSHOT_DEADLINE_SEC = 100
DAILY_DEADLINE_SEC = 80
SAVE_RESERVE_SEC = 10

DEFAULT_TRACKER_URL = "http://127.0.0.1:8787/api/today"
TRACKER_TIMEOUT_SEC = 10
TRACKER_MAX_BYTES = 4 * 1024 * 1024
USER_RE = re.compile(r"[A-Za-z0-9_.@-]{1,128}")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

TELEMETRY_DIR = "/var/lib/homehub-telemetry"
TELEMETRY_MAX_AGE_SEC = 180
MONITOR_DIR = "/var/lib/homehub-monitor"
MEETINGS_STATUS = "/var/lib/homehub-meetings/status.json"
PANEL_TIMEOUT_SEC = 20
PANEL_MAX_BYTES = 256 * 1024
SMART_EVERY_SEC = 3600
SMART_DEVICE = "/dev/nvme0"

# The daily row's schedule. The timer carries the same two times with the same
# zone (asserted by the tests), so the code and the timer cannot disagree about
# which run is the initial and which the revision.
DAILY_ZONE = "America/Chicago"
INITIAL_AT = (10, 0)
REVISION_AT = (14, 5)
LATE_AFTER_SEC = 30 * 60
OVERNIGHT_FROM_HOUR = 22    # local, the evening before the night key
OVERNIGHT_TO_HOUR = 8       # local, on the night key
WEIGHT_STATE = "/var/lib/homehub-weight/weight-state.json"
SLEEP_STATE = "/var/lib/homehub-sleep/sleep-state.json"
STATE_MAX_BYTES = 4 * 1024 * 1024
# The backup units whose result the daily row carries.
BACKUP_UNITS = ("homehub-backup.service", "homehub-library-backup.service")

UNIT_RE = re.compile(r"[A-Za-z0-9@:._-]{1,128}")
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")


# ── small pure helpers ───────────────────────────────────────────────────────


def iso_utc(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(value, digits: int = 3):
    """A finite number rounded to `digits`, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return round(value, digits)
    return value


def _short(text, limit: int = 200):
    return text[:limit] if isinstance(text, str) else None


class Deadline:
    """What is left of one run's time; every external call is cut to it."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self._clock = clock
        self.end = clock() + seconds

    def left(self) -> float:
        return self.end - self._clock()

    def cut(self, wanted: float) -> float:
        """`wanted` seconds, or what is left minus the save reserve (never below 1)."""
        return max(1.0, min(wanted, self.left() - SAVE_RESERVE_SEC))

    def spent(self) -> bool:
        return self.left() - SAVE_RESERVE_SEC < 2


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_bounded_json(path: str, limit: int, default):
    """The decoded file when it is at most `limit` bytes; `default` otherwise."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        return default
    if len(raw) > limit:
        return default
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return default
    return data if isinstance(data, type(default)) else default


# ── gauges: the tracker's /api/today, as its real user (LLR-1322) ───────────


def tracker_target(knobs: Dict[str, str]) -> Tuple[Optional[str], Optional[str], str]:
    """(url, user, refusal). A refusal means nothing is requested at all.

    The URL must be http(s) on loopback: the request carries the tracker's
    bearer token and the household identity, so it never leaves the box. The
    user must be set: a blank user is never replaced by a guess, because the
    tracker mints a user for ANY X-Forwarded-User it is sent.
    """
    url = knobs.get("HOMEHUB_METRICS_TRACKER_URL") or DEFAULT_TRACKER_URL
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in LOOPBACK_HOSTS:
        return None, None, "HOMEHUB_METRICS_TRACKER_URL is not a loopback http(s) URL; the tracker was not asked"
    user = (knobs.get("HOMEHUB_METRICS_USER") or "").strip()
    if not user:
        return None, None, "HOMEHUB_METRICS_USER is unset; the tracker was not asked (it would mint any other user)"
    if not USER_RE.fullmatch(user):
        return None, None, "HOMEHUB_METRICS_USER is not a plain identity; the tracker was not asked"
    return url, user, ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def fetch_today(url: str, user: str, token: str, timeout: float, opener=None) -> Tuple[Optional[dict], str]:
    """GET /api/today -> (payload, why). Never raises; the body of an error is never kept."""
    headers = {"Accept": "application/json", "X-Forwarded-User": user}
    if token:
        headers["Authorization"] = "Bearer " + token
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, headers=headers, method="GET"), timeout=timeout) as resp:
            if resp.status != 200:
                return None, "HTTP %s" % resp.status
            raw = resp.read(TRACKER_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return None, "HTTP %s (body not kept)" % exc.code
    except Exception as exc:  # noqa: BLE001 - any failure is a recorded failure
        return None, type(exc).__name__
    if len(raw) > TRACKER_MAX_BYTES:
        return None, "the answer is larger than %d bytes" % TRACKER_MAX_BYTES
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None, "the answer is not JSON"
    if not isinstance(payload, dict):
        return None, "the answer is not an object"
    return payload, "ok"


GAUGE_FIELDS = ("value", "min", "max", "target", "pace", "deviation", "previous_value")


def compact_gauges(payload: dict) -> dict:
    """The served gauges and a count of the day's items, small enough for a line
    every 5 minutes. Pure. Implements: LLR-1322"""
    out = []
    for view in payload.get("gauges") or []:
        if not isinstance(view, dict) or not isinstance(view.get("id"), str):
            continue
        row = {"id": _short(view["id"], 120)}
        for key in GAUGE_FIELDS:
            if key in view:
                row[key] = _num(view.get(key), 4)
        for key in ("unit", "observed_at", "css", "trend", "previous_observed_at"):
            if isinstance(view.get(key), str):
                row[key] = _short(view[key], 64)
        row["stale"] = view.get("stale") is True
        window = view.get("window")
        if isinstance(window, dict) and isinstance(window.get("kind"), str):
            row["window"] = _short(window["kind"], 32)
        out.append(row)
    items = [i for i in payload.get("items") or [] if isinstance(i, dict)]
    return {
        "ok": True,
        "list": out,
        "items": len(items),
        "done": sum(1 for i in items if i.get("done") is True),
        "delinquent": payload.get("delinquent") is True,
        "revision": _num(payload.get("revision"), 0),
    }


def collect_gauges(knobs: Dict[str, str], deadline: Deadline, fetch=fetch_today) -> dict:
    url, user, refusal = tracker_target(knobs)
    if refusal:
        return {"ok": False, "error": refusal}
    payload, why = fetch(url, user, knobs.get("TRACKER_API_TOKEN", ""), deadline.cut(TRACKER_TIMEOUT_SEC))
    if payload is None:
        return {"ok": False, "error": "/api/today: %s" % why}
    return compact_gauges(payload)


# ── host: the newest telemetry sample, memory, uptime (LLR-1323) ────────────


def read_last_line(path: str, limit: int = 65536) -> Optional[dict]:
    """The last complete JSON line of `path`, reading at most `limit` bytes from its end."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            tail = handle.read(limit)
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            record = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(record, dict):
            return record
    return None


def summarise_telemetry(record: Optional[dict], now: float, tel) -> dict:
    """The few telemetry fields a trend needs. Pure (`tel` is the telemetry module)."""
    if not record or not isinstance(record.get("epoch"), (int, float)):
        return {"ok": False, "error": "no telemetry sample"}
    age = now - record["epoch"]
    mhz = [m for m in record.get("cpu_mhz") or [] if isinstance(m, (int, float))]
    psi = record.get("psi") if isinstance(record.get("psi"), dict) else {}
    out = {
        "ok": 0 <= age <= TELEMETRY_MAX_AGE_SEC,
        "age_s": int(age),
        "cpu_c": tel.cpu_temp(record),
        "nvme_c": tel.nvme_temp(record),
        "busy_pct": _num(record.get("cpu_busy_pct"), 1),
        "mhz_min": min(mhz) if mhz else None,
        "mhz_max": max(mhz) if mhz else None,
        "throttle": record.get("throttle") if isinstance(record.get("throttle"), dict) else None,
        "load": record.get("load") if isinstance(record.get("load"), list) else None,
        "psi_cpu10": _num((psi.get("cpu") or {}).get("some_avg10"), 2),
        "psi_mem10": _num((psi.get("memory") or {}).get("some_avg10"), 2),
        "psi_io10": _num((psi.get("io") or {}).get("some_avg10"), 2),
        "fs": {f.get("mount"): f.get("used_pct") for f in record.get("fs") or []
               if isinstance(f, dict) and isinstance(f.get("mount"), str)},
        "alarms": record.get("alarms") if isinstance(record.get("alarms"), list) else [],
    }
    if not out["ok"]:
        out["error"] = "the newest telemetry sample is %ds old" % age
    return out


def read_meminfo(proc_root: str) -> dict:
    fields = {}
    try:
        with open(os.path.join(proc_root, "meminfo"), encoding="ascii", errors="replace") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                    parts = rest.split()
                    if parts and parts[0].isdigit():
                        fields[key] = int(parts[0]) // 1024
    except OSError:
        return {}
    out = {"total_mb": fields.get("MemTotal"), "avail_mb": fields.get("MemAvailable")}
    if "SwapTotal" in fields and "SwapFree" in fields:
        out["swap_used_mb"] = fields["SwapTotal"] - fields["SwapFree"]
    return out


def read_uptime(proc_root: str) -> Optional[int]:
    try:
        with open(os.path.join(proc_root, "uptime"), encoding="ascii") as handle:
            return int(float(handle.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


# ── containers and units: queries only (LLR-1323) ───────────────────────────


def parse_container_lines(text: str) -> List[dict]:
    """`docker inspect` lines `name<TAB>state<TAB>restarts<TAB>health` -> rows."""
    rows = []
    for line in text.splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 3:
            continue
        name = parts[0].lstrip("/")
        if not NAME_RE.fullmatch(name) or not parts[2].isdigit():
            continue
        row = {"name": name, "state": _short(parts[1], 24), "restarts": int(parts[2])}
        if len(parts) > 3 and parts[3]:
            row["health"] = _short(parts[3], 24)
        rows.append(row)
    return sorted(rows, key=lambda r: r["name"])


def collect_containers(deadline: Deadline, run=mc.run_bounded) -> dict:
    ids = run(["docker", "ps", "-a", "-q", "--no-trunc"], deadline.cut(15), 65536)
    if not ids.ok:
        return {"ok": False, "error": "docker ps did not answer"}
    wanted = [i for i in ids.output.decode("utf-8", "replace").split() if re.fullmatch(r"[0-9a-f]{12,64}", i)]
    if not wanted:
        return {"ok": True, "list": []}
    fmt = "{{.Name}}\t{{.State.Status}}\t{{.RestartCount}}\t{{if .State.Health}}{{.State.Health.Status}}{{end}}"
    done = run(["docker", "inspect", "--format", fmt] + wanted, deadline.cut(15), 262144)
    if not done.ok:
        return {"ok": False, "error": "docker inspect did not answer"}
    return {"ok": True, "list": parse_container_lines(done.output.decode("utf-8", "replace"))}


def parse_unit_list(text: str) -> List[str]:
    """Unit names from `systemctl list-units --plain --no-legend` output."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if parts and UNIT_RE.fullmatch(parts[0]):
            out.append(parts[0])
    return out


def parse_show_blocks(text: str) -> Dict[str, dict]:
    """`systemctl show --timestamp=unix` for several units -> {short name: fields}."""
    out = {}
    for block in re.split(r"\n\s*\n", text.strip()):
        props = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                props[key] = value
        unit = props.get("Id", "")
        if not UNIT_RE.fullmatch(unit):
            continue
        name = re.sub(r"\.service$", "", re.sub(r"^homehub-", "", unit))
        stamp = re.fullmatch(r"@([0-9]+)(?:\.[0-9]+)?", props.get("ExecMainExitTimestamp", "").strip())
        row = {"a": _short(props.get("ActiveState", ""), 16), "r": _short(props.get("Result", ""), 24)}
        if props.get("ExecMainStatus", "").isdigit():
            row["x"] = int(props["ExecMainStatus"])
        if stamp:
            row["t"] = int(stamp.group(1))
        if props.get("NRestarts", "").isdigit() and int(props["NRestarts"]):
            row["n"] = int(props["NRestarts"])
        out[name] = row
    return out


SHOW_PROPS = "Id,ActiveState,Result,ExecMainStatus,ExecMainExitTimestamp,NRestarts"


def collect_units(deadline: Deadline, run=mc.run_bounded) -> dict:
    out = {"ok": True}
    failed = run(["systemctl", "list-units", "--state=failed", "--plain", "--no-legend", "--no-pager"],
                 deadline.cut(10), 65536)
    out["failed"] = parse_unit_list(failed.output.decode("utf-8", "replace")) if failed.ok else None
    if not failed.ok:
        out["error"] = "systemctl list-units --state=failed did not answer"
    ours = run(["systemctl", "list-units", "--all", "--plain", "--no-legend", "--no-pager", "homehub-*.service"],
               deadline.cut(10), 262144)
    names = parse_unit_list(ours.output.decode("utf-8", "replace")) if ours.ok else []
    if not names:
        # A list that could not be taken is a failure, never "no services" (F5).
        out["ok"] = failed.ok and ours.ok
        out["services"] = {}
        if not ours.ok:
            out.setdefault("error", "systemctl list-units did not answer")
        return out
    shown = run(["systemctl", "show", "--timestamp=unix", "-p", SHOW_PROPS] + names, deadline.cut(10), 1048576)
    out["services"] = parse_show_blocks(shown.output.decode("utf-8", "replace")) if shown.ok else {}
    out["ok"] = failed.ok and shown.ok
    if not shown.ok:
        out.setdefault("error", "systemctl show did not answer")
    return out


# ── the monitors' own status files (LLR-1323) ───────────────────────────────


def summarise_monitors(watch: dict, disk: dict, liveness: dict, meetings: dict, now: float) -> dict:
    """Verdicts only, never the monitors' prose. Pure."""
    def age(doc):
        ts = doc.get("ts")
        return int(now - ts) if isinstance(ts, (int, float)) else None

    out = {}
    if watch:
        out["watch"] = {"age_s": age(watch), "rows": {r.get("name"): r.get("verdict")
                        for r in watch.get("rows") or [] if isinstance(r, dict) and isinstance(r.get("name"), str)}}
    if disk:
        out["disk"] = {"age_s": age(disk), "mounts": {m.get("label"): m.get("state")
                       for m in disk.get("mounts") or [] if isinstance(m, dict) and isinstance(m.get("label"), str)}}
    if liveness:
        out["liveness"] = {"age_s": age(liveness), "ok": liveness.get("ok") is True,
                           "incident": liveness.get("incident") is True}
    if meetings:
        out["meeting_pull"] = {"age_s": age(meetings), "exit": meetings.get("exit")
                               if isinstance(meetings.get("exit"), int) else None}
    return out


def collect_monitors(monitor_dir: str, meetings_status: str, now: float, data_dir: Optional[str] = None) -> dict:
    def read(name):
        return _read_bounded_json(os.path.join(monitor_dir, name), 1048576, {})

    out = summarise_monitors(read("watch-status.json"), read("disk-status.json"),
                             read("liveness-status.json"), _read_bounded_json(meetings_status, 65536, {}), now)
    if data_dir:
        copy = _read_bounded_json(os.path.join(data_dir, "copy-status.json"), 65536, {})
        # Present in every snapshot: "never" until the first weekly run writes one.
        out["copy"] = {k: copy.get(k) for k in ("last_run", "last_result", "last_success", "last_skip",
                                                "last_skip_reason", "written", "failed") if k in copy} \
            if copy else {"last_result": "never"}
    return out


# ── the panel's status, over the meeting pull's key (LLR-1324) ──────────────


def panel_argv(knobs: Dict[str, str], meetings_module) -> Tuple[Optional[List[str]], str]:
    """The ssh argv for the panel's `status` verb, or (None, why not).

    It is the meeting pull's own ssh prefix (homehub_meetings.ssh_argv: its key,
    its pinned known_hosts, BatchMode, bounded waits) with one change: a host key
    is NEVER accepted here. First contact is the pull's job; this run only asks
    a panel the pull has already pinned.
    """
    if not mc.knob_bool(knobs, "HOMEHUB_METRICS_PANEL", True):
        return None, "HOMEHUB_METRICS_PANEL=false"
    if not meetings_module:
        return None, "the meeting pull is not on this payload"
    settings = meetings_module.settings_from(knobs)
    if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", str(settings.get("host", ""))):
        return None, "no panel address (MEETING_PANEL_HOST or PANEL_IP)"
    state_dir = getattr(meetings_module, "STATE_DIR", "/var/lib/homehub-meetings")
    if not os.path.exists(os.path.join(state_dir, "known_hosts")):
        return None, "the meeting pull has not pinned the panel's host key yet"
    argv = [("StrictHostKeyChecking=yes" if a == "StrictHostKeyChecking=accept-new" else a)
            for a in meetings_module.ssh_argv(settings, state_dir)]
    return argv + ["status"], ""


def _pct(free, total):
    if isinstance(free, (int, float)) and isinstance(total, (int, float)) and total > 0:
        return round(100.0 * free / total, 1)
    return None


def summarise_panel(doc: dict) -> dict:
    """The panel's `status` answer -> a short summary. Pure. Implements: LLR-1324"""
    out = {"ok": True}
    for key in ("now", "uptime_s"):
        if isinstance(doc.get(key), (int, float)):
            out[key] = int(doc[key])
    disk = doc.get("disk")
    if isinstance(disk, dict) and "error" not in disk:
        out["disk"] = {"low": disk.get("low") is True, "observed_at": _short(disk.get("observedAt"), 32),
                       "free_pct": {m.get("label"): _pct(m.get("freeBytes"), m.get("totalBytes"))
                                    for m in disk.get("mounts") or []
                                    if isinstance(m, dict) and isinstance(m.get("label"), str)}}
    elif isinstance(disk, dict):
        out["disk"] = {"error": _short(disk.get("error"), 80)}
    freeze = doc.get("freeze")
    if isinstance(freeze, dict) and "error" not in freeze:
        out["freeze"] = {k: freeze.get(k) for k in ("ts", "result", "draw_s", "fails", "captured")
                         if isinstance(freeze.get(k), (int, float, str)) and not isinstance(freeze.get(k), bool)}
    elif isinstance(freeze, dict):
        out["freeze"] = {"error": _short(freeze.get("error"), 80)}
    bundles = doc.get("freeze_bundles")
    if isinstance(bundles, dict):
        out.setdefault("freeze", {})["bundles"] = bundles.get("count") if isinstance(bundles.get("count"), int) else None
        out["freeze"]["newest_bundle"] = _short(bundles.get("newest"), 32)
    tele = doc.get("telemetry")
    if isinstance(tele, dict) and "error" not in tele:
        temps = [s.get("deg_c") for s in tele.get("sensors") or []
                 if isinstance(s, dict) and s.get("canonical") is True]
        mhz = [m for m in tele.get("cpufreq_mhz") or [] if isinstance(m, (int, float))]
        pres = tele.get("presentation") if isinstance(tele.get("presentation"), dict) else {}
        payload = pres.get("payload") if isinstance(pres.get("payload"), dict) else {}
        out["telemetry"] = {
            "ts": _short(tele.get("ts_utc"), 32),
            "cpu_pct": _num(tele.get("cpu_pct"), 1),
            "cpu_c": _num(temps[0], 1) if temps else None,
            "mhz_max": max(mhz) if mhz else None,
            "kiosk": {k.get("name"): [_num(k.get("cpu_pct"), 1), k.get("rss_kb")]
                      for k in tele.get("kiosk") or [] if isinstance(k, dict) and isinstance(k.get("name"), str)},
            "presentation": _short(pres.get("status"), 16),
            # The payload's own names (OfficeWallNaglight electron/presentation-ipc.cjs).
            "display_mode": _short(payload.get("displayMode"), 16),
            "selected_tab": _short(payload.get("selectedTab"), 32),
            "display_power": _short(payload.get("displayPower"), 16),
            "music_active": payload.get("musicActive") if isinstance(payload.get("musicActive"), bool) else None,
        }
    elif isinstance(tele, dict):
        out["telemetry"] = {"error": _short(tele.get("error"), 80)}
    return out


def collect_panel(knobs: Dict[str, str], deadline: Deadline, meetings_module, run=mc.run_bounded) -> dict:
    argv, why = panel_argv(knobs, meetings_module)
    if argv is None:
        return {"ok": False, "error": why}
    if deadline.spent():
        return {"ok": False, "error": "no time left in this run"}
    done = run(argv, deadline.cut(PANEL_TIMEOUT_SEC), PANEL_MAX_BYTES)
    if done.timed_out:
        return {"ok": False, "error": "the panel did not answer within the run's limit"}
    if not done.ok:
        return {"ok": False, "error": "ssh exited %s" % done.rc}
    try:
        doc = json.loads(done.output.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": "the panel's answer is not JSON"}
    if not isinstance(doc, dict):
        return {"ok": False, "error": "the panel's answer is not an object"}
    return summarise_panel(doc)


# ── NVMe SMART, hourly (LLR-1323) ───────────────────────────────────────────

SMART_KEYS = ("critical_warning", "temperature", "available_spare", "percentage_used",
              "data_units_read", "data_units_written", "power_on_hours", "unsafe_shutdowns",
              "media_errors", "num_err_log_entries")


def summarise_smart(doc: dict) -> dict:
    log_ = doc.get("nvme_smart_health_information_log")
    if not isinstance(log_, dict):
        return {"ok": False, "error": "no NVMe health log in smartctl's answer"}
    out = {"ok": True}
    for key in SMART_KEYS:
        if isinstance(log_.get(key), int) and not isinstance(log_.get(key), bool):
            out[key] = log_[key]
    passed = (doc.get("smart_status") or {}).get("passed") if isinstance(doc.get("smart_status"), dict) else None
    if isinstance(passed, bool):
        out["passed"] = passed
    return out


def collect_smart(deadline: Deadline, run=mc.run_bounded) -> dict:
    done = run(["smartctl", "-j", "-H", "-A", SMART_DEVICE], deadline.cut(15), 262144)
    if done.error or done.timed_out:
        return {"ok": False, "error": "smartctl did not run (%s)" % ("timeout" if done.timed_out else done.error)}
    try:
        doc = json.loads(done.output.decode("utf-8", "replace"))
    except ValueError:
        return {"ok": False, "error": "smartctl's answer is not JSON (exit %s)" % done.rc}
    return summarise_smart(doc) if isinstance(doc, dict) else {"ok": False, "error": "not an object"}


# ── snapshot ─────────────────────────────────────────────────────────────────


def _telemetry_module():
    try:
        return _load("homehub_telemetry_for_metrics", os.path.join(HERE, "homehub-telemetry.py"))
    except Exception:  # noqa: BLE001 - a missing module costs the host block only
        return None


def _meetings_module():
    path = os.path.join(HERE, "..", "meetings", "homehub_meetings.py")
    if not os.path.exists(path):
        return None
    try:
        return _load("homehub_meetings_for_metrics", path)
    except Exception:  # noqa: BLE001
        return None


def build_snapshot(args, knobs: Dict[str, str], state: dict, now: float, deadline: Deadline,
                   fetch=fetch_today, run=mc.run_bounded, meetings=None) -> dict:
    """One snapshot record. Every block is independent: one that fails is
    recorded as {"ok": false, "error": ...} and costs nothing else."""
    tel = _telemetry_module()
    record = {"v": 1, "ts": iso_utc(now), "epoch": round(now, 3), "boot_id": mc.read_boot_id(args.proc_root)}

    def guarded(collect):
        # A block's own bug (review round 1, F2) must cost that block only:
        # the rest of the line is still written. Type only, never a message.
        try:
            return collect()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "unexpected %s" % type(exc).__name__}

    def host_block():
        host = {"uptime_s": read_uptime(args.proc_root), "mem": read_meminfo(args.proc_root)}
        if tel is not None:
            newest = mc.day_files_between(args.telemetry_dir, "telemetry", now - 86400, now)
            host["telemetry"] = summarise_telemetry(read_last_line(newest[-1]) if newest else None, now, tel)
        else:
            host["telemetry"] = {"ok": False, "error": "homehub-telemetry.py is not on this payload"}
        return host

    record["gauges"] = guarded(lambda: collect_gauges(knobs, deadline, fetch))
    record["host"] = guarded(host_block)
    record["containers"] = guarded(lambda: collect_containers(deadline, run))
    record["units"] = guarded(lambda: collect_units(deadline, run))
    record["monitors"] = guarded(lambda: collect_monitors(args.monitor_dir, args.meetings_status, now, args.data_dir))
    record["panel"] = guarded(lambda: collect_panel(
        knobs, deadline, meetings if meetings is not None else _meetings_module(), run))
    last_smart = state.get("smart_epoch")
    if not isinstance(last_smart, (int, float)) or not 0 <= now - last_smart < SMART_EVERY_SEC - 60:
        record["smart"] = guarded(lambda: collect_smart(deadline, run))
        # Marked done by cmd_snapshot only once the line is on disk (F11).
        state["_smart_due"] = int(now)
    return record


def cmd_snapshot(args, fetch=fetch_today, run=mc.run_bounded, meetings=None) -> int:
    deadline = Deadline(SNAPSHOT_DEADLINE_SEC)
    knobs = mc.read_knobs(args.env_file)
    state_path = os.path.join(args.data_dir, STATE_FILE)
    state = mc.load_json(state_path, {})
    now = time.time()
    record = build_snapshot(args, knobs, state, now, deadline, fetch, run, meetings)
    active = mc.day_file_name(SNAPSHOT_PREFIX, now)
    ok, why = mc.append_jsonl(os.path.join(args.data_dir, active), record, DAY_CAP_BYTES)
    smart_due = state.pop("_smart_due", None)
    if ok and smart_due is not None:
        state["smart_epoch"] = smart_due
    gauges = record["gauges"]
    mc.log("metrics: snapshot %s (gauges %s, panel %s)" % (
        why, len(gauges.get("list", [])) if gauges.get("ok") else gauges.get("error"),
        "ok" if record["panel"].get("ok") else record["panel"].get("error")), warning=not ok)
    if ok:
        for note in mc.enforce_day_files(args.data_dir, SNAPSHOT_PREFIX, active, KEEP_DAYS,
                                         COMPRESS_AFTER_DAYS, TOTAL_CAP_BYTES):
            mc.log("metrics: retention " + note, warning="could not" in note)
    if not mc.save_json_atomic(state_path, state):
        mc.log("metrics: WARN could not save %s (SMART is asked again next run)" % state_path, True)
    return 0 if ok else 1


# ── daily (LLR-1325, LLR-1326, LLR-1327) ────────────────────────────────────


def _zone(zoneinfo_module=None):
    if zoneinfo_module is None:
        import zoneinfo as zoneinfo_module  # Python 3.9+; the hub runs 3.12
    return zoneinfo_module.ZoneInfo(DAILY_ZONE)


def _local_epoch(day: _dt.date, hour: int, minute: int, zone) -> float:
    return _dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).timestamp()


def due_kinds(now: float, zone, written: List[Tuple[str, str]]) -> Tuple[str, List[str]]:
    """(night, kinds due now) for the night that ended on today's local date. Pure.

    `initial` is due from 10:00 local, `revision` from 14:05, each only if no row
    of that kind exists for the night (`written` is [(night, kind)]). A catch-up
    after 14:05 writes both. Nothing is back-filled for an earlier night: its
    gauges have moved on, so a late row would record today's values under
    yesterday's name. Implements: LLR-1325
    """
    today = _dt.datetime.fromtimestamp(now, zone).date()
    night = today.isoformat()
    have = {k for n, k in written if n == night}
    due = []
    if now >= _local_epoch(today, INITIAL_AT[0], INITIAL_AT[1], zone) and "initial" not in have:
        due.append("initial")
    if now >= _local_epoch(today, REVISION_AT[0], REVISION_AT[1], zone) and "revision" not in have:
        due.append("revision")
    return night, due


def written_rows(path: str) -> List[Tuple[str, str]]:
    return [(r.get("night"), r.get("kind")) for r in mc.iter_jsonl(path)]


def overnight_range(records, start: float, end: float, tel) -> dict:
    """Min/max CPU and NVMe temperature over [start, end) from telemetry records. Pure."""
    cpu, nvme = [], []
    for rec in records:
        epoch = rec.get("epoch")
        if not isinstance(epoch, (int, float)) or not start <= epoch < end:
            continue
        c, n = tel.cpu_temp(rec), tel.nvme_temp(rec)
        if c is not None:
            cpu.append(c)
        if n is not None:
            nvme.append(n)
    out = {"from": iso_utc(start), "to": iso_utc(end), "samples": len(cpu)}
    if cpu:
        out["cpu_c"] = [min(cpu), max(cpu)]
    if nvme:
        out["nvme_c"] = [min(nvme), max(nvme)]
    return out


def heart_night(weight_state: dict, night: str) -> Optional[dict]:
    nights = weight_state.get("heart-nights")
    entry = nights.get(night) if isinstance(nights, dict) else None
    if not isinstance(entry, dict):
        return None
    return {k: entry.get(k) for k in ("rhr", "p25", "mean", "n", "window", "device", "observed_at") if k in entry}


def sleep_night(sleep_state: dict, night: str) -> dict:
    out = {}
    usage = sleep_state.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get(night), int) and not isinstance(usage.get(night), bool):
        out["cpap_minutes"] = usage[night]
    nights = sleep_state.get("nights")
    entry = nights.get(night) if isinstance(nights, dict) else None
    if isinstance(entry, dict):
        out.update({k: entry.get(k) for k in ("score", "observed_at") if k in entry})
    return out


def build_daily(night: str, kind: str, now: float, zone, gauges: dict, weight_state: dict,
                sleep_state: dict, overnight: dict, units: Dict[str, dict]) -> dict:
    """One daily row. Pure. Implements: LLR-1326"""
    scheduled = INITIAL_AT if kind == "initial" else REVISION_AT
    day = _dt.date.fromisoformat(night)
    due_at = _local_epoch(day, scheduled[0], scheduled[1], zone)
    row = {"v": 1, "night": night, "kind": kind, "written_at": iso_utc(now), "epoch": round(now, 3),
           "scheduled": "%02d:%02d %s" % (scheduled[0], scheduled[1], DAILY_ZONE),
           "late": now - due_at > LATE_AFTER_SEC}
    if gauges.get("ok"):
        row["gauges"] = {g["id"]: {k: g.get(k) for k in ("value", "target", "unit", "observed_at", "stale", "css")
                                   if k in g} for g in gauges.get("list", [])}
        # A gauge with no observed_at is the tracker's "unavailable" body.
        row["missing"] = sorted(g["id"] for g in gauges.get("list", [])
                                if g.get("stale") or not g.get("observed_at"))
    else:
        row["gauges_error"] = gauges.get("error")
    row["heart_night"] = heart_night(weight_state, night)
    row["sleep"] = sleep_night(sleep_state, night)
    row["overnight"] = overnight
    row["backup"] = {name: units.get(name) for name in units}
    return row


def cmd_daily(args, fetch=fetch_today, run=mc.run_bounded, zone=None, now=None) -> int:
    deadline = Deadline(DAILY_DEADLINE_SEC)
    knobs = mc.read_knobs(args.env_file)
    zone = zone or _zone()
    now = time.time() if now is None else now
    path = os.path.join(args.data_dir, DAILY_FILE)
    night, due = due_kinds(now, zone, written_rows(path))
    if not due:
        mc.log("metrics: daily row for %s already written (or not due yet); nothing to do" % night)
        return 0
    try:
        gauges = collect_gauges(knobs, deadline, fetch)
    except Exception as exc:  # noqa: BLE001 - the row is still written (review round 2, item 6)
        gauges = {"ok": False, "error": "unexpected %s" % type(exc).__name__}
    weight_state = _read_bounded_json(knobs.get("WEIGHT_STATE_FILE") or WEIGHT_STATE, STATE_MAX_BYTES, {})
    sleep_state = _read_bounded_json(knobs.get("SLEEP_STATE_FILE") or SLEEP_STATE, STATE_MAX_BYTES, {})
    day = _dt.date.fromisoformat(night)
    start = _local_epoch(day - _dt.timedelta(days=1), OVERNIGHT_FROM_HOUR, 0, zone)
    end = _local_epoch(day, OVERNIGHT_TO_HOUR, 0, zone)
    tel = _telemetry_module()
    if tel is not None:
        try:
            files = mc.day_files_between(args.telemetry_dir, "telemetry", start, end)
            overnight = overnight_range((r for f in files for r in mc.iter_jsonl(f)), start, end, tel)
        except Exception as exc:  # noqa: BLE001
            overnight = {"error": "unexpected %s" % type(exc).__name__}
    else:
        overnight = {"error": "homehub-telemetry.py is not on this payload"}
    shown = run(["systemctl", "show", "--timestamp=unix", "-p", SHOW_PROPS] + list(BACKUP_UNITS),
                deadline.cut(10), 65536)
    units = parse_show_blocks(shown.output.decode("utf-8", "replace")) if shown.ok else {}
    rc = 0
    for kind in due:
        row = build_daily(night, kind, now, zone, gauges, weight_state, sleep_state, overnight, units)
        ok, why = mc.append_jsonl(path, row, DAILY_CAP_BYTES)
        mc.log("metrics: daily %s row for %s %s (missing: %s)" % (
            kind, night, why, ", ".join(row.get("missing", [])) or "none"), warning=not ok)
        rc = rc or (0 if ok else 1)
    return rc


# ── the weekly copy into the Owner's Docs on the library (LLR-1332) ─────────
#
# The Owner's ruling 2026-10-26, which supersedes "not backed up" in part: once a
# week the closed day files, the daily rows and the raw watch samples are copied
# into a folder of the Owner's private Docs tree on the library drive, so the
# library's own backup carries them. ON THE HUB the files stay out of the config
# backups. The copy is rsync-shaped and never deletes: a file whose size and
# mtime already match at the destination is skipped, anything else is written to
# a temporary name and renamed over, and nothing at the destination is removed.
# Today's UTC files are never copied (still being written), and the snapshot day
# files go across gzip'd - a closed day not yet compressed on the hub is gzip'd
# on the way, to the same bytes the hub will later make (monitor_common's
# deterministic gzip) - so the destination holds one file per day either way.

LIBRARY_ROOT = "/srv/library"
# The copy's own record, beside the log, carried in every snapshot (monitors.copy):
# a skipped week is visible without the journal.
COPY_STATUS_FILE = "copy-status.json"
LIBRARY_MARKER = ".homehub-library"
COPY_SETTLE_SEC = 600            # a day file last written this recently is not closed yet
COPY_DEADLINE_SEC = 1500
MTIME_SLACK_SEC = 2.0


def copy_target(knobs: Dict[str, str], mounts: List[Dict[str, str]], exists=os.path.exists,
                realpath=os.path.realpath, device=None) -> Tuple[Optional[str], str]:
    """(destination, refusal). Pure apart from the injected checks.

    The destination is HOMEHUB_METRICS_COPY_DIR (blank: the copy is off). It
    must be a folder directly inside an EXISTING folder under the library root;
    the library root must be a mount point in the mount table (the bare
    mountpoint underneath is chmod 000 and must never be written into, and root
    is not stopped by a mode) and must carry the library's marker file (a
    blank or stand-in disk is mounted, writable and empty), and the folder
    the destination sits in must be on the library's own filesystem. The copy
    never creates the Owner's folder itself, only its own folder inside it.
    """
    dest = (knobs.get("HOMEHUB_METRICS_COPY_DIR") or "").strip()
    if not dest:
        return None, "HOMEHUB_METRICS_COPY_DIR is unset; the weekly copy is off"
    root = (knobs.get("HOMEHUB_METRICS_COPY_ROOT") or LIBRARY_ROOT).rstrip("/") or "/"
    if not os.path.isabs(dest) or ".." in dest.split("/"):
        return None, "HOMEHUB_METRICS_COPY_DIR is not a plain absolute path"
    if mc.mount_entry(mounts, root) is None:
        return None, "%s is not mounted; nothing copied (the bare mountpoint is never written)" % root
    if not exists(os.path.join(root, LIBRARY_MARKER)):
        return None, "%s carries no %s marker; not the library, nothing copied" % (root, LIBRARY_MARKER)
    real = realpath(dest).rstrip("/\\")
    root_real = _norm(realpath(root))
    if not _norm(real).startswith(root_real + "/"):
        return None, "HOMEHUB_METRICS_COPY_DIR resolves outside %s; nothing copied" % root
    parent = os.path.dirname(real)
    if _norm(parent) == root_real or not exists(parent):
        return None, "the folder HOMEHUB_METRICS_COPY_DIR should be in does not exist; nothing copied"
    # Not a different disk mounted somewhere under the library (review F9).
    device = device or (lambda path: os.stat(path).st_dev)
    try:
        if device(parent) != device(root):
            return None, "HOMEHUB_METRICS_COPY_DIR is on another filesystem than %s; nothing copied" % root
    except OSError:
        return None, "the destination's folder could not be examined; nothing copied"
    return real, ""


def _norm(path: str) -> str:
    """A path for comparison only: case and separators as the OS has them, no
    trailing separator (identity on the hub)."""
    return os.path.normcase(path).replace("\\", "/").rstrip("/")


def copy_plan(sources: List[Tuple[str, str, int, float]], dest_stat, today: str, now: float) -> List[Tuple[str, str, bool]]:
    """Which files to write. Pure.

    Contract:
      Inputs:  sources: [(kind, path, size, mtime)] where kind is "snapshot",
               "daily" or "raw"; dest_stat(rel) -> (size, mtime) or None;
               today: the UTC YYYYMMDD being written now.
      Outputs: [(source path, destination relative path, gzip on the way)].
               A source whose UTC day is today, or last written less than
               COPY_SETTLE_SEC ago, is not copied; one whose destination
               already has its size and its mtime (within MTIME_SLACK_SEC, for
               a filesystem that keeps coarser stamps) is skipped.
    """
    plan = []
    for kind, path, size, mtime in sources:
        name = os.path.basename(path)
        day = re.search(r"-([0-9]{8})\.jsonl", name)
        if kind != "daily" and (day is None or day.group(1) >= today):
            continue
        if kind != "daily" and now - mtime < COPY_SETTLE_SEC:
            continue
        if kind == "snapshot":
            rel, pack = "snapshots/" + (name if name.endswith(".gz") else name + ".gz"), not name.endswith(".gz")
        elif kind == "raw":
            rel, pack = "watch-raw/" + name, False
        else:
            rel, pack = name, False
        have = dest_stat(rel)
        if pack:
            # The size of the gzip'd form is not known without making it; a
            # closed day's (size, mtime) pair is the mtime alone, so match on it.
            if have is not None and abs(have[1] - mtime) < MTIME_SLACK_SEC:
                continue
        elif have is not None and have[0] == size and abs(have[1] - mtime) < MTIME_SLACK_SEC:
            continue
        plan.append((path, rel, pack))
    return plan


# The destination is group-writable for the household (the library mount's
# gid and umask), and this copy runs as root, so nothing under it is trusted:
# every hop is opened relative to the one before with O_NOFOLLOW, a component
# that is a symlink is refused, and the file is created O_EXCL under a temporary
# name and renamed over the target (a rename replaces a link, never follows it).
# Review round 1, F1.
DIR_FD_OPENS = (os.open in getattr(os, "supports_dir_fd", set()) and hasattr(os, "O_DIRECTORY"))
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _open_dir(name: str, parent_fd: int, create: bool, device: Optional[int]) -> int:
    """Open directory `name` under `parent_fd` (creating it when `create`),
    refusing a link and a directory on another filesystem than `device`."""
    if create:
        try:
            os.mkdir(name, 0o775, dir_fd=parent_fd)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=parent_fd)
    if device is not None and os.fstat(fd).st_dev != device:
        os.close(fd)
        raise PermissionError("REFUSED: %s is on another filesystem than the library" % name)
    return fd


def _copy_one(src: str, dest_root: str, rel: str, pack: bool, library_root: Optional[str] = None) -> None:
    """Copy one file to `dest_root/rel`. With `library_root` (the trusted mount
    point) the walk starts THERE, so no component between it and the file is
    resolved by name: each is opened O_NOFOLLOW relative to the one before and
    must be on the library's filesystem (review round 2, items 2 and 3)."""
    import gzip
    import shutil

    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise PermissionError("REFUSED: %r is not a plain relative path" % rel)
    # First, so a source that cannot be read costs no temp file; O_NOFOLLOW and
    # a regular-file check on the OPEN file, so what was listed is what is read.
    # O_NONBLOCK: a FIFO swapped in for a day file cannot hold the open.
    sfd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                  | getattr(os, "O_BINARY", 0))
    if not stat.S_ISREG(os.fstat(sfd).st_mode):
        os.close(sfd)
        raise PermissionError("REFUSED: %s is not a regular file" % src)
    raw = os.fdopen(sfd, "rb")
    try:
        _copy_from(raw, os.fstat(raw.fileno()).st_mtime, dest_root, parts, pack, library_root, gzip, shutil)
    finally:
        raw.close()


def _copy_from(raw, stamp, dest_root, parts, pack, library_root, gzip, shutil) -> None:
    leaf, tmp = parts[-1], "." + parts[-1] + ".part"
    if not DIR_FD_OPENS:                      # pragma: no cover - the Windows dev PC
        walked = dest_root
        for name in [""] + parts[:-1]:
            walked = os.path.join(walked, name) if name else walked
            if os.path.islink(walked):
                raise PermissionError("REFUSED: %s is a symlink" % walked)
            os.makedirs(walked, exist_ok=True)
        target, tmp_path = os.path.join(walked, leaf), os.path.join(walked, tmp)
        for path in (target, tmp_path):
            if os.path.islink(path):
                raise PermissionError("REFUSED: %s is a symlink" % path)
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o664)
        dir_fd = None
    else:
        dest = dest_root.rstrip("/")
        if library_root:
            base_dir = library_root.rstrip("/") or "/"
            between = os.path.relpath(dest, base_dir).split("/")
            if any(p in ("", ".", "..") for p in between):
                raise PermissionError("REFUSED: %s is not inside %s" % (dest, base_dir))
        else:
            base_dir, last = os.path.split(dest)
            between = [last]
        # The existing folders down to the destination are opened, never made;
        # only the destination's own folder and its subfolders are created.
        hops = [(name, False) for name in between[:-1]] + [(between[-1], True)] + [(n, True) for n in parts[:-1]]
        dir_fd = os.open(base_dir, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
        try:
            device = None
            if library_root:
                device = os.fstat(dir_fd).st_dev
                # The marker again, relative to the directory now held open: a
                # library unmounted since copy_target looked leaves the bare
                # mountpoint, which has no marker (review round 3, item 3).
                os.stat(LIBRARY_MARKER, dir_fd=dir_fd)
            for name, create in hops:
                hop = _open_dir(name, dir_fd, create, device)
                os.close(dir_fd)
                dir_fd = hop
            try:
                os.unlink(tmp, dir_fd=dir_fd)      # never follows: a planted link is removed, not its target
            except FileNotFoundError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o664, dir_fd=dir_fd)
        except BaseException:
            os.close(dir_fd)
            raise
    try:
        try:
            with os.fdopen(fd, "wb") as out:
                if pack:
                    with gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=out, mtime=0) as packed:
                        shutil.copyfileobj(raw, packed, 65536)
                else:
                    shutil.copyfileobj(raw, out, 65536)
                out.flush()
                os.fsync(out.fileno())
            if dir_fd is not None:
                os.utime(tmp, (stamp, stamp), dir_fd=dir_fd, follow_symlinks=False)
                os.replace(tmp, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            else:                              # pragma: no cover - the Windows dev PC
                os.utime(tmp_path, (stamp, stamp))
                os.replace(tmp_path, target)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd) if dir_fd is not None else os.unlink(tmp_path)
            except OSError:
                pass
            raise
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def copy_sources(data_dir: str, raw_dir: str) -> List[Tuple[str, str, int, float]]:
    out = []
    for kind, directory, pattern in (("snapshot", data_dir, r"^snapshot-[0-9]{8}\.jsonl(\.gz)?$"),
                                     ("daily", data_dir, r"^daily\.jsonl$"),
                                     ("raw", raw_dir, r"^(hr|sleep)-[0-9]{8}\.jsonl$")):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not re.match(pattern, name):
                continue
            path = os.path.join(directory, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            out.append((kind, path, st.st_size, st.st_mtime))
    # A day both plain and .gz on the hub (an unfinished unlink): the .gz is
    # the copy's source, so the plain one is dropped here.
    packed = {p[:-3] for k, p, _, _ in out if k == "snapshot" and p.endswith(".gz")}
    return [s for s in out if not (s[0] == "snapshot" and s[1] in packed)]


def cmd_copy(args, mounts_text: Optional[str] = None, now: Optional[float] = None) -> int:
    deadline = Deadline(COPY_DEADLINE_SEC)
    knobs = mc.read_knobs(args.env_file)
    if mounts_text is None:
        try:
            with open(os.path.join(args.proc_root, "self", "mountinfo"), encoding="utf-8", errors="replace") as h:
                mounts_text = h.read()
        except OSError:
            mounts_text = ""
    dest, refusal = copy_target(knobs, mc.parse_mountinfo(mounts_text))
    library_root = os.path.realpath((knobs.get("HOMEHUB_METRICS_COPY_ROOT") or LIBRARY_ROOT).rstrip("/") or "/")
    now = time.time() if now is None else now
    status_path = os.path.join(args.data_dir, COPY_STATUS_FILE)
    status = mc.load_json(status_path, {})
    status["last_run"] = int(now)
    if dest is None:
        mc.log("metrics: weekly copy SKIPPED - %s" % refusal, warning=bool(knobs.get("HOMEHUB_METRICS_COPY_DIR")))
        status.update({"last_result": "skipped", "last_skip": int(now), "last_skip_reason": refusal[:200]})
        status.pop("written", None)
        status.pop("failed", None)
        if not mc.save_json_atomic(status_path, status):
            mc.log("metrics: WARN could not save %s" % status_path, True)
        return 0
    today = time.strftime("%Y%m%d", time.gmtime(now))

    def dest_stat(rel):
        try:
            st = os.stat(os.path.join(dest, rel))
        except OSError:
            return None
        return st.st_size, st.st_mtime

    plan = copy_plan(copy_sources(args.data_dir, args.raw_dir), dest_stat, today, now)
    written, failed = 0, 0
    for src, rel, pack in plan:
        if deadline.spent():
            mc.log("metrics: weekly copy stopped at its deadline; %d left for next week" % (len(plan) - written - failed), True)
            break
        try:
            _copy_one(src, dest, rel, pack, library_root=library_root)
            written += 1
        except OSError as exc:
            failed += 1
            mc.log("metrics: weekly copy could not write %s (%s)" % (rel, type(exc).__name__), True)
    mc.log("metrics: weekly copy to the library: %d written, %d skipped (already there, or still open), %d failed"
           % (written, len(copy_sources(args.data_dir, args.raw_dir)) - len(plan), failed), warning=bool(failed))
    status.update({"last_result": "failed" if failed else "ok", "written": written, "failed": failed})
    if not failed:
        status["last_success"] = int(now)
    if not mc.save_json_atomic(status_path, status):
        mc.log("metrics: WARN could not save %s" % status_path, True)
    return 1 if failed else 0


# ── reading it back ──────────────────────────────────────────────────────────


def _parse_iso(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    parsed = _dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def iter_snapshots(data_dir: str, since: Optional[float], until: Optional[float]):
    for path in mc.day_files_between(data_dir, SNAPSHOT_PREFIX, since, until):
        for rec in mc.iter_jsonl(path):
            epoch = rec.get("epoch")
            if isinstance(epoch, (int, float)) and (since is None or epoch >= since) \
                    and (until is None or epoch <= until):
                yield rec


def summarise(records: List[dict]) -> List[str]:
    """A short digest of snapshot records. Pure."""
    if not records:
        return ["no snapshots"]
    first, last = records[0], records[-1]
    lines = ["%d snapshots, %s .. %s" % (len(records), first.get("ts"), last.get("ts"))]
    gauge_ok = sum(1 for r in records if (r.get("gauges") or {}).get("ok"))
    panel_ok = sum(1 for r in records if (r.get("panel") or {}).get("ok"))
    lines.append("gauges read: %d/%d   panel reached: %d/%d" % (gauge_ok, len(records), panel_ok, len(records)))
    for g in (last.get("gauges") or {}).get("list", []):
        lines.append("gauge %-16s %-10s %s%s" % (g.get("id"), g.get("value"), g.get("observed_at") or "-",
                                                  "  STALE" if g.get("stale") else ""))
    failed = (last.get("units") or {}).get("failed")
    lines.append("failed units: %s" % (", ".join(failed) if failed else "none"))
    return lines


def cmd_export(args) -> int:
    if args.daily:
        rows = mc.iter_jsonl(os.path.join(args.data_dir, DAILY_FILE))
    else:
        rows = iter_snapshots(args.data_dir, _parse_iso(args.since), _parse_iso(args.until))
    for rec in rows:
        sys.stdout.write(json.dumps(rec, sort_keys=True) + "\n")
    return 0


def cmd_summary(args) -> int:
    now = time.time()
    for line in summarise(list(iter_snapshots(args.data_dir, now - args.hours * 3600, now))):
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="HomeHub metrics log (SN-048, A79)")
    p.add_argument("--data-dir", default=os.environ.get("HOMEHUB_METRICS_DIR", DEFAULT_DATA_DIR))
    p.add_argument("--env-file", default=os.environ.get("HOMEHUB_MONITOR_ENV_FILE", mc.DEFAULT_ENV_FILE))
    p.add_argument("--telemetry-dir", default=os.environ.get("HOMEHUB_TELEMETRY_DIR", TELEMETRY_DIR))
    p.add_argument("--monitor-dir", default=os.environ.get("HOMEHUB_MONITOR_STATE_DIR", MONITOR_DIR))
    p.add_argument("--meetings-status", default=MEETINGS_STATUS)
    p.add_argument("--proc-root", default=os.environ.get("HOMEHUB_MONITOR_PROC_ROOT", "/proc"))
    p.add_argument("--raw-dir", default=os.environ.get("HOMEHUB_HEALTH_RAW_DIR", DEFAULT_RAW_DIR))
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("snapshot", help="take one snapshot (what the 5-minute timer runs)")
    sub.add_parser("daily", help="write the daily row(s) now due (what the 10:00/14:05 timer runs)")
    sub.add_parser("copy", help="copy closed day files into the library folder (what the weekly timer runs)")
    e = sub.add_parser("export", help="print snapshots (or --daily rows) as JSONL")
    e.add_argument("--daily", action="store_true")
    e.add_argument("--since")
    e.add_argument("--until")
    s = sub.add_parser("summary", help="a short digest of recent snapshots")
    s.add_argument("--hours", type=float, default=24.0)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "snapshot":
        return cmd_snapshot(args)
    if args.cmd == "daily":
        return cmd_daily(args)
    if args.cmd == "copy":
        return cmd_copy(args)
    if args.cmd == "export":
        return cmd_export(args)
    if args.cmd == "summary":
        return cmd_summary(args)
    build_parser().print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
