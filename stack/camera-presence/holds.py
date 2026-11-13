"""Holding room presence through people who lie down or sit still (measured 2026-11-11, evening).

Pure Python (3.8+), no model or I/O code. Two ideas from the day-1 review, both of which only
ever EXTEND a presence that the room detector already turned on; neither can turn it on:

1. The exit-aware hold. When the detector's ordinary hold (20 s) runs out, look at where the
   last counted detections were. If they were at a way out of the picture (the left edge by the
   half-wall and the door, the right edge, the bottom edge next to the lens) or a standing
   person's face, a departure was seen and absence is reported as before. If they were someone
   settled on the couch, keep presence for up to `long_hold_sec` more, because a person who
   lies down or bends over a laptop stops being detected without leaving.
2. The couch check. A body found on a main-stream crop of the couch (a larger input than the
   room detector's sub frame) while presence is on refreshes the hold, as room motion already
   does in the hub detector. It is read only while presence is on and nothing else has been
   seen for a few seconds, so it costs nothing while the room is empty or plainly occupied.

Coordinates are fractions of the full camera frame. A detection is (kind, score, (x, y)) with
the hub detector's anchors: a face's centre, a body's bottom centre.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

Anchor = Tuple[float, float]
Detection = Tuple[str, float, Anchor]


@dataclass
class HoldConfig:
    long_hold_sec: float = 0.0          # absence this long after a settled person's last detection; 0 = off
    exit_left: float = 0.12             # anchors left of this are at the half-wall and the door
    exit_right: float = 0.85            # right of this: the far right edge, past the treadmill column
    exit_bottom: float = 0.97           # a body cut off by the bottom edge is next to the lens
    standing_face_y: float = 0.45       # a face centre above this line is a standing person's
    look_back: int = 2                  # how many of the last positive samples are examined
    couch_conf: float = 0.0             # 0 disables the couch check
    couch_key: str = "body640"
    couch_after_sec: float = 6.0        # the couch crop is read only after this long with nothing seen
    couch_max_sec: float = 300.0        # and never holds longer than this after the last real detection
    couch_max_bottom: float = 1.0       # couch bodies whose bottom reaches this line are ignored (1.0 = off): on
                                        # day 1 a pile of clothes on the ottoman, cut off by the bottom edge, read as a body


def is_exit(det: Detection, cfg: HoldConfig) -> bool:
    kind, _score, (x, y) = det
    if x < cfg.exit_left or x > cfg.exit_right:
        return True
    if kind == "body" and y >= cfg.exit_bottom:
        return True
    if kind == "face" and y < cfg.standing_face_y:
        return True
    return False


def departure_seen(recent: Sequence[Sequence[Detection]], cfg: HoldConfig) -> bool:
    """recent: the counted detections of the last positive samples, oldest first. A departure
    is seen when any detection in the last `look_back` of them is at a way out."""
    if not recent:
        return True
    return any(is_exit(d, cfg) for sample in recent[-cfg.look_back:] for d in sample)


def couch_bodies(boxes: Sequence[Sequence[float]], cfg: HoldConfig, counts) -> List[Detection]:
    """Bodies from the couch crop (score, x0, y0, x1, y1) that count in the room zones."""
    out = []
    for b in boxes:
        if b[0] < cfg.couch_conf:
            continue
        a = (min(0.999, (b[1] + b[3]) / 2.0), min(0.999, b[4]))
        if b[4] >= cfg.couch_max_bottom:
            continue
        if counts(a):
            out.append(("body", b[0], a))
    return out


class HeldPresence:
    """The hub detector's PresenceState with the two extensions. `base` is an instance of
    camera_presence.PresenceState (or anything with its update()); the extensions act by
    refreshing its last_positive, exactly as its own motion hold does, so engage and the
    confirmation window are untouched."""

    def __init__(self, base, cfg: HoldConfig):
        self.base, self.cfg = base, cfg
        self.recent: List[List[Detection]] = []
        self.last_real: Optional[float] = None
        self.couch_reads = 0            # couch-crop reads the rule asked for
        self.couch_hits = 0

    def wants_couch(self, mono: float) -> bool:
        b = self.base
        return (self.cfg.couch_conf > 0 and b.present and self.last_real is not None
                and self.cfg.couch_after_sec <= mono - self.last_real <= self.cfg.couch_max_sec)

    def update(self, mono: float, utc: float, detections: Sequence[Detection], motion: float,
               couch: Optional[Sequence[Detection]] = None):
        b, cfg = self.base, self.cfg
        counted = [d for d in detections if b.s.zones.counts(d[2])]
        if counted:
            self.recent = (self.recent + [list(counted)])[-max(1, cfg.look_back):]
            self.last_real = mono
        refreshed = False
        if b.present and not counted and self.last_real is not None:
            if couch is not None and self.wants_couch(mono):
                self.couch_reads += 1
                if couch:
                    self.couch_hits += 1
                    refreshed = True
            quiet = mono - self.last_real
            # refreshing until quiet reaches long_hold - hold makes absence fall at long_hold
            if (cfg.long_hold_sec > 0 and quiet <= cfg.long_hold_sec - b.s.hold_sec
                    and not departure_seen(self.recent, cfg)):
                refreshed = True
            if refreshed:
                b.last_positive = mono
        events = b.update(mono, utc, list(detections), motion)
        for ev in events:
            if ev.get("event", "").endswith(".presence") and not ev.get("present"):
                self.recent, self.last_real = [], None
        return events
