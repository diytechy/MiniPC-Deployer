#!/usr/bin/env python3
"""homehub_clips.py - motion clips cut from the rolling camera recording (SR-129..SR-131).

WHAT A CLIP IS (the Owner, 2026-10-26, HomeHub open-items A78): from 3 s before
the first event to the end of the panel's own display window - 5 s after the
last sighting, or a 30 s hold for a stopped vehicle (the road-stop cause,
WSN-107). The detector's renewals carry that window (camera_motion.py
SignalSpooler), so an EPISODE here is: an event whose cause starts a clip
(CAMERA_<C>_CLIP_CAUSES), then every record that arrives while the window is
still open (each record keeps it open CAMERA_<C>_CLIP_LEASE_SEC more), capped at
CAMERA_<C>_CLIP_MAX_SEC (a longer one continues as a second clip). No separate
post-roll or merge numbers.

HOW IT IS CUT. Copy-only, from the recorder's rolling MPEG-TS segments
(<segment dir>/YYYY-MM-DD/<cam>-YYYYmmddTHHMMSSZ.ts, UTC, ~27 h kept), by event
time. Each segment's frames are indexed here (the recorder's own TS parser): a
frame's wall time is the segment's start (its name) plus its PTS offset, or,
when the camera sent broken PTS (measured 2026-10-26: a whole 10-minute segment
spanning 0.07 s of PTS), its index at the stream's nominal rate. The cut
starts on the last keyframe at or before the window start (so up to one GOP,
1 s, of extra pre-roll) and ends before the first frame at or after the window
end; the bytes go to ffmpeg's stdin in the relay's pinned image and come out as
MP4 with +faststart, written as `.mp4.part` and renamed. An open `.ts.part` is
read only up to its last frame start, never into the frame being written.
Timestamps are kept, except when they cannot be trusted (index timing, or a
recorder restart inside the window): then they are regenerated at the nominal
rate and the sidecar says so. The result is verified (ffprobe counts exactly
the frames that were cut) before it is published.

THE NAME AND THE RECORD (IF-077). <root>/<cam>/YYYY-MM-DD/
<cam>_YYYY-MM-DD_HH-MM-SS±hhmm_<cause>_<N>s.mp4, local time with the offset (the
time of the first event), plus a same-stem .json sidecar (every merged record,
the requested and actual windows, the source segments, timing, coverage,
gaps, bytes, sha256, the cutter's version, the encode result) and one line per
clip in <root>/<cam>/index.jsonl.

THE ENCODE (SR-130). Clips are re-encoded to compress them, on the UHD 600
(h264_vaapi or hevc_vaapi), in a LOW-PRIORITY BATCH of its own (the encode
timer; the container at --cpu-shares 2), never inline with the cut. The
encoded file replaces the copy only after it is verified (same frame count) and
only when it is at most CLIPS_ENCODE_KEEP_PCT of the copy's size; otherwise,
and on any failure, the copy stays. OFF until measured (CLIPS_ENCODE_ENABLED).
Measured 2026-10-27 on the hub: a 30 s daytime window, copy 2.31 MB; VBR
300 kb/s 1.09 MB; constant QP 30 was LARGER than the copy - the camera's own
stream is already lean in daylight, hence a bitrate target and the keep rule.

RETENTION (SR-131): CLIPS_RETENTION_DAYS (365), capped at CLIPS_CAP_MIB
(95367 MiB = 100 GB) over all cameras, and while the clips quota project is
past CLIPS_QUOTA_HIGH_PCT of its limit: oldest clips first. Hourly.

WHERE (the Owner, 2026-10-26: the media drive is not bought yet, C111):
CLIPS_ROOT=/srv/scratch/clips, the scratch volume's `clips` quota project
(homehub_scratch.py, 5 GiB on the 22 GiB volume) behind the scratch guard. /srv/media/Clips is the
other accepted value, for when the drive exists; it has a generic guard
(a separate ext4 mount with project quotas, writable) until the media build
adds the drive's identity check. A write that hits the quota stops the cutter
(exit 75, an alert, no retry loop) until retention has made room.

Verbs: cut (timer, 60 s), encode (timer, 15 min), retention (timer, hourly),
status (JSON), episodes --camera C (what cut would do, read-only).

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: SR-129, SR-130, SR-131, IF-077, LLR-1363..LLR-1378.
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
for _sub in ("monitoring", "scratch", "door"):
    sys.path.insert(0, os.path.join(HERE, "..", _sub))
sys.path.insert(0, HERE)
import monitor_common as mc  # noqa: E402
import homehub_scratch as scratch  # noqa: E402
import door_relay  # noqa: E402
import door_recorder as rec  # noqa: E402
import clip_events as ev  # noqa: E402

CUTTER_VERSION = "homehub_clips/1"
TS = rec.TS
PTS_WRAP = 1 << 33
EXIT_OK, EXIT_QUOTA, EXIT_GUARD = 0, 75, 76
MIB = 1024 * 1024
SCRATCH_ROOT = scratch.MOUNTPOINT + "/clips"   # hub paths are POSIX wherever this is imported
MEDIA_MOUNT = "/srv/media"
MEDIA_ROOT = MEDIA_MOUNT + "/Clips"
SETTLE_MS = 15000          # after the window closes: the detector's last renewal is written ~lease after it
COVER_WAIT_MS = 180000     # how long to wait for the recording to reach the window's end
LOOKBACK_MS = 30 * 3600 * 1000
LEDGER_KEEP_MS = 3 * 86400 * 1000
MAX_SIDECAR_RECORDS = 200
GAP_SEC = 2.0
MAX_SNAP_SEC = 5.0         # how far back the cut may start for a keyframe
STALE_PART_SEC = 3600
LOW_CPUS = "0.4"           # the batch containers' ceiling (the detector's decoder has the same)
MIN_INDEX_FPS = 0.5        # the slowest rate an index-timed segment anchored by its last write is given
MAX_WAITS = 10             # passes an episode may wait on a listed segment that cannot be opened
# How a person runs a verb that writes the clips area by hand: as the units do,
# without CAP_SYS_RESOURCE (the quota is no wall for a process that holds it,
# so the guard refuses one), at the lowest priority.
MANUAL_RUN = ("sudo systemd-run --wait --pipe --quiet -p CapabilityBoundingSet=~CAP_SYS_RESOURCE "
              "-p NoNewPrivileges=yes -p Nice=19 -p IOSchedulingClass=idle -p CPUQuota=25% "
              "/usr/bin/python3 /opt/homehub/stack/clips/homehub_clips.py")
CUT_TIMEOUT_SEC = 300
ENCODE_LOOKBACK_DAYS = 7   # the encode batch looks at this many days of clips
ENCODE_RUN_SEC = 600       # and stops starting new encodes after this long
MAX_ATTEMPTS = 3
QUOTA_WORDS = ("Disk quota exceeded", "No space left on device")


class Invalid(ValueError):
    """A knob is malformed; the message names the KEY only."""


# ── settings ─────────────────────────────────────────────────────────────────


class CameraSettings:
    """One camera's clip policy.

    Contract:
      Config:  per camera C: CAMERA_<C>_SOURCES (hub-motion), _SEGMENT_DIR
               (/srv/scratch/<c>), _FPS (10), _CLIP_PRE_SEC (3), _CLIP_LEASE_SEC
               (5), _CLIP_MAX_SEC (600), _CLIP_CAUSES ("approach road-stop").
               [stack/.env]
    Implements: LLR-1363.
    """

    def __init__(self, env: Dict[str, str], camera: str):
        if not ev.CAMERA_RE.fullmatch(camera):
            raise Invalid("CAMERAS holds a name that is not [a-z][a-z0-9]{0,15}")
        self.camera = camera
        k = lambda s: "CAMERA_%s_%s" % (camera.upper(), s)  # noqa: E731
        self.sources = mc.knob_words(env, k("SOURCES"), "hub-motion")
        bad = [s for s in self.sources if s not in ev.SOURCES]
        if bad or not self.sources:
            raise Invalid("%s must name one or more of %s" % (k("SOURCES"), " ".join(ev.SOURCES)))
        self.segment_dir = env.get(k("SEGMENT_DIR"), "").strip() or scratch.MOUNTPOINT + "/" + camera
        absolute = self.segment_dir.startswith("/") or os.path.isabs(self.segment_dir)
        if not absolute or ".." in re.split(r"[\\/]", self.segment_dir):
            raise Invalid("%s must be an absolute path" % k("SEGMENT_DIR"))
        self.fps = mc.knob_int(env, k("FPS"), 10, 1, 60)
        self.pre_ms = mc.knob_int(env, k("CLIP_PRE_SEC"), 3, 0, 60) * 1000
        self.lease_ms = mc.knob_int(env, k("CLIP_LEASE_SEC"), 5, 1, 120) * 1000
        self.max_ms = mc.knob_int(env, k("CLIP_MAX_SEC"), 600, 30, 3600) * 1000
        if self.lease_ms >= self.max_ms:
            raise Invalid("%s must be shorter than %s" % (k("CLIP_LEASE_SEC"), k("CLIP_MAX_SEC")))
        self.causes = mc.knob_words(env, k("CLIP_CAUSES"), "approach road-stop")
        if not self.causes or not all(ev.CAUSE_RE.fullmatch(c) for c in self.causes):
            raise Invalid("%s must be one or more causes" % k("CLIP_CAUSES"))


class ClipsSettings:
    """The pipeline's knobs.

    Contract:
      Config:  CAMERAS (door), CLIPS_ENABLED (false), CLIPS_ROOT
               (/srv/scratch/clips | /srv/media/Clips), CLIPS_RETENTION_DAYS
               (365), CLIPS_CAP_MIB (95367 = 100 GB), CLIPS_QUOTA_HIGH_PCT (90),
               CLIPS_EVENT_DAYS (14), CLIPS_ENCODE_ENABLED (false),
               CLIPS_ENCODE_CODEC (h264|hevc), CLIPS_ENCODE_KBPS (300),
               CLIPS_ENCODE_KEEP_PCT (80), CLIPS_ENCODE_MAX_PER_RUN (20),
               CAMERA_VAAPI_DEVICE (/dev/dri/renderD128).  [stack/.env]
    Implements: LLR-1363.
    """

    def __init__(self, env: Dict[str, str]):
        self.env = env
        self.enabled = mc.knob_bool(env, "CLIPS_ENABLED", False)
        self.root = (env.get("CLIPS_ROOT", "").strip() or SCRATCH_ROOT).rstrip("/")
        if self.root not in (SCRATCH_ROOT, MEDIA_ROOT):
            raise Invalid("CLIPS_ROOT must be %s (now) or %s (the media drive)" % (SCRATCH_ROOT, MEDIA_ROOT))
        self.retention_days = mc.knob_int(env, "CLIPS_RETENTION_DAYS", 365, 1, 3650)
        self.cap_mib = mc.knob_int(env, "CLIPS_CAP_MIB", 95367, 64, 1 << 24)
        self.quota_high_pct = mc.knob_int(env, "CLIPS_QUOTA_HIGH_PCT", 90, 50, 99)
        self.event_days = mc.knob_int(env, "CLIPS_EVENT_DAYS", 14, 2, 365)
        self.encode = mc.knob_bool(env, "CLIPS_ENCODE_ENABLED", False)
        self.codec = env.get("CLIPS_ENCODE_CODEC", "").strip() or "h264"
        if self.codec not in ("h264", "hevc"):
            raise Invalid("CLIPS_ENCODE_CODEC must be h264 or hevc")
        self.kbps = mc.knob_int(env, "CLIPS_ENCODE_KBPS", 300, 50, 20000)
        self.keep_pct = mc.knob_int(env, "CLIPS_ENCODE_KEEP_PCT", 80, 10, 100)
        self.max_per_run = mc.knob_int(env, "CLIPS_ENCODE_MAX_PER_RUN", 20, 1, 1000)
        # 0 = unchanged: the native raster and the camera's own frame rate. Set
        # only after `encode-trial` showed what a smaller size or rate costs.
        self.encode_size = mc.knob_int(env, "CLIPS_ENCODE_SIZE", 0, 0, 4096)
        self.encode_fps = mc.knob_int(env, "CLIPS_ENCODE_FPS", 0, 0, 60)
        if self.encode_size and (self.encode_size < 256 or self.encode_size % 2):
            raise Invalid("CLIPS_ENCODE_SIZE must be 0 (native) or an even number of pixels, at least 256")
        self.device = env.get("CAMERA_VAAPI_DEVICE", "").strip() or "/dev/dri/renderD128"
        if not re.fullmatch(r"/dev/dri/renderD[0-9]{3}", self.device):
            raise Invalid("CAMERA_VAAPI_DEVICE must be /dev/dri/renderD<NNN>")
        names = mc.knob_words(env, "CAMERAS", "door")
        self.cameras = [CameraSettings(env, name) for name in names]


# ── segments and their frames ────────────────────────────────────────────────


class Segment:
    __slots__ = ("path", "name", "start", "part")

    def __init__(self, path: str, name: str, start: float, part: bool):
        self.path, self.name, self.start, self.part = path, name, start, part

    def __repr__(self) -> str:
        return "Segment(%r)" % self.name


def segment_re(camera: str):
    return re.compile(r"%s-(\d{8}T\d{6})Z(?:-\d+)?\.ts(\.part)?$" % re.escape(camera))


def list_segments(seg_dir: str, camera: str) -> List[Segment]:
    """Every segment (finished and the open .part) of a camera, oldest first.
    The naming contract is the recorder's: <dir>/YYYY-MM-DD/<cam>-<UTC>Z.ts."""
    pattern = segment_re(camera)
    out = []
    try:
        days = sorted(d for d in os.listdir(seg_dir) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))
    except OSError:
        return out
    for day in days:
        try:
            names = os.listdir(os.path.join(seg_dir, day))
        except OSError:
            continue
        for name in names:
            m = pattern.match(name)
            if not m:
                continue
            try:
                start = float(calendar.timegm(time.strptime(m.group(1), "%Y%m%dT%H%M%S")))
            except (ValueError, OverflowError):
                continue  # a name that is not a time is not a segment
            out.append(Segment(os.path.join(seg_dir, day, name), name, start, bool(m.group(2))))
    out.sort(key=lambda s: (s.start, s.name))
    return out


class FrameIndex:
    """Where every whole video frame of one segment starts, and its PTS.

    offsets[i] is the byte offset of the TS packet that starts frame i's PES;
    keys[i] whether it is a keyframe (random_access_indicator); pts[i] its PTS
    or None. `end` is where the last whole frame ends: the file's size for a
    finished segment, the last frame start for an open .part (the frame being
    written is dropped). `header` is the file's first PAT and PMT packets, to
    put in front of a cut that starts mid-file. Implements: LLR-1364."""

    def __init__(self):
        self.offsets: List[int] = []
        self.keys: List[bool] = []
        self.pts: List[Optional[int]] = []
        self.header = b""
        self.end = 0


def scan_frames(data: bytes, part: bool) -> FrameIndex:
    """Index the video frames in a segment's bytes (pure)."""
    idx = FrameIndex()
    n = len(data)
    pmt_pid = video_pid = None
    pat = pmt = b""
    i = 0
    packets_end = 0  # where the last whole packet ends (alignment may restart mid-file)
    while i + TS <= n:
        if data[i] != rec.SYNC:
            i += 1  # a torn file: search for the next packet start
            continue
        b1 = data[i + 1]
        if b1 & 0x40:  # payload unit start
            pid = ((b1 & 0x1F) << 8) | data[i + 2]
            if pid == video_pid:
                pkt = data[i:i + TS]
                idx.offsets.append(i)
                idx.keys.append(rec.packet_fields(pkt)[2])
                idx.pts.append(rec.pes_pts(pkt))
            elif pid == 0 and not pat:
                found = rec.parse_pat(data[i:i + TS])
                if found is not None:
                    pmt_pid, pat = found, bytes(data[i:i + TS])
            elif pmt_pid is not None and pid == pmt_pid and video_pid is None:
                found = rec.parse_pmt(data[i:i + TS])
                if found is not None:
                    video_pid, pmt = found, bytes(data[i:i + TS])
        i += TS
        packets_end = i
    idx.header = pat + pmt if pat and pmt else b""
    if part and idx.offsets:
        idx.end = idx.offsets.pop()
        idx.keys.pop()
        idx.pts.pop()
    else:
        idx.end = packets_end
    return idx


def frame_times(idx: FrameIndex, start: float, fps: int,
                end: Optional[float] = None, end_is_last_write: bool = False) -> Tuple[List[float], str]:
    """Wall time of each frame: the segment's start plus its PTS offset, or,
    when the PTS cannot be believed, its index spread evenly from this
    segment's start to `end`, where its frames really end (the camera drops its
    frame rate in low light and infrared, so the nominal rate would drift by
    seconds over ten minutes). The caller passes, for a finished segment, the
    earlier of the NEXT segment's start and the file's last write (a short
    recorder outage after it must not stretch it), and for the open .part its
    last write. When the anchor is the next segment's start, a rate under half
    the nominal may hide a recorder restart between them, so the nominal rate
    is used then; when it is the file's own last write it cannot hide one, so the
    measured period is trusted down to MIN_INDEX_FPS (night and infrared go far
    below half) and clamped there. Above the nominal rate (a clock step), the
    nominal is used. PTS is
    believed when every frame has one, every step is forward and under 60 s
    (a hole the recorder rode out without ending the segment), and the whole
    span is at least half what the frame count implies and at most twice that
    plus 60 s.
    Returns (times, "pts"|"index"). Implements: LLR-1365."""
    n = len(idx.offsets)
    if n == 0:
        return [], "none"
    pts = idx.pts
    if all(p is not None for p in pts):
        steps = [((b - a) % PTS_WRAP) for a, b in zip(pts, pts[1:])]  # type: ignore[operator]
        span = sum(steps) / 90000.0
        expected = (n - 1) / float(fps)
        if n == 1 or (all(0 < s < 60 * 90000 for s in steps) and 0.5 * expected <= span <= 2.0 * expected + 60):
            base = pts[0]
            return [start + ((p - base) % PTS_WRAP) / 90000.0 for p in pts], "pts"  # type: ignore[operator]
    period = 1.0 / fps
    if end is not None and n > 1:
        measured = (end - start) / float(n)
        if end_is_last_write and measured >= 0.95 / fps:
            period = min(measured, 1.0 / MIN_INDEX_FPS)
        elif 0.95 / fps <= measured <= 2.0 / fps:
            period = measured
    return [start + i * period for i in range(n)], "index"


class Loaded:
    """One segment, read once into memory and indexed, so a prune or a rename
    by the recorder cannot pull the bytes from under a cut."""

    def __init__(self, seg: Segment, data: bytes, fps: int, next_start: Optional[float] = None,
                 mtime: Optional[float] = None):
        self.seg = seg
        self.data = data
        self.idx = scan_frames(data, seg.part)
        if seg.part:
            end, last_write = mtime, mtime is not None   # the open segment: its frames end at its last write
        elif mtime is not None and (next_start is None or mtime <= next_start):
            end, last_write = mtime, True
        else:
            end, last_write = next_start, False
        self.times, self.timing = frame_times(self.idx, seg.start, fps, end, last_write)
        n = len(self.times)
        # The segment's own frame period: what its frames were actually spaced at.
        self.period = (self.times[-1] - self.times[0]) / (n - 1) if n > 1 and self.times[-1] > self.times[0] \
            else 1.0 / fps

    def span(self, fps: int) -> Tuple[float, float]:
        if not self.times:
            return self.seg.start, self.seg.start
        return self.times[0], self.times[-1] + 1.0 / fps


def load_segment(seg: Segment, fps: int, cache: Dict[tuple, "Loaded"],
                 next_start: Optional[float] = None) -> Optional[Loaded]:
    """The segment read and indexed, or None when it cannot be opened (the
    recorder renamed or pruned it since it was listed)."""
    try:
        with open(seg.path, "rb") as handle:
            st = os.fstat(handle.fileno())
            size = st.st_size
            key = (seg.path, size, next_start)
            if key in cache:
                return cache[key]
            data = handle.read(size)
    except OSError:
        return None
    loaded = Loaded(seg, data, fps, next_start, st.st_mtime)
    cache[key] = loaded
    return loaded


# ── episodes ─────────────────────────────────────────────────────────────────


class Episode:
    """One clip's worth of records (see the module doc)."""

    def __init__(self, first: Dict[str, object], pre_ms: int, lease_ms: int, cause: Optional[str] = None,
                 start_ms: Optional[int] = None):
        self.id = "%s:%s" % (first["source"], first["id"])
        self.cause = cause or str(first["cause"])
        self.event_ms = int(first["utc_ms"])
        self.start_ms = start_ms if start_ms is not None else self.event_ms
        self.end_ms = int(first["utc_ms"]) + lease_ms
        self.pre_ms = pre_ms
        self.records = [first]

    @property
    def window(self) -> Tuple[int, int]:
        return self.start_ms - self.pre_ms, self.end_ms


def build_episodes(records: Sequence[Dict[str, object]], causes: Sequence[str], pre_ms: int, lease_ms: int,
                   max_ms: int) -> Tuple[List[Episode], int]:
    """Group records (oldest first) into episodes (pure).

    A record whose time is inside the open episode's window (at or before its
    end) extends it to that time + lease. Otherwise only an `event` whose cause
    is in `causes` opens a new one; anything else is an orphan (a renewal after
    the window closed, an event of a cause that does not make clips) and is
    counted. An episode longer than max_ms is closed at start + max_ms and the
    record that crossed it opens a `continued` episode with no pre-roll.
    Returns (episodes, orphans). Implements: LLR-1366."""
    out: List[Episode] = []
    cur: Optional[Episode] = None
    orphans = 0
    for r in records:
        t = int(r["utc_ms"])
        if cur is not None and t <= cur.end_ms:
            if t - cur.start_ms >= max_ms:
                cur.end_ms = cur.start_ms + max_ms
                nxt = Episode(r, 0, lease_ms, cause="continued", start_ms=cur.end_ms)
                nxt.end_ms = max(nxt.end_ms, cur.end_ms)
                out.append(nxt)
                cur = nxt
                continue
            cur.records.append(r)
            cur.end_ms = max(cur.end_ms, t + lease_ms)
            continue
        if r["kind"] == "event" and r["cause"] in causes:
            cur = Episode(r, pre_ms, lease_ms)
            out.append(cur)
        else:
            orphans += 1
    return out, orphans


# ── planning a cut ───────────────────────────────────────────────────────────


class Plan:
    """What to cut for one window: byte ranges per segment, and what they cover."""

    def __init__(self):
        self.pieces: List[Tuple[Loaded, int, int, bool]] = []  # (segment, from byte, to byte, header first)
        self.frames = 0
        self.actual: Tuple[float, float] = (0.0, 0.0)
        self.timing = "pts"
        self.regenerate = False
        self.end_covered = False
        self.start_covered = False
        self.gaps: List[Dict[str, object]] = []
        self.rate = 0.0  # frames per second the chosen frames were recorded at

    @property
    def coverage(self) -> str:
        return "full" if self.start_covered and self.end_covered and not self.gaps else "partial"


def plan_window(loaded: Sequence[Loaded], ws: float, we: float, fps: int) -> Optional[Plan]:
    """The cut for [ws, we) over indexed segments (oldest first) - pure.

    Start: the last keyframe at or before ws, at most MAX_SNAP_SEC back (a GOP
    is 1 s); if there is none - the recording begins later, or resumes after a
    gap - the first keyframe inside the window (start not covered). End:
    before the first frame at or after we (end covered), else after the last
    frame on disk. None when no frame of the window is on disk.
    Implements: LLR-1367."""
    flat: List[Tuple[int, int, float]] = []  # (loaded index, frame index, time)
    for li, seg in enumerate(loaded):
        for fi, t in enumerate(seg.times):
            flat.append((li, fi, t))
    inside = next((pos for pos, (_li, _fi, t) in enumerate(flat) if ws <= t < we), None)
    if inside is None:
        return None
    first = None
    for pos in range(inside, -1, -1):
        li, fi, t = flat[pos]
        if t < ws - MAX_SNAP_SEC:
            break
        if loaded[li].idx.keys[fi] and t <= ws:
            first = pos
            break
    plan = Plan()
    plan.start_covered = first is not None
    if first is None:
        first = next((pos for pos in range(inside, len(flat))
                      if loaded[flat[pos][0]].idx.keys[flat[pos][1]] and flat[pos][2] < we), None)
        if first is None:
            return None
    last = None
    for pos in range(first, len(flat)):
        if flat[pos][2] >= we:
            plan.end_covered = True
            break
        last = pos
    if last is None:
        return None
    fps_period = 1.0 / fps
    plan.frames = last - first + 1
    plan.actual = (flat[first][2], flat[last][2] + loaded[flat[last][0]].period)
    if plan.end_covered and flat[last + 1][2] - plan.actual[1] > GAP_SEC and we - plan.actual[1] > fps_period:
        # The recording passes the window's end only after a hole (a recorder
        # restart): waiting cannot fill it, and the clip must not claim it.
        plan.gaps.append({"after_utc_ms": int(plan.actual[1] * 1000),
                          "seconds": round(we - plan.actual[1], 1)})
    # Group the chosen frames by segment into byte ranges.
    recorded = 0.0
    pos = first
    previous: Optional[Loaded] = None
    while pos <= last:
        li, fi, _t = flat[pos]
        seg = loaded[li]
        end_pos = pos
        while end_pos + 1 <= last and flat[end_pos + 1][0] == li:
            end_pos += 1
        lfi = flat[end_pos][1]
        # From the segment's first frame: its first byte, with its own PAT/PMT.
        # From mid-file: the keyframe's packet, with the file's PAT/PMT put first.
        byte_from = 0 if fi == 0 else seg.idx.offsets[fi]
        byte_to = seg.idx.offsets[lfi + 1] if lfi + 1 < len(seg.idx.offsets) else seg.idx.end
        plan.pieces.append((seg, byte_from, byte_to, byte_from > 0))
        recorded += seg.times[lfi] - seg.times[fi] + seg.period
        if seg.timing != "pts":
            plan.timing = "index"
            plan.regenerate = True
        if previous is not None:
            prev_end = previous.times[-1] + previous.period
            gap = seg.times[0] - prev_end
            if gap > GAP_SEC:
                plan.gaps.append({"after_utc_ms": int(prev_end * 1000), "seconds": round(gap, 1)})
            # A recorder restart starts a new PTS line: the muxer would see time
            # jump (or run backwards), so the clip's timestamps are regenerated.
            pp, np_ = previous.idx.pts[-1], seg.idx.pts[0] if seg.idx.pts else None
            if pp is None or np_ is None or not 0 < (np_ - pp) % PTS_WRAP <= 90000 * (2 * fps_period + 0.5):
                plan.regenerate = True
        previous = seg
        pos = end_pos + 1
    # The rate the clip's timestamps are regenerated at: what the camera
    # actually delivered over these frames, not the nominal rate.
    plan.rate = plan.frames / recorded if recorded > 0 else float(fps)
    return plan


def readiness(now_ms: int, ep: Episode, plan: Optional[Plan], oldest_start: Optional[float]) -> str:
    """"wait", "ready" or "lost: <why>" for an episode (pure). A window that
    ended before the oldest segment is gone (pruned, or before the recording
    began); one nothing covers 180 s after its end was never recorded.
    Implements: LLR-1368."""
    ws, we = ep.window
    if now_ms < we + SETTLE_MS:
        return "wait"
    if plan is None:
        if oldest_start is not None and we <= oldest_start * 1000:
            return "lost: the recording no longer covers the window"
        if now_ms - we >= COVER_WAIT_MS:
            return "lost: no recording covers the window"
        return "wait"
    if not plan.end_covered and now_ms - we < COVER_WAIT_MS:
        return "wait"
    return "ready"


# ── names ────────────────────────────────────────────────────────────────────


def clip_stem(camera: str, event_ms: int, cause: str, seconds: float,
              localtime: Callable[[float], time.struct_time] = time.localtime) -> Tuple[str, str]:
    """(day folder, file stem): local time WITH its offset, so the name reads
    naturally in Explorer and is unambiguous across a DST change.
    door_2026-10-26_07-03-46-0500_approach_45s. Implements: IF-077, LLR-1369."""
    t = localtime(event_ms / 1000.0)
    return (time.strftime("%Y-%m-%d", t),
            "%s_%s_%s_%ds" % (camera, time.strftime("%Y-%m-%d_%H-%M-%S%z", t), cause, int(round(seconds))))


# ── the guard (before every file) ────────────────────────────────────────────


def media_guard(root: str, paths: Optional[scratch.Paths] = None) -> Tuple[bool, str]:
    """For CLIPS_ROOT=/srv/media/Clips: a separate ext4 mount with project
    quotas, writable, the root a real directory on it, and this process without
    CAP_SYS_RESOURCE. The drive's identity (label, serial) is the media-drive
    build's to add (HomeHub open-items C111). Implements: LLR-1370."""
    paths = paths or scratch.Paths()
    try:
        with open(paths.mountinfo, encoding="utf-8", errors="replace") as handle:
            entry = scratch.mount_of(scratch.parse_mountinfo(handle.read()), MEDIA_MOUNT)
    except OSError:
        entry = None
    if entry is None:
        return False, "%s is not a mount point (the media drive is not mounted)" % MEDIA_MOUNT
    if entry["fstype"] != "ext4" or "prjquota" not in entry["superopts"].split(","):
        return False, "%s is not ext4 with prjquota" % MEDIA_MOUNT
    if "ro" in entry["superopts"].split(",") or "ro" in entry.get("options", "").split(","):
        return False, "%s is mounted read-only" % MEDIA_MOUNT
    if scratch.holds_cap_sys_resource(paths.status):
        return False, "this process holds CAP_SYS_RESOURCE, which ignores quota hard limits"
    try:
        st = os.lstat(root)
    except OSError:
        return False, "%s does not exist" % root
    if not stat.S_ISDIR(st.st_mode):
        return False, "%s is not a directory" % root
    maj, mnr = (int(x) for x in entry["majmin"].split(":"))
    if st.st_dev != os.makedev(maj, mnr):
        return False, "%s is not on the media drive" % root
    return True, "ok"


def guard(settings: ClipsSettings) -> Tuple[bool, str]:
    """The guard for CLIPS_ROOT, run before every file the pipeline writes."""
    if settings.root == SCRATCH_ROOT:
        return scratch.guard("clips", scratch.Settings(settings.env))
    return media_guard(settings.root)


# ── the ledger: what was cut, what was lost ──────────────────────────────────


def ledger_path(state_dir: str, camera: str) -> str:
    return os.path.join(state_dir, "ledger", "%s.json" % camera)


def load_ledger(state_dir: str, camera: str, now_ms: int) -> Dict[str, object]:
    data = mc.load_json(ledger_path(state_dir, camera), {})
    data.setdefault("since_ms", now_ms)
    data.setdefault("episodes", {})
    return data


def ledger_covers(ledger: Dict[str, object], ep: Episode) -> bool:
    """Already handled: the same episode id, or an episode whose CORE (first
    event to end, without the pre-roll) overlaps one cut before - a second
    source's record arriving late must not cut the same visit twice. Pre-rolls
    may overlap: an episode that starts 1 s after the last one closed is a new
    clip whose first 3 s repeat the end of the previous one."""
    eps = ledger["episodes"]  # type: ignore[index]
    if ep.id in eps:
        return True
    return any(e.get("status") == "cut" and int(e.get("event_ms", e.get("start_ms", 0))) < ep.end_ms
               and ep.start_ms < int(e.get("end_ms", 0)) for e in eps.values())  # type: ignore[union-attr]


def prune_ledger(ledger: Dict[str, object], now_ms: int) -> None:
    eps = ledger["episodes"]  # type: ignore[index]
    for key in [k for k, e in eps.items() if now_ms - int(e.get("end_ms", 0)) > LEDGER_KEEP_MS]:  # type: ignore[union-attr]
        del eps[key]  # type: ignore[union-attr]


# ── ffmpeg in the pinned image ───────────────────────────────────────────────


def docker_base(name: str, out_dir: str, read_only_out: bool = False, shares: int = 256,
                device: Optional[str] = None, stdin: bool = False, cpus: str = "1") -> List[str]:
    """A throwaway container of the pinned image: no network, read-only root,
    no capabilities (so no CAP_SYS_RESOURCE: the quota holds for its writes),
    out_dir bound at /out. CPU weight `shares` against docker's default 1024,
    and a ceiling of `cpus` (the batch work - encode, trial, SSIM - LOW_CPUS).
    The container runs in docker's cgroup, so its unit's Nice/CPUQuota never
    reach it: these flags are its priority."""
    argv = ["docker", "run", "--rm"] + (["-i"] if stdin else []) + [
            "--name", name, "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--log-driver", "none",
            "--pids-limit", "64", "--memory", "384m", "--cpus", cpus, "--cpu-shares", str(shares),
            "-v", "%s:/out%s" % (out_dir, ":ro" if read_only_out else "")]
    if device:
        argv += ["--device", device]
    return argv


def cut_command(camera: str, out_dir: str, part_name: str, regenerate_fps: Optional[float]) -> List[str]:
    """ffmpeg reading MPEG-TS on stdin, stream copy to MP4 (+faststart). With
    regenerate_fps the video timestamps are rewritten N/rate, at the rate the
    frames were recorded at (the setts filter: still no re-encode).
    Implements: LLR-1371."""
    argv = docker_base("homehub-clips-cut-%s" % camera, out_dir, stdin=True) + [
        "--entrypoint", "ffmpeg", door_relay.image_ref(), "-hide_banner", "-loglevel", "error",
        "-f", "mpegts", "-i", "pipe:0", "-map", "0:v:0", "-c", "copy", "-an"]
    if regenerate_fps:
        argv += ["-bsf:v", "setts=ts=N/(%s*TB)" % format(round(regenerate_fps, 4), "g")]
    return argv + ["-movflags", "+faststart", "-f", "mp4", "-y", "/out/" + part_name]


def probe_command(camera: str, out_dir: str, name: str) -> List[str]:
    return docker_base("homehub-clips-probe-%s" % camera, out_dir, read_only_out=True) + [
        "--entrypoint", "ffprobe", door_relay.image_ref(), "-v", "error", "-select_streams", "v:0",
        "-count_packets", "-show_entries", "stream=codec_name,nb_read_packets,width,height:format=duration",
        "-of", "json", "/out/" + name]


def vaapi_encode_args(device: str, src: str, codec: str, kbps: int, size: int = 0, fps: int = 0,
                      loglevel: str = "error") -> List[str]:
    """ffmpeg's arguments for one VA-API encode: decode on the iGPU, optionally
    `fps` and `scale_vaapi` there too (square SIZE: the raw fisheye stays a
    circle, never dewarped), VBR around kbps with a peak of twice that. No grey
    or 4:0:0 output: VA-API encodes 4:2:0."""
    vf = []
    if fps:
        vf.append("fps=%d" % fps)
    if size:
        vf.append("scale_vaapi=w=%d:h=%d" % (size, size))
    argv = ["-nostdin", "-hide_banner", "-loglevel", loglevel,
            "-hwaccel", "vaapi", "-hwaccel_device", device, "-hwaccel_output_format", "vaapi",
            "-i", src, "-map", "0:v:0", "-an"]
    if vf:
        argv += ["-vf", ",".join(vf)]
    argv += ["-c:v", "h264_vaapi" if codec == "h264" else "hevc_vaapi", "-rc_mode", "VBR",
             "-b:v", "%dk" % kbps, "-maxrate", "%dk" % (2 * kbps)]
    if codec == "hevc":
        argv += ["-tag:v", "hvc1"]
    return argv


def encode_command(settings: ClipsSettings, camera: str, out_dir: str, src: str, part_name: str) -> List[str]:
    """VA-API encode of one clip, the container at the lowest CPU weight. VBR
    around CLIPS_ENCODE_KBPS (peak twice that), at CLIPS_ENCODE_SIZE and
    CLIPS_ENCODE_FPS when set (0 = unchanged). Implements: LLR-1374."""
    argv = docker_base("homehub-clips-encode-%s" % camera, out_dir, shares=2, device=settings.device, cpus=LOW_CPUS)
    argv += ["--entrypoint", "ffmpeg", door_relay.image_ref()]
    argv += vaapi_encode_args(settings.device, "/out/" + src, settings.codec, settings.kbps,
                              settings.encode_size, settings.encode_fps)
    return argv + ["-movflags", "+faststart", "-f", "mp4", "-y", "/out/" + part_name]


def run_probe(camera: str, out_dir: str, name: str, runner=subprocess.run) -> Optional[Dict[str, object]]:
    """{frames, duration, codec} of a file in out_dir, or None. The named
    container is removed before the probe and after a timeout (killing the
    docker client does not stop it, and a leftover would refuse every later
    probe by name)."""
    container = "homehub-clips-probe-%s" % camera

    def remove():
        docker_remove(container)
    remove()
    try:
        done = runner(probe_command(camera, out_dir, name), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      timeout=300)
    except subprocess.TimeoutExpired:
        remove()
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        data = json.loads(done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else done.stdout)
        stream = (data.get("streams") or [{}])[0]
        return {"frames": int(stream.get("nb_read_packets") or 0), "codec": stream.get("codec_name"),
                "duration": float((data.get("format") or {}).get("duration") or 0.0),
                "width": int(stream.get("width") or 0), "height": int(stream.get("height") or 0)}
    except (ValueError, TypeError, AttributeError):
        return None


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_tail(handle, limit: int = 1 << 16) -> str:
    """The last `limit` bytes of a stderr file: the error that ended ffmpeg is
    at the end, after whatever it said about a damaged input first."""
    handle.seek(0, os.SEEK_END)
    handle.seek(max(0, handle.tell() - limit))
    return handle.read(limit).decode("utf-8", "replace")


def docker_remove(name: str) -> None:
    """`docker rm -f NAME`, best effort: a slow or restarting daemon must not
    turn a cleanup into the failure of the whole pass."""
    try:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def quota_hit(text: str) -> bool:
    return any(word in text for word in QUOTA_WORDS)


class QuotaStop(Exception):
    """The clips area is full: stop, alert, no retry until retention made room."""


class GuardStop(Exception):
    """The guard refused: the clips area is not where it must be."""


# ── the cut ──────────────────────────────────────────────────────────────────


def write_pieces(stdin, plan: Plan) -> None:
    for seg, byte_from, byte_to, header in plan.pieces:
        if header and seg.idx.header:
            stdin.write(seg.idx.header)
        view = memoryview(seg.data)
        for at in range(byte_from, byte_to, 1 << 20):
            stdin.write(view[at:min(byte_to, at + (1 << 20))])


def cut_episode(settings: ClipsSettings, cam: CameraSettings, ep: Episode, plan: Plan, state_dir: str,
                runner=subprocess.Popen, prober=run_probe, localtime=time.localtime) -> Dict[str, object]:
    """Cut one planned episode into <root>/<cam>/<day>/<stem>.mp4 + .json and
    append its index line. Raises QuotaStop / GuardStop; returns the ledger
    entry (status cut | failed). Implements: LLR-1371, LLR-1372."""
    ok, reason = guard(settings)
    if not ok:
        raise GuardStop(reason)
    seconds = plan.actual[1] - plan.actual[0]
    day, stem = clip_stem(cam.camera, ep.event_ms, ep.cause, seconds, localtime)
    cam_dir = os.path.join(settings.root, cam.camera)
    day_dir = os.path.join(cam_dir, day)
    try:
        os.makedirs(day_dir, mode=0o750, exist_ok=True)
    except OSError as exc:
        if exc.errno in (getattr(errno, "EDQUOT", -1), errno.ENOSPC):
            raise QuotaStop(str(exc))
        raise
    n = 1
    base = stem
    while os.path.exists(os.path.join(day_dir, stem + ".mp4")) or os.path.exists(os.path.join(day_dir, stem + ".json")):
        # A pass killed after the rename but before its ledger save left this
        # episode's clip already here: it is recorded, not cut a second time.
        side = mc.load_json(os.path.join(day_dir, stem + ".json"), {})
        if side.get("episode") == ep.id and os.path.exists(os.path.join(day_dir, stem + ".mp4")):
            mc.log("clips [%s]: %s/%s.mp4 already holds %s - recorded, not cut again" % (cam.camera, day, stem, ep.id))
            return {"status": "cut", "clip": "%s/%s.mp4" % (day, stem), "recovered": True}
        n += 1
        stem = "%s-%d" % (base, n)
    part = stem + ".mp4.part"
    argv = cut_command(cam.camera, day_dir, part, (plan.rate or cam.fps) if plan.regenerate else None)
    docker_remove("homehub-clips-cut-%s" % cam.camera)
    # stderr into a file, not a pipe: an ffmpeg complaining at length about a
    # damaged segment must not block on a full pipe while we block feeding it.
    with tempfile.TemporaryFile() as err_file:
        proc = runner(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err_file)
        # A cut that stops reading its input is killed after CUT_TIMEOUT_SEC,
        # feeding included, and its container removed with it.
        watchdog = threading.Timer(CUT_TIMEOUT_SEC, proc.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            try:
                write_pieces(proc.stdin, plan)
            except (BrokenPipeError, OSError, ValueError):
                pass  # ffmpeg died early (or was killed): its exit status and stderr say why
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                proc.stdin = None  # communicate() must not flush a closed pipe
            proc.communicate()
        finally:
            watchdog.cancel()
        if proc.returncode and proc.returncode < 0:
            docker_remove("homehub-clips-cut-%s" % cam.camera)
        err_text = read_tail(err_file)
    part_path = os.path.join(day_dir, part)
    if quota_hit(err_text):
        _remove(part_path)
        raise QuotaStop(err_text.strip().splitlines()[-1] if err_text.strip() else "quota")
    probed = prober(cam.camera, day_dir, part) if proc.returncode == 0 and os.path.exists(part_path) else None
    why = None
    if proc.returncode:
        why = "ffmpeg exited %s: %s" % (proc.returncode, err_text.strip()[-200:])
    elif not probed or probed.get("frames") != plan.frames:
        why = "the cut holds %s frames, not the %d planned" % ((probed or {}).get("frames"), plan.frames)
    elif "duration" in probed and not duration_plausible(float(probed["duration"]), plan, cam.fps):
        why = "the cut lasts %.2f s for %d frames" % (float(probed["duration"]), plan.frames)
    if why:
        _remove(part_path)
        mc.log("clips [%s]: cut FAILED for %s - %s" % (cam.camera, ep.id, why), warning=True)
        return {"status": "failed", "why": why}
    final = os.path.join(day_dir, stem + ".mp4")
    side_path = os.path.join(day_dir, stem + ".json")
    size = os.path.getsize(part_path)
    sidecar = sidecar_record(cam, ep, plan, stem + ".mp4", size, sha256_file(part_path))
    # The guard again before the next files, and the sidecar BEFORE the clip's
    # final name: a clip never appears without its record.
    ok, reason = guard(settings)
    if not ok:
        _remove(part_path)
        raise GuardStop(reason)
    try:
        write_json_atomic(side_path, sidecar)
        os.rename(part_path, final)
    except OSError as exc:
        _remove(part_path)
        _remove(side_path)
        if exc.errno in (getattr(errno, "EDQUOT", -1), errno.ENOSPC):
            raise QuotaStop(str(exc))
        raise
    try:
        index_line = {"v": 1, "clip": "%s/%s.mp4" % (day, stem), "camera": cam.camera, "cause": ep.cause,
                      "event_utc_ms": ep.event_ms, "start_utc_ms": int(plan.actual[0] * 1000),
                      "end_utc_ms": int(plan.actual[1] * 1000), "seconds": round(seconds, 1),
                      "coverage": plan.coverage}
        ok, why = guard(settings)
        if ok:
            ok, why = mc.append_jsonl(os.path.join(cam_dir, "index.jsonl"), index_line, 64 * MIB)
        if not ok:  # the clip and its record exist; retention's index repair is not needed for a missing line
            mc.log("clips [%s]: index not updated: %s" % (cam.camera, why), warning=True)
    except OSError as exc:
        if exc.errno in (getattr(errno, "EDQUOT", -1), errno.ENOSPC):
            raise QuotaStop(str(exc))
        raise
    mc.log("clips [%s]: cut %s/%s.mp4 (%.0f s, %s, %.1f MiB, %s timing%s)"
           % (cam.camera, day, stem, seconds, plan.coverage, size / MIB, plan.timing,
              ", timestamps regenerated" if plan.regenerate else ""))
    return {"status": "cut", "clip": "%s/%s.mp4" % (day, stem)}


def duration_plausible(duration: float, plan: Plan, fps: int) -> bool:
    """The MP4's duration agrees with its frames: within 1 s of frames / rate
    when the timestamps were regenerated, and never under half of it (a cut of
    broken-PTS frames whose timestamps were NOT restored plays in an instant)."""
    expected = plan.frames / float(plan.rate or fps)
    if duration < 0.5 * expected:
        return False
    return not plan.regenerate or abs(duration - expected) <= 1.0


def sidecar_record(cam: CameraSettings, ep: Episode, plan: Plan, clip: str, size: int, sha: str) -> Dict[str, object]:
    """The IF-077 sidecar. Implements: IF-077, LLR-1372."""
    records = ep.records
    ws, we = ep.window
    return {
        "v": 1, "camera": cam.camera, "clip": clip, "cutter": CUTTER_VERSION, "episode": ep.id,
        "cause": ep.cause, "sources": sorted({str(r["source"]) for r in records}),
        "events": [{k: r[k] for k in ("source", "id", "cause", "kind", "utc_ms")} for r in records[:MAX_SIDECAR_RECORDS]],
        "events_truncated": max(0, len(records) - MAX_SIDECAR_RECORDS),
        "requested": {"start_utc_ms": ws, "end_utc_ms": we},
        "actual": {"start_utc_ms": int(plan.actual[0] * 1000), "end_utc_ms": int(plan.actual[1] * 1000)},
        "segments": [seg.seg.name for seg, _a, _b, _h in plan.pieces],
        "timing": plan.timing, "timestamps": "regenerated" if plan.regenerate else "source",
        "coverage": plan.coverage, "gaps": plan.gaps, "frames": plan.frames,
        "bytes": size, "sha256": sha, "encode": None,
    }


def write_json_atomic(path: str, data: Dict[str, object]) -> None:
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, sort_keys=True, indent=1)
        handle.write("\n")
    os.replace(tmp, path)


def _remove(path: str) -> bool:
    """Unlink; True when the path is gone afterwards (it may never have existed)."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


INDEX_DIRTY = ".index-dirty"


def mark_index_dirty(cam_dir: str) -> None:
    try:
        with open(os.path.join(cam_dir, INDEX_DIRTY), "w"):
            pass
    except OSError:
        pass


def cut_camera(settings: ClipsSettings, cam: CameraSettings, state_dir: str, now_ms: int,
               runner=subprocess.Popen, prober=run_probe, alert=None,
               quota_fn: Optional[Callable[[], Tuple[Optional[int], Optional[int]]]] = None) -> Dict[str, int]:
    """One camera's pass: build episodes from the store, cut what is ready,
    record what is lost. The segments are listed again for every episode (a
    pass can run long behind a backlog while the recorder rotates and prunes),
    and a listed segment that can no longer be opened makes the episode WAIT
    for the next pass, never lost or partial. Before each cut, room for it is
    made by the retention rules (make_room). Returns counts.
    Implements: LLR-1371, LLR-1373."""
    ledger = load_ledger(state_dir, cam.camera, now_ms)
    records, rejected = ev.read(state_dir, cam.camera, cam.sources, now_ms - LOOKBACK_MS)
    episodes, _orphans = build_episodes(records, cam.causes, cam.pre_ms, cam.lease_ms, cam.max_ms)
    quota_fn = quota_fn or (lambda: clips_quota(settings))
    cache: Dict[tuple, Loaded] = {}
    counts = {"cut": 0, "lost": 0, "failed": 0, "waiting": 0, "rejected_records": rejected}
    lost_new: List[str] = []
    try:
        for ep in episodes:
            if ledger_covers(ledger, ep):
                continue
            ws, we = ep.window
            plan = None
            segs = list_segments(cam.segment_dir, cam.camera) if now_ms >= we + SETTLE_MS else []
            oldest = segs[0].start if segs else None
            if now_ms >= we + SETTLE_MS:
                needed = [(s, segs[i + 1].start if i + 1 < len(segs) else None) for i, s in enumerate(segs)
                          if s.start * 1000 < we and (i + 1 == len(segs) or segs[i + 1].start * 1000 > ws - 2000)]
                # Only this episode's segments stay in memory (each is ~50 MB).
                keep = {s.path for s, _n in needed}
                for key in [k for k in cache if k[0] not in keep]:
                    del cache[key]
                got = [(s, load_segment(s, cam.fps, cache, nxt)) for s, nxt in needed]
                waits = ledger.setdefault("waits", {})  # type: ignore[union-attr]
                if any(x is None for _s, x in got):
                    # Renamed (.part -> .ts) or pruned since it was listed: the
                    # next pass lists the tree as it is then. One that still
                    # cannot be opened after MAX_WAITS passes is unreadable.
                    waits[ep.id] = int(waits.get(ep.id, 0)) + 1  # type: ignore[union-attr,index]
                    if waits[ep.id] < MAX_WAITS:  # type: ignore[index]
                        counts["waiting"] += 1
                        continue
                    del waits[ep.id]  # type: ignore[union-attr]
                    entry = {"start_ms": ws, "event_ms": ep.start_ms, "end_ms": we, "at_ms": now_ms,
                             "cause": ep.cause, "status": "lost",
                             "why": "a segment it needs could not be opened in %d passes" % MAX_WAITS}
                    ledger["episodes"][ep.id] = entry  # type: ignore[index]
                    counts["lost"] += 1
                    when = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(ep.event_ms / 1000.0))
                    lost_new.append(when)
                    mc.log("clips [%s]: %s at %s LOST - %s" % (cam.camera, ep.cause, when, entry["why"]), warning=True)
                    continue
                waits.pop(ep.id, None)  # type: ignore[union-attr]
                loaded = [x for _s, x in got if x is not None and x.times]
                plan = plan_window(loaded, ws / 1000.0, we / 1000.0, cam.fps) if loaded else None
            state = readiness(now_ms, ep, plan, oldest)
            entry: Dict[str, object] = {"start_ms": ws, "event_ms": ep.start_ms, "end_ms": we, "at_ms": now_ms,
                                        "cause": ep.cause}
            if state == "wait":
                counts["waiting"] += 1
                continue
            if state.startswith("lost"):
                entry.update(status="lost", why=state[len("lost: "):])
                ledger["episodes"][ep.id] = entry  # type: ignore[index]
                counts["lost"] += 1
                when = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(ep.event_ms / 1000.0))
                if we >= int(ledger["since_ms"]):  # type: ignore[arg-type]
                    lost_new.append(when)
                mc.log("clips [%s]: %s at %s LOST - %s" % (cam.camera, ep.cause, when, entry["why"]), warning=True)
                continue
            make_room(settings, state_dir, now_ms, planned_bytes(plan), quota_fn)  # type: ignore[arg-type]
            result = cut_episode(settings, cam, ep, plan, state_dir, runner, prober)  # type: ignore[arg-type]
            retries = ledger.setdefault("retries", {})  # type: ignore[union-attr]
            attempts = int(retries.pop(ep.id, 0)) + 1  # type: ignore[union-attr]
            if result["status"] == "failed":
                counts["failed"] += 1
                if attempts < MAX_ATTEMPTS:
                    retries[ep.id] = attempts  # type: ignore[index]
                    continue  # tried again next run
            else:
                counts["cut"] += 1
            entry.update(result, attempts=attempts)
            ledger["episodes"][ep.id] = entry  # type: ignore[index]
            # Saved after every cut: a pass killed later must not cut this one again.
            mc.save_json_atomic(ledger_path(state_dir, cam.camera), ledger)
    finally:
        prune_ledger(ledger, now_ms)
        live = {ep.id for ep in episodes}
        for key in [k for k in ledger.get("waits", {}) if k not in live]:  # type: ignore[union-attr]
            del ledger["waits"][key]  # type: ignore[index]
        mc.save_json_atomic(ledger_path(state_dir, cam.camera), ledger)
    if lost_new and alert:
        alert("clips-lost-%s" % cam.camera, "HomeHub: %d %s motion clip(s) could not be cut" % (len(lost_new), cam.camera),
              "The recording no longer (or never) covered: %s. Segments are kept ~27 h." % ", ".join(lost_new[:10]))
    return counts


# ── the encode batch ─────────────────────────────────────────────────────────


def reconcile_encodes(root: str, camera: str, now: Optional[float] = None) -> int:
    """A sidecar written for an encode whose replace never happened (killed
    between the two) still describes the encoded file while the copy is on
    disk: found by size, it is put back to the copy's bytes and hash and the
    encode recorded as not kept. Returns how many were reconciled."""
    fixed = 0
    cam_dir = os.path.join(root, camera)
    since = time.strftime("%Y-%m-%d", time.localtime((now or time.time()) - ENCODE_LOOKBACK_DAYS * 86400))
    for day in sorted(d for d in _days(cam_dir) if d >= since):
        day_dir = os.path.join(cam_dir, day)
        for name in sorted(_listdir(day_dir)):
            if name.endswith(".json"):
                side_path = os.path.join(day_dir, name)
                fixed += reconcile_record(side_path, mc.load_json(side_path, {}))
    return fixed


def reconcile_record(side_path: str, side: Dict[str, object]) -> int:
    """Put one sidecar back to its copy when its encode's replace never
    happened (the file on disk has the copy's size); 1 when it did."""
    enc = side.get("encode") if isinstance(side.get("encode"), dict) else None
    if not enc or not enc.get("kept"):
        return 0
    try:
        size = os.path.getsize(side_path[:-5] + ".mp4")
    except OSError:
        return 0
    if size != side.get("bytes") and size == enc.get("bytes_copy"):
        side["bytes"], side["sha256"] = size, enc.get("sha256_copy")
        enc.update(kept=False, failed="the encoded file never replaced the copy")
        write_json_atomic(side_path, side)
        return 1
    return 0


def encode_candidates(root: str, camera: str, now: Optional[float] = None,
                      reconcile: bool = False) -> List[Tuple[str, str]]:
    """(day dir, stem) of clips whose sidecar has no encode result yet, oldest
    first - looking only at the last ENCODE_LOOKBACK_DAYS day folders, so the
    batch does not read every sidecar of a year of clips each quarter hour
    (older copies, from before the encode was switched on, stay as cut).
    With `reconcile` (the batch, behind the guard) the same scan also puts back
    a record whose replace never happened (reconcile_record): one pass over the
    sidecars, not two."""
    out = []
    cam_dir = os.path.join(root, camera)
    since = time.strftime("%Y-%m-%d", time.localtime((now or time.time()) - ENCODE_LOOKBACK_DAYS * 86400))
    for day in sorted(d for d in _days(cam_dir) if d >= since):
        day_dir = os.path.join(cam_dir, day)
        for name in sorted(_listdir(day_dir)):
            if name.endswith(".json") and os.path.exists(os.path.join(day_dir, name[:-5] + ".mp4")):
                side_path = os.path.join(day_dir, name)
                side = mc.load_json(side_path, {})
                if reconcile:
                    reconcile_record(side_path, side)
                if side.get("v") == 1 and side.get("encode") is None:
                    out.append((day_dir, name[:-5]))
    return out


def _listdir(path: str) -> List[str]:
    """os.listdir, or nothing when the folder went (retention removes empty days)."""
    try:
        return os.listdir(path)
    except OSError:
        return []


def _real_dir(path: str) -> bool:
    """A directory itself, never a symlink to one (lstat): nothing here
    descends through a link out of the clips area."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _days(cam_dir: str) -> List[str]:
    try:
        return [d for d in os.listdir(cam_dir) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)
                and _real_dir(os.path.join(cam_dir, d))]
    except OSError:
        return []


def encode_clip(settings: ClipsSettings, camera: str, day_dir: str, stem: str,
                runner=subprocess.run, prober=run_probe,
                quota_fn: Optional[Callable[[], Tuple[Optional[int], Optional[int]]]] = None) -> Dict[str, object]:
    """Encode one clip; replace the copy only when verified and small enough.
    The sidecar's `encode` records the outcome either way, so a clip is tried
    once. Raises QuotaStop / GuardStop. Implements: LLR-1374, LLR-1375."""
    ok, reason = guard(settings)
    if not ok:
        raise GuardStop(reason)
    src = os.path.join(day_dir, stem + ".mp4")
    side_path = os.path.join(day_dir, stem + ".json")
    if quota_fn is not None:  # the encoded file is written beside the copy
        make_room(settings, "", int(time.time() * 1000), os.path.getsize(src), quota_fn, keep={(day_dir, stem)})
    side = mc.load_json(side_path, {})
    part = stem + ".enc.mp4.part"
    part_path = os.path.join(day_dir, part)
    before = os.path.getsize(src)
    result: Dict[str, object] = {"codec": settings.codec, "kbps": settings.kbps, "bytes_copy": before,
                                 "at_utc_ms": int(time.time() * 1000)}
    docker_remove("homehub-clips-encode-%s" % camera)
    t0 = time.monotonic()
    try:
        done = runner(encode_command(settings, camera, day_dir, stem + ".mp4", part), stdout=subprocess.DEVNULL,
                      stderr=subprocess.PIPE, timeout=900)
        err = (done.stderr or b"").decode("utf-8", "replace") if isinstance(done.stderr, bytes) else (done.stderr or "")
        rc = done.returncode
    except subprocess.TimeoutExpired:
        err, rc = "timed out after 900 s", -1
        docker_remove("homehub-clips-encode-%s" % camera)  # killing the client leaves the container
    result["seconds"] = round(time.monotonic() - t0, 1)
    if quota_hit(err):
        _remove(part_path)
        raise QuotaStop(err.strip().splitlines()[-1] if err.strip() else "quota")
    ok, reason = guard(settings)  # the encode took minutes: is the area still the area?
    if not ok:
        _remove(part_path)
        raise GuardStop(reason)
    probed = prober(camera, day_dir, part) if rc == 0 and os.path.exists(part_path) else None
    want = side.get("frames")
    actual = side.get("actual") if isinstance(side.get("actual"), dict) else {}
    span = ((actual.get("end_utc_ms", 0) - actual.get("start_utc_ms", 0)) / 1000.0
            if isinstance(actual.get("end_utc_ms"), int) and isinstance(actual.get("start_utc_ms"), int) else None)
    replace = False
    if settings.encode_fps:
        # Fewer frames by design: expected from the clip's span when the record has
        # one; without it the frame count says nothing, and only frames > 0 is asked.
        want = int(round(span * settings.encode_fps)) if span else None
    if not probed or int(probed.get("frames") or 0) <= 0 \
            or (isinstance(want, int) and abs(int(probed["frames"]) - want) > (1 if not settings.encode_fps else 2)):
        result.update(kept=False, failed=("ffmpeg exited %s: %s" % (rc, err.strip()[-200:]) if rc else
                                          "the encode holds %s frames, not %s" % ((probed or {}).get("frames"), want)))
    elif span is not None and "duration" in probed and abs(float(probed["duration"]) - span) > 1.5:
        result.update(kept=False, failed="the encode lasts %.2f s, the clip %.2f s" % (float(probed["duration"]), span))
    else:
        after = os.path.getsize(part_path)
        result["bytes_encoded"] = after
        replace = after * 100 <= before * settings.keep_pct
        result["kept"] = replace
        if replace:
            # The copy's identity stays in the record: if the replace below
            # never happens, reconcile_encodes finds the copy by its size.
            result["sha256_copy"] = side.get("sha256")
            side["bytes"] = after
            side["sha256"] = sha256_file(part_path)
    side["encode"] = result
    # The record first, then the file it describes: a failure between them
    # leaves the copy in place, never an encoded clip with no encode record
    # (which the next batch would encode a second time).
    try:
        write_json_atomic(side_path, side)
        if replace:
            os.replace(part_path, src)
    except OSError as exc:
        _remove(part_path)
        if exc.errno in (getattr(errno, "EDQUOT", -1), errno.ENOSPC):
            raise QuotaStop(str(exc))
        raise
    if not replace:
        _remove(part_path)
    mc.log("clips [%s]: encode %s: %s" % (camera, stem, json.dumps(result, sort_keys=True)))
    return result


# ── the encode trial (the Owner, 2026-10-27) ─────────────────────────────────
#
# Before CLIPS_ENCODE_CODEC/KBPS/SIZE/FPS are chosen: every combination below on
# a sample of one day's real clips, day and night, measured for size, CPU, wall
# time and SSIM against the copy. It never replaces or deletes a clip; it writes
# only under <root>/.trial/ (quota-counted; `--clean` removes it), keeping one
# encoded sample per combination for viewing side by side.

TRIAL_DIR = ".trial"
TRIAL_RUN_SEC = 1800           # no new encode is started after this long (the lock is shared with the cut)
NIGHT_HOURS = (19, 7)          # local clock: from 19:00 to 07:00 a clip counts as night (IR on this camera)
TRIAL_COMBOS = [dict(codec=c, size=z, kbps=k, fps=0) for c in ("h264", "hevc") for z in (0, 1024) for k in (300, 200)]
TRIAL_COMBOS.append(dict(codec="h264", size=0, kbps=300, fps=5))
BENCH_RE = re.compile(r"bench: utime=([0-9.]+)s stime=([0-9.]+)s rtime=([0-9.]+)s")
SSIM_RE = re.compile(r"SSIM .*All:([0-9.]+)")


def combo_name(c: Dict[str, object]) -> str:
    return "%s_%s_%dk%s" % (c["codec"], "native" if not c["size"] else "%dsq" % c["size"], c["kbps"],
                            "_%dfps" % c["fps"] if c["fps"] else "")


def trial_sample(day_dir: str, max_clips: int) -> List[Tuple[str, str]]:
    """(stem, "day"|"night") of up to max_clips clips of one day folder, split
    between day and night by the local hour in their names, shortest first so
    the trial stays bounded."""
    stems = sorted(n[:-4] for n in _listdir(day_dir) if n.endswith(".mp4"))

    def kind(stem: str) -> str:
        utc = stem_utc_ms(stem)
        hour = time.localtime(utc / 1000.0).tm_hour if utc else 12
        return "night" if hour >= NIGHT_HOURS[0] or hour < NIGHT_HOURS[1] else "day"

    def length(stem: str) -> int:
        m = re.search(r"_(\d+)s(?:-\d+)?$", stem)
        return int(m.group(1)) if m else 10 ** 6
    day = sorted((s for s in stems if kind(s) == "day"), key=length)
    night = sorted((s for s in stems if kind(s) == "night"), key=length)
    half = max(1, max_clips // 2)
    picked = [(s, "day") for s in day[:half]] + [(s, "night") for s in night[:max_clips - min(half, len(day))]]
    if len(picked) < max_clips:  # one kind is short: fill from the other
        rest = [(s, "day") for s in day[half:]] + [(s, "night") for s in night[max_clips - min(half, len(day)):]]
        picked += rest[:max_clips - len(picked)]
    return picked[:max_clips]


def trial_encode_command(settings: ClipsSettings, camera: str, day_dir: str, out_dir: str, stem: str,
                         combo: Dict[str, object], out_name: str) -> List[str]:
    argv = docker_base("homehub-clips-trial-%s" % camera, out_dir, shares=2, device=settings.device, cpus=LOW_CPUS)
    argv += ["-v", "%s:/src:ro" % day_dir, "--entrypoint", "ffmpeg", door_relay.image_ref()]
    args = vaapi_encode_args(settings.device, "/src/%s.mp4" % stem, str(combo["codec"]), int(combo["kbps"]),
                             int(combo["size"]), int(combo["fps"]), loglevel="info")
    return argv + args + ["-benchmark", "-nostats", "-movflags", "+faststart", "-f", "mp4", "-y", "/out/" + out_name]


def trial_ssim_command(camera: str, day_dir: str, out_dir: str, stem: str, out_name: str, width: int, height: int,
                       fps: int) -> List[str]:
    """SSIM of an encode against its copy, in software at the lowest weight (a
    one-off): the encode scaled back to the copy's raster, the copy taken at the
    encode's rate when that was changed."""
    ref = "fps=%d" % fps if fps else "null"
    graph = "[0:v]scale=%d:%d:flags=bicubic[a];[1:v]%s[b];[a][b]ssim" % (width, height, ref)
    argv = docker_base("homehub-clips-ssim-%s" % camera, out_dir, read_only_out=True, shares=2, cpus=LOW_CPUS)
    return argv + ["-v", "%s:/src:ro" % day_dir, "--entrypoint", "ffmpeg", door_relay.image_ref(),
                   "-nostdin", "-hide_banner", "-loglevel", "info", "-nostats", "-i", "/out/" + out_name,
                   "-i", "/src/%s.mp4" % stem, "-lavfi", graph, "-f", "null", "-"]


def _text(done) -> str:
    out = getattr(done, "stderr", b"") or b""
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else out


def encode_trial(settings: ClipsSettings, camera: str, day: str, max_clips: int,
                 runner=subprocess.run, prober=run_probe,
                 quota_fn: Callable[[], Tuple[Optional[int], Optional[int]]] = lambda: (None, None),
                 clock=time.monotonic) -> Dict[str, object]:
    """Run the trial; returns the report (also written as trial.json and
    trial.txt in the trial folder). Implements: LLR-1374."""
    if not ev.CAMERA_RE.fullmatch(camera or "") or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        raise Invalid("--camera must be a camera name and --day YYYY-MM-DD")
    ok, reason = guard(settings)
    if not ok:
        raise GuardStop(reason)
    day_dir = os.path.join(settings.root, camera, day)
    sample = trial_sample(day_dir, max_clips)
    if not sample:
        raise Invalid("no clips in %s" % day_dir)
    out_dir = os.path.join(settings.root, TRIAL_DIR, "%s-%s" % (camera, day))
    copies = {stem: os.path.getsize(os.path.join(day_dir, stem + ".mp4")) for stem, _k in sample}
    used, limit = quota_fn()
    # The peak the trial holds: one kept encode per combination (of the first
    # clip, at most its copy's size) plus the one in flight.
    need = copies[sample[0][0]] * len(TRIAL_COMBOS) + max(copies.values())
    if limit and used is not None and used + need > limit * settings.quota_high_pct // 100:
        raise QuotaStop("the trial needs up to %.0f MiB and the clips area is too full for it; nothing was "
                        "written (no clip is ever removed for a trial)" % (need / MIB))
    os.makedirs(out_dir, mode=0o750, exist_ok=True)
    deadline = clock() + TRIAL_RUN_SEC
    rows: List[Dict[str, object]] = []
    stopped_early = False
    for combo in TRIAL_COMBOS:
        name = combo_name(combo)
        for index, (stem, kind) in enumerate(sample):
            left = deadline - clock()
            if left <= 0:
                stopped_early = True
                break
            # The first sample of each combination is kept for viewing; the rest are measured and removed.
            out_name = "%s_%s.mp4" % (name, stem) if index == 0 else ".%s_%s.mp4.part" % (name, stem)
            row: Dict[str, object] = {"combo": name, "kind": kind, "clip": stem, "copy_bytes": copies[stem]}
            docker_remove("homehub-clips-trial-%s" % camera)
            try:
                done = runner(trial_encode_command(settings, camera, day_dir, out_dir, stem, combo, out_name),
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=max(1, min(900, left)))
                err, rc = _text(done), done.returncode
            except subprocess.TimeoutExpired:
                docker_remove("homehub-clips-trial-%s" % camera)
                err, rc = "timed out after 900 s", -1
            out_path = os.path.join(out_dir, out_name)
            bench = BENCH_RE.search(err)
            if rc or not os.path.exists(out_path):
                row["failed"] = "ffmpeg exited %s: %s" % (rc, err.strip()[-200:])
                _remove(out_path)  # a broken sample is never kept for viewing
            else:
                size = os.path.getsize(out_path)
                row.update(bytes=size, pct_of_copy=round(100.0 * size / max(1, copies[stem]), 1))
                if bench:
                    row.update(cpu_s=round(float(bench.group(1)) + float(bench.group(2)), 2),
                               wall_s=round(float(bench.group(3)), 2))
                ref = prober(camera, day_dir, stem + ".mp4") or {}
                if ref.get("width"):
                    docker_remove("homehub-clips-ssim-%s" % camera)
                    try:
                        sd = runner(trial_ssim_command(camera, day_dir, out_dir, stem, out_name, int(ref["width"]),
                                                       int(ref["height"]), int(combo["fps"])),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    timeout=max(1, min(900, deadline - clock())))
                        m = SSIM_RE.search(_text(sd))
                        if m:
                            row["ssim"] = float(m.group(1))
                        else:
                            row["ssim_failed"] = "rc %s" % sd.returncode
                    except subprocess.TimeoutExpired:
                        docker_remove("homehub-clips-ssim-%s" % camera)
                        row["ssim_failed"] = "timed out"
                else:
                    row["ssim_failed"] = "the copy could not be probed"
            if index != 0:
                _remove(out_path)
            rows.append(row)
        if stopped_early:
            break
    report = {"camera": camera, "day": day, "clips": [{"clip": s, "kind": k, "bytes": copies[s]} for s, k in sample],
              "rows": rows, "summary": trial_summary(rows), "stopped_early": stopped_early,
              "folder": out_dir}
    write_json_atomic(os.path.join(out_dir, "trial.json"), report)
    with open(os.path.join(out_dir, "trial.txt"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(trial_table(report))
    return report


def trial_summary(rows: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    """Per combination and kind (day, night, all): copy and encoded bytes,
    percent, CPU and wall seconds, mean and worst SSIM."""
    out = []
    names = []
    for r in rows:
        if r["combo"] not in names:
            names.append(r["combo"])
    for name in names:
        for kind in ("day", "night", "all"):
            sel = [r for r in rows if r["combo"] == name and (kind == "all" or r["kind"] == kind) and "bytes" in r]
            if not sel:
                continue
            copy = sum(int(r["copy_bytes"]) for r in sel)
            enc = sum(int(r["bytes"]) for r in sel)
            ssims = [float(r["ssim"]) for r in sel if "ssim" in r]
            out.append({"combo": name, "kind": kind, "clips": len(sel), "copy_bytes": copy, "bytes": enc,
                        "pct_of_copy": round(100.0 * enc / max(1, copy), 1),
                        "cpu_s": round(sum(float(r.get("cpu_s", 0)) for r in sel), 2),
                        "wall_s": round(sum(float(r.get("wall_s", 0)) for r in sel), 2),
                        "ssim_mean": round(sum(ssims) / len(ssims), 4) if ssims else None,
                        "ssim_min": round(min(ssims), 4) if ssims else None})
    return out


def trial_table(report: Dict[str, object]) -> str:
    lines = ["encode trial: %s %s, %d clip(s)%s" % (report["camera"], report["day"], len(report["clips"]),
                                                     " (STOPPED EARLY: time budget)" if report["stopped_early"] else ""),
             "%-28s %-5s %5s %9s %9s %6s %7s %7s %7s" % ("combination", "kind", "clips", "copy KiB", "enc KiB",
                                                          "% copy", "cpu s", "wall s", "SSIM")]
    for r in report["summary"]:  # type: ignore[union-attr]
        lines.append("%-28s %-5s %5d %9d %9d %6.1f %7.2f %7.2f %7s" % (
            r["combo"], r["kind"], r["clips"], r["copy_bytes"] // 1024, r["bytes"] // 1024, r["pct_of_copy"],
            r["cpu_s"], r["wall_s"], "%.4f" % r["ssim_mean"] if r["ssim_mean"] is not None else "-"))
    failed = [r for r in report["rows"] if "failed" in r]  # type: ignore[union-attr]
    for r in failed:
        lines.append("FAILED %s on %s: %s" % (r["combo"], r["clip"], r["failed"]))
    no_ssim = [r for r in report["rows"] if "ssim_failed" in r]  # type: ignore[union-attr]
    if no_ssim:
        lines.append("SSIM missing for %d encode(s) (the means above leave them out): %s"
                     % (len(no_ssim), ", ".join("%s on %s" % (r["combo"], r["clip"]) for r in no_ssim[:6])))
    lines.append("samples to compare side by side: %s" % report["folder"])
    return "\n".join(lines) + "\n"


def trial_folders(root: str) -> List[str]:
    """The trial's per-day folders: real directories only, never through a link."""
    base = os.path.join(root, TRIAL_DIR)
    if not _real_dir(base):
        return []
    return [os.path.join(base, top) for top in sorted(_listdir(base)) if _real_dir(os.path.join(base, top))]


def trial_bytes(root: str) -> int:
    """Bytes the encode trial's folder holds (it counts against the quota)."""
    total = 0
    for folder in trial_folders(root):
        for name in _listdir(folder):
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def clean_trials(root: str, count_bytes: bool = False) -> int:
    """Remove the trial folder (only it). Returns the files removed, or with
    count_bytes the bytes freed."""
    base = os.path.join(root, TRIAL_DIR)
    removed = freed = 0
    if not _real_dir(base):
        return 0
    for top in sorted(_listdir(base)):
        folder = os.path.join(base, top)
        if not _real_dir(folder):
            if _remove(folder):      # a link (or a stray file): the entry itself goes, never its target
                removed += 1
            continue
        for name in _listdir(folder):
            path = os.path.join(folder, name)
            try:
                size = os.lstat(path).st_size
            except OSError:
                size = 0
            if _remove(path):
                removed += 1
                freed += size
        try:
            os.rmdir(folder)
        except OSError:
            pass
    try:
        os.rmdir(base)
    except OSError:
        pass
    return freed if count_bytes else removed


# ── retention ────────────────────────────────────────────────────────────────


STEM_TIME_RE = re.compile(r"[a-z][a-z0-9]{0,15}_(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})([+-])(\d{2})(\d{2})_")


def stem_utc_ms(stem: str) -> Optional[int]:
    """The UTC time a clip's name says (its local time and offset), or None."""
    m = STEM_TIME_RE.match(stem)
    if not m:
        return None
    y, mo, d, h, mi, se = (int(x) for x in m.groups()[:6])
    oh, om = int(m.group(8)), int(m.group(9))
    if not (oh <= 14 and om < 60):
        return None
    offset = (oh * 3600 + om * 60) * (1 if m.group(7) == "+" else -1)
    try:
        import datetime
        stamp = datetime.datetime(y, mo, d, h, mi, se)  # validates every field
    except ValueError:
        return None
    return (calendar.timegm(stamp.timetuple()) - offset) * 1000


def camera_dirs(root: str) -> List[str]:
    """Every camera folder under the clips root - configured or not, so a
    camera dropped from CAMERAS still ages out and still counts."""
    try:
        return sorted(d for d in os.listdir(root) if ev.CAMERA_RE.fullmatch(d) and _real_dir(os.path.join(root, d)))
    except OSError:
        return []


def list_clips(root: str, cameras: Optional[Sequence[str]] = None) -> List[Tuple[int, str, str, int]]:
    """(time utc ms, day dir, stem, bytes of mp4 + json) of every clip, oldest
    first. The time is the name's (the first event's), read without opening the
    sidecar; a name that does not parse falls back to the file's mtime.
    `cameras` None = every camera folder under the root."""
    out = []
    for camera in (camera_dirs(root) if cameras is None else cameras):
        cam_dir = os.path.join(root, camera)
        if not _real_dir(cam_dir):
            continue
        for day in _days(cam_dir):
            day_dir = os.path.join(cam_dir, day)
            for name in _listdir(day_dir):
                if not name.endswith(".mp4"):
                    continue
                try:
                    st = os.lstat(os.path.join(day_dir, name))
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue      # only clips this pipeline wrote: never a link
                stem = name[:-4]
                start = stem_utc_ms(stem)
                if start is None:
                    start = int(st.st_mtime * 1000)
                size = st.st_size
                try:
                    size += os.lstat(os.path.join(day_dir, stem + ".json")).st_size
                except OSError:
                    pass
                out.append((start, day_dir, stem, size))
    out.sort()
    return out


def retention_plan(clips: Sequence[Tuple[int, str, str, int]], now_ms: int, keep_days: int, cap_bytes: int,
                   quota_used: Optional[int] = None, quota_limit: Optional[int] = None,
                   high_pct: int = 90) -> List[Tuple[int, str, str, int]]:
    """Which clips go (pure): every clip older than keep_days; then, oldest
    first, until the total is within cap_bytes; then, while the quota project
    is past high_pct of its limit, until it is 5 points under. The cap wins
    over the age (the Owner, 2026-10-26). Implements: LLR-1376."""
    old = [now_ms - c[0] > keep_days * 86400 * 1000 for c in clips]
    doomed = [c for c, gone in zip(clips, old) if gone]
    rest = [c for c, gone in zip(clips, old) if not gone]
    total = sum(c[3] for c in rest)
    used = quota_used
    target = None
    if quota_used is not None and quota_limit and quota_used * 100 > quota_limit * high_pct:
        target = quota_limit * (high_pct - 5) // 100
        used = quota_used - sum(c[3] for c in doomed)
    for c in rest:
        over_cap = total > cap_bytes
        over_quota = target is not None and used is not None and used > target
        if not (over_cap or over_quota):
            break
        doomed.append(c)
        total -= c[3]
        if used is not None:
            used -= c[3]
    return doomed


def clips_quota(settings: ClipsSettings) -> Tuple[Optional[int], Optional[int]]:
    """(used, limit) bytes of the clips quota project on scratch, or (None, None)."""
    if settings.root != SCRATCH_ROOT:
        return None, None
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
            entry = scratch.mount_of(scratch.parse_mountinfo(handle.read()), scratch.MOUNTPOINT)
        if not entry:
            return None, None
        return scratch.get_project_quota(entry["source"], scratch.PROJECT_ID["clips"])
    except (OSError, KeyError):
        return None, None


def apply_retention(settings: ClipsSettings, state_dir: str, now_ms: int,
                    quota_fn: Callable[[], Tuple[Optional[int], Optional[int]]] = lambda: (None, None)
                    ) -> Dict[str, object]:
    """Stale .part files first (they count against the quota too), then the
    quota is read, then the plan, then the deletions, empty days and the index.
    Every camera folder under the root, configured or not. Implements: LLR-1376."""
    cameras = camera_dirs(settings.root)
    stale = 0
    for camera in cameras:
        cam_dir = os.path.join(settings.root, camera)
        if os.path.exists(os.path.join(cam_dir, INDEX_DIRTY)):
            # A pass was killed while this index was out of date: rebuild it.
            ok, reason = guard(settings)
            if not ok:
                raise GuardStop(reason)
            if rebuild_index(cam_dir):
                _remove(os.path.join(cam_dir, INDEX_DIRTY))
        for day in _days(cam_dir):
            day_dir = os.path.join(cam_dir, day)
            for name in _listdir(day_dir):
                path = os.path.join(day_dir, name)
                # A crashed cut or encode leaves a .part, or a sidecar whose clip
                # was never renamed into place; an hour old, neither is in flight.
                orphan = name.endswith(".json") and not os.path.exists(path[:-len(".json")] + ".mp4")
                if name.endswith(".part") or orphan:
                    try:
                        if now_ms / 1000.0 - os.path.getmtime(path) > STALE_PART_SEC:
                            _remove(path)
                            stale += 1
                    except OSError:
                        pass
    for folder in trial_folders(settings.root):
        for name in _listdir(folder):
            path = os.path.join(folder, name)
            try:
                if name.endswith(".part") and now_ms / 1000.0 - os.lstat(path).st_mtime > STALE_PART_SEC:
                    _remove(path)
                    stale += 1
            except OSError:
                pass
    used, limit = quota_fn()
    clips = list_clips(settings.root, cameras)
    doomed = retention_plan(clips, now_ms, settings.retention_days, settings.cap_mib * MIB,
                            used, limit, settings.quota_high_pct)
    remove_clips(settings, doomed)
    for camera in cameras:
        cam_dir = os.path.join(settings.root, camera)
        for day in _days(cam_dir):
            try:
                os.rmdir(os.path.join(cam_dir, day))  # only when empty
            except OSError:
                pass
    events_removed = ev.prune_days(state_dir, sorted(set(cameras) | {c.camera for c in settings.cameras}),
                                   settings.event_days, now_ms / 1000.0)
    if doomed:
        mc.log("clips: retention removed %d clip(s), %.1f MiB" % (len(doomed), sum(c[3] for c in doomed) / MIB))
    return {"removed": len(doomed), "removed_mib": round(sum(c[3] for c in doomed) / MIB, 1),
            "stale_parts": stale, "event_days_removed": len(events_removed), "kept": len(clips) - len(doomed)}


def planned_bytes(plan: Plan) -> int:
    """What a cut will write: its TS bytes plus the MP4's own tables (a few
    percent) and the sidecar."""
    raw = sum(b - a + (len(seg.idx.header) if h else 0) for seg, a, b, h in plan.pieces)
    return int(raw * 1.05) + 64 * 1024


def remove_clips(settings: ClipsSettings, doomed: Sequence[Tuple[int, str, str, int]]) -> Tuple[set, int]:
    """Delete clips (mp4, then its sidecar) with the guard asked first and the
    camera's index marked dirty while it is out of date; empty day folders go;
    the index is rewritten without them. Returns (gone, bytes freed)."""
    if not doomed:
        return set(), 0
    ok, reason = guard(settings)
    if not ok:
        raise GuardStop(reason)
    gone = set()
    freed = 0
    cameras = {os.path.basename(os.path.dirname(c[1])) for c in doomed}
    for camera in cameras:
        mark_index_dirty(os.path.join(settings.root, camera))
    for _start, day_dir, stem, size in doomed:
        if _remove(os.path.join(day_dir, stem + ".mp4")):
            _remove(os.path.join(day_dir, stem + ".json"))
            gone.add((os.path.basename(os.path.dirname(day_dir)), "%s/%s.mp4" % (os.path.basename(day_dir), stem)))
            freed += size
            try:
                os.rmdir(day_dir)  # only when empty
            except OSError:
                pass
        else:
            mc.log("clips: could not remove %s/%s.mp4 - its index line stays" % (day_dir, stem), warning=True)
    for camera in cameras:
        cam_dir = os.path.join(settings.root, camera)
        ok, reason = guard(settings)  # the deletions took a while: the index is a write
        if not ok:
            raise GuardStop(reason)
        if rewrite_index(os.path.join(cam_dir, "index.jsonl"), {clip for c, clip in gone if c == camera}):
            _remove(os.path.join(cam_dir, INDEX_DIRTY))
    return gone, freed


def make_room(settings: ClipsSettings, state_dir: str, now_ms: int, need: int,
              quota_fn: Callable[[], Tuple[Optional[int], Optional[int]]],
              keep: Optional[set] = None) -> int:
    """Before a write of `need` bytes: when the clips project would pass
    CLIPS_QUOTA_HIGH_PCT, remove clips by the retention rules first - those
    past their age or the cap, then the oldest - until it fits. On the 5 GiB
    scratch area this, not the hourly retention, is the normal cycle; a full
    quota stays an alert only when retention cannot free enough (QuotaStop).
    `keep` holds (day dir, stem) pairs never removed here - the clip an encode
    is working on. Returns the bytes freed. Implements: LLR-1377."""
    used, limit = quota_fn()
    if not limit or used is None:
        return 0
    ceiling = limit * settings.quota_high_pct // 100
    if used + need <= ceiling:
        return 0
    # The encode trial's samples are disposable next to real footage: they go
    # first, so no clip is removed to make room for them.
    trial_freed = clean_trials(settings.root, count_bytes=True) if trial_bytes(settings.root) else 0
    used -= trial_freed
    if used + need <= ceiling:
        mc.log("clips: made room for %.1f MiB by removing the encode trial's samples" % (need / MIB))
        return trial_freed
    clips = [c for c in list_clips(settings.root) if (c[1], c[2]) not in (keep or set())]
    # Feasibility FIRST: if removing every candidate would still not fit, nothing is removed.
    if used + need - sum(c[3] for c in clips) > limit:
        raise QuotaStop("the clips area (%d MiB) cannot hold %.1f MiB more even after removing all %d older "
                        "clip(s); none was removed" % (limit // MIB, need / MIB, len(clips)))
    target = ceiling if used + need - sum(c[3] for c in clips) <= ceiling else limit
    rules = retention_plan(clips, now_ms, settings.retention_days, settings.cap_mib * MIB)
    doomed = list(rules)
    projected = used + need - sum(c[3] for c in doomed)
    chosen = {id(c) for c in doomed}
    for c in clips:  # oldest first
        if projected <= target:
            break
        if id(c) not in chosen:
            doomed.append(c)
            projected -= c[3]
    _gone, freed = remove_clips(settings, doomed)
    mc.log("clips: made room for %.1f MiB: removed %d clip(s), %.1f MiB" % (need / MIB, len(_gone), freed / MIB))
    if used + need - freed > limit:  # a removal failed (it is logged): still too full
        raise QuotaStop("the clips area (%d MiB) cannot hold %.1f MiB more after removing %d older clip(s)"
                        % (limit // MIB, need / MIB, len(_gone)))
    return freed + trial_freed


def index_line_from_sidecar(day: str, stem: str, side: Dict[str, object]) -> Optional[Dict[str, object]]:
    actual = side.get("actual") if isinstance(side.get("actual"), dict) else {}
    events = side.get("events") if isinstance(side.get("events"), list) else []
    start, end = actual.get("start_utc_ms"), actual.get("end_utc_ms")  # type: ignore[union-attr]
    if side.get("v") != 1 or not isinstance(start, int) or not isinstance(end, int):
        return None
    first = events[0].get("utc_ms") if events and isinstance(events[0], dict) else None
    return {"v": 1, "clip": "%s/%s.mp4" % (day, stem), "camera": side.get("camera"), "cause": side.get("cause"),
            "event_utc_ms": first if isinstance(first, int) else start, "start_utc_ms": start,
            "end_utc_ms": end, "seconds": round((end - start) / 1000.0, 1), "coverage": side.get("coverage")}


def rebuild_index(cam_dir: str) -> bool:
    """The index made again from the sidecars of the clips that exist, oldest
    first: the repair for a pass killed while the index was marked dirty
    (lines lost as well as lines left behind)."""
    path = os.path.join(cam_dir, "index.jsonl")
    tmp = path + ".part"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            for day in sorted(_days(cam_dir)):
                day_dir = os.path.join(cam_dir, day)
                for name in sorted(_listdir(day_dir)):
                    if not name.endswith(".json") or not os.path.exists(os.path.join(day_dir, name[:-5] + ".mp4")):
                        continue
                    line = index_line_from_sidecar(day, name[:-5], mc.load_json(os.path.join(day_dir, name), {}))
                    if line:
                        handle.write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        _remove(tmp)
        mc.log("clips: could not rebuild %s: %s" % (path, exc), warning=True)
        return False


def rewrite_index(path: str, gone: set) -> bool:
    """Drop the lines of removed clips - those just removed, and any whose file
    no longer exists (a pass killed between the deletions and this rewrite) -
    streaming line by line, never holding the index in memory."""
    cam_dir = os.path.dirname(path)
    tmp = path + ".part"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            for r in mc.iter_jsonl(path):
                clip = r.get("clip") if isinstance(r, dict) else None
                if not isinstance(clip, str) or clip in gone or not os.path.exists(os.path.join(cam_dir, clip)):
                    continue
                handle.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        _remove(tmp)
        mc.log("clips: could not rewrite %s: %s" % (path, exc), warning=True)
        return False


# ── the quota stop marker ────────────────────────────────────────────────────


def stop_path(state_dir: str) -> str:
    return os.path.join(state_dir, "quota-stopped.json")


def set_quota_stop(state_dir: str, why: str, now_ms: int) -> None:
    mc.save_json_atomic(stop_path(state_dir), {"since_ms": now_ms, "why": why})


def quota_stopped(state_dir: str) -> Optional[Dict[str, object]]:
    data = mc.load_json(stop_path(state_dir), {})
    return data or None


# ── status ───────────────────────────────────────────────────────────────────


def status(settings: ClipsSettings, state_dir: str, now_ms: int) -> Dict[str, object]:
    """Read-only: per camera the newest event and clip, counts, the detector's
    own status file; the clips area's size, quota and stop marker. Implements: LLR-1378."""
    out: Dict[str, object] = {"enabled": settings.enabled, "root": settings.root, "encode": settings.encode,
                              "quota_stopped": quota_stopped(state_dir)}
    used, limit = clips_quota(settings)
    out["quota"] = {"used_mib": used // MIB, "limit_mib": limit // MIB} if limit else None
    clips = list_clips(settings.root)  # every camera folder: the quota counts them all
    out["clips"] = len(clips)
    out["clips_mib"] = round(sum(c[3] for c in clips) / MIB, 1)
    cams = {}
    for cam in settings.cameras:
        records, rejected = ev.read(state_dir, cam.camera, cam.sources, now_ms - 86400 * 1000)
        events = [r for r in records if r["kind"] == "event"]
        ledger = mc.load_json(ledger_path(state_dir, cam.camera), {}).get("episodes", {})
        mine = [c for c in clips if os.path.basename(os.path.dirname(c[1])) == cam.camera]
        cams[cam.camera] = {
            "events_24h": len(events),
            "last_event_utc_ms": int(events[-1]["utc_ms"]) if events else None,
            "rejected_records_24h": rejected,
            "last_clip": "%s/%s.mp4" % (os.path.basename(mine[-1][1]), mine[-1][2]) if mine else None,
            "last_clip_age_min": round((now_ms - mine[-1][0]) / 60000.0, 1) if mine else None,
            "lost_ledger": sum(1 for e in ledger.values() if e.get("status") == "lost"),
            "failed_ledger": sum(1 for e in ledger.values() if e.get("status") == "failed"),
            "encode_backlog": (len(encode_candidates(settings.root, cam.camera, now_ms / 1000.0))
                               if settings.encode else None),
            "detector": mc.load_json(os.path.join(state_dir, "motion", "%s.json" % cam.camera), {}) or None,
        }
    out["cameras"] = cams
    return out


# ── main ─────────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def pipeline_lock(state_dir: str) -> Iterator[bool]:
    """One of cut, encode and retention at a time; a busy lock is a skipped run."""
    try:
        import fcntl
    except ImportError:  # the dev PC's test runner
        yield True
        return
    os.makedirs(state_dir, mode=0o750, exist_ok=True)
    handle = open(os.path.join(state_dir, "pipeline.lock"), "w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        handle.close()


def run_encode_trial(settings: ClipsSettings, args) -> int:
    """The encode-trial verb: under the pipeline lock (bounded by
    TRIAL_RUN_SEC), at the lowest priority, writing only the trial folder."""
    with pipeline_lock(args.state_dir) as got:
        if not got:
            mc.log("clips: another clips run holds the lock - try the trial again shortly", warning=True)
            return 1
        if args.clean:
            ok, reason = guard(settings)
            if not ok:
                mc.log("clips: trial clean skipped - guard: %s" % reason, warning=True)
                if "CAP_SYS_RESOURCE" in reason:
                    mc.log("clips: run it as `%s encode-trial --clean`" % MANUAL_RUN, warning=True)
                return EXIT_GUARD
            print("removed %d trial file(s)" % clean_trials(settings.root))
            return 0
        if not args.camera or not args.day or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.day):
            mc.log("clips: encode-trial needs --camera and --day YYYY-MM-DD (or --clean)", warning=True)
            return 2
        try:
            os.nice(19)
        except (AttributeError, OSError):
            pass
        try:
            report = encode_trial(settings, args.camera, args.day, max(1, args.max_clips),
                                  quota_fn=lambda: clips_quota(settings))
        except (Invalid, QuotaStop, GuardStop) as exc:
            mc.log("clips: encode-trial REFUSED - %s" % exc, warning=True)
            if "CAP_SYS_RESOURCE" in str(exc):
                mc.log("clips: run it as `%s encode-trial ...`" % MANUAL_RUN, warning=True)
            return 2
        sys.stdout.write(trial_table(report))
        return 0


def make_alert(env_file: str):
    def alert(key: str, title: str, body: str) -> None:
        mc.send_alert(key, "high", title, body, env_file, mc.DEFAULT_STATE_DIR, repeat_sec=86400,
                      log_tag="homehub-clips")
    return alert


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", default=mc.DEFAULT_ENV_FILE)
    p.add_argument("--state-dir", default=ev.STATE_DIR)
    sub = p.add_subparsers(dest="verb")
    for verb in ("cut", "encode", "retention", "status"):
        sub.add_parser(verb)
    e = sub.add_parser("episodes")
    e.add_argument("--camera", required=True)
    e.add_argument("--hours", type=float, default=24.0)
    t = sub.add_parser("encode-trial", help="measure every encode option on one day's clips; replaces nothing")
    t.add_argument("--camera")
    t.add_argument("--day", help="the local day folder, YYYY-MM-DD")
    t.add_argument("--max-clips", type=int, default=4)
    t.add_argument("--clean", action="store_true", help="remove the trial folder and stop")
    args = p.parse_args(argv)
    if not args.verb:
        p.print_help()
        return 2
    try:
        settings = ClipsSettings(mc.read_knobs(args.env_file))
    except Invalid as exc:
        mc.log("clips: REFUSED - %s" % exc, warning=True)
        return 2
    now_ms = int(time.time() * 1000)
    alert = make_alert(args.env_file)
    if args.verb == "status":
        print(json.dumps(status(settings, args.state_dir, now_ms), sort_keys=True))
        return 0
    if args.verb == "episodes":
        cam = next((c for c in settings.cameras if c.camera == args.camera), None)
        if cam is None:
            mc.log("clips: %s is not in CAMERAS" % args.camera, warning=True)
            return 2
        records, rejected = ev.read(args.state_dir, cam.camera, cam.sources, now_ms - int(args.hours * 3600000))
        eps, orphans = build_episodes(records, cam.causes, cam.pre_ms, cam.lease_ms, cam.max_ms)
        ledger = mc.load_json(ledger_path(args.state_dir, cam.camera), {}).get("episodes", {})
        for ep in eps:
            ws, we = ep.window
            print(json.dumps({"id": ep.id, "cause": ep.cause, "records": len(ep.records),
                              "start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ws / 1000.0)),
                              "seconds": round((we - ws) / 1000.0, 1),
                              "ledger": (ledger.get(ep.id) or {}).get("status")}, sort_keys=True))
        print(json.dumps({"episodes": len(eps), "orphans": orphans, "rejected": rejected}, sort_keys=True))
        return 0
    if args.verb == "encode-trial":
        return run_encode_trial(settings, args)
    if not settings.enabled:
        mc.log("clips: CLIPS_ENABLED is not true - nothing to do")
        return 0
    with pipeline_lock(args.state_dir) as got:
        if not got:
            mc.log("clips: another clips run holds the lock - skipping this %s" % args.verb)
            return 0
        if args.verb == "retention":
            ok, reason = guard(settings)
            if not ok:  # nothing is deleted or rewritten on a volume that is not the clips area
                mc.log("clips: retention skipped - guard: %s" % reason, warning=True)
                return EXIT_GUARD
            try:
                result = apply_retention(settings, args.state_dir, now_ms, lambda: clips_quota(settings))
            except GuardStop as exc:
                mc.log("clips: retention STOPPED - guard: %s" % exc, warning=True)
                alert("clips-guard", "HomeHub: motion clips retention stopped (clips area)", str(exc))
                return EXIT_GUARD
            stop = quota_stopped(args.state_dir)
            used, limit = clips_quota(settings)
            if stop and (not limit or (used or 0) * 100 < limit * settings.quota_high_pct):
                _remove(stop_path(args.state_dir))
                mc.log("clips: retention made room - the quota stop is lifted")
            print(json.dumps(result, sort_keys=True))
            return 0
        stop = quota_stopped(args.state_dir)
        if stop:
            mc.log("clips: stopped since a full quota (%s); waiting for retention" % stop.get("why"), warning=True)
            return EXIT_QUOTA
        try:
            if args.verb == "cut":
                for cam in settings.cameras:
                    counts = cut_camera(settings, cam, args.state_dir, now_ms, alert=alert)
                    if counts["cut"] or counts["lost"] or counts["failed"]:
                        mc.log("clips [%s]: %s" % (cam.camera, json.dumps(counts, sort_keys=True)))
                return 0
            if args.verb == "encode":
                if not settings.encode:
                    mc.log("clips: CLIPS_ENCODE_ENABLED is not true - the copies stay as cut")
                    return 0
                done = 0
                started = time.monotonic()
                spent = lambda: done >= settings.max_per_run or time.monotonic() - started > ENCODE_RUN_SEC  # noqa: E731
                ok, reason = guard(settings)  # the reconcile below writes sidecars
                if not ok:
                    raise GuardStop(reason)
                for cam in settings.cameras:
                    if spent():
                        break
                    for day_dir, stem in encode_candidates(settings.root, cam.camera, reconcile=True):
                        # Bounded in count AND time: the batch shares the pipeline lock
                        # with the cut, which must not wait long behind it.
                        if spent():
                            break
                        if not os.path.exists(os.path.join(day_dir, stem + ".mp4")):
                            continue  # removed since the scan (room made for an earlier encode)
                        encode_clip(settings, cam.camera, day_dir, stem, quota_fn=lambda: clips_quota(settings))
                        done += 1
                return 0
        except QuotaStop as exc:
            set_quota_stop(args.state_dir, str(exc), now_ms)
            mc.log("clips: STOPPING - the clips area is full (%s); nothing is written elsewhere" % exc, warning=True)
            alert("clips-quota", "HomeHub: motion clips stopped (clips area full)",
                  "%s. Retention runs hourly and lifts the stop once there is room." % exc)
            return EXIT_QUOTA
        except GuardStop as exc:
            mc.log("clips: STOPPING - guard: %s" % exc, warning=True)
            alert("clips-guard", "HomeHub: motion clips stopped (clips area)", str(exc))
            return EXIT_GUARD
    return 2


if __name__ == "__main__":
    sys.exit(main())
