#!/usr/bin/env python3
"""voice_gate.py - what stands between the room's sound and the wake-word engine.

PURE CORE: no I/O, no clock of its own, no engine. Every decision that cuts the
hub voice listener's standing cost, or that decides whether it may hear at all,
is made here from values handed in, so each is tested without a hub.

WHY IT EXISTS. On the hub an always-on wake word costs 15.7 % of one core with
one stream (measured 2026-11-11, HomeHub docs/HUB_VOICE_COST_TEST_2026-11-11.md)
against a proposed limit of 5 %. Nothing makes the engine itself cheaper, so
the listener runs it for less of the time:

    arm_decision     may the listener read the microphone at all right now?
                     (a hold file, MINI-SERV's own state, the room's presence)
    ActivityGate     while armed, is there sound worth giving the engine?
                     (a loudness rule over a learned background level)
    DutyLimiter      a hard ceiling on the engine's share of the time

AUDIO IN THIS FILE is 16 kHz mono signed 16-bit little-endian, in frames of
FRAME_MS. A frame is any bytes-like object. The only audio held here is the
activity gate's pre-roll (WipedBuffer, at most GATE_PRE_ROLL_MS, only while the
listener is armed); it is zeroed when the gate opens, when it is released and
when the listener disarms. Nothing here writes anywhere.

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: SR-164, SR-165, LLR-1531..LLR-1535.
"""

from __future__ import annotations

import array
import collections
import math
import sys
from typing import Deque, Dict, List, Optional, Tuple

try:  # C speed; deprecated in 3.11 and gone in 3.13, so the fallback below is kept and tested
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import audioop as _audioop
except ImportError:  # pragma: no cover - exercised by forcing the fallback in the tests
    _audioop = None

SAMPLE_RATE = 16000
SAMPLE_BYTES = 2
FRAME_MS = 80                      # EntertainmentPC Core's frame (VoiceAudio.DefaultFrameSamples)
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_BYTES
FULL_SCALE = 32768.0
SILENT_DBFS = -120.0


# ── levels ───────────────────────────────────────────────────────────────────


def bytes_for(ms: float) -> int:
    """Whole samples covering `ms`, as a byte count."""
    return int(SAMPLE_RATE * ms / 1000.0) * SAMPLE_BYTES


def ms_for(byte_count: int) -> float:
    return 1000.0 * (byte_count // SAMPLE_BYTES) / SAMPLE_RATE


def rms_pure(frame) -> float:
    """Root mean square of a frame in sample units, without audioop."""
    samples = array.array("h")
    samples.frombytes(bytes(frame[:len(frame) - len(frame) % SAMPLE_BYTES]))
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / float(len(samples)))


def frame_rms(frame) -> float:
    """Root mean square of a frame in sample units (0..32768).

    audioop where the interpreter has it (the hub's 3.12 does), the pure rule
    otherwise; both give the same number to within one unit.
    Implements: LLR-1531.
    """
    if not len(frame):
        return 0.0
    if _audioop is not None:
        return float(_audioop.rms(bytes(frame[:len(frame) - len(frame) % SAMPLE_BYTES]), SAMPLE_BYTES))
    return rms_pure(frame)


def to_dbfs(level: float) -> float:
    return SILENT_DBFS if level <= 0 else max(SILENT_DBFS, 20.0 * math.log10(level / FULL_SCALE))


def from_dbfs(dbfs: float) -> float:
    return FULL_SCALE * math.pow(10.0, dbfs / 20.0)


class NoiseFloor:
    """The background level, followed down quickly and up slowly.

    EntertainmentPC Core's rule (VoiceSession.LearnNoise): a burst of speech
    barely moves it, steady sound becomes background. Here it is learned on
    every frame the gate sees while no request is open, so a television that
    plays on stops holding the gate open: with the defaults, sound of a steady
    level stops counting after about five seconds, and a pause of about a
    second lets the next burst count again.
    Implements: LLR-1531.
    """

    def __init__(self, fall_sec: float = 1.0, rise_sec: float = 10.0):
        self.fall_ms, self.rise_ms = 1000.0 * fall_sec, 1000.0 * rise_sec
        self.level = -1.0   # sample units; below 0: not learned yet

    def learn(self, level: float, ms: float = FRAME_MS) -> None:
        if self.level < 0:
            self.level = level
            return
        tau = self.fall_ms if level < self.level else self.rise_ms
        self.level += (level - self.level) * (1.0 - math.exp(-ms / tau))

    def speech_level(self, min_speech_dbfs: float, over_noise_db: float) -> float:
        """The level a frame must reach to count as sound worth hearing."""
        over = 0.0 if self.level <= 0 else self.level * math.pow(10.0, over_noise_db / 20.0)
        return max(from_dbfs(min_speech_dbfs), over)


# ── the only buffer of room audio in this file ───────────────────────────────


class WipedBuffer:
    """A bounded byte buffer that is zeroed before any part of it is let go.

    Contract:
      Inputs:  capacity: bytes (0 holds nothing).
      Outputs: take() hands the content over as one bytearray, which the caller
               now owns and must wipe (wipe_bytes) when done.
    """

    def __init__(self, capacity: int):
        self.capacity = max(0, int(capacity))
        self._data = bytearray()

    def __len__(self) -> int:
        return len(self._data)

    def write(self, frame) -> None:
        if self.capacity == 0:
            return
        self._data.extend(frame)
        extra = len(self._data) - self.capacity
        if extra > 0:
            self._data[:extra] = bytes(extra)
            del self._data[:extra]

    def take(self) -> bytearray:
        out, self._data = self._data, bytearray()
        return out

    def wipe(self) -> None:
        wipe_bytes(self._data)
        del self._data[:]

    @property
    def is_cleared(self) -> bool:
        return len(self._data) == 0


def wipe_bytes(buffer) -> None:
    """Zero a bytearray (or writable memoryview) in place."""
    buffer[:] = bytes(len(buffer))


# ── the activity gate ────────────────────────────────────────────────────────


class ActivityGate:
    """Lets sound through to the wake-word engine only around sound that stands
    out from the background.

    Closed, each frame goes into a short pre-roll and nowhere else. When
    OPEN_FRAMES of the last OPEN_WINDOW frames stood out, the gate opens and
    hands over the pre-roll first, so the engine hears the phrase from its
    start. Open, every frame passes; the gate closes HANG_MS after the last
    frame that stood out (the engine needs about half a second of sound after
    the phrase before it fires, measured by EntertainmentPC lane J).

    Contract:
      Inputs:  feed(frame, level, now_ms): one frame, its frame_rms, a clock in
               milliseconds that only goes forward.
      Outputs: (frames to hand on, "opened"|"closed"|None). Pre-roll frames
               are copies the caller wipes; a passed frame is the caller's own.
      Config:  min_speech_dbfs (-50), over_noise_db (8): Core's speech rule;
               open_frames (2) of open_window (3); hang_ms (1200);
               pre_roll_ms (480); enabled (False passes everything).
    Implements: LLR-1532.
    """

    def __init__(self, min_speech_dbfs: float = -50.0, over_noise_db: float = 8.0, open_frames: int = 2,
                 open_window: int = 3, hang_ms: int = 1200, pre_roll_ms: int = 480,
                 floor: Optional[NoiseFloor] = None, enabled: bool = True):
        self.min_speech_dbfs, self.over_noise_db = min_speech_dbfs, over_noise_db
        self.open_frames, self.hang_ms, self.enabled = open_frames, hang_ms, enabled
        self.floor = floor or NoiseFloor()
        self.pre_roll = WipedBuffer(bytes_for(pre_roll_ms))
        self.recent = collections.deque(maxlen=max(open_window, open_frames))  # type: Deque[bool]
        self.is_open = False
        self.last_active_ms = 0
        self.opens = 0
        self.open_ms = 0.0
        self.closed_ms = 0.0

    def threshold(self) -> float:
        return self.floor.speech_level(self.min_speech_dbfs, self.over_noise_db)

    def feed(self, frame, level: float, now_ms: int) -> Tuple[List[bytearray], Optional[str]]:
        active = level >= self.threshold()
        self.floor.learn(level, ms_for(len(frame)))
        if not self.enabled:
            self.open_ms += ms_for(len(frame))
            return [frame], None
        if self.is_open:
            self.open_ms += ms_for(len(frame))
            if active:
                self.last_active_ms = now_ms
            if now_ms - self.last_active_ms >= self.hang_ms:
                self._close()
                return [frame], "closed"
            return [frame], None
        self.pre_roll.write(frame)
        self.recent.append(active)
        if sum(self.recent) < self.open_frames:
            self.closed_ms += ms_for(len(frame))
            return [], None
        self.is_open = True
        self.opens += 1
        self.last_active_ms = now_ms
        self.recent.clear()
        held = self.pre_roll.take()
        self.open_ms += ms_for(len(held))
        frames = [held[i:i + FRAME_BYTES] for i in range(0, len(held), FRAME_BYTES)]
        wipe_bytes(held)
        return frames, "opened"

    def _close(self) -> None:
        self.is_open = False
        self.recent.clear()
        self.pre_roll.wipe()

    def release(self) -> None:
        """Close and forget: after a request, a duty stop or a disarm. The learned
        background level is kept (it is a number, not sound)."""
        self._close()

    @property
    def holds_no_audio(self) -> bool:
        return self.pre_roll.is_cleared


# ── a hard ceiling on the engine's share of the time ─────────────────────────


class DutyLimiter:
    """A token bucket over the audio handed to the wake-word engine.

    `max_pct` of the wall clock may be spent feeding the engine, with at most
    `burst_ms` saved up. 100 is no limit. When the bucket is empty the listener
    stops feeding (it is deaf until the bucket refills), which is the price of
    a guaranteed ceiling; the count of refused milliseconds is reported so the
    price is visible. Implements: LLR-1533.
    """

    def __init__(self, max_pct: int = 100, burst_ms: int = 10000):
        self.max_pct = max(1, min(100, int(max_pct)))
        self.burst_ms = float(max(FRAME_MS, burst_ms))
        self.tokens = self.burst_ms
        self.last_ms = None   # type: Optional[int]
        self.refused_ms = 0.0
        self.spent_ms = 0.0

    @property
    def unlimited(self) -> bool:
        return self.max_pct >= 100

    def allow(self, now_ms: int, ms: float = FRAME_MS) -> bool:
        if self.unlimited:
            self.spent_ms += ms
            return True
        if self.last_ms is not None and now_ms > self.last_ms:
            self.tokens = min(self.burst_ms, self.tokens + (now_ms - self.last_ms) * self.max_pct / 100.0)
        self.last_ms = now_ms
        if self.tokens >= ms:
            self.tokens -= ms
            self.spent_ms += ms
            return True
        self.refused_ms += ms
        return False

    def ready(self, now_ms: int) -> bool:
        """Enough saved up to be worth opening for again (two seconds of engine time)."""
        if self.unlimited:
            return True
        if self.last_ms is not None and now_ms > self.last_ms:
            self.tokens = min(self.burst_ms, self.tokens + (now_ms - self.last_ms) * self.max_pct / 100.0)
            self.last_ms = now_ms
        return self.tokens >= min(self.burst_ms, 2000.0)


# ── may the listener read the microphone at all? ─────────────────────────────

PRESENCE_POLICIES = ("required", "ignored")

# Reasons, in the order they are tested. Each is also the `detail` the status
# file and the feed show, so the household can see WHY nothing is listening.
DISARM_HOLD = "hold"                        # the hold file exists: someone switched listening off
DISARM_PEER_UNREACHABLE = "peer-unreachable"  # MINI-SERV's action interface does not answer
DISARM_PEER_MUTED = "peer-muted"            # Bruno is muted on MINI-SERV: a mute there mutes the hub's ear
DISARM_PEER_LISTENING = "peer-listening"    # MINI-SERV's own listener is armed: never two listeners
DISARM_PRESENCE_UNKNOWN = "presence-unknown"
DISARM_ROOM_EMPTY = "room-empty"
ARMED = "armed"


class PeerState:
    """What MINI-SERV's GET /v1/state last said (bruno.state), or that it did not answer."""

    __slots__ = ("reachable", "state", "voice")

    def __init__(self, reachable: bool = False, state: str = "", voice: str = ""):
        self.reachable, self.state, self.voice = reachable, state, voice


class PresenceReading:
    """The presence detector's last word: known at all, and present or not."""

    __slots__ = ("known", "present", "age_sec")

    def __init__(self, known: bool = False, present: bool = False, age_sec: Optional[float] = None):
        self.known, self.present, self.age_sec = known, present, age_sec


def read_presence(status: Optional[Dict[str, object]], now_utc_ms: int, stale_sec: float,
                  event: str = "living_room.presence") -> PresenceReading:
    """The presence detector's status file (stack/camera-presence, status.json)
    as a reading. Unknown when the file is absent or not a record, the detector
    stopped, it has not judged its first frames, its newest frame is older than
    `stale_sec` (the file is rewritten every 60 s and on every change, so the
    limit must exceed a minute), or it carries no presence record.
    Implements: LLR-1534.
    """
    if not isinstance(status, dict) or status.get("stopped_utc_ms") or not status.get("known"):
        return PresenceReading()
    last = status.get("last_frame_utc_ms")
    if not isinstance(last, (int, float)) or isinstance(last, bool):
        return PresenceReading()
    age = (now_utc_ms - last) / 1000.0
    if age > stale_sec or age < -60.0:
        return PresenceReading(age_sec=age)
    for record in status.get("state") or []:
        if isinstance(record, dict) and record.get("event") == event and isinstance(record.get("present"), bool):
            return PresenceReading(True, bool(record["present"]), age)
    return PresenceReading(age_sec=age)


def arm_decision(hold: bool, peer: Optional[PeerState], presence: Optional[PresenceReading],
                 presence_policy: str = "required", peer_required: bool = True) -> Tuple[bool, str]:
    """(armed, reason): whether the listener may read the microphone now.

    Disarmed means the audio reader is stopped and nothing of the room reaches
    the hub listener at all. The rules, first match wins:
      - the hold file exists;
      - (peer_required) MINI-SERV's action interface does not answer: a
        transcript would have nowhere to go, so nothing is heard;
      - MINI-SERV reports its voice `muted`: the household's mute holds here too;
      - MINI-SERV reports its voice `armed`: its own listener is on, and two
        listeners would act twice on one sentence;
      - (presence `required`) the presence detector does not know, or says
        nobody is in the room.
    With peer_required false (the synthetic file source) the peer is not asked.
    Implements: LLR-1535.
    """
    if hold:
        return False, DISARM_HOLD
    if peer_required:
        if peer is None or not peer.reachable:
            return False, DISARM_PEER_UNREACHABLE
        if peer.voice == "muted":
            return False, DISARM_PEER_MUTED
        if peer.voice == "armed":
            return False, DISARM_PEER_LISTENING
    if presence_policy == "required":
        if presence is None or not presence.known:
            return False, DISARM_PRESENCE_UNKNOWN
        if not presence.present:
            return False, DISARM_ROOM_EMPTY
    return True, ARMED
