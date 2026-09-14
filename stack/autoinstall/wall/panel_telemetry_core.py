"""Pure logic core for the panel thermal/CPU telemetry collector (item S).

One responsibility: turn raw counters/sensor reads/presentation payloads into
the values that go on disk, without touching a filesystem, clock or process
table itself -- every function here takes plain data and returns plain data,
so the whole decision surface (CPU delta math, sensor dedup, staleness,
suspend-gap detection, rotation/cap arithmetic, presentation validation) is
exhaustively unit-testable without root, sysfs or a running renderer.

The thin shell that reads sysfs, /proc/stat, the process table and the
presentation snapshot file, and writes JSONL, is `panel-telemetry`; it is the
only thing allowed to decide *when* to call in here.

Schema version: SCHEMA_VERSION below. Bump it, and add a migration note, on
any incompatible field change.
"""

from __future__ import annotations

import time

SCHEMA_VERSION = 1

# CPU field order as they appear on the aggregate "cpu" line of /proc/stat.
# Older kernels may omit the trailing guest/guest_nice fields.
STAT_FIELDS = (
    "user", "nice", "system", "idle", "iowait",
    "irq", "softirq", "steal", "guest", "guest_nice",
)

DISPLAY_MODES = ("FULL", "TABBED")
FULLSCREEN_OWNERS = ("DOOR", "PROJECTM", "VISUALIZER", "MEDIA_SCREENSAVER", "NAGLIGHT")


# ── /proc/stat parsing and CPU delta ─────────────────────────────────────────

def parse_stat_line(line):
    """Parse one '/proc/stat' aggregate 'cpu ...' line into a fields dict.

    Missing trailing fields (older kernels) are treated as 0. Raises
    ValueError if the line does not start with 'cpu ' or has no numeric
    fields at all.
    """
    parts = line.split()
    if not parts or parts[0] != "cpu":
        raise ValueError("not an aggregate 'cpu' line: %r" % (line,))
    values = [int(p) for p in parts[1:]]
    values += [0] * (len(STAT_FIELDS) - len(values))
    return dict(zip(STAT_FIELDS, values[: len(STAT_FIELDS)]))


def cpu_percent(prev, cur):
    """Whole-system CPU percent (0-100) across total capacity from two
    parsed /proc/stat snapshots. Returns None when there is no valid prior
    sample (first sample, or a detected counter reset) -- callers must treat
    None as "unknown", never as 0.
    """
    if prev is None:
        return None
    total_prev = sum(prev.values())
    total_cur = sum(cur.values())
    total_delta = total_cur - total_prev
    if total_delta <= 0:
        # Counter reset (reboot without a restarted collector, or a bogus
        # reading) -- unknown, not zero.
        return None
    idle_delta = (cur["idle"] + cur["iowait"]) - (prev["idle"] + prev["iowait"])
    busy_delta = total_delta - idle_delta
    pct = (busy_delta / total_delta) * 100.0
    return max(0.0, min(100.0, pct))


# ── sensor dedup ─────────────────────────────────────────────────────────────

def build_sensor_record(source, label, deg_c, canonical):
    """One sensor reading. `deg_c` is None when unavailable this sample --
    never coerced to 0."""
    return {
        "source": source,
        "label": label,
        "deg_c": deg_c,
        "canonical": bool(canonical),
    }


# ── suspend-gap detection ────────────────────────────────────────────────────

def detect_suspend_gap(prev_mono_s, cur_mono_s, interval_s, factor=2.0):
    """Return the observed gap in seconds if the monotonic distance between
    two samples is more than `factor` times the configured interval (i.e. we
    were not woken to sample through a suspend), else None. Never used to
    trigger a wake -- purely a post-hoc classification on the next sample
    the collector takes on its own schedule/resume.
    """
    if prev_mono_s is None:
        return None
    gap = cur_mono_s - prev_mono_s
    if gap > interval_s * factor:
        return gap
    return None


# ── presentation snapshot staleness & validation ─────────────────────────────

def is_stale(record_mono_ms, now_mono_ms, threshold_s=15.0):
    """True when a presentation snapshot's monotonic timestamp is older than
    threshold_s relative to now. A missing record (record_mono_ms is None)
    counts as stale."""
    if record_mono_ms is None:
        return True
    age_s = (now_mono_ms - record_mono_ms) / 1000.0
    return age_s > threshold_s


REQUIRED_PRESENTATION_FIELDS = {
    "displayMode": str,
    "selectedTab": (str, type(None)),
    "fullscreenOwner": (str, type(None)),
    "mediaPreference": (str, type(None)),
    "visualizerStatus": (str, type(None)),
    "displayPower": str,
    "musicActive": bool,
    "revision": int,
    "monotonicMs": (int, float),
}


def validate_presentation(payload):
    """Validate a presentation snapshot payload. Returns (ok, errors) where
    errors is a list of human-readable strings. Does not mutate payload.
    This is the host-side gate the plan requires ("Validate the payload
    shape in the host") -- an invalid payload must never be logged as if it
    were a real sample.
    """
    errors = []
    if not isinstance(payload, dict):
        return False, ["payload is not an object"]
    for field, types in REQUIRED_PRESENTATION_FIELDS.items():
        if field not in payload:
            errors.append("missing field: %s" % field)
            continue
        if not isinstance(payload[field], types):
            errors.append("field %s has wrong type: %r" % (field, type(payload[field]).__name__))
    if "displayMode" in payload and payload["displayMode"] not in DISPLAY_MODES:
        errors.append("displayMode not one of %s: %r" % (DISPLAY_MODES, payload.get("displayMode")))
    owner = payload.get("fullscreenOwner")
    if owner is not None and owner not in FULLSCREEN_OWNERS:
        errors.append("fullscreenOwner not one of %s: %r" % (FULLSCREEN_OWNERS, owner))
    return (len(errors) == 0), errors


# ── retention / rotation arithmetic ──────────────────────────────────────────

def files_to_delete_for_retention(files, now_epoch_s, retention_days=7):
    """files: iterable of (name, mtime_epoch_s). Returns the subset of names
    older than retention_days. Pure arithmetic; caller does the unlink."""
    cutoff = now_epoch_s - retention_days * 86400
    return [name for name, mtime in files if mtime < cutoff]


def files_to_delete_for_cap(files, cap_bytes=50 * 1024 * 1024):
    """files: iterable of (name, mtime_epoch_s, size_bytes), any order.
    Deletes oldest-first until total size <= cap_bytes. Returns the list of
    names to delete, oldest first."""
    ordered = sorted(files, key=lambda f: f[1])
    total = sum(f[2] for f in ordered)
    to_delete = []
    i = 0
    while total > cap_bytes and i < len(ordered):
        name, _mtime, size = ordered[i]
        to_delete.append(name)
        total -= size
        i += 1
    return to_delete


# ── record assembly ──────────────────────────────────────────────────────────

def build_record(
    ts_utc,
    mono_s,
    boot_id,
    session_id,
    cpu_pct,
    sensors,
    gpu,
    cooling,
    cpufreq_mhz,
    kiosk,
    presentation,
    event=None,
):
    return {
        "v": SCHEMA_VERSION,
        "ts_utc": ts_utc,
        "mono_s": mono_s,
        "boot_id": boot_id,
        "session_id": session_id,
        "units": {"deg_c": "celsius", "cpu_pct": "percent_0_100", "rss": "kilobytes"},
        "cpu_pct": cpu_pct,
        "sensors": sensors,
        "gpu": gpu,
        "cooling": cooling,
        "cpufreq_mhz": cpufreq_mhz,
        "kiosk": kiosk,
        "presentation": presentation,
        "event": event,
    }


def now_utc_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
