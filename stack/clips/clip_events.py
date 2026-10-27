#!/usr/bin/env python3
"""clip_events.py - the camera motion-event contract (IF-076, SR-128).

ONE RECORD, ONE STORE, ANY SOURCE. Every event source - the hub's own detector
(`hub-motion`, stack/camera-motion), and later a camera's own ONVIF events
(`onvif`) or the panel's spool (`panel`) - writes the same record into the same
store, and the clip cutter (homehub_clips.py) reads only the store. A source
knows nothing of clips; the cutter knows nothing of how a source detects. A new
camera, or a camera that detects motion itself, is a new source writing here,
not a change to the cutter.

THE RECORD (one compact JSON object per line, at most MAX_LINE bytes):

    v        1
    camera   [a-z][a-z0-9]{0,15}      the camera (CAMERAS in the stack .env)
    source   hub-motion | onvif | panel
    id       [A-Za-z0-9:._-]{1,64}    unique within its source, so a record read
                                      twice (or pulled twice) is the same record
    cause    [a-z][a-z-]{0,23}        what the source saw. The hub detector:
                                      approach, road-stop (new events); visitor,
                                      bridge, road-hold (renewals) - WSN-107
    kind     event | renewal | end    a new sighting; still there; gone
    utc_ms   integer milliseconds since the epoch, the source's wall clock

Never an image, never a zone, never a credential.

THE STORE: <state>/events/<camera>/<source>-YYYY-MM-DD.jsonl, keyed by the UTC
day of utc_ms. Appended, never rewritten; whole day files older than
CLIPS_EVENT_DAYS are removed by `homehub_clips.py retention`. A torn line (a
full disk) is skipped by the reader, never fatal.

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: IF-076, SR-128, LLR-1356.
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "monitoring"))
import monitor_common as mc  # noqa: E402

VERSION = 1
STATE_DIR = "/var/lib/homehub-clips"
SOURCES = ("hub-motion", "onvif", "panel")
KINDS = ("event", "renewal", "end")
CAMERA_RE = re.compile(r"[a-z][a-z0-9]{0,15}")
ID_RE = re.compile(r"[A-Za-z0-9:._-]{1,64}")
CAUSE_RE = re.compile(r"[a-z][a-z-]{0,23}")
MAX_LINE = 512
DAY_FILE_CAP = 8 * 1024 * 1024  # a day of renewals is ~1 MiB at the worst
DAY_RE = re.compile(r"(?P<source>[a-z][a-z-]{0,23})-(?P<day>\d{4}-\d{2}-\d{2})\.jsonl$")
# 2000-01-01 .. 2100-01-01: a clock that says otherwise is not a time to cut by.
UTC_MS_MIN, UTC_MS_MAX = 946684800000, 4102444800000


def invalid(record: object) -> Optional[str]:
    """Why `record` is not an IF-076 record, or None when it is.

    Contract:
      Inputs:  any decoded JSON value.
      Outputs: None, or one short reason naming the field (never its value,
               which may be anything a broken source wrote).
    Implements: IF-076, LLR-1356.
    """
    if not isinstance(record, dict):
        return "not an object"
    if record.get("v") != VERSION or isinstance(record.get("v"), bool):
        return "v is not %d" % VERSION
    for key, pattern in (("camera", CAMERA_RE), ("id", ID_RE), ("cause", CAUSE_RE)):
        value = record.get(key)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            return "%s is malformed" % key
    if record.get("source") not in SOURCES:
        return "source is not one of %s" % ", ".join(SOURCES)
    if record.get("kind") not in KINDS:
        return "kind is not one of %s" % ", ".join(KINDS)
    utc = record.get("utc_ms")
    if isinstance(utc, bool) or not isinstance(utc, int) or not UTC_MS_MIN <= utc <= UTC_MS_MAX:
        return "utc_ms is not an integer time"
    extra = set(record) - {"v", "camera", "source", "id", "cause", "kind", "utc_ms"}
    if extra:
        return "unexpected field(s): %s" % ", ".join(sorted(extra))
    return None


def make(camera: str, source: str, ident: str, cause: str, kind: str, utc_ms: int) -> Dict[str, object]:
    """One record; raises ValueError naming the field when it would be invalid."""
    record = {"v": VERSION, "camera": camera, "source": source, "id": ident,
              "cause": cause, "kind": kind, "utc_ms": int(utc_ms)}
    why = invalid(record)
    if why:
        raise ValueError(why)
    return record


def day_path(state_dir: str, camera: str, source: str, utc_ms: int) -> str:
    """The store file a record with this camera, source and time belongs in."""
    day = time.strftime("%Y-%m-%d", time.gmtime(utc_ms / 1000.0))
    return os.path.join(state_dir, "events", camera, "%s-%s.jsonl" % (source, day))


def append(state_dir: str, record: Dict[str, object]) -> Tuple[bool, str]:
    """Append one valid record to its day file. (ok, why); never raises."""
    why = invalid(record)
    if why:
        return False, "refused an invalid record: " + why
    path = day_path(state_dir, str(record["camera"]), str(record["source"]), int(record["utc_ms"]))
    return mc.append_jsonl(path, record, DAY_FILE_CAP)


def read(state_dir: str, camera: str, sources: Sequence[str], since_ms: int,
         until_ms: Optional[int] = None) -> Tuple[List[Dict[str, object]], int]:
    """Every valid record of `camera` from `sources` with since_ms <= utc_ms
    (<= until_ms), oldest first, each (source, id) once. Returns (records,
    rejected): `rejected` counts lines that parsed as JSON but are not IF-076
    records, or name another camera; torn lines are not counted (iter_jsonl
    skips them). Implements: IF-076, LLR-1356."""
    directory = os.path.join(state_dir, "events", camera)
    lo = time.strftime("%Y-%m-%d", time.gmtime(since_ms / 1000.0))
    hi = time.strftime("%Y-%m-%d", time.gmtime(until_ms / 1000.0)) if until_ms is not None else "9999-99-99"
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return [], 0
    seen = set()
    out: List[Dict[str, object]] = []
    rejected = 0
    for name in names:
        m = DAY_RE.match(name)
        if not m or m.group("source") not in sources or not lo <= m.group("day") <= hi:
            continue
        for record in mc.iter_jsonl(os.path.join(directory, name)):
            if invalid(record) or record.get("camera") != camera or record.get("source") != m.group("source"):
                rejected += 1
                continue
            utc = int(record["utc_ms"])
            if utc < since_ms or (until_ms is not None and utc > until_ms):
                continue
            key = (record["source"], record["id"])
            if key in seen:
                continue
            seen.add(key)
            out.append(record)
    out.sort(key=lambda r: (int(r["utc_ms"]), str(r["source"]), str(r["id"])))
    return out, rejected


def prune_days(state_dir: str, cameras: Iterable[str], keep_days: int, now: float) -> List[str]:
    """Remove whole day files older than keep_days (UTC). Returns what went."""
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(now - keep_days * 86400))
    removed = []
    for camera in cameras:
        directory = os.path.join(state_dir, "events", camera)
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            m = DAY_RE.match(name)
            if m and m.group("day") < cutoff:
                try:
                    os.unlink(os.path.join(directory, name))
                    removed.append(os.path.join(directory, name))
                except OSError:
                    pass
    return removed
