#!/usr/bin/env python3
"""voice_listener.py - the hub's voice listener for the living-room assistant (SN-056).

One instance per microphone camera (homehub-voice-listener@<camera>.service).
It reads that camera's sound from the camera's own relay (stack/camera-relay),
waits for the wake phrase, turns the request that follows into text and posts
the text to MINI-SERV's action interface (POST /v1/utterance, IF-085), where
MINI-SERV's own matcher decides and acts. The hub matches nothing, speaks
nothing, and stores no sound.

IT SHIPS OFF, AND THREE THINGS MUST ALL BE SET BEFORE IT HEARS THE ROOM:
    CAMERA_<C>_LISTENER_ENABLED=true            the knob (ExecCondition)
    CAMERA_<C>_LISTENER_SOURCE=relay            the template ships `none`
    CAMERA_<C>_LISTENER_ROOM_AUDIO_RULING=...   empty until the Owner has ruled
The third exists because the hub's standing rule is that no hub process takes
the living-room camera's audio (SR-156; HomeHub DECISIONS_RATIFIED 2026-11-10).
Only the Owner can change that rule; until the ruling is recorded and named in
this knob, `run` refuses (exit 78) before any reader starts. SOURCE=file plays
a recording of SYNTHETIC speech instead of a microphone (the cost test); it
never posts and needs no ruling.

WHILE IT RUNS it hears only when it must (voice_gate.arm_decision): not while
the hold file exists, not while MINI-SERV does not answer or reports its voice
muted or its own listener armed, and (presence `required`) not while the
presence detector says the room is empty or does not know. Disarmed, the audio
reader is stopped: no sound reaches this process at all.

WHAT IT KEEPS: nothing. Sound lives in a frame, the gate's pre-roll and the
request being spoken, all zeroed (voice_gate, voice_session). The transcript
goes to MINI-SERV and nowhere else. The journal, the status file and the feed
carry states, counts, reasons and outcome codes: never a transcript, never a
sample, never the token.

Verbs (all take --camera C):
    condition      exit 0 only when CAMERA_<C>_LISTENER_ENABLED is exactly true.
    check          what it would run with, and every reason it would refuse.
    run            the service.
    clips          run WAV files of synthetic speech through the whole path with
                   the real engines and print what happened to each (no post).
    measure        the engine's CPU on a looped WAV in real time, gate on or off.
    cost           CPU of the running unit, its reader and the relay, and PSI,
                   against CAMERA_<C>_LISTENER_BUDGET_ARMED_PCT.
    levels         what the microphone hears, as loudness numbers only (behind
                   the same locks as the room's sound; no engine, no post).
    import-token   MINISERV_ACTION_TOKEN from STDIN into the stack .env.

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: SR-163..SR-169, LLR-1530, LLR-1544..LLR-1552.
"""

from __future__ import annotations

import argparse
import calendar
import copy
import datetime
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
import wave
from typing import Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
for _sub in ("monitoring", "door", "camera-relay", "camera-motion", "camera-presence"):
    sys.path.insert(0, os.path.join(HERE, "..", _sub))
sys.path.insert(0, HERE)
import monitor_common as mc  # noqa: E402
import door_relay  # noqa: E402
import camera_relay  # noqa: E402
import camera_presence  # noqa: E402
import event_feed as feed  # noqa: E402
import voice_engines as ve  # noqa: E402
import voice_gate as vg  # noqa: E402
import voice_post as vp  # noqa: E402
import voice_session as vs  # noqa: E402

STATE_DIR = "/var/lib/homehub-voice"
RUN_DIR = "/run/homehub-voice"
PRESENCE_STATE_DIR = camera_presence.STATE_DIR
DEFAULT_MODEL_DIR = "/opt/homehub-voice/models"
EXIT_REFUSED = 78
SOURCES = ("none", "file", "relay")
RELAY_TRACKS = {"audio": "?audio", "video-audio": "?video&audio"}   # what the reader asks the relay for
RULING_RE = re.compile(r"ratified-20[0-9]{2}-[0-9]{2}-[0-9]{2}")
UNTIL_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
ENDED = "ended"                   # shown when CAMERA_<C>_LISTENER_UNTIL has passed
TOKEN_KEY, PORT_KEY, PEER_KEY = "MINISERV_ACTION_TOKEN", "MINISERV_ACTION_PORT", "MAIN_BOX_IP"
STATUS_EVERY_SEC = 60.0
MAX_FRAMES_PER_STEP = 25          # two seconds: more than that waiting means we are behind live
# The loop's own cost is its wake-ups, so it takes the sound in batches of three
# frames and asks whether it may hear only twice a second (dev PC, 2026-11-11:
# woken fifty times a second it cost more than the gate saved in a quiet room).
BATCH_SEC = 0.24
ARM_EVERY_MS = 500
STALLS_BEFORE_EXIT = 3
ENGINE_ERRORS_BEFORE_EXIT = 5
SHOW_DISARMED = "disarmed"

Invalid = door_relay.Invalid


def knob(camera: str, suffix: str) -> str:
    return "CAMERA_%s_LISTENER_%s" % (camera.upper(), suffix)


def iso(utc: float) -> str:
    return datetime.datetime.fromtimestamp(utc, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── settings ─────────────────────────────────────────────────────────────────


class ListenerSettings:
    """Camera C's listener knobs, read from the stack .env.

    Contract:
      Config:  CAMERA_<C>_LISTENER_ENABLED (false); _SOURCE (none|file|relay;
               none); _ROOM_AUDIO_RULING (empty; `ratified-YYYY-MM-DD`);
               _UNTIL (empty; UTC YYYY-mm-ddTHH:MM:SSZ: a trial's end);
               _RELAY (C: whose relay it reads), _RELAY_TRACKS (audio|video-audio);
               _FILE (the synthetic WAV of SOURCE=file);
               _PRESENCE (required|ignored), _PRESENCE_CAMERA (C),
               _PRESENCE_STALE_SEC (150); _PEER_POLL_SEC (5);
               _WAKE_TOKENS ("^HE Y ^B RU N O"), _WAKE_LABEL (HEY_BRUNO),
               _WAKE_THRESHOLD (0.25), _WAKE_SCORE (1.0), _WAKE_STREAMS (1),
               _WAKE_STAGGER_MS (800), _WAKE_DITHER_DBFS (-70);
               _GATE (true), _GATE_MIN_SPEECH_DBFS (-50), _GATE_OVER_NOISE_DB
               (8), _GATE_HANG_MS (1200), _GATE_PRE_ROLL_MS (480),
               _NOISE_RISE_SEC (10), _NOISE_FALL_SEC (1); _DUTY_MAX_PCT (100);
               _PRE_ROLL_MS (0), _END_SILENCE_MS (700), _NO_SPEECH_MS (3000),
               _MAX_UTTERANCE_MS (8000), _HOLD_AFTER_POST_MS (3000);
               _STT_THREADS (1); _STALL_SEC (20); _FEED_PORT (0 = off),
               _FEED_PEERS (default MAIN_BOX_IP); _BUDGET_PCT (5: a day's
               average), _BUDGET_ARMED_PCT (20: while hearing); _MODEL_DIR.
               Shared with any other hub caller of MINI-SERV's action
               interface: MAIN_BOX_IP, MINISERV_ACTION_PORT (0 = not set),
               MINISERV_ACTION_TOKEN (T3). [stack/.env]
      Raises:  Invalid naming the key, never a value.
    Implements: LLR-1530.
    """

    def __init__(self, env: Dict[str, str], camera: str, raw: Optional[Dict[str, str]] = None):
        self.camera = camera_relay.camera_name(camera)
        k = lambda s: knob(camera, s)  # noqa: E731

        def text(suffix: str, default: str = "") -> str:
            return env.get(k(suffix), "").strip() or default

        def number(suffix: str, default: float, low: float, high: float) -> float:
            value = env.get(k(suffix), "").strip()
            if not value:
                return default
            if not re.fullmatch(r"-?[0-9]*\.?[0-9]+", value) or not low <= float(value) <= high:
                raise Invalid("%s must be a number in %g..%g" % (k(suffix), low, high))
            return float(value)

        def choice(suffix: str, default: str, allowed: Sequence[str]) -> str:
            value = text(suffix, default)
            if value not in allowed:
                raise Invalid("%s must be one of %s" % (k(suffix), ", ".join(allowed)))
            return value

        def flag(suffix: str, default: bool) -> bool:
            value = env.get(k(suffix), "").strip()
            if value not in ("", "true", "false"):
                raise Invalid("%s must be true or false" % k(suffix))
            return default if not value else value == "true"

        self.enabled = mc.knob_bool(env, k("ENABLED"), False)
        self.source = choice("SOURCE", "none", SOURCES)
        self.ruling = text("ROOM_AUDIO_RULING")
        if self.ruling and not RULING_RE.fullmatch(self.ruling):
            raise Invalid("%s must be empty or ratified-YYYY-MM-DD, the date of the Owner's recorded ruling"
                          % k("ROOM_AUDIO_RULING"))
        self.relay_camera = text("RELAY", camera)
        self.relay_query = RELAY_TRACKS[choice("RELAY_TRACKS", "audio", tuple(RELAY_TRACKS))]
        self.file = text("FILE")
        self.presence_policy = choice("PRESENCE", "required", vg.PRESENCE_POLICIES)
        self.presence_camera = camera_relay.camera_name(text("PRESENCE_CAMERA", camera))
        self.presence_stale_sec = number("PRESENCE_STALE_SEC", 150, 70, 3600)
        self.presence_event = (env.get(camera_presence.knob(self.presence_camera, "EVENT_PREFIX"), "").strip()
                               or "living_room") + ".presence"
        self.peer_poll_sec = number("PEER_POLL_SEC", 5, 1, 300)
        self.wake_tokens = text("WAKE_TOKENS", "^HE Y ^B RU N O")
        self.wake_label = text("WAKE_LABEL", "HEY_BRUNO")
        if not ve.LABEL_RE.fullmatch(self.wake_label):
            raise Invalid("%s must match [A-Z][A-Z0-9_]{0,31}" % k("WAKE_LABEL"))
        if any(not ve.TOKEN_RE.fullmatch(p) for p in self.wake_tokens.split()) or len(self.wake_tokens.split()) < 2:
            raise Invalid("%s must be the wake phrase as the model's tokens, '^' marking a word start"
                          % k("WAKE_TOKENS"))
        self.wake_threshold = number("WAKE_THRESHOLD", 0.25, 0.01, 0.99)
        self.wake_score = number("WAKE_SCORE", 1.0, 0.1, 10)
        self.wake_streams = int(number("WAKE_STREAMS", 1, 1, 4))
        self.wake_stagger_ms = int(number("WAKE_STAGGER_MS", 800, 0, 5000))
        self.wake_dither_dbfs = number("WAKE_DITHER_DBFS", -70, -120, -30)
        self.gate = flag("GATE", True)
        self.gate_min_speech_dbfs = number("GATE_MIN_SPEECH_DBFS", -50, -90, -10)
        self.gate_over_noise_db = number("GATE_OVER_NOISE_DB", 8, 1, 40)
        self.gate_hang_ms = int(number("GATE_HANG_MS", 1200, 300, 10000))
        self.gate_pre_roll_ms = int(number("GATE_PRE_ROLL_MS", 480, 0, 2000))
        self.noise_rise_sec = number("NOISE_RISE_SEC", 10, 1, 600)
        self.noise_fall_sec = number("NOISE_FALL_SEC", 1, 0.1, 60)
        self.duty_max_pct = int(number("DUTY_MAX_PCT", 100, 1, 100))
        self.pre_roll_ms = int(number("PRE_ROLL_MS", 0, 0, 3000))
        self.end_silence_ms = int(number("END_SILENCE_MS", 700, 100, 5000))
        self.no_speech_ms = int(number("NO_SPEECH_MS", 3000, 200, 30000))
        self.max_utterance_ms = int(number("MAX_UTTERANCE_MS", 8000, 1000, 30000))
        self.hold_after_post_ms = int(number("HOLD_AFTER_POST_MS", 3000, 0, 60000))
        self.stt_threads = int(number("STT_THREADS", 1, 1, 4))
        self.stall_sec = number("STALL_SEC", 20, 5, 600)
        self.budget_pct = number("BUDGET_PCT", 5, 0.1, 400)
        self.budget_armed_pct = int(number("BUDGET_ARMED_PCT", 20, 1, 400))
        self.model_dir = text("MODEL_DIR", DEFAULT_MODEL_DIR)
        # An end date for a trial: past it the listener stops itself and starts
        # nothing (the training capture's rule: it cannot be forgotten on).
        raw_until = text("UNTIL")
        if raw_until and not UNTIL_RE.fullmatch(raw_until):
            raise Invalid("%s must be empty or UTC YYYY-mm-ddTHH:MM:SSZ" % k("UNTIL"))
        try:
            self.until = float(calendar.timegm(time.strptime(raw_until, "%Y-%m-%dT%H:%M:%SZ"))) if raw_until else None
        except ValueError:
            raise Invalid("%s must be empty or UTC YYYY-mm-ddTHH:MM:SSZ" % k("UNTIL"))
        raw_port = env.get(k("FEED_PORT"), "").strip()
        self.feed_port = mc.knob_int(env, k("FEED_PORT"), 0, feed.PORT_LOW, feed.PORT_HIGH)
        if raw_port not in ("", "0") and self.feed_port == 0:
            raise Invalid("%s must be 0 or a port in %d..%d" % (k("FEED_PORT"), feed.PORT_LOW, feed.PORT_HIGH))
        listed = env.get(k("FEED_PEERS"), "").split()
        if any(not feed.IPV4_RE.fullmatch(p) for p in listed):
            raise Invalid("%s holds something that is not an IPv4 address" % k("FEED_PEERS"))
        self.peer_host = env.get(PEER_KEY, "").strip()
        self.feed_peers = [p for p in (listed or [self.peer_host]) if feed.IPV4_RE.fullmatch(p)]
        if self.feed_port and not self.feed_peers:
            raise Invalid("%s (or %s) must name at least one IPv4 address" % (k("FEED_PEERS"), PEER_KEY))
        raw_peer_port = env.get(PORT_KEY, "").strip()
        self.peer_port = mc.knob_int(env, PORT_KEY, 0, 1, 65535)
        if raw_peer_port not in ("", "0") and self.peer_port == 0:
            raise Invalid("%s must be 0 or a port number" % PORT_KEY)
        self.lan_ip = env.get("LAN_IP", "").strip()
        # The token is read from the raw file (a knob reader would strip a ` #...` tail).
        self._token = door_relay.env_secret_value((raw or {}).get(TOKEN_KEY, ""))

    @property
    def hears_room(self) -> bool:
        """Whether these settings ask for the room's sound (the relay source)."""
        return self.source == "relay"

    @property
    def token(self) -> str:
        return self._token

    def session_options(self) -> vs.SessionOptions:
        return vs.SessionOptions(self.pre_roll_ms, self.end_silence_ms, self.no_speech_ms, self.max_utterance_ms,
                                 2000, self.hold_after_post_ms)

    def new_gate(self) -> vg.ActivityGate:
        return vg.ActivityGate(self.gate_min_speech_dbfs, self.gate_over_noise_db, 2, 3, self.gate_hang_ms,
                               self.gate_pre_roll_ms, vg.NoiseFloor(self.noise_fall_sec, self.noise_rise_sec),
                               self.gate)


def load(env_file: str, camera: str) -> ListenerSettings:
    return ListenerSettings(mc.read_knobs(env_file), camera, door_relay.read_env_raw(env_file))


def wav_problem(path: str) -> Optional[str]:
    """Why a file cannot be the synthetic source, or None: it must be a WAV of
    16 kHz mono signed 16-bit sound, at least one second long."""
    try:
        with wave.open(path, "rb") as handle:
            ok = handle.getframerate() == vg.SAMPLE_RATE and handle.getnchannels() == 1 and handle.getsampwidth() == 2
            return None if ok and handle.getnframes() >= vg.SAMPLE_RATE else "is not 16 kHz mono 16-bit, 1 s or more"
    except (OSError, wave.Error, EOFError):
        return "is missing or not a WAV file"


def refusals(s: ListenerSettings, env: Dict[str, str]) -> List[str]:
    """Every reason `run` would refuse, each naming a knob and never a value.

    The first three are the locks on the room's sound. Implements: LLR-1547.
    """
    k = lambda suffix: knob(s.camera, suffix)  # noqa: E731
    reasons = []   # type: List[str]
    if s.source == "none":
        return ["%s is none: no audio source is configured" % k("SOURCE")]
    if s.source == "file":
        problem = wav_problem(s.file) if s.file else "is not set"
        if problem:
            reasons.append("%s %s (a recording of synthetic speech)" % (k("FILE"), problem))
        return reasons
    if not s.ruling:
        reasons.append("%s is empty: a hub process may take the room's sound only after the Owner's ruling on the "
                       "hub's audio rule is recorded in HomeHub DECISIONS_RATIFIED.md; name its date here"
                       % k("ROOM_AUDIO_RULING"))
    for relay_knob in ("ENABLED", "AUDIO"):
        key = camera_relay.knob(s.relay_camera, relay_knob)
        if not mc.knob_bool(env, key, False):
            reasons.append("%s is not true: there is no relay sound to read" % key)
    if not feed.IPV4_RE.fullmatch(s.peer_host):
        reasons.append("%s is not an IPv4 address: there is nowhere to send a transcript" % PEER_KEY)
    if not s.peer_port:
        reasons.append("%s is not set: there is nowhere to send a transcript" % PORT_KEY)
    if not vp.token_ok(s.token):
        reasons.append("%s is missing or not one line of 32 to 256 visible characters" % TOKEN_KEY)
    if not feed.IPV4_RE.fullmatch(s.lan_ip):
        reasons.append("LAN_IP is not an IPv4 address")
    return reasons


# ── audio sources ────────────────────────────────────────────────────────────


def container_name(camera: str) -> str:
    return "homehub-voice-listener-%s" % camera


def decoder_command(relay: camera_relay.CameraRelaySettings, camera: str, query: str = "?audio",
                    name: Optional[str] = None):
    """The one reader of the room's sound: the relay's audio track, decoded to
    16 kHz mono signed 16-bit on stdout, by the pinned relay image's ffmpeg.

    The audio is asked for only inside the stdin document (the relay's URL
    never reaches argv or the environment), video is dropped (-vn), and the
    container is capped at a twentieth of a core at the lowest CPU weight.
    Implements: LLR-1544.
    """
    if query not in RELAY_TRACKS.values():
        raise Invalid("the relay query must be one of %s" % ", ".join(RELAY_TRACKS.values()))
    asking = copy.copy(relay)
    asking.consumer_query = query
    return door_relay.relay_consumer(
        asking, name or container_name(camera), "ffmpeg", ["-nostdin", "-v", "error", "-fflags", "nobuffer"],
        ["-vn", "-sn", "-dn", "-ac", "1", "-ar", str(vg.SAMPLE_RATE), "-f", "s16le", "pipe:1"],
        docker_extra=["--cpus", "0.05", "--cpu-shares", "2"], timeout_us=10000000)


class FrameQueue:
    """Bytes in, whole frames out; what is waiting can be thrown away unheard."""

    def __init__(self):
        self._pending = bytearray()

    def add(self, chunk) -> None:
        self._pending.extend(chunk)

    def frames(self, limit: int) -> List[bytearray]:
        out = []
        while len(out) < limit and len(self._pending) >= vg.FRAME_BYTES:
            out.append(self._pending[:vg.FRAME_BYTES])
            self._pending[:vg.FRAME_BYTES] = bytes(vg.FRAME_BYTES)
            del self._pending[:vg.FRAME_BYTES]
        return out

    @property
    def waiting_frames(self) -> int:
        return len(self._pending) // vg.FRAME_BYTES

    def flush(self) -> int:
        dropped = self.waiting_frames
        vg.wipe_bytes(self._pending)
        del self._pending[:]
        return dropped


class RelayAudio:
    """The decoder container as a frame source. start() opens the microphone
    path; stop() closes it and removes the container. Implements: LLR-1544."""

    def __init__(self, relay: camera_relay.CameraRelaySettings, camera: str, query: str,
                 log: Callable[..., None] = mc.log, name: Optional[str] = None):
        self.relay, self.camera, self.query, self.log = relay, camera, query, log
        self.name = name or container_name(camera)
        self.proc = None   # type: Optional[subprocess.Popen]
        self.queue = FrameQueue()

    @property
    def running(self) -> bool:
        return self.proc is not None

    def fileno(self) -> Optional[int]:
        return self.proc.stdout.fileno() if self.proc is not None else None

    def start(self) -> None:
        door_relay.remove_containers(self.name)
        argv, payload = decoder_command(self.relay, self.camera, self.query, self.name)
        self.proc = door_relay.start_consumer(argv, payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        os.set_blocking(self.proc.stdout.fileno(), False)
        proc = self.proc

        def relay_stderr():
            for line in proc.stderr:
                self.log("voice listener [%s]: ffmpeg: %s"
                         % (self.camera, door_relay.redact(line.decode("utf-8", "replace").rstrip(), self.relay)))
        threading.Thread(target=relay_stderr, daemon=True).start()

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        self.queue.flush()
        if proc is None:
            return
        door_relay.remove_containers(self.name)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        try:
            proc.stdout.close()      # whatever was still in the pipe goes with it, unheard
        except OSError:
            pass

    def read_frames(self, limit: int) -> List[bytearray]:
        if self.proc is not None:
            try:
                chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            except (BlockingIOError, InterruptedError):
                chunk = None
            if chunk == b"":
                raise EOFError("the audio reader ended")
            if chunk:
                self.queue.add(chunk)
        return self.queue.frames(limit)

    def waiting_frames(self) -> int:
        return self.queue.waiting_frames

    def flush(self) -> int:
        if self.proc is not None:
            try:
                while os.read(self.proc.stdout.fileno(), 1 << 16):
                    pass
            except (BlockingIOError, InterruptedError, OSError):
                pass
        return self.queue.flush()


class FileAudio:
    """A WAV of synthetic speech, looped and paced to the clock (the cost test's
    source). It is never a microphone. Implements: LLR-1544."""

    def __init__(self, path: str, clock: Callable[[], float] = time.monotonic, realtime: bool = True):
        with wave.open(path, "rb") as handle:
            self._data = handle.readframes(handle.getnframes())
        self._data = self._data[:len(self._data) - len(self._data) % vg.FRAME_BYTES]
        self._clock, self._realtime = clock, realtime
        self._at = 0
        self._started = None   # type: Optional[float]
        self._given = 0
        self.running = False

    def fileno(self) -> Optional[int]:
        return None

    def start(self) -> None:
        self.running, self._started, self._given = True, self._clock(), 0

    def stop(self) -> None:
        self.running = False

    def read_frames(self, limit: int) -> List[bytearray]:
        if not self.running:
            return []
        due = limit
        if self._realtime:
            due = min(limit, int((self._clock() - self._started) * 1000 / vg.FRAME_MS) - self._given)
        out = []
        for _ in range(max(0, due)):
            out.append(bytearray(self._data[self._at:self._at + vg.FRAME_BYTES]))
            self._at = (self._at + vg.FRAME_BYTES) % len(self._data)
            self._given += 1
        return out

    def waiting_frames(self) -> int:
        if not self._realtime or self._started is None or not self.running:
            return 0
        return max(0, int((self._clock() - self._started) * 1000 / vg.FRAME_MS) - self._given)

    def flush(self) -> int:
        if not self._realtime or self._started is None:
            return 0
        behind = int((self._clock() - self._started) * 1000 / vg.FRAME_MS) - self._given
        if behind > 0:
            self._at = (self._at + behind * vg.FRAME_BYTES) % len(self._data)
            self._given += behind
        return max(0, behind)


# ── MINI-SERV's state, watched from the side ─────────────────────────────────


class PeerWatch:
    """Polls GET /v1/state on its own thread, so a slow MINI-SERV never stalls
    the audio. Two failures in a row are `unreachable`. Implements: LLR-1540."""

    def __init__(self, get_state: Callable[[], Tuple[bool, str, str]], poll_sec: float):
        self._get_state, self._poll_sec = get_state, poll_sec
        self._lock = threading.Lock()
        self._state = vg.PeerState()
        self._failures = 0
        self._stop = threading.Event()

    def poll_once(self) -> None:
        reachable, state, voice = self._get_state()
        with self._lock:
            if reachable:
                self._failures = 0
                self._state = vg.PeerState(True, state, voice)
            else:
                self._failures += 1
                if self._failures >= 2:
                    self._state = vg.PeerState()

    def current(self) -> vg.PeerState:
        with self._lock:
            return self._state

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception:  # noqa: BLE001 - a watcher that dies would leave a stale "reachable"
                    with self._lock:
                        self._state = vg.PeerState()
                self._stop.wait(self._poll_sec)
        threading.Thread(target=loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


# ── the listener ─────────────────────────────────────────────────────────────


class Listener:
    """One pass at a time: decide whether to hear, read what is waiting, move
    the request along, and say what state it is in.

    Contract:
      Inputs:  pipeline: voice_session.Pipeline; source: start/stop/
               read_frames/flush/running; arm_inputs() -> (hold, PeerState or
               None, PresenceReading or None); report(record) on every change
               of the shown state; now_ms: a forward-only millisecond clock.
      Outputs: step() -> the state events of this pass. `fatal` is set when the
               run should end (the unit restarts it).
    Implements: LLR-1545, LLR-1546.
    """

    def __init__(self, s: ListenerSettings, pipeline: vs.Pipeline, source,
                 arm_inputs: Callable[[], Tuple[bool, Optional[vg.PeerState], Optional[vg.PresenceReading]]],
                 report: Callable[[Dict[str, object]], None], now_ms: Callable[[], int],
                 wall: Callable[[], float] = time.time, log: Callable[..., None] = mc.log,
                 peer_required: bool = True):
        self.s, self.pipeline, self.source = s, pipeline, source
        self.arm_inputs, self.report, self.now_ms, self.wall, self.log = arm_inputs, report, now_ms, wall, log
        self.peer_required = peer_required
        self.armed = False
        self.reason = "starting"
        self.shown = (SHOW_DISARMED, "starting")
        self.since = wall()
        self.last_audio_ms = 0
        self.stalls_in_a_row = 0
        self.fatal = None   # type: Optional[str]
        self.ended = False
        self.counts = {"arms": 0, "disarms": 0, "stalls": 0, "skipped_frames": 0}
        self.armed_ms = 0.0
        pipeline.on_show = self._show      # each change is reported as it happens, "working" included
        self._last_step_ms = None   # type: Optional[int]
        self._armed_checked_ms = None   # type: Optional[int]

    def step(self) -> List[Dict[str, object]]:
        now = self.now_ms()
        if self._last_step_ms is not None and self.armed:
            self.armed_ms += now - self._last_step_ms
        self._last_step_ms = now
        if self.s.until is not None and self.wall() >= self.s.until:
            # The window is over: stop hearing, say so, and let the run end cleanly.
            if self.armed or self.reason != ENDED:
                self._rearm(False, ENDED, now)
            self.ended = True
            self._show()
            return []
        if self._armed_checked_ms is None or now - self._armed_checked_ms >= ARM_EVERY_MS:
            self._armed_checked_ms = now
            hold, peer, presence = self.arm_inputs()
            armed, reason = vg.arm_decision(hold, peer, presence, self.s.presence_policy, self.peer_required)
            if armed != self.armed or (not armed and reason != self.reason):
                self._rearm(armed, reason, now)
        events = []   # type: List[Dict[str, object]]
        if self.armed:
            events = self._hear(now)
        self._show()
        return events

    def _rearm(self, armed: bool, reason: str, now: int) -> None:
        if armed and not self.armed:
            self.counts["arms"] += 1
            self.last_audio_ms = now
            self.source.start()
            self.log("voice listener [%s]: armed - the microphone is being read" % self.s.camera)
        elif not armed:
            if self.armed:
                self.counts["disarms"] += 1
                self._close_source()
            self.log("voice listener [%s]: disarmed (%s) - nothing is being read" % (self.s.camera, reason))
        self.armed, self.reason = armed, reason

    def _close_source(self) -> None:
        self.source.stop()
        self.pipeline.disarm()

    def _hear(self, now: int) -> List[Dict[str, object]]:
        events = []   # type: List[Dict[str, object]]
        try:
            frames = self.source.read_frames(MAX_FRAMES_PER_STEP)
        except (EOFError, OSError):
            frames = []
            self.last_audio_ms = min(self.last_audio_ms, now - int(self.s.stall_sec * 1000) - 1)
        if frames:
            self.last_audio_ms = now
            self.stalls_in_a_row = 0
        for frame in frames:
            try:
                for event in self.pipeline.feed(frame, now):
                    events.append(event)
                    self._log_event(event)
            finally:
                vg.wipe_bytes(frame)
            if self.pipeline.flush_source:
                break
        if self.pipeline.flush_source or self.source.waiting_frames() > MAX_FRAMES_PER_STEP:
            # Never a backlog: what queued up while we were busy, or because we
            # fell behind live, is thrown away unheard.
            self.counts["skipped_frames"] += self.source.flush()
            self.pipeline.flush_source = False
        if not frames:
            for event in self.pipeline.tick(now):
                events.append(event)
                self._log_event(event)
            if now - self.last_audio_ms > self.s.stall_sec * 1000:
                self.counts["stalls"] += 1
                self.stalls_in_a_row += 1
                self.log("voice listener [%s]: no sound from the reader for %ds - restarting it"
                         % (self.s.camera, self.s.stall_sec), warning=True)
                self._close_source()
                if self.stalls_in_a_row >= STALLS_BEFORE_EXIT:
                    self.fatal = "the audio reader stalled %d times in a row" % self.stalls_in_a_row
                else:
                    self.source.start()
                    self.last_audio_ms = now
        if self.pipeline.engine_errors_in_a_row >= ENGINE_ERRORS_BEFORE_EXIT:
            self.fatal = "an engine failed %d times in a row" % self.pipeline.engine_errors_in_a_row
        return events

    def _log_event(self, event: Dict[str, object]) -> None:
        """The journal's record of a request: its end reason or outcome code,
        never what was said."""
        if "outcome" in event:
            self.log("voice listener [%s]: a request was sent to MINI-SERV: %s" % (self.s.camera, event["outcome"]))
        elif event.get("reason") in (vs.END_ENGINE_ERROR,):
            self.log("voice listener [%s]: an engine failed; the request was dropped" % self.s.camera, warning=True)
        elif event.get("show") == vs.SHOW_LISTENING:
            self.log("voice listener [%s]: the wake phrase was heard" % self.s.camera)

    def _show(self) -> None:
        shown = (self.pipeline.show, "") if self.armed else (SHOW_DISARMED, self.reason)
        if shown != self.shown:
            self.shown, self.since = shown, self.wall()
            self.report(self.state_record())

    def state_record(self) -> Dict[str, object]:
        """What the feed carries (IF-086): the state, why, and since when."""
        return {"v": 1, "type": "state", "event": self.s.presence_event.rsplit(".", 1)[0] + ".listener",
                "state": self.shown[0], "detail": self.shown[1], "since": iso(self.since)}

    def status_record(self) -> Dict[str, object]:
        """The status file: states and numbers only. Implements: LLR-1546."""
        gate, limiter = self.pipeline.gate, self.pipeline.limiter
        heard_ms = gate.open_ms + gate.closed_ms
        return {"camera": self.s.camera, "source": self.s.source, "state": self.shown[0], "detail": self.shown[1],
                "since": iso(self.since), "armed": self.armed, "armed_sec": round(self.armed_ms / 1000.0, 1),
                "counts": dict(self.pipeline.counts, **self.counts), "outcomes": dict(self.pipeline.outcomes),
                "gate": {"enabled": gate.enabled, "opens": gate.opens,
                         "open_pct": round(100.0 * gate.open_ms / heard_ms, 1) if heard_ms else 0.0},
                "engine_sec": round(limiter.spent_ms / 1000.0, 1),
                "duty_refused_sec": round(limiter.refused_ms / 1000.0, 1),
                "background_dbfs": round(vg.to_dbfs(gate.floor.level), 1),
                "holds_no_audio": self.pipeline.holds_no_audio}

    def close(self) -> None:
        if self.armed:
            self._close_source()
        self.armed, self.reason = False, ENDED if self.ended else "stopped"
        self._show()


# ── status, hold, presence ───────────────────────────────────────────────────


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
        mc.log("voice listener [%s]: status not written (%s)" % (camera, type(exc).__name__), warning=True)


def hold_path(camera: str, run_dir: str = RUN_DIR) -> str:
    """The hold file: while it exists nothing is heard. `touch` it to switch
    listening off without stopping the unit; it is gone after a reboot."""
    return os.path.join(run_dir, camera, "hold")


def presence_reading(s: ListenerSettings, presence_dir: str = PRESENCE_STATE_DIR,
                     wall: Callable[[], float] = time.time) -> vg.PresenceReading:
    status = mc.load_json(os.path.join(presence_dir, s.presence_camera, "status.json"), None)
    return vg.read_presence(status, int(wall() * 1000), s.presence_stale_sec, s.presence_event)


# ── the engines, built from the pinned files ─────────────────────────────────


def build_engines(s: ListenerSettings, keywords_dir: str):
    """(wake word, speech-to-text) on the pinned model files. Raises ve.Invalid
    or ImportError (the runtime is not installed)."""
    manifest = ve.read_manifest()
    wake_files = ve.model_files(s.model_dir, manifest, ve.WAKE_MODEL, ("encoder", "decoder", "joiner", "tokens"))
    stt_files = ve.model_files(s.model_dir, manifest, ve.STT_MODEL, ("encoder", "merged-decoder", "tokens"))
    os.makedirs(keywords_dir, mode=0o750, exist_ok=True)
    wake = ve.SherpaWakeWord(wake_files, s.wake_tokens, s.wake_label, os.path.join(keywords_dir, "keywords.txt"),
                             threshold=s.wake_threshold, score=s.wake_score, streams=s.wake_streams,
                             stagger_ms=s.wake_stagger_ms, dither_dbfs=s.wake_dither_dbfs)
    return wake, ve.SherpaSpeechToText(stt_files, s.stt_threads)


def new_pipeline(s: ListenerSettings, wake, stt, post: Callable[[str, bool], str]) -> vs.Pipeline:
    return vs.Pipeline(s.new_gate(), vg.DutyLimiter(s.duty_max_pct), vs.ListenSession(s.session_options()),
                       wake, stt, post)


def dry_post(_text: str, _wake_checked: bool) -> str:
    """The synthetic source's poster: the transcript goes nowhere."""
    return "dry-run"


# ── the service ──────────────────────────────────────────────────────────────


def refuse(camera: str, reason: str) -> int:
    mc.log("voice listener [%s]: REFUSED - %s" % (camera, reason), warning=True)
    return EXIT_REFUSED


def run_service(env_file: str, camera: str, state_dir: str = STATE_DIR, run_dir: str = RUN_DIR) -> int:
    """homehub-voice-listener@<camera>.service's ExecStart. Implements: LLR-1545, LLR-1547."""
    try:
        s = load(env_file, camera)
    except Invalid as exc:
        return refuse(camera, str(exc))
    if s.until is not None and time.time() >= s.until:
        mc.log("voice listener [%s]: %s has passed - the listening window is over; nothing was started"
               % (camera, knob(camera, "UNTIL")))
        return 0
    env = mc.read_knobs(env_file)
    reasons = refusals(s, env)
    if reasons:
        return refuse(camera, "; ".join(reasons))
    try:
        relay = camera_relay.CameraRelaySettings(env_file, s.relay_camera) if s.hears_room else None
        wake, stt = build_engines(s, os.path.join(run_dir, camera))
    except (Invalid, ve.Invalid) as exc:
        return refuse(camera, str(exc))
    except ImportError as exc:
        return refuse(camera, "the voice runtime is not installed (%s): run "
                              "stack/voice-listener/install-voice-runtime.sh" % exc.name)
    watch = None
    if s.hears_room:
        client = vp.ActionClient(s.peer_host, s.peer_port, s.token, source_ip=s.lan_ip)
        watch = PeerWatch(client.get_state, s.peer_poll_sec)
        post = lambda text, wake_checked: client.post_utterance(text, wake_checked).outcome  # noqa: E731
        source = RelayAudio(relay, camera, s.relay_query)
    else:
        post = dry_post
        source = FileAudio(s.file)
    pipeline = new_pipeline(s, wake, stt, post)
    relabel = lambda m, warning=False: mc.log(m.replace("camera presence [", "voice listener [", 1),  # noqa: E731
                                              warning=warning)
    out = camera_presence.PresenceFeed(camera, s.lan_ip, s.feed_port, s.feed_peers, log=relabel)
    out.open()

    def report(record: Dict[str, object]) -> None:
        out.current = [record]
        out.publish_state(record)
        write_status(state_dir, camera, listener.status_record())

    def arm_inputs():
        return (os.path.exists(hold_path(camera, run_dir)), watch.current() if watch else None,
                presence_reading(s) if s.presence_policy == "required" else None)

    listener = Listener(s, pipeline, source, arm_inputs, report, lambda: int(time.monotonic() * 1000),
                        peer_required=watch is not None)
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    if watch:
        watch.start()
    mc.log("voice listener [%s]: started - source %s, presence %s, gate %s, duty ceiling %d%%, %d wake stream(s), "
           "pre-roll %d ms; it hears nothing until it is armed"
           % (camera, s.source, s.presence_policy, "on" if s.gate else "off", s.duty_max_pct, s.wake_streams,
              s.pre_roll_ms))
    last_status = last_pass = time.monotonic()
    code = 1
    try:
        while not stopping and listener.fatal is None and not listener.ended:
            fd = source.fileno() if listener.armed else None
            try:
                ready, writable, _x = select.select(([fd] if fd is not None else []) + out.read_fds(),
                                                    out.write_fds(), [],
                                                    BATCH_SEC if fd is None and listener.armed else 0.5)
            except (OSError, ValueError):
                out.close()
                out.disabled = True
                ready, writable = [], []
                time.sleep(BATCH_SEC)
            # Sound is taken in batches: a pass that came round early waits out the
            # rest of the batch, so the loop wakes about four times a second.
            early = BATCH_SEC - (time.monotonic() - last_pass)
            if listener.armed and early > 0:
                time.sleep(early)
            mono = last_pass = time.monotonic()
            out.service(set(ready), set(writable))
            out.tick(mono, int(mono * 1000) - listener.last_audio_ms if listener.armed else 0)
            listener.step()
            if mono - last_status >= STATUS_EVERY_SEC:
                write_status(state_dir, camera, listener.status_record())
                last_status = mono
        if listener.fatal is None:
            code = 0
        else:
            mc.log("voice listener [%s]: ending this run - %s" % (camera, listener.fatal), warning=True)
    finally:
        if watch:
            watch.stop()
        listener.close()
        write_status(state_dir, camera, dict(listener.status_record(), stopped_utc_ms=int(time.time() * 1000)))
        out.close()
    return code


# ── check ────────────────────────────────────────────────────────────────────


def check(env_file: str, camera: str) -> int:
    """What the listener would run with and every reason it would refuse.
    Never prints the token, an address or the wake tokens' file. Implements: LLR-1547."""
    report = {"camera": camera}   # type: Dict[str, object]
    try:
        s = load(env_file, camera)
    except Invalid as exc:
        door_relay.emit(dict(report, eligible=False, reasons=[str(exc)]))
        return 1
    reasons = refusals(s, mc.read_knobs(env_file))
    try:
        manifest = ve.read_manifest()
        ve.model_files(s.model_dir, manifest, ve.WAKE_MODEL, ("encoder", "decoder", "joiner", "tokens"))
        ve.model_files(s.model_dir, manifest, ve.STT_MODEL, ("encoder", "merged-decoder", "tokens"))
    except ve.Invalid as exc:
        reasons.append(str(exc))
    engine = ve.engine_version()
    if not engine:
        reasons.append("sherpa_onnx is not importable by this interpreter")
    elif engine != ve.ENGINE_VERSION:
        reasons.append("sherpa_onnx is %s, not the pinned %s" % (engine, ve.ENGINE_VERSION))
    door_relay.emit(dict(report, eligible=not reasons, reasons=reasons, enabled=s.enabled, source=s.source,
                         hears_room=s.hears_room, ruling_named=bool(s.ruling), presence=s.presence_policy,
                         gate=s.gate, duty_max_pct=s.duty_max_pct, wake_streams=s.wake_streams,
                         pre_roll_ms=s.pre_roll_ms, feed_port=s.feed_port, peer_port_set=bool(s.peer_port),
                         token_set=vp.token_ok(s.token), engine=engine or None))
    return 0 if not reasons else 1


# ── trying it on synthetic speech ────────────────────────────────────────────


def read_wav(path: str) -> bytes:
    problem = wav_problem(path)
    if problem and "1 s or more" not in problem:
        raise Invalid("%s %s" % (os.path.basename(path), problem))
    with wave.open(path, "rb") as handle:
        if handle.getframerate() != vg.SAMPLE_RATE or handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise Invalid("%s is not 16 kHz mono 16-bit" % os.path.basename(path))
        return handle.readframes(handle.getnframes())


def faint_noise(byte_count: int, dbfs: float = -65.0, seed: int = 7) -> bytes:
    """Steady quiet-room noise at about `dbfs`, the same every run."""
    import random
    rng = random.Random(seed)
    peak = int(vg.from_dbfs(dbfs) * 1.732)
    return b"".join(int(rng.uniform(-peak, peak)).to_bytes(2, "little", signed=True)
                    for _ in range(byte_count // vg.SAMPLE_BYTES))


def run_clip(s: ListenerSettings, wake, stt, audio: bytes, lead_ms: int, tail_ms: int = 2500,
             keep_text: bool = False) -> Dict[str, object]:
    """One clip through gate, wake word, request and speech-to-text, as fast as
    it will go on a clock that counts the audio. Returns what happened; the
    words only when asked (synthetic speech only)."""
    heard = []   # type: List[Tuple[str, bool]]

    def post(text: str, wake_checked: bool) -> str:
        heard.append((text, wake_checked))
        return "dry-run"

    wake.reset()
    pipeline = new_pipeline(s, wake, stt, post)
    stream = faint_noise(vg.bytes_for(lead_ms)) + audio + faint_noise(vg.bytes_for(tail_ms), seed=8)
    now, wake_at, ended = 0, None, None
    for at in range(0, len(stream) - vg.FRAME_BYTES + 1, vg.FRAME_BYTES):
        now += vg.FRAME_MS
        for event in pipeline.feed(bytearray(stream[at:at + vg.FRAME_BYTES]), now):
            if event.get("show") == vs.SHOW_LISTENING and wake_at is None:
                wake_at = now
            if "outcome" in event or event.get("reason") in (vs.END_NO_SPEECH, vs.END_NOTHING_HEARD):
                ended = ended or now
    record = {"wake": wake_at is not None, "wake_ms_after_clip_start": (wake_at - lead_ms) if wake_at else None,
              "requests": pipeline.counts["requests"], "posted": len(heard), "ended_ms": ended,
              "words": len(heard[0][0].split()) if heard else 0,
              "wake_checked": heard[0][1] if heard else None,
              "gate_open_pct": round(100.0 * pipeline.gate.open_ms / max(1.0, pipeline.gate.open_ms
                                                                         + pipeline.gate.closed_ms), 1),
              "holds_no_audio": pipeline.holds_no_audio}   # type: Dict[str, object]
    if keep_text:
        record["text"] = heard[0][0] if heard else ""
    return record


def clips(env_file: str, camera: str, wavs: Sequence[str], lead_ms: int, show_text: bool, work_dir: str) -> int:
    """`clips`: each WAV of synthetic speech through the whole path. Implements: LLR-1551."""
    s = load(env_file, camera)
    wake, stt = build_engines(s, work_dir)
    fired = 0
    for path in wavs:
        record = run_clip(s, wake, stt, read_wav(path), lead_ms, keep_text=show_text)
        fired += 1 if record["wake"] else 0
        door_relay.emit(dict(record, clip=os.path.basename(path)))
    door_relay.emit({"clips": len(wavs), "wake_heard_in": fired, "gate": s.gate, "wake_streams": s.wake_streams,
                     "pre_roll_ms": s.pre_roll_ms, "lead_ms": lead_ms})
    return 0


def process_cpu_sec() -> float:
    usage = os.times()
    return usage[0] + usage[1]


class TimedStt:
    """Speech-to-text with its CPU and wall time kept apart, so `measure` can
    tell the standing cost (reader, gate, wake word) from the bursts."""

    def __init__(self, stt):
        self.stt, self.cpu_sec, self.walls = stt, 0.0, []   # type: ignore[var-annotated]

    def transcribe(self, audio) -> str:
        cpu, wall = process_cpu_sec(), time.monotonic()
        try:
            return self.stt.transcribe(audio)
        finally:
            self.cpu_sec += process_cpu_sec() - cpu
            self.walls.append(time.monotonic() - wall)


def measure(env_file: str, camera: str, wav: str, seconds: float, realtime: bool, work_dir: str) -> int:
    """`measure`: the listener's own CPU on a looped WAV of synthetic speech,
    paced to the clock: percent of one core, the standing part (everything but
    speech-to-text) apart from the bursts, the share of the time the gate was
    open, wake fires. The hub cost test's harness, and the dev PC's.
    Implements: LLR-1551."""
    s = load(env_file, camera)
    wake, stt = build_engines(s, work_dir)
    timed = TimedStt(stt)
    pipeline = new_pipeline(s, wake, timed, dry_post)
    source = FileAudio(wav, realtime=realtime)
    source.start()
    start_wall, start_cpu = time.monotonic(), process_cpu_sec()
    fed_ms, skipped = 0.0, 0
    while fed_ms < seconds * 1000:
        if realtime:
            time.sleep(BATCH_SEC)               # the service's own rhythm
        frames = source.read_frames(MAX_FRAMES_PER_STEP)
        for frame in frames:
            fed_ms += vg.FRAME_MS
            pipeline.feed(frame, int(fed_ms))
        if pipeline.flush_source or source.waiting_frames() > MAX_FRAMES_PER_STEP:
            dropped = source.flush()            # as the service does: never a backlog
            skipped += dropped
            fed_ms += dropped * vg.FRAME_MS
            pipeline.flush_source = False
    wall, cpu = time.monotonic() - start_wall, process_cpu_sec() - start_cpu
    gate, standing = pipeline.gate, cpu - timed.cpu_sec
    walls = sorted(timed.walls)
    door_relay.emit({"audio_sec": round(fed_ms / 1000.0, 1), "wall_sec": round(wall, 1), "cpu_sec": round(cpu, 2),
                     "pct_one_core": round(100.0 * cpu / wall, 2) if realtime else None,
                     "standing_pct_one_core": round(100.0 * standing / wall, 2) if realtime else None,
                     "standing_cpu_sec_per_audio_min": round(60000.0 * standing / fed_ms, 3),
                     "stt_cpu_sec": round(timed.cpu_sec, 2), "stt_calls": len(walls),
                     "stt_wall_sec_median": round(walls[len(walls) // 2], 3) if walls else None,
                     "stt_wall_sec_max": round(walls[-1], 3) if walls else None,
                     "gate": s.gate,
                     "gate_open_pct": round(100.0 * gate.open_ms / max(1.0, gate.open_ms + gate.closed_ms), 1),
                     "gate_opens": gate.opens, "wake_streams": s.wake_streams, "duty_max_pct": s.duty_max_pct,
                     "duty_refused_sec": round(pipeline.limiter.refused_ms / 1000.0, 1),
                     "wake_fires": pipeline.counts["wake_fires"], "requests": pipeline.counts["requests"],
                     "skipped_frames": skipped, "engine": ve.engine_version()})
    return 0


# ── the room as numbers ──────────────────────────────────────────────────────


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Nearest-rank percentile (q in 0..100), or None for no values."""
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(1, int(-(-q * len(ordered) // 100))) - 1]


def levels(env_file: str, camera: str, seconds: float, sleep: Callable[[float], None] = time.sleep,
           clock: Callable[[], float] = time.monotonic) -> int:
    """`levels`: what the relay's microphone hears, as loudness numbers only.

    The level test nobody has run yet: is speech louder than the room at this
    microphone? It reads the room's sound, so it sits behind the same two locks
    as the service (SOURCE=relay and the ruling's date) and the relay's own
    switches; it needs no MINI-SERV, loads no engine, posts nothing and keeps
    nothing: each frame becomes one number and is zeroed. Prints the level's
    spread in dBFS, the background the gate learned, and how often and for how
    long the gate would have opened. Implements: LLR-1553.
    """
    try:
        s = load(env_file, camera)
    except Invalid as exc:
        return refuse(camera, str(exc))
    env = mc.read_knobs(env_file)
    locked = [r for r in refusals(s, env) if TOKEN_KEY not in r and PORT_KEY not in r and PEER_KEY not in r]
    if not s.hears_room:
        locked = ["%s is not relay: there is no room sound to measure" % knob(camera, "SOURCE")]
    if locked:
        return refuse(camera, "; ".join(locked))
    try:
        relay = camera_relay.CameraRelaySettings(env_file, s.relay_camera)
    except Invalid as exc:
        return refuse(camera, str(exc))
    # Its own container name: measuring the room never takes a running listener's reader away.
    source = RelayAudio(relay, camera, s.relay_query, name="homehub-voice-levels-%s" % camera)
    gate, heard = s.new_gate(), []   # type: ignore[var-annotated]
    mc.log("voice listener [%s]: levels - reading the microphone for %ds; numbers only, nothing is kept"
           % (camera, seconds))
    started = clock()
    source.start()
    try:
        while clock() - started < seconds:
            sleep(BATCH_SEC)
            try:
                frames = source.read_frames(MAX_FRAMES_PER_STEP)
            except (EOFError, OSError):
                break
            for frame in frames:
                level = vg.frame_rms(frame)
                heard.append(vg.to_dbfs(level))
                passed, _change = gate.feed(frame, level, int(len(heard) * vg.FRAME_MS))
                for piece in passed:
                    if piece is not frame:
                        vg.wipe_bytes(piece)
                vg.wipe_bytes(frame)
    finally:
        source.stop()
        gate.release()
    total = gate.open_ms + gate.closed_ms
    door_relay.emit({"camera": camera, "seconds_heard": round(len(heard) * vg.FRAME_MS / 1000.0, 1),
                     "level_dbfs": {name: (round(value, 1) if value is not None else None) for name, value in
                                    (("p10", percentile(heard, 10)), ("p50", percentile(heard, 50)),
                                     ("p90", percentile(heard, 90)), ("p99", percentile(heard, 99)))},
                     "background_dbfs": round(vg.to_dbfs(gate.floor.level), 1), "gate_opens": gate.opens,
                     "gate_open_pct": round(100.0 * gate.open_ms / total, 1) if total else 0.0})
    return 0 if heard else 1


# ── cost, token ──────────────────────────────────────────────────────────────


def unit_cgroup(camera: str) -> Optional[str]:
    done = subprocess.run(["systemctl", "show", "--property", "ControlGroup", "--value",
                           "homehub-voice-listener@%s.service" % camera], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, universal_newlines=True, timeout=20)
    rel = done.stdout.strip()
    return "/sys/fs/cgroup" + rel if done.returncode == 0 and rel.startswith("/") else None


def cost(env_file: str, camera: str, seconds: int) -> int:
    """CPU of the unit, its audio reader and its relay, and PSI, over `seconds`
    while it is ARMED; the verdict is reader + listener against
    BUDGET_ARMED_PCT (a missing part fails: it was not hearing). Implements: LLR-1548."""
    import camera_motion
    try:
        s = load(env_file, camera)
    except Invalid as exc:
        return refuse(camera, str(exc))
    groups = {"detector": unit_cgroup(camera) or "/nonexistent/listener",
              "decoder": door_relay.container_cgroup(container_name(camera)) or "/nonexistent/reader",
              "relay": door_relay.container_cgroup(camera_relay.container(s.relay_camera)) or "/nonexistent/relay"}
    result = door_relay.cost(groups, seconds)
    result.update(camera_motion.cost_verdict(result, s.budget_armed_pct))
    door_relay.emit(result)
    return 0 if result.get("pass") else 1


def import_token(env_file: str, blob: bytes) -> str:
    """Write MINISERV_ACTION_TOKEN from STDIN into the stack .env: one line,
    never in argv, never printed; refused unless it is a token the interface
    would accept. Returns the key name. Implements: LLR-1552."""
    if len(blob) > 1024:
        raise Invalid("the token on stdin is longer than 1 KiB")
    text = blob.decode("ascii", "replace").rstrip("\r\n")
    if not vp.token_ok(text):
        raise Invalid("the token on stdin is not one line of 32 to 256 visible ASCII characters")
    with open(env_file, encoding="utf-8") as handle:
        current = handle.read()
    st = os.stat(env_file)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(env_file)), prefix=".env.tmp-")
    try:
        if hasattr(os, "fchmod"):  # the hub; the dev PC's Windows test runner has neither
            os.fchmod(fd, st.st_mode & 0o7777)
            try:
                os.fchown(fd, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(door_relay.set_env_keys(current, {TOKEN_KEY: door_relay.env_encode(text)}))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, env_file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return TOKEN_KEY


# ── main ─────────────────────────────────────────────────────────────────────


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", default=mc.DEFAULT_ENV_FILE)
    p.add_argument("--camera", required=True)
    sub = p.add_subparsers(dest="verb")
    for verb in ("condition", "check", "run", "import-token"):
        sub.add_parser(verb)
    le = sub.add_parser("levels")
    le.add_argument("--seconds", type=float, default=60)
    cl = sub.add_parser("clips")
    cl.add_argument("--wav", action="append", required=True)
    cl.add_argument("--lead-ms", type=int, default=2000)
    cl.add_argument("--show-text", action="store_true", help="print the words (synthetic speech only)")
    cl.add_argument("--work-dir", default="")
    me = sub.add_parser("measure")
    me.add_argument("--wav", required=True)
    me.add_argument("--seconds", type=float, default=420)
    me.add_argument("--fast", action="store_true", help="not paced to the clock: CPU per minute of audio only")
    me.add_argument("--work-dir", default="")
    co = sub.add_parser("cost")
    co.add_argument("--seconds", type=int, default=1800)
    args = p.parse_args(argv)
    try:
        camera = camera_relay.camera_name(args.camera)
    except Invalid as exc:
        mc.log("voice listener: REFUSED - %s" % exc, warning=True)
        return 2
    if args.verb == "condition":
        on = mc.knob_bool(mc.read_knobs(args.env_file), knob(camera, "ENABLED"), False)
        mc.log("voice listener [%s]: %s is %s" % (camera, knob(camera, "ENABLED"),
                                                  "true - starting" if on else "not true - clean skip"))
        return 0 if on else 1
    if args.verb == "check":
        return check(args.env_file, camera)
    if args.verb == "run":
        return run_service(args.env_file, camera)
    if args.verb == "cost":
        return cost(args.env_file, camera, args.seconds)
    if args.verb == "levels":
        return levels(args.env_file, camera, args.seconds)
    if args.verb == "import-token":
        try:
            key = import_token(args.env_file, sys.stdin.buffer.read(2048))
        except (Invalid, OSError) as exc:
            mc.log("voice listener: REFUSED - %s" % (exc if isinstance(exc, Invalid) else type(exc).__name__),
                   warning=True)
            return 1
        mc.log("voice listener: wrote %s into %s (value not shown)" % (key, args.env_file))
        return 0
    if args.verb in ("clips", "measure"):
        work = args.work_dir or tempfile.mkdtemp(prefix="voice-listener-")
        try:
            if args.verb == "clips":
                return clips(args.env_file, camera, args.wav, args.lead_ms, args.show_text, work)
            return measure(args.env_file, camera, args.wav, args.seconds, not args.fast, work)
        except (Invalid, ve.Invalid) as exc:
            mc.log("voice listener [%s]: REFUSED - %s" % (camera, exc), warning=True)
            return EXIT_REFUSED
        except ImportError as exc:
            mc.log("voice listener [%s]: REFUSED - the voice runtime is not importable (%s)" % (camera, exc.name),
                   warning=True)
            return EXIT_REFUSED
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
