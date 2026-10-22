#!/usr/bin/env python3
"""door_recorder.py - copy-only door segments on the scratch volume (SR-107).

ONE ffmpeg, from the relay's pinned image, reads the relay once and writes a
continuous copy-only MPEG-TS stream (`-c copy -an`: video only, never decoded)
to this process's stdin. THIS process - not ffmpeg - cuts it into ten-minute
files at video keyframes and is the only thing that ever opens a file on the
scratch volume, so the mount guard runs before EVERY new file, exactly as the
plan requires: a new segment is opened only after `homehub_scratch.guard`
says /srv/scratch is still the scratch filesystem, writable, inside door's
quota. ffmpeg's own segment muxer would open the next file itself, between two
guard checks.

    run          the service (homehub-door-recorder.service).
    continuity   downtime in a window (--hours back from now, or --since /
                 --until), computed from the segments themselves (file start
                 time + PTS duration, the open .part included): every gap
                 between segments and both uncovered edges of the window.
    verify       decode a set of segments with ffmpeg -v error (via the image).

Layout: /srv/scratch/door/YYYY-MM-DD/door-YYYYmmddTHHMMSSZ.ts (UTC, so a DST
change can neither collide two files nor leave an hour out). A file being
written is `.ts.part` and becomes `.ts` when it is closed; a `.part` left by a
crash is finalised at the next start (it is a valid, shorter recording).

THE CUT. MPEG-TS from ffmpeg marks a video keyframe's first packet with the
adaptation-field random_access_indicator. A segment starts on such a packet,
preceded by the latest PAT and PMT packets, so every file decodes from its
first byte. A cut is taken at the first keyframe after each multiple of
DOOR_RECORDER_SEGMENT_SEC on the UTC clock.

THE BUDGET. Pruning is by whole segments, oldest first, before each new file,
so the directory stays under DOOR_RECORDER_CAP_MIB with room for the next
segment; the project quota on door/ (8 GiB) is the hard wall a runaway write
hits instead. EDQUOT or ENOSPC from a write stops the recorder cleanly
(exit 75, alert, no restart: RestartPreventExitStatus) - it never writes
anywhere else. A guard refusal is exit 76, also no restart; BindsTo= on the
mount unit stops the recorder anyway when the volume goes.

Python 3.8-compatible. Implements: SR-107, LLR-1230..LLR-1236.
"""

from __future__ import annotations

import argparse
import calendar
import collections
import errno
import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "monitoring"))
sys.path.insert(0, os.path.join(HERE, "..", "scratch"))
sys.path.insert(0, HERE)
import monitor_common as mc  # noqa: E402
import homehub_scratch as scratch  # noqa: E402
import door_relay  # noqa: E402

TS = 188
SYNC = 0x47
VIDEO_STREAM_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24}  # MPEG-1/2, MPEG-4, H.264, HEVC
CONTAINER = "homehub-door-recorder"
EXIT_QUOTA, EXIT_GUARD = 75, 76
MIB = 1024 * 1024
SEGMENT_RE = re.compile(r"door-(\d{8}T\d{6})Z(?:-\d+)?\.ts$")


# ── pure core: MPEG-TS packets ───────────────────────────────────────────────


def packet_fields(pkt: bytes) -> Tuple[int, bool, bool]:
    """(pid, payload_unit_start, random_access) of one 188-byte TS packet."""
    pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
    pusi = bool(pkt[1] & 0x40)
    afc = (pkt[3] >> 4) & 0x3
    rai = bool(afc & 0x2 and pkt[4] > 0 and pkt[5] & 0x40)
    return pid, pusi, rai


def _section(pkt: bytes) -> Optional[bytes]:
    """The PSI section starting in a PUSI packet (pointer field honoured)."""
    afc = (pkt[3] >> 4) & 0x3
    start = 4 + (1 + pkt[4] if afc & 0x2 else 0)
    if not afc & 0x1 or start >= TS:
        return None
    start += 1 + pkt[start]
    return pkt[start:] if start < TS else None


def parse_pat(pkt: bytes) -> Optional[int]:
    """The first program's PMT pid from a PAT packet, or None."""
    sec = _section(pkt)
    if not sec or sec[0] != 0x00 or len(sec) < 12:
        return None
    length = ((sec[1] & 0x0F) << 8) | sec[2]
    for i in range(8, min(3 + length - 4, len(sec) - 3), 4):
        program = (sec[i] << 8) | sec[i + 1]
        if program != 0:
            return ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
    return None


def parse_pmt(pkt: bytes) -> Optional[int]:
    """The first video elementary pid from a PMT packet, or None."""
    sec = _section(pkt)
    if not sec or sec[0] != 0x02 or len(sec) < 16:
        return None
    length = ((sec[1] & 0x0F) << 8) | sec[2]
    end = min(3 + length - 4, len(sec))
    i = 12 + (((sec[10] & 0x0F) << 8) | sec[11])
    while i + 5 <= end:
        stream_type = sec[i]
        pid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
        if stream_type in VIDEO_STREAM_TYPES:
            return pid
        i += 5 + (((sec[i + 3] & 0x0F) << 8) | sec[i + 4])
    return None


def pes_pts(pkt: bytes) -> Optional[int]:
    """The 33-bit PTS of a PES that starts in this packet, or None."""
    afc = (pkt[3] >> 4) & 0x3
    i = 4 + (1 + pkt[4] if afc & 0x2 else 0)
    if not afc & 0x1 or i + 14 > TS or pkt[i:i + 3] != b"\x00\x00\x01":
        return None
    if not pkt[i + 7] & 0x80:
        return None
    b = pkt[i + 9:i + 14]
    return (((b[0] >> 1) & 0x07) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


class Packetiser:
    """Arbitrary byte chunks in, whole aligned TS packets out; resyncs on loss.

    Bytes before the first sync are dropped; a lost sync searches for a 0x47
    that repeats 188 bytes later, so a torn pipe read never misaligns a file.
    """

    def __init__(self):
        self.buf = bytearray()
        self.dropped = 0

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        self.buf.extend(chunk)
        i = 0
        n = len(self.buf)
        while n - i >= TS:
            if self.buf[i] == SYNC and (n - i < 2 * TS or self.buf[i + TS] == SYNC):
                yield bytes(self.buf[i:i + TS])
                i += TS
                continue
            i += 1
            self.dropped += 1
        del self.buf[:i]


class Splitter:
    """Decide, packet by packet, where segments start (pure; no I/O).

    feed(pkt, now) returns ("drop" | "start" | "write", header) where header is
    the PAT+PMT bytes to write before a "start" packet. A segment starts on the
    first video keyframe once the stream's PAT and PMT are known, then again on
    the first keyframe at or after each `segment_sec` boundary of `now`.
    """

    def __init__(self, segment_sec: int):
        self.segment_sec = segment_sec
        self.pat = self.pmt = None  # type: Optional[bytes]
        self.pmt_pid = self.video_pid = None  # type: Optional[int]
        self.next_cut = None  # type: Optional[float]

    def boundary_after(self, now: float) -> float:
        return (int(now) // self.segment_sec + 1) * self.segment_sec

    def feed(self, pkt: bytes, now: float) -> Tuple[str, bytes]:
        pid, pusi, rai = packet_fields(pkt)
        if pid == 0 and pusi:
            pmt_pid = parse_pat(pkt)
            if pmt_pid is not None:
                self.pat, self.pmt_pid = pkt, pmt_pid
        elif self.pmt_pid is not None and pid == self.pmt_pid and pusi:
            video = parse_pmt(pkt)
            if video is not None:
                self.pmt, self.video_pid = pkt, video
        keyframe = pusi and rai and pid == self.video_pid and self.pat is not None and self.pmt is not None
        if keyframe and (self.next_cut is None or now >= self.next_cut):
            self.next_cut = self.boundary_after(now)
            return "start", (self.pat or b"") + (self.pmt or b"")
        return ("drop" if self.next_cut is None else "write"), b""


# ── pure core: names, pruning, continuity ────────────────────────────────────


def segment_name(now: float) -> Tuple[str, str]:
    """(day directory, file name) for a segment starting at `now` (UTC)."""
    t = time.gmtime(now)
    return time.strftime("%Y-%m-%d", t), time.strftime("door-%Y%m%dT%H%M%SZ.ts", t)


def segment_start(name: str) -> Optional[float]:
    """The UTC epoch second a segment's file name says it started, or None.
    A `.ts.part` (the segment being written) parses as its final name."""
    m = SEGMENT_RE.search(name[:-len(".part")] if name.endswith(".ts.part") else name)
    if not m:
        return None
    return float(calendar.timegm(time.strptime(m.group(1), "%Y%m%dT%H%M%S")))


def list_segments(root: str) -> List[Tuple[str, int]]:
    """[(path, size)] of finished segments under root, oldest first (by name)."""
    out = []
    try:
        days = sorted(d for d in os.listdir(root) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))
    except OSError:
        return out
    for day in days:
        ddir = os.path.join(root, day)
        try:
            names = sorted(n for n in os.listdir(ddir) if SEGMENT_RE.search(n))
        except OSError:
            continue
        for name in names:
            path = os.path.join(ddir, name)
            try:
                out.append((path, os.path.getsize(path)))
            except OSError:
                pass
    return out


def prune_plan(segments: List[Tuple[str, int]], cap_bytes: int, headroom: int, current: int = 0) -> List[str]:
    """The oldest whole segments to delete so that what remains, plus the file
    being written and room for the next, fits under the cap. Never a partial
    file, never the file being written (it is not in `segments`)."""
    total = sum(size for _p, size in segments) + current
    doomed = []
    for path, size in segments:
        if total + headroom <= cap_bytes:
            break
        doomed.append(path)
        total -= size
    return doomed


def prune(root: str, cap_bytes: int, headroom: int, current: int = 0) -> List[str]:
    """Apply prune_plan; remove day directories it empties. Returns what it removed."""
    removed = []
    for path in prune_plan(list_segments(root), cap_bytes, headroom, current):
        try:
            os.unlink(path)
            removed.append(path)
        except FileNotFoundError:
            pass
        day = os.path.dirname(path)
        try:
            os.rmdir(day)
        except OSError:
            pass
    return removed


def ts_duration(path: str, max_bytes: int = 64 * MIB) -> Optional[float]:
    """Seconds between the first and last video PTS in a TS file (wrap-safe)."""
    split = Splitter(10 ** 9)
    first = last = None
    with open(path, "rb") as handle:
        pk = Packetiser()
        read = 0
        while read < max_bytes:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            read += len(chunk)
            for pkt in pk.feed(chunk):
                split.feed(pkt, 0.0)
                pid, pusi, _rai = packet_fields(pkt)
                if pusi and pid == split.video_pid:
                    pts = pes_pts(pkt)
                    if pts is not None:
                        if first is None:
                            first = pts
                        last = pts
    if first is None or last is None:
        return None
    return ((last - first) % (1 << 33)) / 90000.0


EDGE_TOLERANCE = 15.0


def continuity(segments: List[Tuple[float, float]], window_start: float, window_end: float,
               tolerance: float = 2.0, edge_tolerance: float = EDGE_TOLERANCE) -> Dict[str, object]:
    """Downtime in [window_start, window_end] from segments [(start, duration)].

    The segments are clipped to the window. Downtime is every stretch they do
    not cover: between consecutive segments over `tolerance` (a relay or
    recorder restart), and at either EDGE over `edge_tolerance` - before the
    first segment (the recorder was not yet recording) and after the last (it
    stopped, or nothing has been written since). The edge allowance is wider
    because a run's first keyframe and the open segment's last frame trail the
    wall clock by the source's latency and up to a GOP. No segment in the
    window is the whole window down. The rule for the 24 h acceptance: at most
    3 gaps and 5 minutes in total.
    Implements: LLR-1235.
    """
    rows = sorted((s, d) for s, d in segments if s < window_end and s + d > window_start)
    gaps: List[Dict[str, object]] = []
    cursor = window_start
    for i, (s0, d0) in enumerate(rows):
        gap = s0 - cursor
        if gap > (edge_tolerance if i == 0 else tolerance):
            gaps.append({"kind": "leading" if i == 0 else "between", "after": cursor, "seconds": round(gap, 1)})
        cursor = max(cursor, s0 + d0)
    tail = window_end - cursor
    if tail > edge_tolerance:
        gaps.append({"kind": "trailing" if rows else "empty", "after": cursor, "seconds": round(tail, 1)})
    total = sum(g["seconds"] for g in gaps)
    covered = sum(max(0.0, min(s0 + d0, window_end) - max(s0, window_start)) for s0, d0 in rows)
    return {"segments": len(rows), "window_hours": round((window_end - window_start) / 3600.0, 2),
            "covered_hours": round(covered / 3600.0, 2), "gaps": len(gaps),
            "downtime_seconds": round(total, 1), "gap_list": gaps,
            "pass": bool(rows) and len(gaps) <= 3 and total <= 300}


def list_parts(root: str) -> List[str]:
    """Paths of the `.ts.part` files under root (the segment being written)."""
    out = []
    try:
        days = sorted(d for d in os.listdir(root) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))
    except OSError:
        return out
    for day in days:
        try:
            out += [os.path.join(root, day, n) for n in sorted(os.listdir(os.path.join(root, day)))
                    if n.endswith(".ts.part") and segment_start(n) is not None]
        except OSError:
            pass
    return out


def parse_when(text: str) -> float:
    """An epoch second, or UTC ISO 8601 YYYY-mm-ddTHH:MM:SSZ, as an epoch second."""
    if re.fullmatch(r"[0-9]+(\.[0-9]+)?", text):
        return float(text)
    return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))


# ── the service ──────────────────────────────────────────────────────────────


class RecorderSettings:
    """DOOR_RECORDER_SEGMENT_SEC (600), DOOR_RECORDER_CAP_MIB (7680, kept under
    door's 8192 MiB quota), DOOR_RECORDER_STALL_SEC (30: no bytes from ffmpeg for
    this long ends the run, and systemd restarts it).  [stack/.env + override]"""

    def __init__(self, env: Dict[str, str]):
        self.segment_sec = mc.knob_int(env, "DOOR_RECORDER_SEGMENT_SEC", 600, 10, 3600)
        self.cap_mib = mc.knob_int(env, "DOOR_RECORDER_CAP_MIB", 7680, 16, 1 << 22)
        self.stall_sec = mc.knob_int(env, "DOOR_RECORDER_STALL_SEC", 30, 5, 600)


class Recorder:
    """The supervisor: guard, prune, open, write, rotate, stop. I/O shell."""

    def __init__(self, env_file: str, root: str, settings: RecorderSettings,
                 scratch_settings, paths=None, alert=None, clock=time.time):
        self.env_file, self.root, self.s = env_file, root, settings
        self.scratch_settings, self.paths = scratch_settings, paths
        self.alert = alert or self._alert
        self.clock = clock
        self.handle = None
        self.part = None  # type: Optional[str]
        self.dir_fd = None  # type: Optional[int]
        self.written = 0
        self.first_frame_start = 0  # offset of the segment's first video PES
        self.last_frame_start = 0   # offset of the latest video PES start
        # Recent video PES start offsets (about 100 s at 10 fps): the truncation
        # point is the latest one the disk actually holds.
        self.frame_starts = collections.deque(maxlen=1024)
        self.last_size = 64 * MIB
        self.stopping = False

    def _alert(self, key: str, title: str, body: str) -> None:
        mc.send_alert(key, "high", title, body, self.env_file, mc.DEFAULT_STATE_DIR, log_tag="homehub-door-recorder")

    def guard(self) -> Tuple[bool, str]:
        return scratch.guard("door", self.scratch_settings, self.paths)

    def finalise(self, at_keyframe: bool = False) -> None:
        """Close the open segment and give it its final name.

        At a rotation (`at_keyframe`) the file ends just before the next
        keyframe, so its last frame is whole. Any OTHER end - the stream
        stopping, a stop request, a full quota - can leave a frame cut short,
        which `ffmpeg -v error` reports; so the file is truncated back to the
        last video PES start that is actually on disk (the frame that never
        finished goes). A file left with no whole frame is removed. The file is
        unbuffered, so what `written` counts is exactly what the disk holds.
        Implements: LLR-1232.
        """
        if self.handle is None:
            return
        fd = self.handle.fileno()
        if not at_keyframe:
            cut = max((o for o in self.frame_starts if o <= self.written), default=0)
            try:
                os.ftruncate(fd, cut)
                self.written = cut
            except OSError:
                pass
        try:
            os.fsync(fd)
        except OSError:
            pass
        self.handle.close()
        self.handle = None
        final = self.part[:-len(".part")]
        # Renamed through the directory's own fd, opened with the file: after a
        # lazy unmount the PATH names the bare mount point on /, but the fd
        # still names the scratch filesystem, so the segment is closed where it
        # was written and nothing is ever created on / (LLR-1232). os.rename,
        # not os.replace: only rename takes dir fds (both overwrite on POSIX).
        part_name, final_name = os.path.basename(self.part), os.path.basename(final)
        dfd, self.dir_fd = self.dir_fd, None
        try:
            if not at_keyframe and self.written <= self.first_frame_start:
                if dfd is not None:
                    os.unlink(part_name, dir_fd=dfd)
                else:
                    os.unlink(self.part)
                mc.log("door recorder: dropped %s (no whole frame before the stream ended)" % final)
            else:
                if dfd is not None:
                    os.rename(part_name, final_name, src_dir_fd=dfd, dst_dir_fd=dfd)
                else:
                    os.replace(self.part, final)
                self.last_size = max(self.written, 1)
                mc.log("door recorder: closed %s (%.1f MiB)" % (final, self.written / MIB))
        finally:
            if dfd is not None:
                os.close(dfd)
            self.part = None

    def recover_parts(self) -> None:
        """A `.part` from a killed run is a valid shorter recording: finalise it."""
        for day in os.listdir(self.root) if os.path.isdir(self.root) else []:
            ddir = os.path.join(self.root, day)
            if not os.path.isdir(ddir):
                continue
            for name in os.listdir(ddir):
                if name.endswith(".ts.part"):
                    os.replace(os.path.join(ddir, name), os.path.join(ddir, name[:-len(".part")]))
                    mc.log("door recorder: finalised %s left by an earlier run" % name)

    def open_segment(self, now: float) -> None:
        """Guard, prune, then open the next file. Raises SystemExit on a refusal."""
        ok, reason = self.guard()
        if not ok:
            mc.log("door recorder: STOPPING before the next segment - scratch guard: %s" % reason, warning=True)
            self.alert("door-recorder-guard", "HomeHub: door recorder stopped (scratch volume)", reason)
            raise SystemExit(EXIT_GUARD)
        headroom = max(int(self.last_size * 1.5), 16 * MIB)
        for path in prune(self.root, self.s.cap_mib * MIB, headroom):
            mc.log("door recorder: pruned %s (cap %d MiB)" % (path, self.s.cap_mib))
        day, name = segment_name(now)
        ddir = os.path.join(self.root, day)
        try:
            os.makedirs(ddir, mode=0o750, exist_ok=True)
            # Two runs starting in the same second (a fast restart) must not
            # overwrite each other's segment: the second gets a -N suffix.
            n = 0
            while True:
                stem = name[:-3] + ("" if n == 0 else "-%d" % n)
                self.part = os.path.join(ddir, stem + ".ts.part")
                if not os.path.exists(self.part) and not os.path.exists(self.part[:-5]):
                    break
                n += 1
            # Unbuffered: no hidden buffer can be flushed after a truncation.
            self.handle = open(self.part, "xb", buffering=0)
            if os.name == "posix":
                self.dir_fd = os.open(ddir, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            self.quota_stop(exc, ddir)
            raise
        self.written = self.first_frame_start = self.last_frame_start = 0
        self.frame_starts.clear()

    def quota_stop(self, exc: OSError, where: str) -> None:
        """EDQUOT/ENOSPC anywhere (a write, a new file, a new day directory):
        close what is open, alert, exit 75. Never a retry, never another path.
        Any other error returns, for the caller to raise. Implements: LLR-1234."""
        if exc.errno not in (getattr(errno, "EDQUOT", -1), errno.ENOSPC):
            return
        what = errno.errorcode.get(exc.errno)
        mc.log("door recorder: STOPPING - %s at %s; nothing is written anywhere else" % (what, where), warning=True)
        try:
            self.finalise()
        except OSError:
            pass
        self.alert("door-recorder-quota", "HomeHub: door recorder stopped (%s)" % what,
                   "door/ on the scratch volume is full; the recorder will not restart by itself.")
        raise SystemExit(EXIT_QUOTA)

    def write(self, data: bytes) -> None:
        """Write all of `data` (an unbuffered file may take it in parts)."""
        view = memoryview(data)
        try:
            while view:
                n = self.handle.write(view)
                self.written += n
                view = view[n:]
        except OSError as exc:
            self.quota_stop(exc, self.part or self.root)
            raise

    def consume(self, stream, stall_sec: Optional[int] = None) -> int:
        """Read the TS stream until EOF, a stall or a stop; returns the exit code."""
        stall = stall_sec or self.s.stall_sec
        pk, split = Packetiser(), Splitter(self.s.segment_sec)
        fd = stream.fileno()
        last_data = time.monotonic()
        while not self.stopping:
            ready, _w, _x = select.select([fd], [], [], 1.0)
            if not ready:
                if time.monotonic() - last_data > stall:
                    mc.log("door recorder: no data from the relay for %ds - ending this run" % stall, warning=True)
                    return 1
                continue
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                mc.log("door recorder: the relay stream ended", warning=True)
                return 1
            last_data = time.monotonic()
            now = self.clock()
            out = bytearray()
            for pkt in pk.feed(chunk):
                action, header = split.feed(pkt, now)
                if action == "start":
                    if out:
                        self.write(bytes(out))
                        out.clear()
                    self.finalise(at_keyframe=True)
                    self.open_segment(now)
                    out.extend(header)
                    self.first_frame_start = self.last_frame_start = len(header)
                    self.frame_starts.append(len(header))
                    out.extend(pkt)
                elif action == "write":
                    pid, pusi, _rai = packet_fields(pkt)
                    if pusi and pid == split.video_pid:
                        self.last_frame_start = self.written + len(out)
                        self.frame_starts.append(self.last_frame_start)
                    out.extend(pkt)
            if out and self.handle is not None:
                self.write(bytes(out))
        return 0


def ffmpeg_argv(env_file_path: str) -> List[str]:
    """The copy-only reader, in the relay's pinned image. The source URL is in an
    env file (0600) and expanded inside the container: never in argv."""
    script = ('exec ffmpeg -nostdin -hide_banner -loglevel warning -rtsp_transport tcp '
              '-timeout 15000000 -i "$DOOR_URL" -map 0:v:0 -c copy -an -f mpegts pipe:1')
    return ["docker", "run", "--rm", "--name", CONTAINER, "--network", "host", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
            "--memory", "256m", "--log-driver", "none", "--env-file", env_file_path,
            "--entrypoint", "/bin/sh", door_relay.image_ref(), "-c", script]


def run_service(env_file: str) -> int:
    """homehub-door-recorder.service's ExecStart."""
    knobs = mc.read_knobs(env_file)
    rs = RecorderSettings(knobs)
    ss = scratch.Settings(knobs)
    relay = door_relay.RelaySettings(env_file)
    root = os.path.join(scratch.MOUNTPOINT, "door")
    rec = Recorder(env_file, root, rs, ss)
    ok, reason = rec.guard()
    if not ok:
        mc.log("door recorder: REFUSING to start - scratch guard: %s" % reason, warning=True)
        rec.alert("door-recorder-guard", "HomeHub: door recorder refused to start (scratch volume)", reason)
        return EXIT_GUARD
    rec.recover_parts()
    run_dir = os.environ.get("RUNTIME_DIRECTORY", "/run/homehub-door-recorder")
    os.makedirs(run_dir, mode=0o700, exist_ok=True)
    fd, envpath = tempfile.mkstemp(dir=run_dir, prefix="source-", suffix=".env")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("DOOR_URL=%s\n" % relay.consumer_url())
    subprocess.run(["docker", "rm", "-f", CONTAINER], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60)
    proc = subprocess.Popen(ffmpeg_argv(envpath), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def stop(_sig, _frm):
        rec.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    import threading

    def relay_stderr():
        for line in proc.stderr:
            mc.log("door recorder: ffmpeg: " + door_relay.redact(line.decode("utf-8", "replace").rstrip(), relay))
    threading.Thread(target=relay_stderr, daemon=True).start()
    mc.log("door recorder: reading /%s from the relay at %s:%d; %ds segments into %s, cap %d MiB"
           % (relay.stream, relay.lan_ip, relay.rtsp_port, rs.segment_sec, root, rs.cap_mib))
    code = 1
    try:
        code = rec.consume(proc.stdout)
    finally:
        try:
            rec.finalise()
        except OSError as exc:
            mc.log("door recorder: could not finalise the open segment: %s" % exc, warning=True)
        subprocess.run(["docker", "rm", "-f", CONTAINER], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.unlink(envpath)
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", default=mc.DEFAULT_ENV_FILE)
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("run")
    c = sub.add_parser("continuity")
    c.add_argument("--root", default=os.path.join(scratch.MOUNTPOINT, "door"))
    c.add_argument("--hours", type=float, default=24.0, help="the window: this many hours back from --until")
    c.add_argument("--since", help="window start instead (epoch or UTC YYYY-mm-ddTHH:MM:SSZ), e.g. the "
                                   "moment the recorder was started for a test")
    c.add_argument("--until", help="window end (default now)")
    v = sub.add_parser("verify")
    v.add_argument("paths", nargs="+")
    args = p.parse_args(argv)
    if args.verb == "run":
        try:
            return run_service(args.env_file)
        except door_relay.Invalid as exc:
            mc.log("door recorder: REFUSED - %s" % exc, warning=True)
            return 2
    if args.verb == "continuity":
        try:
            window_end = parse_when(args.until) if args.until else time.time()
            window_start = parse_when(args.since) if args.since else window_end - args.hours * 3600
        except ValueError:
            mc.log("door recorder: --since/--until must be an epoch second or YYYY-mm-ddTHH:MM:SSZ", warning=True)
            return 2
        rows = []
        # Every finished segment that can overlap the window (one that started
        # up to an hour before it may still cover its first minutes), and the
        # open .part, which is the recording in progress.
        paths = [p for p, _size in list_segments(args.root)] + list_parts(args.root)
        for path in paths:
            start = segment_start(os.path.basename(path))
            if start is None or start < window_start - 3600 or start >= window_end:
                continue
            duration = ts_duration(path)
            rows.append((start, duration or 0.0))
        result = continuity(rows, window_start, window_end)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["pass"] else 1
    if args.verb == "verify":
        bad = 0
        for path in args.paths:
            done = subprocess.run(
                ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                 "--log-driver", "none", "-v", "%s:/seg.ts:ro" % os.path.abspath(path),
                 "--entrypoint", "ffmpeg", door_relay.image_ref(), "-v", "error", "-i", "/seg.ts", "-f", "null", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=600)
            errors = [l for l in done.stderr.splitlines() if l.strip()]
            bad += 1 if (done.returncode or errors) else 0
            print("%s %s rc=%d errors=%d" % ("OK " if not (done.returncode or errors) else "BAD", path,
                                            done.returncode, len(errors)))
        return 1 if bad else 0
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
