"""Bounded, frame-retention-free door motion detection.

The hub's copy of OfficeWallNaglight doorstream/motion.py at the commit named in
motion-engine.pin, with the source's comments removed and two docstrings
rewritten in neutral terms. The CODE is the panel's, token for token:
tests/test_camera_motion.py compares the two abstract syntax trees with the
docstrings set aside, so a change to the logic here cannot pass.

It runs on the camera's raw fisheye, scaled to a small grey square; zones are
normalised polygons on that square, and are private configuration. Between
frames it keeps one grey background, one age plane, the last accepted frame
and at most MAX_TRACKS numeric tracks; it never keeps an encoded frame and
writes nothing.

Two observations qualify, each firing once: an APPROACH (a region in the
approach zone that really moved and persisted, of a person-like shape, fresh
and not merely lit) and a ROAD STOP (a road-zone region that travelled and then
stayed still for the dwell time with vertical structure the background lacks).
Global changes, tear-like frames and exposure jumps are rejected before the
background learns them. A visitor lost to a whole-frame rejection is bridged
for a bounded time while the display lease is still up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

MOTION_SIZE = 256
MAX_TRACKS = 48
MAX_POLYGON_POINTS = 16
MAX_IGNORE_POLYGONS = 8
MAX_POLYGON_TEXT = 4096

WARMUP_SECONDS = 3.0
DIFFERENCE_THRESHOLD = 18
ROAD_AREA_FRACTION = .4
PRESENCE_LEASE_SECONDS = 10.0
TRACK_GAP_SECONDS = .6
BACKGROUND_SECONDS = 90.0
FOREGROUND_HOLD_SECONDS = 120.0
GLOBAL_CHANGE_RATIO = .40
TEAR_ROW_COVERAGE = .65
TEAR_MIN_ROWS = 2
EXPOSURE_JUMP = 22
SCENE_RELEARN_SECONDS = 2.0
RESET_GAP_SECONDS = 2.0

BRIDGE_SECONDS = 15.0
BLIND_QUALITIES = ("rejected", "relearn", "warmup")
DOOR_LEASE_SECONDS = 5.0
ROAD_HOLD_SECONDS = 30.0
MOTION_CAUSES = ("approach", "road-stop", "visitor", "bridge", "road-hold")
F1_RECENT_MOVE_SECONDS = 1.0
F2_ZONE_FRACTION = .10
LIT_ONLY_BRIGHT_FRACTION = .90
LIT_ONLY_DARK_FRACTION = .02

Polygon = tuple

DEFAULT_APPROACH: Polygon = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
DEFAULT_ROAD = None


def polygon_area(polygon: Polygon) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]))) / 2


def polygon_text(polygon: Polygon) -> str:
    return ";".join(f"{x:g},{y:g}" for x, y in polygon)


@dataclass(frozen=True)
class MotionConfig:
    enabled: bool = False
    calibrated: bool = False
    sample_fps: float = 5.0
    min_area_ratio: float = .0015
    persistence_seconds: float = .4
    dwell_seconds: float = 3.0
    stationary_ratio: float = .008
    approach_polygon: Polygon = DEFAULT_APPROACH
    road_polygon: Polygon | None = DEFAULT_ROAD
    ignore_polygons: tuple[Polygon, ...] = ()
    diagnostics: bool = False

    @property
    def eligible(self):
        return self.enabled and self.calibrated and self.approach_polygon != DEFAULT_APPROACH


def _boolean(value: str, name: str) -> bool:
    if value == "true": return True
    if value == "false": return False
    raise ValueError(f"{name} must be true or false")


def _number(value: str, name: str, low: float, high: float) -> float:
    try: result = float(value)
    except (TypeError, ValueError): raise ValueError(f"{name} must be numeric") from None
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} is out of range")
    return result


def _polygon(value: str, name: str) -> Polygon:
    """`x,y;x,y;x,y...`: 3 to 16 normalised points enclosing some area."""
    points = value.strip().split(";")
    if not 3 <= len(points) <= MAX_POLYGON_POINTS:
        raise ValueError(f"{name} needs 3 to {MAX_POLYGON_POINTS} points")
    result = []
    for point in points:
        parts = point.split(",")
        if len(parts) != 2: raise ValueError(f"{name} points are x,y pairs")
        result.append(tuple(_number(p.strip(), name, 0, 1) for p in parts))
    polygon = tuple(result)
    if polygon_area(polygon) < .0001: raise ValueError(f"{name} has no area")
    return polygon


def motion_config(values: dict[str, str]) -> MotionConfig:
    ignore_text = values.get("DOORBELL_MOTION_IGNORE_POLYGONS", "").strip()
    if len(ignore_text.encode()) > MAX_POLYGON_TEXT: raise ValueError("motion ignore polygons are too large")
    ignore_parts = [] if not ignore_text else ignore_text.split("|")
    if len(ignore_parts) > MAX_IGNORE_POLYGONS: raise ValueError("too many motion ignore polygons")
    road_text = values.get("DOORBELL_MOTION_ROAD_POLYGON", "").strip()
    return MotionConfig(
        enabled=_boolean(values.get("DOORBELL_MOTION_ENABLED", "false").strip(), "motion enabled"),
        calibrated=_boolean(values.get("DOORBELL_MOTION_CALIBRATED", "false").strip(), "motion calibrated"),
        sample_fps=_number(values.get("DOORBELL_MOTION_SAMPLE_FPS", "5"), "motion sample fps", 1, 10),
        min_area_ratio=_number(values.get("DOORBELL_MOTION_MIN_AREA_RATIO", ".0015"), "motion minimum area", .0001, .1),
        persistence_seconds=_number(values.get("DOORBELL_MOTION_PERSISTENCE_SECONDS", ".4"), "motion persistence", .2, 5),
        dwell_seconds=_number(values.get("DOORBELL_MOTION_DWELL_SECONDS", "3"), "motion dwell", 1, 30),
        stationary_ratio=_number(values.get("DOORBELL_MOTION_STATIONARY_RATIO", ".008"), "motion stationary ratio", .001, .05),
        approach_polygon=_polygon(values.get("DOORBELL_MOTION_APPROACH_POLYGON", polygon_text(DEFAULT_APPROACH)), "motion approach polygon"),
        road_polygon=_polygon(road_text, "motion road polygon") if road_text else None,
        ignore_polygons=tuple(_polygon(part, "motion ignore polygon") for part in ignore_parts),
        diagnostics=_boolean(values.get("DOORBELL_MOTION_DIAGNOSTICS", "false").strip(), "motion diagnostics"),
    )


@dataclass
class _Track:
    ident: int
    zone: str
    box: tuple
    first: float
    last: float
    origin: tuple
    travel: float = 0.0
    motion_hits: int = 0
    shape_first: float = float("inf")
    emitted: bool = False
    last_move: float = float("-inf")
    confirmed: bool = False
    last_shaped: float = float("-inf")
    points: list = field(default_factory=list)


def _center(box):
    x, y, w, h = box
    return x + w / 2, y + h / 2


def _overlap(a, b):
    x, y = max(a[0], b[0]), max(a[1], b[1])
    w = max(0, min(a[0] + a[2], b[0] + b[2]) - x)
    h = max(0, min(a[1] + a[3], b[1] + b[3]) - y)
    return w * h / max(1e-9, a[2] * a[3] + b[2] * b[3] - w * h)


class Detector:
    """Grey background, age plane and bounded numeric tracks; see the module doc.

    `observe(grey, now)` takes one MOTION_SIZE-square uint8 array and a strictly
    increasing time in seconds, and returns `(fired, summary)`. `summary["reset"]`
    is true on the sample that reset the background after a gap (or the first
    one), whatever quality that sample then reports.
    """

    def __init__(self, config: MotionConfig):
        import cv2
        import numpy as np
        self.cv2, self.np, self.c = cv2, np, config
        self.n = MOTION_SIZE
        self.masks = {"approach": self._mask(config.approach_polygon)}
        if config.road_polygon is not None:
            self.masks["road"] = self._mask(config.road_polygon)
        self.component_keep = np.full((self.n, self.n), 255, np.uint8) if config.ignore_polygons else None
        for polygon in config.ignore_polygons:
            excluded = self._mask(polygon)
            self.component_keep[excluded > 0] = 0
            for mask in self.masks.values():
                mask[excluded > 0] = 0
        if any(not np.any(mask) for mask in self.masks.values()):
            raise ValueError("motion ignore polygons removed a whole detection zone")
        self.zone_bounds = {}
        for zone, mask in self.masks.items():
            yy, xx = np.nonzero(mask)
            x1, y1 = int(xx.min()) // 2 * 2, int(yy.min()) // 2 * 2
            x2 = min(self.n, (int(xx.max()) + 2) // 2 * 2)
            y2 = min(self.n, (int(yy.max()) + 2) // 2 * 2)
            self.zone_bounds[zone] = (x1, y1, x2, y2)
        self.area = self.n * self.n
        self.approach_minimum = config.min_area_ratio * self.area
        self.road_minimum = config.min_area_ratio * ROAD_AREA_FRACTION * self.area
        self.road_y = ((min(p[1] for p in config.road_polygon) + max(p[1] for p in config.road_polygon)) / 2
                       if config.road_polygon else None)
        self.track_gap = max(TRACK_GAP_SECONDS, 3 / config.sample_fps)
        self.kernel = np.ones((3, 3), np.uint8)
        self.age = np.zeros((self.n, self.n), np.float32)
        self.background = self.previous = None
        self.tracks: list[_Track] = []
        self.next_id = 1
        self.start = self.last_time = self.bad_since = None
        self.lease_until = float("-inf")
        self.visitor_seen = float("-inf")

    def _mask(self, polygon):
        mask = self.np.zeros((self.n, self.n), self.np.uint8)
        points = self.np.rint(self.np.array(polygon) * (self.n - 1)).astype(self.np.int32)
        self.cv2.fillPoly(mask, [points], 255)
        return mask

    def reset(self, frame, now):
        self.background = frame.astype(self.np.float32)
        self.previous = frame.copy()
        self.age.fill(0)
        self.tracks.clear()
        self.start, self.bad_since = now, None
        self.lease_until = float("-inf")
        self.visitor_seen = float("-inf")

    @property
    def present(self):
        return self.last_time is not None and self.last_time < self.lease_until

    @property
    def visitor_visible(self):
        """A confirmed approach region was observed within one track gap.

        Unlike `present` (which runs a 10 s lease past the last sighting, to
        group split regions into one episode), this ends as soon as the visitor
        stops being seen -- it is what the panel's Door latch follows."""
        return self.last_time is not None and self.last_time - self.visitor_seen <= self.track_gap

    def observe(self, grey, now):
        cv2, np, c = self.cv2, self.np, self.c
        if grey.shape != (self.n, self.n) or grey.dtype != np.uint8:
            raise ValueError("expected a MOTION_SIZE square uint8 grey frame")
        if self.last_time is not None and now <= self.last_time:
            raise ValueError("sample times must increase")
        dt = 1 / c.sample_fps if self.last_time is None else now - self.last_time
        self.last_time = now
        frame = cv2.GaussianBlur(grey, (3, 3), 0)
        summary = {"quality": "ok", "regions": 0, "foreground": 0.0}
        if self.background is None or dt > RESET_GAP_SECONDS:
            self.reset(frame, now)
            summary["quality"] = "reset"
            summary["reset"] = True
        signed = frame.astype(np.int16) - self.previous.astype(np.int16)
        shift = float(np.median(signed[::4, ::4]))
        changed = np.abs(signed - shift) > DIFFERENCE_THRESHOLD
        wide_rows = np.count_nonzero(changed.mean(axis=1) > TEAR_ROW_COVERAGE)
        if changed.mean() > GLOBAL_CHANGE_RATIO or wide_rows >= TEAR_MIN_ROWS or abs(shift) > EXPOSURE_JUMP:
            summary["quality"] = "rejected"
            self.tracks.clear()
            self.lease_until = float("-inf")
            if self.bad_since is None:
                self.bad_since = now
            if now - self.bad_since >= SCENE_RELEARN_SECONDS:
                self.reset(frame, now)
                summary["quality"] = "relearn"
            return False, summary
        self.bad_since = None
        self.background = np.clip(self.background + shift, 0, 255)
        foreground = (cv2.absdiff(frame, cv2.convertScaleAbs(self.background)) > DIFFERENCE_THRESHOLD).astype(np.uint8) * 255
        foreground = cv2.morphologyEx(foreground, cv2.MORPH_OPEN, self.kernel)
        foreground = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, self.kernel)
        self.age = np.where(foreground > 0, self.age + dt, 0).astype(np.float32)
        learn = ((foreground == 0) | (self.age > FOREGROUND_HOLD_SECONDS)).astype(np.uint8) * 255
        fired = False
        if now - self.start < WARMUP_SECONDS:
            cv2.accumulateWeighted(frame, self.background, .2)
            summary["quality"] = "warmup"
        else:
            cv2.accumulateWeighted(frame, self.background, min(1.0, dt / BACKGROUND_SECONDS), mask=learn)
            fired = self._track(foreground, changed, frame, now, summary)
        self.previous = frame
        return fired, summary

    def _track(self, foreground, changed, frame, now, summary):
        cv2, np, c, n = self.cv2, self.np, self.c, self.n
        fired = False
        available = set(range(len(self.tracks)))
        road_foreground = foreground
        full_labels = full_stats = zone_pixels = None
        if cv2.countNonZero(cv2.bitwise_and(foreground, self.masks["approach"])) >= self.approach_minimum:
            component_source = (foreground if self.component_keep is None else
                                cv2.bitwise_and(foreground, self.component_keep))
            count, full_labels, full_stats, _ = cv2.connectedComponentsWithStats(component_source, connectivity=8)
            zone_pixels = np.bincount(full_labels[self.masks["approach"] > 0], minlength=count)
            if "road" in self.masks:
                near = zone_pixels >= self.approach_minimum
                near[0] = False
                road_foreground = foreground.copy()
                road_foreground[near[full_labels]] = 0
        for zone, mask in self.masks.items():
            x0, y0, x2, y2 = self.zone_bounds[zone]
            zone_source = road_foreground if zone == "road" else foreground
            work = cv2.bitwise_and(zone_source[y0:y2, x0:x2], mask[y0:y2, x0:x2])
            summary["foreground"] += cv2.countNonZero(work) / self.area
            count, labels, stats, _ = cv2.connectedComponentsWithStats(work, connectivity=8)
            minimum = self.road_minimum if zone == "road" else self.approach_minimum
            for i in sorted(range(1, count), key=lambda k: int(stats[k, 4]), reverse=True)[:MAX_TRACKS // 2]:
                x, y, w, h, area = (int(v) for v in stats[i])
                if area < minimum:
                    continue
                summary["regions"] += 1
                region = labels[y:y + h, x:x + w] == i
                x, y = x + x0, y + y0
                box = (x / n, y / n, w / n, h / n)
                point = _center(box)
                moving = np.count_nonzero(changed[y:y + h, x:x + w] & region) >= max(3, area * .08)
                choices = []
                for index in available:
                    track = self.tracks[index]
                    if track.zone != zone:
                        continue
                    old = _center(track.box)
                    distance = math.hypot(point[0] - old[0], point[1] - old[1])
                    overlap = _overlap(box, track.box)
                    if distance <= (.05 if zone == "road" else .12) and (overlap > .02 or distance < .025):
                        choices.append((distance - overlap * .03, index))
                if choices:
                    index = min(choices)[1]
                    available.remove(index)
                    track = self.tracks[index]
                    if now - track.last > 1.5 / c.sample_fps:
                        track.first, track.motion_hits = now, 0
                        track.last_move, track.confirmed = float("-inf"), False
                        track.last_shaped = float("-inf")
                        track.shape_first = float("inf")
                        track.points.clear()
                    track.box, track.last = box, now
                else:
                    track = _Track(self.next_id, zone, box, now, now, point)
                    self.next_id += 1
                    self.tracks.append(track)
                track.motion_hits += int(moving)
                if moving:
                    track.last_move = now
                track.travel = max(track.travel, math.hypot(point[0] - track.origin[0], point[1] - track.origin[1]))
                if zone == "road" and not self._structured(frame, x, y, w, h, region, area):
                    track.points.clear()
                else:
                    track.points.append((now, point))
                while track.points and track.points[0][0] < now - c.dwell_seconds:
                    track.points.pop(0)
                shape_ready = True
                if zone == "approach":
                    row, column = divmod(int(np.argmax(region)), w)
                    label = full_labels[y + row, x + column]
                    full = full_stats[label]
                    shaped = zone_pixels[label] >= F2_ZONE_FRACTION * int(full[4])
                    if self.road_y is not None:
                        bottom = int(full[1]) + int(full[3])
                        shaped = shaped and int(full[3]) >= max(.04 * n, .5 * (bottom - self.road_y * n))
                    if shaped:
                        track.last_shaped = now
                    elif track.confirmed and now - track.last_shaped > self.track_gap + 1e-6:
                        track.confirmed = False
                    if not shaped:
                        track.shape_first = float("inf")
                    elif not math.isfinite(track.shape_first):
                        track.shape_first = now
                    shape_ready = now - track.shape_first + 1e-6 >= c.persistence_seconds
                approach = (zone == "approach" and track.motion_hits >= 2
                            and now - track.first + 1e-6 >= c.persistence_seconds
                            and shape_ready
                            and (track.confirmed or now - track.last_move <= F1_RECENT_MOVE_SECONDS + 1e-6))
                if approach and not track.confirmed and self._lit_only(frame, x, y, w, h, region):
                    approach = False
                if approach:
                    track.confirmed = True
                stopped = False
                if zone == "road" and len(track.points) >= 2 and track.travel >= .012 and track.motion_hits >= 2:
                    xs, ys = [p[0] for _, p in track.points], [p[1] for _, p in track.points]
                    stable = max(max(xs) - min(xs), max(ys) - min(ys)) <= c.stationary_ratio
                    dwell = track.points[-1][0] - track.points[0][0]
                    stopped = (stable and dwell + 1.01 / c.sample_fps >= c.dwell_seconds
                               and not self._lit_only(frame, x, y, w, h, region))
                already_present = now < self.lease_until
                if approach:
                    self.lease_until = now + PRESENCE_LEASE_SECONDS
                    self.visitor_seen = now
                if not track.emitted and (approach or stopped):
                    track.emitted = True
                    if stopped or not already_present:
                        fired = True
                        if approach or summary.get("fired") != "approach":
                            summary["fired"] = "approach" if approach else "road-stop"
        self.tracks = [t for t in self.tracks if now - t.last <= self.track_gap][-MAX_TRACKS:]
        return fired

    def _lit_only(self, frame, x, y, w, h, region):
        """The region is light on the ground: nearly all of it brighter than the
        background by DIFFERENCE_THRESHOLD and almost none of it darker."""
        cv2, np = self.cv2, self.np
        delta = (frame[y:y + h, x:x + w].astype(np.int16)
                 - cv2.convertScaleAbs(self.background[y:y + h, x:x + w]).astype(np.int16))[region]
        return bool(np.count_nonzero(delta > DIFFERENCE_THRESHOLD) >= LIT_ONLY_BRIGHT_FRACTION * delta.size
                    and np.count_nonzero(delta < -DIFFERENCE_THRESHOLD) < LIT_ONLY_DARK_FRACTION * delta.size)

    def _structured(self, frame, x, y, w, h, region, area):
        """New vertical edges inside the region that the background lacks."""
        cv2, np, n = self.cv2, self.np, self.n
        y1, y2, x1, x2 = max(0, y - 2), min(n, y + h + 2), max(0, x - 2), min(n, x + w + 2)
        current = frame[y1:y2, x1:x2]
        baseline = cv2.convertScaleAbs(self.background[y1:y2, x1:x2])
        dx = np.abs(cv2.Sobel(current, cv2.CV_16S, 1, 0, ksize=3))
        dy = np.abs(cv2.Sobel(current, cv2.CV_16S, 0, 1, ksize=3))
        baseline_dx = np.abs(cv2.Sobel(baseline, cv2.CV_16S, 1, 0, ksize=3))
        edges = (dx > 35) & (baseline_dx < 20) & (dx > dy * .7)
        support = edges[y - y1:y - y1 + h, x - x1:x - x1 + w] & region
        return np.count_nonzero(support) >= max(3, area * .04)


class MotionEngine:
    """Feeds raw MOTION_SIZE-square grey frames to a Detector.

    `process(frame, now)` returns `(motion, diagnostic)`; `diagnostic` is None
    unless diagnostics are enabled, and holds only bounded integers.

    TIME. Frames advance stream time by one sample period each, unless the
    arrival clock shows a real gap, which is added so a long one resets the
    background. LATCH: an approach fires at once and, while the visitor stays
    visible, the observation repeats every RENEW_SECONDS. ROAD HOLD: a road stop
    is renewed on the arrival clock until DOOR_LEASE_SECONDS before
    ROAD_HOLD_SECONDS have passed. BRIDGE: after a visible visitor, while frames
    are rejected, relearning or warming up, renewals continue up to
    BRIDGE_SECONDS after the last sighting, only while the display lease
    (DOOR_LEASE_SECONDS on the arrival clock) is still up. CAUSE: after each
    signal `self.cause` names it, one of MOTION_CAUSES.
    """
    RENEW_SECONDS = 1.0

    def __init__(self, config: MotionConfig, detector=None):
        self.config = config
        self.detector = detector if detector is not None else Detector(config)
        self.period = 1.0 / config.sample_fps
        self.stream_time = self.last_arrival = None
        self.sample_sequence = 0
        self.last_signal = float("-inf")
        self.last_signal_arrival = float("-inf")
        self._last_visible = None
        self._blind_since_visible = False
        self._road_stop_arrival = None
        self.cause = None

    def process(self, frame: bytes, now: float):
        import numpy as np
        if len(frame) != MOTION_SIZE * MOTION_SIZE:
            raise ValueError("motion frame has the wrong size")
        if self.stream_time is None:
            self.stream_time = 0.0
        else:
            gap = now - self.last_arrival
            self.stream_time += gap if gap > 5 * self.period else self.period
        self.last_arrival = now
        fired, summary = self.detector.observe(np.frombuffer(frame, np.uint8).reshape(MOTION_SIZE, MOTION_SIZE),
                                               self.stream_time)
        self.sample_sequence += 1
        due = self.stream_time - self.last_signal >= self.RENEW_SECONDS - self.period / 2
        visible = self.detector.visitor_visible
        door_up = now - self.last_signal_arrival < DOOR_LEASE_SECONDS
        if summary.get("reset"):
            self._last_visible, self._blind_since_visible = None, False
            self._road_stop_arrival = None
        elif not door_up:
            self._last_visible, self._blind_since_visible = None, False
        if visible:
            self._last_visible, self._blind_since_visible = self.stream_time, False
        elif (summary["quality"] in BLIND_QUALITIES and self._last_visible is not None and door_up):
            self._blind_since_visible = True
        bridging = (not visible and self._blind_since_visible
                    and self.stream_time - self._last_visible <= BRIDGE_SECONDS)
        if fired and summary.get("fired") == "road-stop":
            self._road_stop_arrival = now
        holding = (self._road_stop_arrival is not None
                   and now - self._road_stop_arrival <= ROAD_HOLD_SECONDS - DOOR_LEASE_SECONDS + 1e-6)
        self.cause = None
        if fired:
            self.cause = summary.get("fired", "approach")
        elif due and (visible or bridging):
            self.cause = "visitor" if visible else "bridge"
        elif holding and now - self.last_signal_arrival >= self.RENEW_SECONDS - 1e-6:
            self.cause = "road-hold"
        if self.cause is not None:
            fired, self.last_signal, self.last_signal_arrival = True, self.stream_time, now
        diagnostic = None
        if self.config.diagnostics:
            diagnostic = {"sequence": min(self.sample_sequence, 2**31 - 1),
                          "components": min(summary["regions"], MAX_TRACKS),
                          "tracks": min(len(self.detector.tracks), MAX_TRACKS),
                          "foregroundPermille": max(0, min(1000, round(summary["foreground"] * 1000))),
                          "globalChange": int(summary["quality"] in ("rejected", "relearn")),
                          "masksApplied": min(len(self.config.ignore_polygons), 32),
                          "decision": int(fired)}
        return fired, diagnostic
