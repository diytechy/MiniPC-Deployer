#!/usr/bin/env python3
"""camera_motion.py - the hub's motion detector for a relayed camera (SR-128).

THE EVENT SOURCE FOR CLIPS. The panel's door detector runs only while the panel
is lit and someone is in the office, so on its own it would give no clips at
night or with the office empty. The Owner ruled on 2026-10-26 (HomeHub
open-items A78) that the detector runs on the hub and is the event source for
clips. This module is that detector: one instance per camera
(homehub-camera-motion@<camera>.service), each writing IF-076 records
(stack/clips/clip_events.py) that the clip cutter reads. A later camera with
its own motion events (ONVIF) is another source writing the same records; the
cutter does not change.

ONE ENGINE, NOT TWO. motion_engine.py is OfficeWallNaglight doorstream/motion.py
at a pinned commit (motion-engine.pin) with its comments removed - this
repository is public - and its code unchanged (a test compares the syntax
trees): the replay-validated detector the panel runs,
fed exactly what the panel feeds it: the RAW fisheye decoded from the relay,
`fps=<sample_fps>,scale=256:256:flags=area,format=gray`, the arrival clock
from time.monotonic(). The run refuses to start when the file is not the pinned
bytes. The zones are PRIVATE (CAMERA_<C>_MOTION_*_POLYGON, HomeHub
config.homehub.psd1, T1); the public defaults are the whole image and no road,
which MotionConfig.eligible refuses, so a hub without its private config never
runs a detector over a placeholder.

THE RELAY IS THE CAMERA'S ONLY CONNECTION. The decoder is a consumer of the
go2rtc relay, built by door_relay.relay_consumer() like every other consumer -
the credential only in an ffconcat document on its stdin - never a connection
to the camera.

DECODE. `software` (the default) is the panel's own filter chain, bit for bit.
`vaapi` decodes on the UHD 600 and scales there (scale_vaapi): measured on the
hub 2026-10-27 at about a third of the software path's CPU, but the raster is
NOT the panel's (a different scaler: mean 4 grey levels apart, 5.8 % of pixels
past the detector's 18-level threshold on one 4-minute window, where the events
still matched). It is a knob, to be switched only after `replay` agrees with
the software path over real recordings (stack/clips/README.md, deploy step 4).
Decoding on the GPU and scaling on the CPU (`hwdownload` of every 1536x1536
frame) was measured too and costs MORE than software decode; it is not offered.

Verbs:
    condition --camera C   exit 0 only when CAMERA_<C>_MOTION_ENABLED is exactly
                           true and C is listed in CAMERAS (the unit's
                           ExecCondition: a clean skip otherwise).
    check --camera C       what the detector would run with: eligibility, the
                           decode mode, the engine pin. Never prints a polygon.
    run --camera C         the service.
    replay --camera C      the same engine over recorded segments
                           (--since/--until UTC), printing its new events; the
                           tool for comparing decode modes and for re-checking a
                           moved engine pin. Index-timed (every Kth frame), so
                           it does not depend on segment PTS, which the camera
                           sometimes sends broken.
    cost --camera C        CPU of the running detector (its decoder container
                           and its unit) and PSI, against CAMERA_MOTION_BUDGET_PCT
                           (25 % of one core, the Owner's budget).

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: SR-128, LLR-1357..LLR-1362.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
for _sub in ("monitoring", "door", "scratch", "clips"):
    sys.path.insert(0, os.path.join(HERE, "..", _sub))
sys.path.insert(0, HERE)
import monitor_common as mc  # noqa: E402
import door_relay  # noqa: E402
import clip_events as ev  # noqa: E402

ENGINE_FILE = os.path.join(HERE, "motion_engine.py")
PIN_FILE = os.path.join(HERE, "motion-engine.pin")
MOTION_SIZE = 256
DECODES = ("software", "vaapi")
# The engine's knobs, by their suffix. The hub names them per camera
# (CAMERA_<C>_MOTION_<SUFFIX>) and hands them to the engine's own parser under
# the panel's names (DOORBELL_MOTION_<SUFFIX>), so one parser validates both.
ENGINE_KEYS = ("ENABLED", "CALIBRATED", "SAMPLE_FPS", "MIN_AREA_RATIO", "PERSISTENCE_SECONDS",
               "DWELL_SECONDS", "STATIONARY_RATIO", "APPROACH_POLYGON", "ROAD_POLYGON", "IGNORE_POLYGONS")
NEW_EVENT_CAUSES = ("approach", "road-stop")
# The renderer's Door lease after a signal (the engine's DOOR_LEASE_SECONDS, 5 s):
# what "the panel's own display window" means. A renewal reaches the store at
# most every THROTTLE_SEC; with the engine's ~1 s beat the written records are
# then at most ~4.2 s apart, inside the lease, so the cutter sees one unbroken
# window. The last renewal of an episode is always written (tick()).
LEASE_SEC = 5.0
THROTTLE_SEC = 3.0
# The decoder's hard ceiling (docker's cgroup, outside the unit's CPUQuota):
# about twice the measured 17.7 % of one core, so a runaway decode cannot take a
# core from the hub's DNS while a normal one never meets it. The unit's
# CPUQuota bounds the engine the same way. The Owner's budget (25 % for both)
# is the cost test's verdict, not these ceilings.
DECODER_CPUS = "0.4"
EXIT_REFUSED = 78  # EX_CONFIG: refused (pin, config, zones, missing package); no restart
STATUS_EVERY_SEC = 60.0
REPLAY_SEGMENT_SEC = 900.0   # one segment's replay decode may take this long, then it is killed


class Invalid(ValueError):
    """A configuration field is missing or malformed; the message names the KEY only."""


def knob(camera: str, suffix: str) -> str:
    return "CAMERA_%s_%s" % (camera.upper(), suffix)


def camera_name(text: str) -> str:
    if not ev.CAMERA_RE.fullmatch(text or ""):
        raise Invalid("a camera name is [a-z][a-z0-9]{0,15}")
    return text


class MotionSettings:
    """One camera's detector knobs, read from the stack .env.

    Contract:
      Config:  CAMERAS (space-separated names); per camera C:
               CAMERA_<C>_MOTION_ENABLED (false), _MOTION_RELAY_STREAM (C: the
               go2rtc stream it reads), _MOTION_DECODE (software|vaapi),
               _MOTION_STALL_SEC (30), _FPS (10, the stream's nominal rate, for
               replay), the engine's knobs _MOTION_<ENGINE_KEYS>; and
               CAMERA_VAAPI_DEVICE (/dev/dri/renderD128),
               CAMERA_MOTION_BUDGET_PCT (25).  [stack/.env]
      Raises:  Invalid naming the key, never a value.
    Implements: LLR-1358.
    """

    def __init__(self, env: Dict[str, str], camera: str):
        self.camera = camera_name(camera)
        self.listed = camera in env.get("CAMERAS", "").split()
        self.enabled = mc.knob_bool(env, knob(camera, "MOTION_ENABLED"), False)
        self.stream = env.get(knob(camera, "MOTION_RELAY_STREAM"), "").strip() or camera
        if not door_relay.STREAM_RE.fullmatch(self.stream):
            raise Invalid("%s must match [a-z][a-z0-9_-]{0,31}" % knob(camera, "MOTION_RELAY_STREAM"))
        self.decode = env.get(knob(camera, "MOTION_DECODE"), "").strip() or "software"
        if self.decode not in DECODES:
            raise Invalid("%s must be one of %s" % (knob(camera, "MOTION_DECODE"), ", ".join(DECODES)))
        self.device = env.get("CAMERA_VAAPI_DEVICE", "").strip() or "/dev/dri/renderD128"
        if not re.fullmatch(r"/dev/dri/renderD[0-9]{3}", self.device):
            raise Invalid("CAMERA_VAAPI_DEVICE must be /dev/dri/renderD<NNN>")
        self.stall_sec = mc.knob_int(env, knob(camera, "MOTION_STALL_SEC"), 30, 5, 600)
        self.stream_fps = mc.knob_int(env, knob(camera, "FPS"), 10, 1, 60)
        self.budget_pct = mc.knob_int(env, "CAMERA_MOTION_BUDGET_PCT", 25, 1, 400)
        values = {}
        for suffix in ENGINE_KEYS:
            key = knob(camera, "MOTION_" + suffix)
            if key in env:
                values["DOORBELL_MOTION_" + suffix] = env[key].strip()
        # The panel's diagnostics are a renderer feature; the hub has no use for them.
        values["DOORBELL_MOTION_DIAGNOSTICS"] = "false"
        self.engine_values = values


# ── the pinned engine ────────────────────────────────────────────────────────


def read_pin(path: str = PIN_FILE) -> Dict[str, str]:
    pin = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            m = re.match(r"([A-Z0-9_]+)=(\S+)$", line.strip())
            if m:
                pin[m.group(1)] = m.group(2)
    return pin


def engine_sha256(path: str = ENGINE_FILE) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def load_engine(path: Optional[str] = None, pin_path: Optional[str] = None):
    """The pinned engine module; Invalid when the file is not the pinned bytes.
    Implements: LLR-1357."""
    path, pin_path = path or ENGINE_FILE, pin_path or PIN_FILE
    want = read_pin(pin_path).get("SHA256", "")
    got = engine_sha256(path)
    if got != want:
        raise Invalid("motion_engine.py is not the pinned engine (sha256 %s, pin %s): it was edited or "
                      "replaced; restore it from motion-engine.pin's COMMIT" % (got[:12], want[:12]))
    import importlib.util
    spec = importlib.util.spec_from_file_location("motion_engine", path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves the module by name while the class is built.
    sys.modules["motion_engine"] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except BaseException:
        sys.modules.pop("motion_engine", None)
        raise
    return module


def motion_config(engine, settings: MotionSettings):
    """The engine's MotionConfig from the camera's knobs, by the engine's own
    parser; Invalid with the parser's message (which names a field, never a
    value) when a knob is malformed."""
    try:
        return engine.motion_config(settings.engine_values)
    except ValueError as exc:
        raise Invalid("CAMERA_%s_MOTION_*: %s" % (settings.camera.upper(), exc))


# ── the decoder ──────────────────────────────────────────────────────────────


def software_filter(sample_fps: float) -> str:
    """The panel's motion raster filter (OfficeWallNaglight doorstream/service.py
    motion_filter), character for character: the RAW fisheye, square, grey."""
    return "fps=%s,scale=%d:%d:flags=area,format=gray" % (format(sample_fps, "g"), MOTION_SIZE, MOTION_SIZE)


def vaapi_filter(sample_fps: float) -> str:
    return ("fps=%s,scale_vaapi=w=%d:h=%d:format=nv12,hwdownload,format=nv12,format=gray"
            % (format(sample_fps, "g"), MOTION_SIZE, MOTION_SIZE))


def hwaccel_args(device: str) -> List[str]:
    return ["-hwaccel", "vaapi", "-hwaccel_device", device, "-hwaccel_output_format", "vaapi"]


def container_name(camera: str) -> str:
    return "homehub-camera-motion-%s" % camera


def decoder_command(relay: "door_relay.RelaySettings", settings: MotionSettings, sample_fps: float):
    """(argv, stdin payload) of the detector's decoder: a relay consumer in the
    pinned image, raw 256x256 grey frames on stdout. Low CPU priority
    (--cpu-shares 256 against docker's default 1024) so the hub's services win
    under contention; one core at most. Implements: LLR-1359."""
    hw: List[str] = []
    extra = ["--pids-limit", "64", "--memory", "192m", "--cpus", DECODER_CPUS, "--cpu-shares", "256"]
    if settings.decode == "vaapi":
        hw = hwaccel_args(settings.device)
        extra += ["--device", settings.device]
        vf = vaapi_filter(sample_fps)
    else:
        vf = software_filter(sample_fps)
    return door_relay.relay_consumer(
        relay, container_name(settings.camera), "ffmpeg",
        ["-nostdin", "-hide_banner", "-loglevel", "warning"] + hw,
        ["-map", "0:v:0", "-an", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
        docker_extra=extra, timeout_us=15000000)


class RawFramer:
    """Split a raw grey byte stream into fixed-size frames (the panel's RawFramer)."""

    def __init__(self, size: int):
        self.size, self.buffer = size, bytearray()

    def feed(self, chunk: bytes) -> List[bytes]:
        self.buffer.extend(chunk)
        count = len(self.buffer) // self.size
        frames = [bytes(self.buffer[i * self.size:(i + 1) * self.size]) for i in range(count)]
        del self.buffer[:count * self.size]
        return frames


# ── which signals reach the store ────────────────────────────────────────────


class SignalSpooler:
    """Decides which of the engine's signals become IF-076 records (pure).

    New events (approach, road-stop) are written at once. Renewals (visitor,
    bridge, road-hold: one about every second while the panel's Door would be
    up) are written at most every `throttle` seconds, and the LAST renewal of an
    episode is always written once no signal has come for `lease` seconds - so
    the cutter's window, last record + lease, ends where the panel's display
    would have ended. Times are the caller's: `mono` for the throttle (the
    arrival clock), `utc_ms` for the record. Implements: LLR-1360.
    """

    def __init__(self, emit: Callable[[str, str, int], None], lease: float = LEASE_SEC,
                 throttle: float = THROTTLE_SEC):
        if throttle > lease - 1.5:
            raise ValueError("the renewal throttle must leave the ~1 s beat inside the lease")
        self.emit, self.lease, self.throttle = emit, lease, throttle
        self.last_written = float("-inf")
        self.last_signal = float("-inf")
        self.pending = None  # type: Optional[Tuple[str, int]]

    def signal(self, cause: str, mono: float, utc_ms: int) -> None:
        self.last_signal = mono
        if cause in NEW_EVENT_CAUSES:
            self.emit(cause, "event", utc_ms)
        elif mono - self.last_written >= self.throttle:
            self.emit(cause, "renewal", utc_ms)
        else:
            self.pending = (cause, utc_ms)
            return
        self.last_written, self.pending = mono, None

    def tick(self, mono: float) -> None:
        if self.pending is not None and mono - self.last_signal >= self.lease:
            self.flush()
            self.last_written = mono

    def flush(self) -> None:
        if self.pending is not None:
            cause, utc_ms = self.pending
            self.pending = None
            self.emit(cause, "renewal", utc_ms)


class EventWriter:
    """Appends this run's records to the store, with ids unique across runs and boots."""

    def __init__(self, state_dir: str, camera: str, clock=time.time):
        boot = re.sub(r"[^0-9a-f]", "", mc.read_boot_id() or "")[:8] or "noboot"
        self.prefix = "%s.%d.%d" % (boot, int(clock()), os.getpid())
        self.state_dir, self.camera = state_dir, camera
        self.sequence = 0
        self.counts = {"event": 0, "renewal": 0, "failed": 0}

    def emit(self, cause: str, kind: str, utc_ms: int) -> None:
        self.sequence += 1
        try:
            record = ev.make(self.camera, "hub-motion", "%s.%d" % (self.prefix, self.sequence), cause, kind, utc_ms)
        except ValueError as exc:
            self.counts["failed"] += 1
            mc.log("camera motion [%s]: record refused (%s)" % (self.camera, exc), warning=True)
            return
        ok, why = ev.append(self.state_dir, record)
        if ok:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            if kind == "event":
                mc.log("camera motion [%s]: %s" % (self.camera, cause))
        else:
            self.counts["failed"] += 1
            mc.log("camera motion [%s]: could not record a %s: %s" % (self.camera, kind, why), warning=True)


# ── the service ──────────────────────────────────────────────────────────────


def refuse(env_file: str, camera: str, reason: str) -> int:
    mc.log("camera motion [%s]: REFUSED - %s" % (camera, reason), warning=True)
    mc.send_alert("camera-motion-%s" % camera, "high", "HomeHub: %s motion detector refused to start" % camera,
                  reason, env_file, mc.DEFAULT_STATE_DIR, repeat_sec=6 * 3600, log_tag="homehub-camera-motion")
    return EXIT_REFUSED


def prepare(env_file: str, camera: str, preflight: bool = False):
    """(settings, engine module, MotionConfig) or Invalid; the checks `check` and
    `run` share, so they cannot disagree. `preflight` (check, before the knob is
    turned on) judges the zones and calibration as if the knob were on."""
    settings = MotionSettings(mc.read_knobs(env_file), camera)
    try:
        engine = load_engine()
    except (OSError, ImportError, SyntaxError) as exc:
        raise Invalid("the pinned engine cannot be loaded (%s): restore stack/camera-motion from the repo"
                      % type(exc).__name__)
    if preflight:
        settings.engine_values["DOORBELL_MOTION_ENABLED"] = "true"
    config = motion_config(engine, settings)
    if not config.eligible:
        raise Invalid("%s/_CALIBRATED are not both true, or %s is still the whole-image placeholder: "
                      "the zones are private configuration (HomeHub config.homehub.psd1)"
                      % (knob(camera, "MOTION_ENABLED"), knob(camera, "MOTION_APPROACH_POLYGON")))
    return settings, engine, config


def write_status(state_dir: str, camera: str, status: Dict[str, object]) -> None:
    mc.save_json_atomic(os.path.join(state_dir, "motion", "%s.json" % camera), status)


def run_service(env_file: str, camera: str, state_dir: str = ev.STATE_DIR) -> int:
    """homehub-camera-motion@<camera>.service's ExecStart."""
    try:
        settings, engine, config = prepare(env_file, camera)
        relay = door_relay.RelaySettings(env_file)
    except (Invalid, door_relay.Invalid) as exc:
        return refuse(env_file, camera, str(exc))
    try:
        import cv2  # noqa: F401  (the engine imports it too; asked here to name the package)
    except ImportError:
        return refuse(env_file, camera, "python3-opencv is not installed (firstboot installs it when a "
                                        "camera's motion knob is true; or: apt-get install --no-install-recommends "
                                        "python3-opencv)")
    # Small rasters: OpenCV's worker pool costs more than it saves, and spins.
    cv2.setNumThreads(1)
    relay.stream = settings.stream
    motion = engine.MotionEngine(config)
    writer = EventWriter(state_dir, camera)
    spool = SignalSpooler(writer.emit)
    subprocess.run(["docker", "rm", "-f", container_name(camera)], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60)
    argv, payload = decoder_command(relay, settings, config.sample_fps)
    proc = door_relay.start_consumer(argv, payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))

    def relay_stderr():
        for line in proc.stderr:
            mc.log("camera motion [%s]: ffmpeg: %s" % (camera, door_relay.redact(line.decode("utf-8", "replace").rstrip(), relay)))
    threading.Thread(target=relay_stderr, daemon=True).start()
    mc.log("camera motion [%s]: reading /%s from the relay at %s:%d, %s decode at %g fps; engine %s"
           % (camera, settings.stream, relay.lan_ip, relay.rtsp_port, settings.decode, config.sample_fps,
              read_pin().get("COMMIT", "?")[:12]))
    status = {"camera": camera, "decode": settings.decode, "pid": os.getpid(),
              "started_utc_ms": int(time.time() * 1000), "frames": 0, "last_frame_utc_ms": None}
    framer = RawFramer(MOTION_SIZE * MOTION_SIZE)
    fd = proc.stdout.fileno()
    last_data = last_status = time.monotonic()
    code = 1
    try:
        while not stopping:
            ready, _w, _x = select.select([fd], [], [], 1.0)
            mono = time.monotonic()
            spool.tick(mono)
            if mono - last_status >= STATUS_EVERY_SEC:
                write_status(state_dir, camera, dict(status, written=dict(writer.counts)))
                last_status = mono
            if not ready:
                if mono - last_data > settings.stall_sec:
                    mc.log("camera motion [%s]: no frame for %ds - ending this run" % (camera, settings.stall_sec),
                           warning=True)
                    break
                continue
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                mc.log("camera motion [%s]: the decoder ended" % camera, warning=True)
                break
            last_data = mono
            for frame in framer.feed(chunk):
                now = time.monotonic()
                utc_ms = int(time.time() * 1000)
                fired, _diag = motion.process(frame, now)
                status["frames"] = int(status["frames"]) + 1
                status["last_frame_utc_ms"] = utc_ms
                if fired and motion.cause:
                    spool.signal(motion.cause, now, utc_ms)
        else:
            code = 0
    finally:
        spool.flush()
        write_status(state_dir, camera, dict(status, written=dict(writer.counts), stopped_utc_ms=int(time.time() * 1000)))
        subprocess.run(["docker", "rm", "-f", container_name(camera)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return code


# ── replay over recorded segments ────────────────────────────────────────────


def replay_command(path: str, settings: MotionSettings, sample_fps: float, image: str) -> List[str]:
    """ffmpeg over one recorded segment (bound read-only, no network), every Kth
    frame by INDEX (select), so broken segment PTS cannot thin the samples.
    Implements: LLR-1361."""
    step = max(1, int(round(settings.stream_fps / float(sample_fps))))
    pick = "select=not(mod(n\\,%d))" % step
    argv = ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--log-driver", "none", "--cpus", "1", "--cpu-shares", "64",
            "-v", "%s:/seg.ts:ro" % (path if path.startswith("/") else os.path.abspath(path))]
    if settings.decode == "vaapi":
        argv += ["--device", settings.device]
        vf = "%s,scale_vaapi=w=%d:h=%d:format=nv12,hwdownload,format=nv12,format=gray" % (pick, MOTION_SIZE, MOTION_SIZE)
        hw = hwaccel_args(settings.device)
    else:
        vf = "%s,scale=%d:%d:flags=area,format=gray" % (pick, MOTION_SIZE, MOTION_SIZE)
        hw = []
    # -fps_mode passthrough: rawvideo output is constant-rate by default, which
    # would re-time the selected frames - duplicating them back to the input
    # rate, or, over a segment whose PTS is broken, dropping all but a few.
    return argv + ["--entrypoint", "ffmpeg", image, "-nostdin", "-v", "error"] + hw + [
        "-i", "/seg.ts", "-an", "-vf", vf, "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray",
        "pipe:1"]


def replay_frames(engine_obj, frames: Sequence[bytes], start: float, period: float, last: float,
                  on_signal: Callable[[float, str], None]) -> float:
    """Feed frames timed start + i*period (kept strictly increasing past `last`)
    to the engine; report each signal. Returns the last time used."""
    for i, frame in enumerate(frames):
        now = max(start + i * period, last + 1e-3)
        fired, _diag = engine_obj.process(frame, now)
        if fired and engine_obj.cause:
            on_signal(now, engine_obj.cause)
        last = now
    return last


def replay(env_file: str, camera: str, since: float, until: float, decode: Optional[str], show_all: bool,
           runner=subprocess.Popen, out=print) -> int:
    import homehub_clips as clips  # segment naming lives with the cutter
    env = mc.read_knobs(env_file)
    settings = MotionSettings(env, camera)
    if decode:
        settings.decode = decode
    engine = load_engine()
    # Replay is a measurement: the zones must be the real ones, but the run knob may be off.
    values = dict(settings.engine_values, DOORBELL_MOTION_ENABLED="true")
    try:
        config = engine.motion_config(values)
    except ValueError as exc:
        raise Invalid("CAMERA_%s_MOTION_*: %s" % (camera.upper(), exc))
    if not config.eligible:
        raise Invalid("%s is the whole-image placeholder or the camera is not calibrated" %
                      knob(camera, "MOTION_APPROACH_POLYGON"))
    try:
        import cv2
        cv2.setNumThreads(1)
    except ImportError:
        pass
    cam = clips.CameraSettings(env, camera)
    segs = [s for s in clips.list_segments(cam.segment_dir, camera)
            if not s.part and since - 900 <= s.start < until]
    motion = engine.MotionEngine(config)
    step = settings.stream_fps / float(config.sample_fps)
    if step < 1 or abs(step - round(step)) > 1e-9:
        raise Invalid("replay feeds every Nth frame: %s (%d) must be a whole multiple of the sample rate (%g)"
                      % (knob(camera, "FPS"), settings.stream_fps, config.sample_fps))
    step = int(round(step))
    period = step / float(settings.stream_fps)
    counts: Dict[str, int] = {}
    frames_total = 0
    last = float("-inf")
    image = door_relay.image_ref()

    def on_signal(now: float, cause: str) -> None:
        # The segments before `since` only warm the engine up; nothing outside
        # the window is reported or counted.
        if not since <= now < until:
            return
        counts[cause] = counts.get(cause, 0) + 1
        if show_all or cause in NEW_EVENT_CAUSES:
            out(json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + ".%dZ" % int(now * 10 % 10),
                            "cause": cause}, sort_keys=True))

    failed: List[str] = []
    for seg in segs:
        argv = replay_command(seg.path, settings, config.sample_fps, image)
        with tempfile.TemporaryFile() as err_file:
            proc = runner(argv, stdout=subprocess.PIPE, stderr=err_file)
            # A decoder that stops producing is killed: replay must end.
            watchdog = threading.Timer(REPLAY_SEGMENT_SEC, proc.kill)
            watchdog.daemon = True
            watchdog.start()
            framer = RawFramer(MOTION_SIZE * MOTION_SIZE)
            index = 0
            try:
                while True:
                    chunk = proc.stdout.read(1 << 18)
                    if not chunk:
                        break
                    frames = framer.feed(chunk)
                    last = replay_frames(motion, frames, seg.start + index * period, period, last, on_signal)
                    index += len(frames)
                    if seg.start + index * period >= until:
                        proc.kill()  # past the window: the rest of the segment is not needed
                        break
            finally:
                watchdog.cancel()
                proc.wait()
            frames_total += index
            if proc.returncode and seg.start + index * period < until:
                err_file.seek(0)
                failed.append("%s: rc %s %s" % (seg.name, proc.returncode,
                                                 err_file.read(400).decode("utf-8", "replace").strip()))
    out(json.dumps({"camera": camera, "decode": settings.decode, "segments": len(segs), "frames": frames_total,
                    "signals": counts, "failed_segments": failed,
                    "engine": read_pin().get("COMMIT", "?")[:12]}, sort_keys=True))
    # A decode that failed is not "no events": the comparison it feeds would lie.
    return 1 if failed else 0


# ── cost ─────────────────────────────────────────────────────────────────────


def unit_cgroup(camera: str) -> Optional[str]:
    done = subprocess.run(["systemctl", "show", "--property", "ControlGroup", "--value",
                           "homehub-camera-motion@%s.service" % camera], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, universal_newlines=True, timeout=20)
    rel = done.stdout.strip()
    return "/sys/fs/cgroup" + rel if done.returncode == 0 and rel.startswith("/") else None


def cost_verdict(result: Dict[str, object], budget_pct: int) -> Dict[str, object]:
    """Total CPU of the decoder and the detector against the budget (pure).
    A missing part is not a pass: the detector is not running. Implements: LLR-1362."""
    parts = [result.get(k) for k in ("decoder", "detector")]
    if not all(isinstance(p, dict) and "cpu_pct_one_core" in p for p in parts):
        return {"pass": False, "reason": "the detector is not running (a cgroup is missing)"}
    total = round(sum(float(p["cpu_pct_one_core"]) for p in parts), 2)  # type: ignore[index]
    return {"total_cpu_pct_one_core": total, "budget_pct": budget_pct, "pass": total <= budget_pct}


def cost(env_file: str, camera: str, seconds: int, out=print) -> int:
    settings = MotionSettings(mc.read_knobs(env_file), camera)
    groups = {"decoder": door_relay.container_cgroup(container_name(camera)) or "/nonexistent/decoder",
              "detector": unit_cgroup(camera) or "/nonexistent/detector",
              "relay": door_relay.container_cgroup(door_relay.CONTAINER) or "/nonexistent/relay"}
    result = door_relay.cost(groups, seconds)
    result.update(cost_verdict(result, settings.budget_pct))
    result["decode"] = settings.decode
    out(json.dumps(result, sort_keys=True))
    return 0 if result.get("pass") else 1


# ── main ─────────────────────────────────────────────────────────────────────


def parse_when(text: str) -> float:
    if re.fullmatch(r"[0-9]+(\.[0-9]+)?", text):
        return float(text)
    import calendar
    return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", default=mc.DEFAULT_ENV_FILE)
    p.add_argument("--state-dir", default=ev.STATE_DIR)
    sub = p.add_subparsers(dest="verb")
    for verb in ("condition", "check", "run", "replay", "cost"):
        sp = sub.add_parser(verb)
        sp.add_argument("--camera", required=True)
        if verb == "replay":
            sp.add_argument("--since", required=True, help="UTC YYYY-mm-ddTHH:MM:SSZ or epoch")
            sp.add_argument("--until", required=True)
            sp.add_argument("--decode", choices=DECODES)
            sp.add_argument("--all", action="store_true", help="renewals too, not only new events")
        if verb == "cost":
            sp.add_argument("--seconds", type=int, default=1800)
    args = p.parse_args(argv)
    if not args.verb:
        p.print_help()
        return 2
    try:
        if args.verb == "condition":
            # Only the knob and the list decide: a malformed OTHER knob must reach
            # `run`, whose refusal alerts - an ExecCondition failure is a silent skip.
            camera_name(args.camera)
            env = mc.read_knobs(args.env_file)
            on = (mc.knob_bool(env, knob(args.camera, "MOTION_ENABLED"), False)
                  and args.camera in env.get("CAMERAS", "").split())
            mc.log("camera motion [%s]: %s" % (args.camera, "on - starting" if on else
                   "%s is not true or the camera is not in CAMERAS - clean skip" % knob(args.camera, "MOTION_ENABLED")))
            return 0 if on else 1
        if args.verb == "check":
            settings, _engine, config = prepare(args.env_file, args.camera, preflight=True)
            print(json.dumps({"camera": args.camera, "eligible": True, "listed": settings.listed,
                              "enabled": settings.enabled, "decode": settings.decode, "stream": settings.stream,
                              "sample_fps": config.sample_fps, "road_zone": config.road_polygon is not None,
                              "ignore_zones": len(config.ignore_polygons),
                              "engine": read_pin().get("COMMIT", "?")}, sort_keys=True))
            return 0
        if args.verb == "run":
            return run_service(args.env_file, args.camera, args.state_dir)
        if args.verb == "replay":
            return replay(args.env_file, args.camera, parse_when(args.since), parse_when(args.until),
                          args.decode, args.all)
        if args.verb == "cost":
            return cost(args.env_file, args.camera, args.seconds)
    except (Invalid, door_relay.Invalid) as exc:
        mc.log("camera motion [%s]: REFUSED - %s" % (args.camera, exc), warning=True)
        return EXIT_REFUSED
    return 2


if __name__ == "__main__":
    sys.exit(main())
