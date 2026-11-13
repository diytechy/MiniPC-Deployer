#!/usr/bin/env python3
"""camera_presence.py - room presence from a relayed camera (SR-157..SR-160).

HomeHub LIVING_ROOM_PRESENCE_PLAN_2026-11-09 section 4. One instance per camera
(homehub-camera-presence@<camera>.service) reads one frame every two seconds
(FPS, 0.5) from the camera's own relay (stack/camera-relay), decides whether
someone is in the room OUTSIDE the excluded zones (the treadmill), and
publishes state changes only:

    living_room.presence  {present, zone, since, confidence}
    living_room.engage    {engaged, zone, since}     (only with an engage zone)

on a small LAN feed (IF-081) and in a status file. It never writes a frame to
disk, keeps no identity, and nothing it publishes carries a picture, a box or a
zone. The audio is never decoded (-an), and the relay withholds it from a
consumer that does not ask anyway.

THE METHOD (HomeHub METHOD_PRESENCE_BODY_CLASSIFIER_2026-09-15; the settings
the Owner adopted 2026-11-12, HomeHub DECISIONS_RATIFIED "Ratified 2026-11-12",
measured offline over two recorded days by HomeHub scripts/livingroom): never
drop presence while someone is there. Per sample: a head/face detector
(faceyolo) on the room's crop, and a body detector (yolov8n_320 at 0.25,
person) when face found nobody (`cascade`). A detection counts only when its
anchor - a face box's centre, a body box's bottom centre - is inside the room
polygon and outside every exclude polygon. Presence turns on after
CONFIRM_COUNT positive samples out of CONFIRM_WINDOW, and off after HOLD_SEC
(30) with none. Room motion (a grey difference, excluded zones masked) may
refresh the hold while present but never turns presence on: a TV picture moves,
so the TV belongs in an exclude polygon. A frame darker than DARK_LEVEL (the
camera's black frames while it switches to infrared) is skipped, not read as
"nobody"; a frame whose WHOLE picture changed by WHOLE_MOTION_MAX or more (a
light switched) carries no motion.

THE COUCH CHECK (holds.py, moved unchanged from the offline build): a person
lying down or still face-down stops being detected without leaving. While
presence is on and nothing has counted for COUCH_AFTER_SEC (6), the newest
main-stream keyframe of the couch (COUCH_CROP, from the relay's /<stream>-main,
video only) is read by COUCH_MODEL (yolov8n_640) at COUCH_CONF; a body that
counts refreshes the hold. Bodies reaching COUCH_MAX_BOTTOM (cut off by the
bottom edge, next to the lens) are ignored. It never turns presence on.

MODELS are not in this public repository: models.manifest.json pins each file's
sha256 and the run refuses other bytes. THE RUNTIME is onnxruntime in
/opt/homehub-presence/venv (install-presence-runtime.sh); OpenCV 4.6, the hub's
apt build, cannot load these graphs (measured on the hub 2026-11-09).

Verbs (all take --camera C):
    condition   exit 0 only when CAMERA_<C>_PRESENCE_ENABLED is exactly true
                (the unit's ExecCondition).
    check       what the detector would run with; never prints a polygon.
    run         the service.
    still       ONE frame from the relay as a JPEG at --out (0600), to draw the
                zones on with the Owner. Nothing keeps it; delete it after.
    cost        CPU of the running detector (unit + decoder) and PSI, against
                CAMERA_<C>_PRESENCE_BUDGET_PCT.

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: SR-157..SR-160, LLR-1496..LLR-1511, LLR-1524..LLR-1529.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "monitoring"))
sys.path.insert(0, os.path.join(HERE, "..", "door"))
sys.path.insert(0, os.path.join(HERE, "..", "camera-relay"))
sys.path.insert(0, os.path.join(HERE, "..", "camera-motion"))
import monitor_common as mc  # noqa: E402
import door_relay  # noqa: E402
import camera_relay  # noqa: E402
import event_feed as feed  # noqa: E402
import holds  # noqa: E402

STATE_DIR = "/var/lib/homehub-presence"
MANIFEST_FILE = os.path.join(HERE, "models.manifest.json")
DEFAULT_MODEL_DIR = "/opt/homehub-presence/models"
FRAME_W, FRAME_H = 640, 360           # the decoder's output, RGB24
MOTION_W, MOTION_H = 160, 90
MOTION_LEVEL = 18                     # grey levels: the door engine's threshold
EXIT_REFUSED = 78
STATUS_EVERY_SEC = 60.0
BODY_MODES = ("cascade", "always", "off")
COCO_PERSON = 0
EDGE = 0.999                          # anchors are clamped to [0, EDGE]
WHOLE_FRAME = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
COUCH_W = 960                         # the couch crop's width, as the offline build read it
COUCH_MAX_AGE_SEC = 4.5               # a couch keyframe older than this is no data (the offline replay's rule)
COUCH_RESTART_SEC = 15.0              # a couch decoder that ended is started again after this
MIN_CROP = 0.1                        # the couch crop's least width and height, fractions of the frame
NO_CAP = 1e9                          # COUCH_MAX_SEC 0: the couch check holds without a cap


class Invalid(ValueError):
    """A configuration field is missing or malformed; the message names the KEY only."""


def knob(camera: str, suffix: str) -> str:
    return "CAMERA_%s_PRESENCE_%s" % (camera.upper(), suffix)


# ── zones ────────────────────────────────────────────────────────────────────

Point = Tuple[float, float]
Polygon = List[Point]


def parse_polygon(text: str, key: str) -> Polygon:
    """`x,y;x,y;...` in 0..1, at least three points. Raises Invalid naming the key.

    Implements: LLR-1496.
    """
    points = []
    for part in [p for p in (text or "").strip().split(";") if p.strip()]:
        m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*,\s*([0-9]*\.?[0-9]+)\s*", part)
        if not m:
            raise Invalid("%s holds a point that is not x,y" % key)
        x, y = float(m.group(1)), float(m.group(2))
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise Invalid("%s holds a point outside 0..1" % key)
        points.append((x, y))
    if len(points) < 3:
        raise Invalid("%s needs at least three points" % key)
    return points


def parse_polygons(text: str, key: str) -> List[Polygon]:
    """`|`-separated polygons; empty is none."""
    return [parse_polygon(part, key) for part in (text or "").split("|") if part.strip()]


Crop = Tuple[float, float, float, float]


def parse_crop(text: str, key: str) -> Crop:
    """`x0,y0,x1,y1` in 0..1 with x0 < x1 and y0 < y1. Raises Invalid naming the key.

    Implements: LLR-1525.
    """
    parts = [p.strip() for p in (text or "").split(",")]
    if len(parts) != 4 or not all(re.fullmatch(r"[0-9]*\.?[0-9]+", p) for p in parts):
        raise Invalid("%s must be x0,y0,x1,y1" % key)
    x0, y0, x1, y1 = (float(p) for p in parts)
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        raise Invalid("%s must lie inside 0..1 with x0 < x1 and y0 < y1" % key)
    if x1 - x0 < MIN_CROP or y1 - y0 < MIN_CROP:
        # A sliver would make the 960-wide image absurdly tall (review 2026-11-13).
        raise Invalid("%s must be at least %g of the frame each way" % (key, MIN_CROP))
    return x0, y0, x1, y1


def inside(point: Point, polygon: Polygon) -> bool:
    """Even-odd ray casting; a point on an edge may fall either way."""
    x, y = point
    hit = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                hit = not hit
    return hit


def is_whole_frame(polygon: Polygon) -> bool:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return min(xs) <= 0.001 and min(ys) <= 0.001 and max(xs) >= 0.999 and max(ys) >= 0.999 and len(polygon) == 4


class Zones:
    """The room, its exclusions and the optional engage area (normalised).

    Implements: LLR-1496.
    """

    def __init__(self, room: Polygon, exclude: List[Polygon], engage: Optional[Polygon]):
        self.room, self.exclude, self.engage = room, exclude, engage

    def counts(self, point: Point) -> bool:
        return inside(point, self.room) and not any(inside(point, p) for p in self.exclude)

    def engaged(self, point: Point) -> bool:
        return self.engage is not None and self.counts(point) and inside(point, self.engage)

    def crop(self, pad: float = 0.04) -> Tuple[float, float, float, float]:
        """The room's bounding box, padded, clamped: (x0, y0, x1, y1) in 0..1."""
        xs = [p[0] for p in self.room]
        ys = [p[1] for p in self.room]
        return (max(0.0, min(xs) - pad), max(0.0, min(ys) - pad), min(1.0, max(xs) + pad), min(1.0, max(ys) + pad))

    def motion_mask(self, width: int, height: int):
        """A boolean array (height, width): in the room and in no exclusion."""
        import numpy as np
        mask = np.zeros((height, width), dtype=bool)
        for row in range(height):
            y = (row + 0.5) / height
            for col in range(width):
                mask[row, col] = self.counts(((col + 0.5) / width, y))
        return mask


# ── settings ─────────────────────────────────────────────────────────────────


class PresenceSettings:
    """Camera C's presence knobs, read from the stack .env.

    Contract:
      Config:  CAMERA_<C>_PRESENCE_ENABLED (false); _RELAY (C: whose relay it
               reads); _ROOM_POLYGON (required; the whole frame is refused),
               _EXCLUDE_POLYGONS (|-separated), _ENGAGE_POLYGON (empty: no
               engage events); _FACE_MODEL (faceyolo_320.onnx), _FACE_CONF
               (0.15), _BODY_MODEL (yolov8n_320.onnx), _BODY_CONF (0.25),
               _BODY_MODE (cascade|always|off); _CONFIRM_COUNT (2) of
               _CONFIRM_WINDOW (3); _HOLD_SEC (30); _ENGAGE_SEC (20);
               _FPS (0.5); _DARK_LEVEL (8; 0 = off); _WHOLE_MOTION_MAX (0.5;
               1 = off); _COUCH_CROP (x0,y0,x1,y1 of the main frame; empty:
               no couch check), _COUCH_MODEL (yolov8n_640.onnx), _COUCH_CONF
               (0.25), _COUCH_AFTER_SEC (6), _COUCH_MAX_BOTTOM (0.97),
               _COUCH_MAX_SEC (0 = no cap);
               _MOTION_HOLDS (true), _MOTION_MIN_RATIO (0.004); _STALL_SEC (20);
               _FEED_PORT (0 = off), _FEED_PEERS (space-separated IPv4;
               default MAIN_BOX_IP); _BUDGET_PCT (30); _MODEL_DIR
               (/opt/homehub-presence/models); _EVENT_PREFIX (living_room).
               [stack/.env]
      Raises:  Invalid naming the key, never a value.
    Implements: LLR-1497.
    """

    def __init__(self, env: Dict[str, str], camera: str):
        self.camera = camera_relay.camera_name(camera)
        k = lambda s: knob(camera, s)  # noqa: E731

        def text(suffix: str, default: str = "") -> str:
            return env.get(k(suffix), "").strip() or default

        def number(suffix: str, default: float, low: float, high: float) -> float:
            raw = env.get(k(suffix), "").strip()
            if not raw:
                return default
            if not re.fullmatch(r"[0-9]*\.?[0-9]+", raw) or not low <= float(raw) <= high:
                raise Invalid("%s must be a number in %g..%g" % (k(suffix), low, high))
            return float(raw)

        self.enabled = mc.knob_bool(env, k("ENABLED"), False)
        self.relay_camera = text("RELAY", camera)
        room = parse_polygon(text("ROOM_POLYGON"), k("ROOM_POLYGON"))
        self.zones = Zones(room, parse_polygons(text("EXCLUDE_POLYGONS"), k("EXCLUDE_POLYGONS")),
                           parse_polygon(text("ENGAGE_POLYGON"), k("ENGAGE_POLYGON"))
                           if text("ENGAGE_POLYGON") else None)
        self.placeholder = is_whole_frame(room) and not self.zones.exclude
        self.face_model = text("FACE_MODEL", "faceyolo_320.onnx")
        self.body_model = text("BODY_MODEL", "yolov8n_320.onnx")
        self.couch_model = text("COUCH_MODEL", "yolov8n_640.onnx")
        for suffix, value in (("FACE_MODEL", self.face_model), ("BODY_MODEL", self.body_model),
                              ("COUCH_MODEL", self.couch_model)):
            if not re.fullmatch(r"[a-z0-9_]+\.onnx", value):
                raise Invalid("%s must be a model file name such as faceyolo_320.onnx" % k(suffix))
        self.face_conf = number("FACE_CONF", 0.15, 0.01, 0.99)
        self.body_conf = number("BODY_CONF", 0.25, 0.01, 0.99)
        # The 2026-11-12 settings (LLR-1524): the sampling rate, the dark-frame
        # skip and the whole-frame motion gate the offline replay applied.
        self.fps = number("FPS", 0.5, 0.1, 2)
        self.dark_level = number("DARK_LEVEL", 8, 0, 255)
        self.whole_motion_max = number("WHOLE_MOTION_MAX", 0.5, 0.01, 1)
        # The couch check (LLR-1525). The crop is the room's geometry, so it is
        # private like the polygons: empty (the template) switches the check off.
        # Its boxes are mapped to fractions of the MAIN frame and tested against
        # zones drawn on the SUB frame: right while both streams show the same
        # view at the same aspect (this camera's 1920x1080 and 768x432, both
        # 16:9 of one sensor), as the offline build assumed (review 2026-11-13).
        self.couch_crop = parse_crop(text("COUCH_CROP"), k("COUCH_CROP")) if text("COUCH_CROP") else None
        self.couch_conf = number("COUCH_CONF", 0.25, 0.01, 0.99)
        self.couch_after_sec = number("COUCH_AFTER_SEC", 6, 0, 600)
        self.couch_max_bottom = number("COUCH_MAX_BOTTOM", 0.97, 0.5, 1)
        self.couch_max_sec = number("COUCH_MAX_SEC", 0, 0, 86400)
        if self.couch_max_sec and self.couch_max_sec < self.couch_after_sec:
            raise Invalid("%s may not be below %s (the check would never run)"
                          % (k("COUCH_MAX_SEC"), k("COUCH_AFTER_SEC")))
        self.body_mode = text("BODY_MODE", "cascade")
        if self.body_mode not in BODY_MODES:
            raise Invalid("%s must be one of %s" % (k("BODY_MODE"), ", ".join(BODY_MODES)))
        for suffix in ("CONFIRM_COUNT", "CONFIRM_WINDOW"):
            raw = env.get(k(suffix), "").strip()
            if raw and not re.fullmatch(r"[0-9]{1,2}", raw):
                raise Invalid("%s must be a whole number" % k(suffix))
        self.confirm_count = int(number("CONFIRM_COUNT", 2, 1, 10))
        self.confirm_window = int(number("CONFIRM_WINDOW", 3, 1, 10))
        if self.confirm_count > self.confirm_window:
            raise Invalid("%s may not exceed %s" % (k("CONFIRM_COUNT"), k("CONFIRM_WINDOW")))
        self.hold_sec = number("HOLD_SEC", 30, 2, 120)
        self.engage_sec = number("ENGAGE_SEC", 20, 1, 3600)
        holds = env.get(k("MOTION_HOLDS"), "").strip()
        if holds not in ("", "true", "false"):
            raise Invalid("%s must be true or false" % k("MOTION_HOLDS"))
        self.motion_holds = holds != "false"
        self.motion_min_ratio = number("MOTION_MIN_RATIO", 0.004, 0.0001, 0.5)
        # Motion may extend presence at most this long past the last real
        # detection: light from a TV left on, a pet or a fan must not hold an
        # empty room present all night (review 2026-11-09).
        self.motion_max_sec = number("MOTION_MAX_SEC", 60, 0, 3600)
        self.stall_sec = number("STALL_SEC", 20, 5, 600)
        self.budget_pct = int(number("BUDGET_PCT", 30, 1, 400))
        self.model_dir = text("MODEL_DIR", DEFAULT_MODEL_DIR)
        self.event_prefix = text("EVENT_PREFIX", "living_room")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", self.event_prefix):
            raise Invalid("%s must match [a-z][a-z0-9_]{0,31}" % k("EVENT_PREFIX"))
        raw_port = env.get(k("FEED_PORT"), "").strip()
        self.feed_port = mc.knob_int(env, k("FEED_PORT"), 0, feed.PORT_LOW, feed.PORT_HIGH)
        if raw_port not in ("", "0") and self.feed_port == 0:
            raise Invalid("%s must be 0 or a port in %d..%d" % (k("FEED_PORT"), feed.PORT_LOW, feed.PORT_HIGH))
        listed = env.get(k("FEED_PEERS"), "").split()
        if any(not feed.IPV4_RE.fullmatch(p) for p in listed):
            raise Invalid("%s holds something that is not an IPv4 address" % k("FEED_PEERS"))
        peers = listed or [env.get("MAIN_BOX_IP", "").strip()]
        self.feed_peers = [p for p in peers if feed.IPV4_RE.fullmatch(p)]
        if self.feed_port and not self.feed_peers:
            raise Invalid("%s (or MAIN_BOX_IP) must name at least one IPv4 address" % k("FEED_PEERS"))

    def hold_config(self) -> "holds.HoldConfig":
        """holds.HoldConfig for these knobs: the couch check only (the exit-aware
        hold is off: it did not work at 0.5 fps, offline 2026-11-11)."""
        return holds.HoldConfig(long_hold_sec=0.0,
                                couch_conf=self.couch_conf if self.couch_crop is not None else 0.0,
                                couch_after_sec=self.couch_after_sec,
                                couch_max_sec=self.couch_max_sec or NO_CAP,
                                couch_max_bottom=self.couch_max_bottom)


# ── models ───────────────────────────────────────────────────────────────────


def read_manifest(path: str = MANIFEST_FILE) -> Dict[str, Dict[str, object]]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)["models"]


def verify_model(model_dir: str, name: str, manifest: Dict[str, Dict[str, object]]) -> str:
    """The model's path when its bytes are the pinned ones; Invalid otherwise.

    Implements: LLR-1498.
    """
    entry = manifest.get(name)
    if not entry:
        raise Invalid("model %s is not in models.manifest.json" % name)
    path = os.path.join(model_dir, name)
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        raise Invalid("model %s is missing from %s" % (name, model_dir))
    if digest.hexdigest() != entry["sha256"]:
        raise Invalid("model %s is not the pinned bytes (models.manifest.json)" % name)
    return path


# ── detection ────────────────────────────────────────────────────────────────


def letterbox(rgb, size: int, resize: Optional[Callable] = None):
    """Aspect-preserving resize into a zero-padded square anchored top-left; RGB
    0..1 NCHW float32. The panel's layout (OfficeWallNaglight
    tools/sensor-corpus/replay_body.py). Returns (blob, scale).

    Implements: LLR-1499.
    """
    import numpy as np
    height, width = rgb.shape[:2]
    if height > width:
        new_h, new_w = size, max(1, int(size / height * width))
    else:
        new_w, new_h = size, max(1, int(size / width * height))
    if resize is None:
        import cv2
        resized = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)
    else:
        resized = resize(rgb, (new_w, new_h))
    canvas = np.zeros((size, size, 3), dtype=np.float32)
    canvas[:new_h, :new_w, :] = resized.astype(np.float32)
    blob = (canvas / 255.0).transpose(2, 0, 1)[np.newaxis]
    return np.ascontiguousarray(blob, dtype=np.float32), new_w / float(width)


def decode_yolo(output, threshold: float, class_index: int = 0) -> List[Tuple[float, float, float, float, float]]:
    """YOLOv8 output (1, 4+C, anchors) or (anchors, 4+C) -> [(score, cx, cy, w, h)]
    in letterbox pixels, for one class, at or above threshold. No objectness
    channel (that is YOLOv5's layout, which these models are not).

    Implements: LLR-1499.
    """
    import numpy as np
    array = np.asarray(output)
    if array.ndim == 3:
        array = array[0]
    if array.shape[0] > array.shape[1]:
        array = array.T
    scores = array[4 + class_index]
    keep = np.nonzero(scores >= threshold)[0]
    return [(float(scores[i]), float(array[0, i]), float(array[1, i]), float(array[2, i]), float(array[3, i]))
            for i in keep]


Detection = Tuple[str, float, Point]   # (kind, score, anchor in 0..1 of the whole frame)


def to_frame(boxes, kind: str, scale: float, crop_px: Tuple[int, int, int, int],
             frame_w: int, frame_h: int) -> List[Detection]:
    """Letterbox boxes -> anchors on the whole frame: a face's centre, a body's
    bottom centre (where a person sits or stands). Implements: LLR-1500."""
    x0, y0, _x1, _y1 = crop_px
    out = []
    for score, cx, cy, _w, h in boxes:
        ax = x0 + cx / scale
        ay = y0 + ((cy + h / 2.0) if kind == "body" else cy) / scale
        # Clamped just inside the frame: the even-odd test never contains a
        # point ON the bottom or right edge, and a body cut off by the frame's
        # bottom has its box there (review 2026-11-09).
        out.append((kind, score, (min(EDGE, max(0.0, ax / frame_w)), min(EDGE, max(0.0, ay / frame_h)))))
    return out


class OnnxDetector:
    """One single-threaded onnxruntime session (more threads is slower on these
    boxes: measured on the panel 2026-09-15)."""

    def __init__(self, path: str, conf: float, class_index: int):
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        self.size = int(self.session.get_inputs()[0].shape[2])
        self.conf, self.class_index = conf, class_index

    def detect(self, crop):
        blob, scale = letterbox(crop, self.size)
        return decode_yolo(self.session.run(None, {self.input: blob})[0], self.conf, self.class_index), scale


class Motion:
    """Room motion between consecutive samples: the changed fraction of the
    room's pixels (excluded zones masked). Implements: LLR-1501."""

    def __init__(self, zones: Zones):
        self.mask = zones.motion_mask(MOTION_W, MOTION_H)
        self.area = max(1, int(self.mask.sum()))
        self.previous = None

    def update(self, grey) -> float:
        import numpy as np
        if self.previous is None:
            self.previous = grey
            return 0.0
        changed = np.abs(grey.astype(np.int16) - self.previous.astype(np.int16)) > MOTION_LEVEL
        self.previous = grey
        return float((changed & self.mask).sum()) / self.area


class FrameMeasures:
    """A sample's three numbers from its grey copy: brightness (the mean grey
    level), room motion (Motion) and whole-frame motion (the changed fraction of
    every pixel), computed as the offline build computed them (HomeHub
    scripts/livingroom/detect_cache.py do_sub). Implements: LLR-1524."""

    def __init__(self, zones: Zones):
        self.room = Motion(zones)
        self.previous = None

    def update(self, grey) -> Tuple[float, float, float]:
        import numpy as np
        current = grey.astype(np.int16)
        whole = 0.0 if self.previous is None else float((np.abs(current - self.previous) > MOTION_LEVEL).mean())
        self.previous = current
        return float(grey.mean()), self.room.update(grey), whole


# ── the state ────────────────────────────────────────────────────────────────


def iso(utc: float) -> str:
    return datetime.datetime.fromtimestamp(utc, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PresenceState:
    """Presence and engage from one second's evidence; returns the events.

    present: on after CONFIRM_COUNT positive samples of the last CONFIRM_WINDOW;
    off once HOLD_SEC passes with no positive sample (and, with motion_holds,
    no room motion). A positive refreshes the hold even when the window would
    not confirm, so a flicker never drops a present person. Motion never turns
    presence on.
    engage: detections anchored in the engage zone, unbroken by more than
    HOLD_SEC, lasting ENGAGE_SEC; off once HOLD_SEC passes without one.
    Implements: LLR-1502, LLR-1503.
    """

    def __init__(self, s: PresenceSettings):
        self.s = s
        self.window = collections.deque(maxlen=s.confirm_window)
        self.present = False
        self.since = None          # type: Optional[float]   (wall clock)
        self.last_positive = None  # type: Optional[float]   (monotonic)
        self.last_detection = None  # type: Optional[float]  (monotonic; detections only, never motion)
        self.frames = 0
        self.scores = collections.deque()   # (mono, score) within the hold
        self.engaged = False
        self.engage_start = None   # type: Optional[float]   (monotonic)
        self.engage_start_utc = None  # type: Optional[float]
        self.engage_since = None   # type: Optional[float]
        self.last_engage = None    # type: Optional[float]

    def confidence(self, mono: float) -> float:
        while self.scores and mono - self.scores[0][0] > self.s.hold_sec:
            self.scores.popleft()
        return round(max([sc for _m, sc in self.scores] or [0.0]), 3)

    def presence_record(self, mono: float) -> Dict[str, object]:
        return {"v": 1, "type": "state", "event": self.s.event_prefix + ".presence", "present": self.present,
                "zone": "room", "since": iso(self.since) if self.since is not None else None,
                "confidence": self.confidence(mono) if self.present else 0.0}

    def engage_record(self) -> Dict[str, object]:
        return {"v": 1, "type": "state", "event": self.s.event_prefix + ".engage", "engaged": self.engaged,
                "zone": "engage", "since": iso(self.engage_since) if self.engage_since is not None else None}

    def update(self, mono: float, utc: float, detections: List[Detection], motion: float) -> List[Dict[str, object]]:
        counted = [d for d in detections if self.s.zones.counts(d[2])]
        positive = bool(counted)
        self.window.append(positive)
        events = []
        for _kind, score, _anchor in counted:
            self.scores.append((mono, score))
        self.frames += 1
        if positive:
            self.last_positive = self.last_detection = mono
        elif (self.present and self.s.motion_holds and motion >= self.s.motion_min_ratio
              and self.last_detection is not None and mono - self.last_detection < self.s.motion_max_sec):
            self.last_positive = mono
        if not self.present and sum(self.window) >= self.s.confirm_count:
            self.present, self.since = True, utc
            events.append(self.presence_record(mono))
        elif self.present and (self.last_positive is None or mono - self.last_positive >= self.s.hold_sec):
            self.present, self.since = False, utc
            self.window.clear()
            events.append(self.presence_record(mono))
        if self.s.zones.engage is not None:
            in_engage = any(self.s.zones.engaged(d[2]) for d in counted)
            if in_engage:
                if self.engage_start is None or (self.last_engage is not None
                                                 and mono - self.last_engage >= self.s.hold_sec):
                    self.engage_start, self.engage_start_utc = mono, utc
                self.last_engage = mono
            if not self.engaged and in_engage and mono - self.engage_start >= self.s.engage_sec:
                self.engaged, self.engage_since = True, self.engage_start_utc
                events.append(self.engage_record())
            elif self.engaged and (not self.present or mono - self.last_engage >= self.s.hold_sec):
                self.engaged, self.engage_since = False, utc
                self.engage_start = None
                events.append(self.engage_record())
            elif not in_engage and self.last_engage is not None and mono - self.last_engage >= self.s.hold_sec:
                self.engage_start = None
        return events

    @property
    def known(self) -> bool:
        """Enough frames evaluated for the state to mean something. Until then a
        fresh run (a restart after a stall, say) announces nothing rather than a
        false absence (review 2026-11-09)."""
        return self.frames >= self.s.confirm_window

    def snapshot(self, mono: float) -> List[Dict[str, object]]:
        out = [self.presence_record(mono)]
        if self.s.zones.engage is not None:
            out.append(self.engage_record())
        return out


class Evaluator:
    """One frame -> detections, with the face-first cascade. Implements: LLR-1500."""

    def __init__(self, s: PresenceSettings, face, body, frame_w: int = FRAME_W, frame_h: int = FRAME_H):
        self.s, self.face, self.body = s, face, body
        x0, y0, x1, y1 = s.zones.crop()
        self.crop_px = (int(x0 * frame_w), int(y0 * frame_h), max(int(x0 * frame_w) + 1, int(round(x1 * frame_w))),
                        max(int(y0 * frame_h) + 1, int(round(y1 * frame_h))))
        self.frame_w, self.frame_h = frame_w, frame_h
        self.ran = {"face": 0, "body": 0}

    def evaluate(self, rgb) -> List[Detection]:
        x0, y0, x1, y1 = self.crop_px
        crop = rgb[y0:y1, x0:x1]
        found = []
        if self.face is not None:
            boxes, scale = self.face.detect(crop)
            self.ran["face"] += 1
            found += to_frame(boxes, "face", scale, self.crop_px, self.frame_w, self.frame_h)
        counted_face = any(self.s.zones.counts(d[2]) for d in found)
        if self.body is not None and (self.s.body_mode == "always" or not counted_face):
            boxes, scale = self.body.detect(crop)
            self.ran["body"] += 1
            found += to_frame(boxes, "body", scale, self.crop_px, self.frame_w, self.frame_h)
        return found


class Pacer:
    """Judge about FPS frames a second of WALL time (at most 1.25 x FPS: the
    0.8 is tolerance for a steady stream's arrival jitter). The decoder's fps filter
    paces by the stream's timestamps, and a jump in them (an RTP glitch, a camera
    whose clock was set) makes it emit a backlog of duplicates as fast as the
    pipe drains: measured on the hub 2026-11-13, 1.6 frames a second for four
    minutes and 74 % of a core, every one a stale repeat. A frame that comes
    sooner than 0.8/FPS after the last judged one is read and dropped, so a
    backlog drains at pipe speed and costs no inference. Implements: LLR-1529."""

    def __init__(self, fps: float):
        self.min_gap = 0.8 / fps
        self.last = None  # type: Optional[float]

    def due(self, mono: float) -> bool:
        if self.last is not None and mono - self.last < self.min_gap:
            return False
        self.last = mono
        return True


class RoomJudge:
    """One sample's decision with the 2026-11-12 settings, pure (no model, no
    I/O): the dark-frame skip, the whole-frame motion gate, and the presence
    state wrapped in holds.HeldPresence for the couch check. The service and
    the offline replay of the recorded days both drive THIS object, so the
    replay's verdicts are the service's. Implements: LLR-1524, LLR-1526."""

    def __init__(self, s: PresenceSettings):
        self.s = s
        self.state = PresenceState(s)
        self.held = holds.HeldPresence(self.state, s.hold_config())

    def dark(self, bright: float) -> bool:
        """A frame to skip, not to read as "nobody": the camera's black frames
        while it switches to infrared."""
        return self.s.dark_level > 0 and bright < self.s.dark_level

    def motion(self, room_motion: float, whole_motion: float) -> float:
        """Room motion, or none when the whole picture changed (a light switched)."""
        gated = self.s.whole_motion_max < 1 and whole_motion >= self.s.whole_motion_max
        return 0.0 if gated else room_motion

    def wants_couch(self, mono: float) -> bool:
        return self.s.couch_crop is not None and self.held.wants_couch(mono)

    def update(self, mono: float, utc: float, detections: List[Detection], room_motion: float,
               whole_motion: float, couch: Optional[List[Detection]] = None) -> List[Dict[str, object]]:
        """couch: the couch check's counted bodies for this sample, or None when
        it was not read (not wanted, or no fresh keyframe)."""
        return self.held.update(mono, utc, detections, self.motion(room_motion, whole_motion), couch)


def couch_boxes(boxes, scale: float, width: int, height: int, crop: Crop) -> List[Tuple[float, ...]]:
    """Letterbox boxes on the couch image -> (score, x0, y0, x1, y1) as fractions
    of the WHOLE main frame, holds.couch_bodies' input. Implements: LLR-1525."""
    x0, y0, x1, y1 = crop
    out = []
    for score, cx, cy, w, h in boxes:
        left, top = (cx - w / 2.0) / scale, (cy - h / 2.0) / scale
        right, bottom = (cx + w / 2.0) / scale, (cy + h / 2.0) / scale
        out.append((score, x0 + left / width * (x1 - x0), y0 + top / height * (y1 - y0),
                    x0 + right / width * (x1 - x0), y0 + bottom / height * (y1 - y0)))
    return out


def couch_height(crop: Crop, main_aspect: float = 16.0 / 9.0) -> int:
    """The couch image's height at COUCH_W wide, even, for a main frame of
    main_aspect (the camera's 1920x1080)."""
    x0, y0, x1, y1 = crop
    return max(2, int(round(COUCH_W * (y1 - y0) / ((x1 - x0) * main_aspect) / 2.0)) * 2)


class CouchCheck:
    """The couch check's reads: the newest couch keyframe, judged ONCE per
    keyframe (a keyframe comes every ~4 s and a sample every 2 s; the offline
    replay judged the same keyframe for both samples, with the same result).
    `source.latest(mono, max_age)` gives (sequence, frame) or None.
    Implements: LLR-1525."""

    def __init__(self, s: PresenceSettings, detector, source):
        self.s, self.detector, self.source = s, detector, source
        self.cfg = s.hold_config()
        self.sequence = None
        self.result = []  # type: List[Detection]
        self.inferences = self.missing = 0

    def read(self, mono: float) -> Optional[List[Detection]]:
        got = self.source.latest(mono, COUCH_MAX_AGE_SEC)
        if got is None:
            self.missing += 1
            return None
        sequence, frame = got
        if sequence != self.sequence:
            boxes, scale = self.detector.detect(frame)
            height, width = frame.shape[:2]
            self.result = holds.couch_bodies(couch_boxes(boxes, scale, width, height, self.s.couch_crop),
                                             self.cfg, self.s.zones.counts)
            self.sequence = sequence
            self.inferences += 1
        return self.result


class KeyframeReader:
    """The couch decoder's frames, read on a thread; `latest` gives the newest
    with its arrival time. A decoder that ends is started again after
    COUCH_RESTART_SEC; while it is down there is no fresh frame, which the
    couch check counts as missing and the room detector does not notice.
    Implements: LLR-1527."""

    def __init__(self, start: Callable, stop: Callable, width: int, height: int, log: Callable):
        self.start_decoder, self.stop_decoder, self.log = start, stop, log
        self.frame_bytes = width * height * 3
        self.width, self.height = width, height
        self.lock = threading.Lock()
        self.newest = None   # (sequence, monotonic, frame)
        self.sequence = 0
        self.stopping = threading.Event()
        self.restarts = 0
        self.proc = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def begin(self) -> None:
        self.thread.start()

    def latest(self, mono: float, max_age: float):
        with self.lock:
            if self.newest is None or mono - self.newest[1] > max_age:
                return None
            return self.newest[0], self.newest[2]

    def _run(self) -> None:
        """The reader thread owns the decoder: it starts it, reads it, and stops
        it (review 2026-11-13: a start that raises is retried, never the end of
        the thread; end() only signals)."""
        import numpy as np
        while not self.stopping.is_set():
            try:
                proc = self.start_decoder()
            except Exception as exc:  # noqa: BLE001 - a failed start is retried, never fatal
                proc = None
                self.log("couch decoder did not start (%s)" % type(exc).__name__, warning=True)
            if proc is not None:
                with self.lock:
                    self.proc = proc
                if self.stopping.is_set():      # end() came between the start and the handle
                    self._kill(proc)
                try:
                    while not self.stopping.is_set():
                        data = proc.stdout.read(self.frame_bytes)
                        if not data or len(data) < self.frame_bytes:
                            break
                        frame = np.frombuffer(data, dtype=np.uint8).reshape(self.height, self.width, 3)
                        with self.lock:
                            self.sequence += 1
                            self.newest = (self.sequence, time.monotonic(), frame)
                except (OSError, ValueError):
                    pass
                with self.lock:
                    self.proc = None
                try:
                    self.stop_decoder(proc)
                except Exception as exc:  # noqa: BLE001 - cleanup must not end the thread
                    self.log("couch decoder not stopped cleanly (%s)" % type(exc).__name__, warning=True)
            if self.stopping.is_set():
                return
            self.restarts += 1
            self.log("couch decoder ended; starting it again in %ds" % COUCH_RESTART_SEC, warning=True)
            self.stopping.wait(COUCH_RESTART_SEC)

    @staticmethod
    def _kill(proc) -> None:
        try:
            proc.kill()
        except (OSError, AttributeError):
            pass

    def end(self, join_sec: float = 5.0) -> None:
        """Signal the thread and kill its decoder's client, which ends the read
        it is blocked in; the thread removes the container itself, and the
        unit's ExecStopPost does so anyway if the process exits first."""
        self.stopping.set()
        with self.lock:
            proc = self.proc
        if proc is not None:
            self._kill(proc)
        self.thread.join(timeout=join_sec)


# ── the feed (IF-081): IF-079's listener, with a peer list and state on connect ──


class PresenceFeed(feed.EventFeed):
    """event_feed.EventFeed (bounded, non-blocking, never able to touch
    detection) with several admitted peers, and the current state sent to a new
    subscriber after its hello. Implements: LLR-1504."""

    def __init__(self, camera: str, host: str, port: int, peers: Sequence[str], log=feed._noop_log):
        relabel = lambda m, warning=False: log(m.replace("camera motion [", "camera presence [", 1)  # noqa: E731
                                               .replace("PANEL_IP", "an admitted peer"), warning=warning)
        super().__init__(camera, host, port, (list(peers) or [""])[0], log=relabel)
        self.peers = set(peers)
        self.current = []  # type: List[Dict[str, object]]

    def publish_state(self, record: Dict[str, object]) -> None:
        if not self.active:
            return
        try:
            self._broadcast(feed.encode(record))
        except Exception as exc:  # noqa: BLE001
            self._fault("publish_state", exc)

    def _accept(self) -> None:
        for _ in range(feed.ACCEPTS_PER_PASS):
            try:
                conn, addr = self.listener.accept()  # type: ignore[union-attr]
            except (BlockingIOError, InterruptedError, OSError):
                return
            if addr[0] not in self.peers or len(self.clients) >= feed.MAX_CLIENTS:
                self._close_quietly(conn)
                continue
            try:
                conn.setblocking(False)
                conn.setsockopt(feed.socket.SOL_SOCKET, feed.socket.SO_KEEPALIVE, 1)
                conn.setsockopt(feed.socket.SOL_SOCKET, feed.socket.SO_SNDBUF, feed.CLIENT_BUFFER)
            except OSError:
                pass
            client = feed._Client(conn)
            self.clients.append(client)
            self._queue(client, feed.encode({"v": feed.PROTOCOL, "type": "hello", "camera": self.camera}))
            for record in self.current:
                if client in self.clients:
                    self._queue(client, feed.encode(record))


# ── the service ──────────────────────────────────────────────────────────────


def container_name(camera: str) -> str:
    return "homehub-camera-presence-%s" % camera


def decoder_command(relay: camera_relay.CameraRelaySettings, camera: str, fps: float = 0.5):
    """The room decoder: video only, FPS frames a second, 640x360 RGB24 on stdout.

    Implements: LLR-1505, LLR-1524.
    """
    return door_relay.relay_consumer(
        relay, container_name(camera), "ffmpeg", ["-nostdin", "-v", "error", "-fflags", "nobuffer"],
        ["-an", "-sn", "-dn", "-vf", "fps=%g,scale=%d:%d:flags=area" % (fps, FRAME_W, FRAME_H),
         "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
        docker_extra=["--cpus", "0.3", "--cpu-shares", "256"], timeout_us=10000000)


def couch_container_name(camera: str) -> str:
    return "homehub-camera-presence-%s-couch" % camera


def couch_decoder_command(main: camera_relay.CameraRelaySettings, camera: str, crop: Crop, height: int):
    """The couch decoder: the relay's main stream (`main`: for_main()), keyframes
    only, video only, cropped to COUCH_CROP and scaled to COUCH_W x height RGB24
    on stdout, every frame passed through as decoded (no duplicates to fill a
    frame rate). The crop and scale are the offline build's (HomeHub
    scripts/livingroom/lr_common.py INPUTS main-couch). Implements: LLR-1527.
    """
    x0, y0, x1, y1 = crop
    vf = "crop=iw*%.4f:ih*%.4f:iw*%.4f:ih*%.4f,scale=%d:%d" % (x1 - x0, y1 - y0, x0, y0, COUCH_W, height)
    return door_relay.relay_consumer(
        main, couch_container_name(camera), "ffmpeg", ["-nostdin", "-v", "error", "-skip_frame", "nokey"],
        ["-an", "-sn", "-dn", "-vf", vf, "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo",
         "pipe:1"],
        docker_extra=["--cpus", "0.3", "--cpu-shares", "256"], timeout_us=10000000)


def load(env_file: str, camera: str) -> Tuple[PresenceSettings, camera_relay.CameraRelaySettings]:
    env = mc.read_knobs(env_file)
    s = PresenceSettings(env, camera)
    relay = camera_relay.CameraRelaySettings(env_file, s.relay_camera)
    return s, relay


def refuse(camera: str, reason: str) -> int:
    mc.log("camera presence [%s]: REFUSED - %s" % (camera, reason), warning=True)
    return EXIT_REFUSED


def write_status(state_dir: str, camera: str, record: Dict[str, object]) -> None:
    path = os.path.join(state_dir, camera, "status.json")
    try:
        os.makedirs(os.path.dirname(path), mode=0o750, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except OSError as exc:
        mc.log("camera presence [%s]: status not written (%s)" % (camera, type(exc).__name__), warning=True)


def log_stream(stream, camera: str, label: str, relay) -> None:
    """A decoder's stderr into the journal, redacted, on a daemon thread."""
    def pump():
        for line in stream:
            mc.log("camera presence [%s]: %s: %s"
                   % (camera, label, door_relay.redact(line.decode("utf-8", "replace").rstrip(), relay)))
    threading.Thread(target=pump, daemon=True).start()


def couch_reader(s: PresenceSettings, relay: camera_relay.CameraRelaySettings, camera: str) -> KeyframeReader:
    """The couch decoder on its own thread, started again when it ends.
    Implements: LLR-1527."""
    main = relay.for_main()
    height = couch_height(s.couch_crop)

    def start():
        door_relay.remove_containers(couch_container_name(camera))
        argv, payload = couch_decoder_command(main, camera, s.couch_crop, height)
        proc = door_relay.start_consumer(argv, payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        log_stream(proc.stderr, camera, "couch ffmpeg", main)
        return proc

    def stop(proc):
        door_relay.remove_containers(couch_container_name(camera))
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)     # reaped, not left a zombie (review 2026-11-13)
        try:
            proc.stdout.close()      # this thread's own pipe; stderr ends with the process for its logger
        except (OSError, AttributeError):
            pass

    return KeyframeReader(start, stop, COUCH_W, height,
                          lambda m, warning=False: mc.log("camera presence [%s]: %s" % (camera, m), warning=warning))


def run_service(env_file: str, camera: str, state_dir: str = STATE_DIR) -> int:
    """homehub-camera-presence@<camera>.service's ExecStart. Implements: LLR-1506, LLR-1526."""
    try:
        s, relay = load(env_file, camera)
        if s.couch_crop is not None:
            relay.for_main()        # refused now, not after the room decoder started
    except (Invalid, door_relay.Invalid) as exc:
        return refuse(camera, str(exc))
    if s.placeholder:
        return refuse(camera, "%s is the whole-frame placeholder and nothing is excluded: draw the zones first"
                      % knob(camera, "ROOM_POLYGON"))
    relay_knob = camera_relay.knob(s.relay_camera, "ENABLED")
    if not mc.knob_bool(mc.read_knobs(env_file), relay_knob, False):
        return refuse(camera, "%s is not true: there is no relay to read" % relay_knob)
    try:
        import numpy as np
        import cv2
        cv2.setNumThreads(1)
        manifest = read_manifest()
        face = OnnxDetector(verify_model(s.model_dir, s.face_model, manifest), s.face_conf, 0)
        body = (OnnxDetector(verify_model(s.model_dir, s.body_model, manifest), s.body_conf, COCO_PERSON)
                if s.body_mode != "off" else None)
        couch_model = (OnnxDetector(verify_model(s.model_dir, s.couch_model, manifest), s.couch_conf, COCO_PERSON)
                       if s.couch_crop is not None else None)
    except Invalid as exc:
        return refuse(camera, str(exc))
    except ImportError as exc:
        return refuse(camera, "the presence runtime is not installed (%s): run "
                              "stack/camera-presence/install-presence-runtime.sh" % exc.name)
    evaluator = Evaluator(s, face, body)
    measures = FrameMeasures(s.zones)
    judge = RoomJudge(s)
    state = judge.state
    out = PresenceFeed(camera, relay.lan_ip, s.feed_port, s.feed_peers, log=mc.log)
    out.current = []   # nothing is announced until the state is known
    out.open()
    subprocess.run(["docker", "rm", "-f", container_name(camera)], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60)
    argv, payload = decoder_command(relay, camera, s.fps)
    proc = door_relay.start_consumer(argv, payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    reader = couch = None
    if couch_model is not None:
        reader = couch_reader(s, relay, camera)
        couch = CouchCheck(s, couch_model, reader)
    log_stream(proc.stderr, camera, "ffmpeg", relay)
    mc.log("camera presence [%s]: reading /%s from its relay on port %d at %g fps (video only); face %s, body %s "
           "(%s at %g); hold %gs; dark below %g skipped; whole-frame motion from %g ignored; %d exclude zone(s); "
           "engage %s; couch check %s"
           % (camera, relay.stream, relay.rtsp_port, s.fps, s.face_model, s.body_model if body else "off",
              s.body_mode, s.body_conf, s.hold_sec, s.dark_level, s.whole_motion_max, len(s.zones.exclude),
              "on" if s.zones.engage else "off",
              ("%s at %g after %gs quiet, from /%s" % (s.couch_model, s.couch_conf, s.couch_after_sec,
                                                       relay.main_stream)) if couch else "off"))
    status = {"camera": camera, "pid": os.getpid(), "started_utc_ms": int(time.time() * 1000), "frames": 0,
              "last_frame_utc_ms": None, "changes": 0, "dark_skipped": 0, "paced": 0}
    pacer = Pacer(s.fps)
    frame_bytes = FRAME_W * FRAME_H * 3
    buffer = bytearray()
    fd = proc.stdout.fileno()
    last_data = last_status = time.monotonic()
    infer_ms = collections.deque(maxlen=60)
    announced = False
    code = 1

    def status_record():
        rec = dict(status, known=state.known, state=state.snapshot(time.monotonic()), feed=out.status(),
                   ran=dict(evaluator.ran), infer_ms_p50=sorted(infer_ms)[len(infer_ms) // 2] if infer_ms else None)
        if couch is not None:
            # Counts only: never a box, a crop or a frame.
            rec["couch"] = {"wanted": judge.held.couch_reads, "hits": judge.held.couch_hits,
                            "inferences": couch.inferences, "missing": couch.missing,
                            "decoder_restarts": reader.restarts}
        return rec
    try:
        if reader is not None:
            reader.begin()          # inside the try: the finally ends it whatever happens next
        while not stopping:
            try:
                ready, writable, _x = select.select([fd] + out.read_fds(), out.write_fds(), [], 1.0)
            except (OSError, ValueError):
                out.close()
                out.disabled = True
                ready, writable, _x = select.select([fd], [], [], 1.0)
            mono = time.monotonic()
            out.service(set(ready), set(writable))
            out.tick(mono, int((mono - last_data) * 1000))
            if mono - last_status >= STATUS_EVERY_SEC:
                write_status(state_dir, camera, status_record())
                last_status = mono
            if fd not in ready:
                if mono - last_data > s.stall_sec:
                    mc.log("camera presence [%s]: no frame for %ds - ending this run" % (camera, s.stall_sec),
                           warning=True)
                    break
                continue
            chunk = os.read(fd, 1 << 18)
            if not chunk:
                mc.log("camera presence [%s]: the decoder ended" % camera, warning=True)
                break
            last_data = mono
            buffer.extend(chunk)
            if len(buffer) >= 2 * frame_bytes:
                # Behind live (a slow pass, a throttled quota): judge the newest
                # whole frame, never a backlog - the hold is timed by the clock.
                skip = (len(buffer) // frame_bytes - 1) * frame_bytes
                del buffer[:skip]
                status["skipped"] = int(status.get("skipped", 0)) + skip // frame_bytes
            while len(buffer) >= frame_bytes:
                frame = np.frombuffer(bytes(buffer[:frame_bytes]), dtype=np.uint8).reshape(FRAME_H, FRAME_W, 3)
                del buffer[:frame_bytes]
                started = time.monotonic()
                status["last_frame_utc_ms"] = int(time.time() * 1000)
                if not pacer.due(started):
                    status["paced"] = int(status["paced"]) + 1     # a backlog's duplicate: read, not judged
                    continue
                grey = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY), (MOTION_W, MOTION_H),
                                  interpolation=cv2.INTER_AREA)
                bright, room_motion, whole_motion = measures.update(grey)
                status["frames"] = int(status["frames"]) + 1
                if judge.dark(bright):
                    status["dark_skipped"] = int(status["dark_skipped"]) + 1
                    continue
                detections = evaluator.evaluate(frame)
                now = time.monotonic()
                seen = couch.read(now) if couch is not None and judge.wants_couch(now) else None
                infer_ms.append(int((time.monotonic() - started) * 1000))
                changed = False
                for record in judge.update(now, time.time(), detections, room_motion, whole_motion, seen):
                    changed = True
                    status["changes"] = int(status["changes"]) + 1
                    if announced:
                        out.publish_state(record)
                    # The journal is the record of the live proof: state, the
                    # best score, never a box, a zone or a picture.
                    mc.log("camera presence [%s]: %s %s (since %s, confidence %s)"
                           % (camera, record["event"], json.dumps({k: record[k] for k in ("present", "engaged")
                                                                  if k in record}),
                              record.get("since"), record.get("confidence", "-")))
                if state.known:
                    out.current = state.snapshot(now)
                    if not announced:
                        announced = True
                        for record in out.current:
                            out.publish_state(record)
                if changed:
                    write_status(state_dir, camera, status_record())
                    last_status = now
        else:
            code = 0
    finally:
        write_status(state_dir, camera, dict(status_record(), stopped_utc_ms=int(time.time() * 1000)))
        out.close()
        if reader is not None:
            try:
                reader.end()
            except Exception as exc:  # noqa: BLE001 - the room decoder's cleanup below must still run
                mc.log("camera presence [%s]: couch reader not ended cleanly (%s)" % (camera, type(exc).__name__),
                       warning=True)
        # Bounded, so the whole stop fits TimeoutStopSec (60): a docker rm that
        # hung past 24 s got the unit SIGKILLed on 2026-11-13; ExecStopPost
        # removes both containers again anyway.
        door_relay.remove_containers(container_name(camera), timeout=15)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    return code


def still(env_file: str, camera: str, out_path: str) -> int:
    """One JPEG from the relay, written 0600 to out_path; nothing else kept.

    Implements: LLR-1507.
    """
    try:
        s, relay = load(env_file, camera)
    except (Invalid, door_relay.Invalid) as exc:
        return refuse(camera, str(exc))
    name = "homehub-camera-still-%s" % camera
    argv, payload = door_relay.relay_consumer(
        relay, name, "ffmpeg", ["-nostdin", "-v", "error"],
        ["-an", "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-q:v", "3", "pipe:1"], timeout_us=10000000)
    try:
        raw = subprocess.run(argv, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except subprocess.TimeoutExpired:
        door_relay.remove_containers(name)
        mc.log("camera presence [%s]: still timed out" % camera, warning=True)
        return 1
    if raw.returncode != 0 or not raw.stdout.startswith(b"\xff\xd8"):
        mc.log("camera presence [%s]: still failed: %s" % (camera, door_relay.redact(
            raw.stderr.decode("utf-8", "replace").strip(), relay)[-300:]), warning=True)
        return 1
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):  # an existing file keeps its mode through O_CREAT: set it
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw.stdout)
    mc.log("camera presence [%s]: one still written to %s (%d bytes, 0600); delete it once the zones are drawn"
           % (camera, out_path, len(raw.stdout)))
    return 0


def check(env_file: str, camera: str) -> int:
    """What the detector would run with. Never prints a polygon or the couch
    crop. Implements: LLR-1508."""
    report = {"camera": camera}
    try:
        s, relay = load(env_file, camera)
    except (Invalid, door_relay.Invalid) as exc:
        door_relay.emit(dict(report, eligible=False, reason=str(exc)))
        return 1
    reasons = []
    if s.placeholder:
        reasons.append("the room zone is the whole-frame placeholder")
    manifest = read_manifest()
    names = ([s.face_model] + ([s.body_model] if s.body_mode != "off" else [])
             + ([s.couch_model] if s.couch_crop is not None else []))
    for name in names:
        try:
            verify_model(s.model_dir, name, manifest)
        except Invalid as exc:
            reasons.append(str(exc))
    main_stream = None
    if s.couch_crop is not None:
        try:
            main_stream = relay.for_main().stream
        except door_relay.Invalid as exc:
            reasons.append(str(exc))
    try:
        import onnxruntime  # noqa: F401
        runtime = getattr(onnxruntime, "__version__", "?")
    except ImportError:
        runtime = None
        reasons.append("onnxruntime is not importable by this interpreter")
    door_relay.emit(dict(report, eligible=not reasons, reasons=reasons, enabled=s.enabled, relay_stream=relay.stream,
                         relay_port=relay.rtsp_port, face=s.face_model, body=s.body_model, body_mode=s.body_mode,
                         body_conf=s.body_conf, hold_sec=s.hold_sec, fps=s.fps, dark_level=s.dark_level,
                         whole_motion_max=s.whole_motion_max, exclude_zones=len(s.zones.exclude),
                         engage=s.zones.engage is not None, couch_check=s.couch_crop is not None,
                         couch_model=s.couch_model if s.couch_crop is not None else None,
                         couch_stream=main_stream, feed_port=s.feed_port,
                         feed_peers=len(s.feed_peers), onnxruntime=runtime))
    return 0 if not reasons else 1


def unit_cgroup(camera: str) -> Optional[str]:
    done = subprocess.run(["systemctl", "show", "--property", "ControlGroup", "--value",
                           "homehub-camera-presence@%s.service" % camera], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, universal_newlines=True, timeout=20)
    rel = done.stdout.strip()
    return "/sys/fs/cgroup" + rel if done.returncode == 0 and rel.startswith("/") else None


def cost_verdict(result: Dict[str, object], budget_pct: int, couch: bool) -> Dict[str, object]:
    """The detector, its room decoder and (with the couch check) its couch
    decoder against the budget, pure. A missing part is not a pass: something
    is not running. Implements: LLR-1509, LLR-1528."""
    keys = ("detector", "decoder") + (("decoder_couch",) if couch else ())
    parts = [result.get(k) for k in keys]
    if not all(isinstance(p, dict) and "cpu_pct_one_core" in p for p in parts):
        return {"pass": False, "reason": "a part is not running (a cgroup is missing)"}
    total = round(sum(float(p["cpu_pct_one_core"]) for p in parts), 2)  # type: ignore[index]
    return {"total_cpu_pct_one_core": total, "budget_pct": budget_pct, "pass": total <= budget_pct}


def cost(env_file: str, camera: str, seconds: int) -> int:
    """CPU of the unit, its decoders and its relay, and PSI, over `seconds`.
    Implements: LLR-1509, LLR-1528."""
    try:
        s, _relay = load(env_file, camera)
    except (Invalid, door_relay.Invalid) as exc:
        return refuse(camera, str(exc))
    groups = {"detector": unit_cgroup(camera) or "/nonexistent/detector",
              "decoder": door_relay.container_cgroup(container_name(camera)) or "/nonexistent/decoder",
              "relay": door_relay.container_cgroup(camera_relay.container(s.relay_camera)) or "/nonexistent/relay"}
    if s.couch_crop is not None:
        groups["decoder_couch"] = (door_relay.container_cgroup(couch_container_name(camera))
                                   or "/nonexistent/decoder_couch")
    result = door_relay.cost(groups, seconds)
    result.update(cost_verdict(result, s.budget_pct, s.couch_crop is not None))
    door_relay.emit(result)
    return 0 if result.get("pass") else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", default=mc.DEFAULT_ENV_FILE)
    p.add_argument("--camera", required=True)
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("condition")
    sub.add_parser("check")
    sub.add_parser("run")
    st = sub.add_parser("still")
    st.add_argument("--out", required=True)
    co = sub.add_parser("cost")
    co.add_argument("--seconds", type=int, default=1800)
    args = p.parse_args(argv)
    try:
        camera = camera_relay.camera_name(args.camera)
    except door_relay.Invalid as exc:
        mc.log("camera presence: REFUSED - %s" % exc, warning=True)
        return 2
    if args.verb == "condition":
        on = mc.knob_bool(mc.read_knobs(args.env_file), knob(camera, "ENABLED"), False)
        mc.log("camera presence [%s]: %s is %s" % (camera, knob(camera, "ENABLED"),
                                                   "true - starting" if on else "not true - clean skip"))
        return 0 if on else 1
    if args.verb == "check":
        return check(args.env_file, camera)
    if args.verb == "run":
        return run_service(args.env_file, camera)
    if args.verb == "still":
        return still(args.env_file, camera, args.out)
    if args.verb == "cost":
        return cost(args.env_file, camera, args.seconds)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
