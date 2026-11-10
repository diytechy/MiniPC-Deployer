#!/usr/bin/env python3
"""Power the speaker amplifier while audio is playing.

The level detector is independent of the actuator. The only actuator is one
channel of the measured LCUS-2 USB relay, commanded through its CH340 serial
interface (ratified 2026-09-13). The retired alternative put a continuous tone
on the ALC255 headphone jack for a far-end rectifier; that jack is now free and
belongs to the headset leg of the item 23 audio routing redesign, so nothing
here may open it.

WHY A DETECTOR AND NOT AN ANALOG ONE ON THE AMPLIFIER FEED (measured
2026-09-12): the feed is downstream of the volume control, so an analog
detector's sensitivity would track the rocker and quiet listening would never
switch the amplifier on.

WHY BOTH CAPTURE SOURCES: the line input carries only the desktop. The kiosk's
Pandora and local library play digitally and never pass through it, so a
detector watching the line input alone would leave the amplifier off whenever
the panel played its own music. The Loopback tap is what closes that.

THE ONE LINE THAT MATTERS MOST is the DC removal. The adapter's line input
carries a large DC offset -- measured 571 LSB left, 708 right, about -35 dBFS --
so a naive RMS reads -33 dBFS in total silence and would hold the amplifier on
permanently. With DC stripped the floor is -83 dBFS, and that 50 dB window is
where every threshold here lives.

NO NEW DEPENDENCIES: the panel's installer is an offline, hash-locked
wheelhouse, so capture is done by driving arecord rather than importing a
binding, and the relay is driven over a plain POSIX serial file descriptor.

SPEAKER SLEEPS WITH THE SCREEN (SR-140, 2026-11-01). In bus mode, in the Speaker
position, with the persisted preference on (the default), this daemon also
follows the panel's backlight: when the screen goes dark the speaker leg is
faded to silence through its own softvol, THEN the relay is opened and verified;
when the screen lights with audio present the relay is closed and verified, the
boards get 8.5 seconds to start, and the fade comes back up. See SpeakerSleep.

THE SETTLE IS ABOUT THE RELAY, NOT THE SCREEN (SR-143, Owner 2026-11-05: "wait 5
seconds after relay turns on before fading in audio, otherwise can fade in as
soon as presence is detected"; Owner 2026-11-09: 8.5 s settle, with the one-second
ramp unchanged). In the Speaker position every close of the relay
is followed by the same settle and fade, whichever rule closed it, and whether
or not the sleep preference is on. A relay that is already closed has nothing to
settle, so the fade starts at once.

Exit codes: 78 configuration unusable.
"""

import errno
import json
import math
import os
import posixpath
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time

RATE = 48000
CHANNELS = 2
BLOCK_SECONDS = 0.1
BLOCK_FRAMES = int(RATE * BLOCK_SECONDS)
BLOCK_BYTES = BLOCK_FRAMES * CHANNELS * 2

# Capture sources. Each is (pcm name, threshold offset in dB).
#
# The offset exists because full scale arrives at very different levels by
# source: -13 dBFS through the line input, where the desktop's output is
# attenuated 12.9 dB by the analog path, and about 0 dBFS from the kiosk, which
# is digital. A single threshold tuned on the kiosk would drop out during quiet
# desktop content.
SOURCES = (
    ("linein_shared", -13.0),
    ("kiosk_monitor", 0.0),
)

# BUS MODE WATCHES ONE PCM, AND IT IS NOT A SOURCE (item 23, review finding 2).
# `speaker_tap` is the second loopback substream that sits between the switch
# and the amplifier feed, so:
#   * in Headset or Mute the forwarders that fill it are stopped, the tap goes
#     quiet, and the ordinary hold-off runs it down -- no immediate relay open,
#     which is the Owner's amendment to finding 1, and no branch here at all;
#   * a source playing into the bus while the switch is elsewhere is invisible,
#     which is the whole complaint;
#   * the tap is upstream of the volume control, so a quiet room does not drop
#     the amplifier.
# The offset is 0: everything reaching the tap is digital full scale, unlike the
# analog line input's measured -13 dB.
BUS_SOURCES = (
    ("speaker_tap", 0.0),
)


def sources_for(mode):
    """The capture PCMs this mode's detector reads. Implements: SR-028, LLR-014."""
    return BUS_SOURCES if mode == "bus" else SOURCES


def log(message):
    print(message, file=sys.stderr, flush=True)


def _reap(proc):
    """Terminate, then kill, then ALWAYS wait.

    A child that is killed but never waited on stays a zombie for the life of
    this service, and this one respawns children continuously.
    """
    for step in (proc.terminate, proc.kill):
        try:
            step()
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            break
    try:
        proc.wait(timeout=2)
    except (OSError, subprocess.SubprocessError):
        pass


def _env(name, default, lo=None, hi=None):
    """Tunable from /etc/wall-panel/amp-trigger.env without editing this file.

    Every one of these is a number someone will want to change after living with
    it for a week, and a bad value must not stop the amplifier working -- so an
    unparseable override falls back to the default rather than failing to start.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        log("ignoring unparseable %s=%r" % (name, raw))
        return default
    # Syntactic validity is not enough. nan compares false against everything,
    # so a nan hold-off leaves the amplifier on forever and a nan attack leaves
    # it off forever -- both silent. Infinities and negative durations do the
    # same in other directions.
    if not math.isfinite(value):
        log("ignoring non-finite %s=%r" % (name, raw))
        return default
    if lo is not None and value < lo:
        log("clamping %s=%r up to %g" % (name, raw, lo))
        return lo
    if hi is not None and value > hi:
        log("clamping %s=%r down to %g" % (name, raw, hi))
        return hi
    return value


class ConfigurationError(ValueError):
    """An actuator selection cannot be made safe or deterministic."""


# Thresholds on the DC-stripped RMS of the louder channel, relative to each
# source's nominal full scale. The measured floor is -83 dBFS, so -60 sits 23 dB
# clear of it. Hysteresis, because a single threshold chatters at the boundary.
ON_DBFS = _env("WALL_AMP_ON_DBFS", -60.0, lo=-110.0, hi=0.0)
OFF_DBFS = _env("WALL_AMP_OFF_DBFS", -68.0, lo=-110.0, hi=0.0)
if OFF_DBFS >= ON_DBFS:
    # Reversed or equal thresholds are not hysteresis; they are a relay that
    # cycles while the same signal is still playing.
    log("OFF_DBFS %.1f >= ON_DBFS %.1f; forcing 8 dB of hysteresis"
        % (OFF_DBFS, ON_DBFS))
    OFF_DBFS = ON_DBFS - 8.0

# A click or a pop must not cycle the relay.
ATTACK_SECONDS = _env("WALL_AMP_ATTACK_SECONDS", 0.3, lo=0.0, hi=60.0)
# Gaps between tracks, dialogue pauses and quiet passages all dip below any
# usable threshold, so the hold-off is minutes rather than seconds.
HOLD_OFF_SECONDS = _env("WALL_AMP_HOLD_OFF_SECONDS", 240.0, lo=0.0, hi=86400.0)
# Protect the relay contacts and the amplifier from rapid cycling whatever the
# audio does.
MIN_ON_SECONDS = _env("WALL_AMP_MIN_ON_SECONDS", 30.0, lo=0.0, hi=3600.0)
MIN_OFF_SECONDS = _env("WALL_AMP_MIN_OFF_SECONDS", 10.0, lo=0.0, hi=3600.0)

# Do not reopen a failing serial device on every 100 ms block.
ACTUATOR_RETRY_SECONDS = 5.0
# ...but five seconds is too slow for the first few, and terra found exactly why
# (2026-09-13): after a port move this service restarts, and if the CH340 is
# still missing when the startup wait gives up, the next attempt on a five
# second beat lands at about eleven seconds -- outside the ten the acceptance
# allows -- for a relay that was actually back at seven. So for a bounded spell
# after start, retry once a second. Bounded, because a relay that is genuinely
# absent must not be probed once a second for the life of the panel, and
# restricted to the relay, which is the only actuator there is.
ACTUATOR_FAST_RETRY_SECONDS = 1.0
ACTUATOR_FAST_RETRY_WINDOW = 15.0
# How long a service START waits for the relay's device node before giving up.
#
# ITEM 25 (2026-09-13): this service is now stopped and restarted by the USB
# adapter's re-enumeration, and the LCUS-2 sits on the SAME hub. The restart can
# therefore land while the CH340 is still being re-probed and /dev/wall-amp-relay
# does not exist yet. Without this wait the start-up ensure_off() fails on the
# first ENOENT, main() returns 1, and the amplifier stays dark for a RestartSec
# -- with the relay possibly still latched ON from before. Bounded on purpose: a
# relay that is genuinely absent must still fail, and be seen to fail.
#
# SIX SECONDS, NOT TWENTY, and the number comes from the acceptance budget
# rather than from a guess about USB: this wait happens BEFORE the capture
# threads start, so every second of it is a second the detector is not
# measuring and the amplifier is not coming back on. Ten seconds is the whole
# budget. A relay still missing at six is picked up either by the in-loop
# ACTUATOR_RETRY_SECONDS reopen -- which opens a fresh transport too -- or by
# the next RestartSec, and neither of those blocks the detector.
RELAY_WAIT_SECONDS = _env("WALL_AMP_RELAY_WAIT_SECONDS", 6.0, lo=0.0, hi=300.0)
RELAY_WAIT_INTERVAL = 1.0
SAFE_STATE_FILE = "/run/wall-amp-trigger/off-verified"

# A source whose last block is older than this is treated as silent.
#
# THIS IS NOT BELT AND BRACES, IT IS LOAD-BEARING (measured 2026-09-12): when the
# kiosk stops playing, the loopback's playback side closes and its capture
# STALLS rather than returning silence. arecord simply delivers no more blocks,
# so without this the last level -- a loud one -- stands forever and the
# amplifier never powers down. The off path did not fire at all until this
# existed.
STALE_SECONDS = 1.0

MODE_FILE = "/etc/wall-panel/audio-mode"


# ── speaker sleeps with the screen (SR-140) ───────────────────────────────
# Where each fact comes from. All read-only: this daemon owns the relay and the
# fade control and NOTHING else here. The kiosk writes the backlight, the
# applier owns the state file, wall-bt-call owns the call document.
BACKLIGHT_ROOT = "/sys/class/backlight"
AUDIO_STATE_FILE = "/etc/wall-panel/audio-state.json"
AUDIO_CARDS_FILE = "/etc/wall-panel/audio-cards.env"
CALL_STATE_FILE = "/run/wall-panel/call-state.json"
# The same bound the broker puts on this document (routed_backend
# MAX_DOCUMENT_AGE_SECONDS): wall-bt-call touches it while a call continues, so
# an older file means the supervisor died and its last word is not a fact.
CALL_MAX_AGE_SECONDS = 30.0
# The fade stage: a softvol declared in asound-bus-mode.conf as pcm.speaker_fade.
# 61 steps of 1 dB, value 60 = unity (0 dB), value 0 = -60 dB. It is NOT the
# panel's volume ("Bus Playback Volume"), and nothing here may ever write that.
FADE_CONTROL = "Speaker Fade Playback Volume"
FADE_UNITY = 60
FADE_FLOOR = 0
DEFAULT_LOOPBACK_CARD = "Loopback"
AMIXER = "/usr/bin/amixer"
FADE_OUT_SECONDS = _env("WALL_AMP_SLEEP_FADE_OUT_SECONDS", 2.0, lo=0.1, hi=10.0)
FADE_IN_SECONDS = _env("WALL_AMP_SLEEP_FADE_IN_SECONDS", 1.0, lo=0.1, hi=10.0)
# After the relay closes, how long the amplifier boards get before audio
# returns. A cold start is when they reliably select AUX. The Owner's wait is
# 8.5 seconds (SR-143, 2026-11-09); previously five (2026-11-05), and one before.
WAKE_SETTLE_SECONDS = _env("WALL_AMP_SLEEP_WAKE_SETTLE_SECONDS", 8.5, lo=0.0, hi=10.0)
FADE_STEP_SECONDS = 0.05
# How often the files above are re-read when no ramp is running.
SLEEP_SAMPLE_SECONDS = 0.5
# Consecutive failed fade writes after which a ramp is abandoned: the relay must
# still open on time, so an unwritable control costs the fade and nothing else.
FADE_FAIL_LIMIT = 3
FADE_WRITE_TIMEOUT_SECONDS = 1.0
# While the unity value is unconfirmed (the control does not exist until the
# speaker leg has opened its PCM once), how often to try again.
UNITY_RETRY_SECONDS = 30.0
# While the machine is enabled and the screen is lit, an unconfirmed unity is a
# quiet room, so it is retried on the actuator beat instead (see step()).


# The modes in which this daemon commands the amplifier at all. `panel` sends
# everything out the panel's own speaker and never touches the relay; `trigger`
# and `bus` both feed the amplifier, and differ only in which PCM is watched
# (see sources_for).
COMMANDING_MODES = ("trigger", "bus")


def current_mode():
    """trigger, panel or bus. A missing file means trigger, as it always has."""
    try:
        with open(MODE_FILE) as fh:
            value = fh.read().strip()
    except OSError:
        return "trigger"
    return value if value in ("trigger", "panel", "bus") else "trigger"


# ── bus telemetry (item L, coordinator addendum 2026-09-14) ─────────────────
# WHY IT LIVES HERE AND NOT IN THE BROKER. Panel telemetry -- the levels the
# shell's visualizer draws -- has been reporting `available: false` since the
# switch backend replaced the routed one, and the reason is structural rather
# than an oversight: the broker runs as `panel` under ProtectSystem=strict with
# PrivateDevices=true and AF_UNIX as its only address family. It cannot open a
# sound device at all, and giving it one would move the audio-capture privilege
# to the process the renderer talks to, which is the one boundary SR-023 exists
# to hold.
#
# THIS SERVICE ALREADY HAS THE CAPTURE. It opens `speaker_tap` -- the post-
# switch, pre-volume tap -- every 100 ms for the amplifier detector, and has
# done since step 3. So the telemetry is a by-product of a capture that is
# already running: NO second capture is started, no routing changes, and when
# the switch leaves Speaker the tap goes quiet and the telemetry says so,
# exactly as the relay hold-off already does.
#
# THE SHAPE IS A FILE, for the same reason the AEC status block is: the broker
# can read /run, and a file is the only channel that crosses this privilege
# boundary without opening a new one.
BUS_TELEMETRY_FILE = "/run/wall-amp-trigger/bus-telemetry.json"
# 5 Hz. The visualizer is a wall decoration and the renderer polls at 4 Hz
# anyway (js/main.js), so publishing faster would be work nobody reads. It is
# also HALF the block rate, which is what keeps the band analysis below off
# every other block and bounds its cost.
BUS_TELEMETRY_INTERVAL = 0.2
# Band edges in Hz, geometric from 60 Hz. EIGHT bands, and the top one stops at
# 6 kHz because the analysis below runs on a 4x-decimated mono signal whose
# Nyquist is 6 kHz. That is a real limitation and it is written down rather than
# hidden: this is a loudness-per-band readout for a decoration, not a
# spectrum analyser, and the alternative -- a full-rate filter bank in Python on
# a 100 ms budget -- would put the amplifier detector's own duty at risk.
BUS_BANDS_HZ = (60.0, 120.0, 240.0, 480.0, 960.0, 1900.0, 3400.0, 5200.0)
# DECIMATE BY TWO, NOT FOUR, AND THE DIFFERENCE IS AN HONEST TOP BAND (terra,
# second pass). At 4x the decimated Nyquist is 6 kHz and a 4-tap boxcar keeps
# about 57 % of a 6.8 kHz tone, which then folds down and LIGHTS the band
# labelled 5.2 kHz -- a readout that is not imprecise but wrong about where the
# energy is. At 2x the Nyquist is 12 kHz, the same boxcar's first null sits
# exactly there, and nothing in the audible band folds into a displayed one at
# all. It costs twice the Goertzel work on every other block, which is a price
# worth paying for a number that means what its label says.
BUS_DECIMATE = 2
# The boxcar length, which stays at four: its first null is at RATE/4 = 12 kHz,
# exactly the decimated Nyquist, so it is the anti-alias filter this decimation
# needs rather than merely a smoothing of it.
BUS_BOXCAR = 4
# The dB window the published 0..1 band values are mapped onto. The same floor
# the canceller's level scalar uses, and the same reasoning: linear in decibels,
# because a band linear in AMPLITUDE sits at zero until somebody shouts.
BUS_FLOOR_DBFS = -60.0
BUS_REFERENCE_DBFS = -12.0


def band_levels(samples, channels, frames):
    """Eight bounded band energies in [0,1] from one interleaved block.

    ONE GOERTZEL PER BAND OVER A DECIMATED MONO SUM, which is the cheapest thing
    that answers the question honestly. The cost is `bands * frames / decimate`
    multiply-adds -- about 9,600 for a 100 ms block, against the roughly 9,600
    the detector's own high-pass already spends -- so it roughly doubles a cost
    that was already comfortable, and it runs on every OTHER block.

    Goertzel gives the energy at ONE frequency, not in a band, so what comes out
    is a sample of the spectrum at the band centre rather than an integral over
    it. For a decoration that is the right trade: it tracks what a listener
    hears move, it cannot be made expensive by a busy signal, and calling it a
    band level rather than a band energy would be the overclaim.

    Inputs:  samples: the unpacked interleaved block
             channels/frames: its shape
    Outputs: a list of 8 floats in [0,1]
    Implements: SR-028, LLR-015
    """
    # ANTI-ALIAS BEFORE DECIMATING, WHICH PICKING EVERY FOURTH SAMPLE DOES NOT
    # (terra, 2026-09-14). Without it a 6.8 kHz tone folds down to 5.2 kHz and
    # lights the band labelled 5.2 kHz -- a readout that is not merely imprecise
    # but actively wrong about where the energy is. Averaging each group of four
    # is a 4-tap boxcar: a crude low-pass, with its first null exactly at the
    # decimated Nyquist, and it costs nothing because the samples are being
    # summed anyway.
    step = BUS_DECIMATE * channels
    span = BUS_BOXCAR * channels
    mono = []
    divisor = float(channels * BUS_BOXCAR * 32768)
    for i in range(0, frames * channels - span, step):
        total = 0.0
        for tap in range(BUS_BOXCAR):
            base = i + tap * channels
            for ch in range(channels):
                total += samples[base + ch]
        mono.append(total / divisor)
    count = len(mono)
    if count < 64:
        return [0.0] * len(BUS_BANDS_HZ)
    rate = RATE / BUS_DECIMATE
    out = []
    for centre in BUS_BANDS_HZ:
        if centre >= rate / 2.0:
            out.append(0.0)
            continue
        coefficient = 2.0 * math.cos(2.0 * math.pi * centre / rate)
        s1 = s2 = 0.0
        for value in mono:
            s0 = value + coefficient * s1 - s2
            s2 = s1
            s1 = s0
        power = s1 * s1 + s2 * s2 - coefficient * s1 * s2
        # Normalised by the block length so the figure does not grow with it,
        # and expressed as an amplitude so the dB mapping below is the same one
        # the canceller uses.
        amplitude = math.sqrt(max(0.0, power)) * 2.0 / count
        out.append(scalar_from_amplitude(amplitude))
    return out


def scalar_from_amplitude(amplitude):
    """An amplitude in [0,1] as a bounded 0..1 scalar, linear in DECIBELS.

    The same normalisation the canceller's microphone level uses and for the
    same reason, with a reference 6 dB higher because this is a programme signal
    on a bus rather than a voice in a room.
    """
    if not amplitude > 0.0:
        return 0.0
    dbfs = 20.0 * math.log10(amplitude)
    span = BUS_REFERENCE_DBFS - BUS_FLOOR_DBFS
    return max(0.0, min(1.0, (dbfs - BUS_FLOOR_DBFS) / span))


def publish_bus_telemetry(level, active, path=BUS_TELEMETRY_FILE):
    """Write one bounded telemetry document, atomically.

    Contract:
      Inputs:  level: the Level watching `speaker_tap`, or None when this mode
                      has no bus tap at all
               active: whether the amplifier is currently commanded on
      Outputs: None; writes `path`
      Config:  none
      Raises:  nothing -- telemetry must never be able to stop the detector.
    Implements: SR-028, LLR-015
    """
    try:
        fresh = level is not None and level.alive and not level.stale
        document = {
            "schema": 1,
            # NAMED, so the renderer can never present a level from somewhere
            # else as the bus. `speaker_tap` is post-switch and pre-volume: it
            # carries what the room is being sent, not what the room is set to.
            "source": "speaker_tap",
            "valid": bool(fresh),
            "active": bool(active),
            "rms": scalar_from_amplitude(
                10.0 ** (level.dbfs / 20.0)) if fresh else 0.0,
            "peak": level.peak if fresh else 0.0,
            "bands": list(level.bands) if fresh else [0.0] * len(BUS_BANDS_HZ),
            "observed_monotonic_ms": int(level.updated * 1000) if fresh else 0,
            "reference_dbfs": BUS_REFERENCE_DBFS,
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        payload = json.dumps(document, sort_keys=True) + "\n"
        temporary = path + ".new"
        # NO fsync, DELIBERATELY, and this is the one place in this tree where
        # that is the right call (terra, 2026-09-14). /run is tmpfs and this
        # document is disposable -- it is republished five times a second and
        # means nothing after a reboot -- while the write happens inline in the
        # loop that decides when the amplifier switches. Paying a durability
        # barrier for a decoration, on the relay's own beat, trades something
        # that matters for something that does not. The atomic rename stays:
        # a reader must never see half a document.
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary, path)
        # 0644: the broker runs as `panel` and has to read it. There is nothing
        # secret in a loudness figure, and the directory is already 0755.
        os.chmod(path, 0o644)
    except (OSError, ValueError) as exc:
        # NEVER FATAL. A full /run or a missing directory must not stop the
        # amplifier detector, whose job is the relay and not the decoration.
        log("bus telemetry: %s" % exc)


class Level:
    """DC-stripped block RMS for one capture source.

    The high-pass is a continuous one-pole at about 8 Hz, not a per-block mean
    subtraction: subtracting each block's mean also attenuates genuine
    low-frequency content, which would bias the detector against bass-heavy
    material.
    """

    def __init__(self, pcm, offset_db):
        self.pcm = pcm
        self.offset_db = offset_db
        self.dbfs = -120.0
        # Item L's by-products. They are published, never acted on: the
        # detector's own decision is `dbfs` against the two thresholds and
        # nothing here may change it.
        self.peak = 0.0
        self.bands = [0.0] * len(BUS_BANDS_HZ)
        self._band_turn = False
        self.alive = False
        self.updated = 0.0
        self._hp = [0.0, 0.0]
        self._prev = [0.0, 0.0]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        # 2*pi*fc/rate for a one-pole high-pass; fc = 8 Hz.
        k = 1.0 / (1.0 + 2 * math.pi * 8.0 / RATE)
        backoff = 1.0
        while not self._stop.is_set():
            proc = None
            try:
                proc = subprocess.Popen(
                    ["/usr/bin/arecord", "-D", self.pcm, "-f", "S16_LE",
                     "-r", str(RATE), "-c", str(CHANNELS), "-q", "-t", "raw"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.alive = True
                while not self._stop.is_set():
                    raw = proc.stdout.read(BLOCK_BYTES)
                    if not raw or len(raw) < BLOCK_BYTES:
                        break
                    # Reset only once data has actually flowed. Resetting on a
                    # successful Popen instead means a PCM that fails to open --
                    # which happens inside the child, so nothing is raised here --
                    # respawns arecord every second forever and never reaches the
                    # backoff this loop exists to apply.
                    backoff = 1.0
                    samples = struct.unpack("<%dh" % (len(raw) // 2), raw)
                    peak_rms = 0.0
                    for ch in range(CHANNELS):
                        acc = 0.0
                        hp, prev = self._hp[ch], self._prev[ch]
                        for i in range(ch, len(samples), CHANNELS):
                            x = float(samples[i])
                            hp = k * (hp + x - prev)
                            prev = x
                            acc += hp * hp
                        self._hp[ch], self._prev[ch] = hp, prev
                        peak_rms = max(peak_rms, math.sqrt(acc / BLOCK_FRAMES))
                    self.dbfs = (20 * math.log10(peak_rms / 32768.0)
                                 if peak_rms > 0 else -120.0)
                    # Item L. Computed from the block ALREADY in hand -- no
                    # second capture, no routing change -- and on every OTHER
                    # block, which is what bounds the cost against the
                    # detector's own 100 ms budget. The peak is free; the bands
                    # are not, so they alternate.
                    self.peak = max(abs(v) for v in samples) / 32768.0 if samples else 0.0
                    self._band_turn = not self._band_turn
                    if self._band_turn:
                        self.bands = band_levels(samples, CHANNELS, BLOCK_FRAMES)
                    self.updated = time.monotonic()
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    log("capture %s: %s" % (self.pcm, exc))
            finally:
                self.alive = False
                self.dbfs = -120.0
                # A dead capture publishes zeros, not the last thing it saw. A
                # frozen visualizer is indistinguishable from a quiet room.
                self.peak = 0.0
                self.bands = [0.0] * len(BUS_BANDS_HZ)
                if proc is not None:
                    # ALSA reports open failures on the CHILD's stderr, so
                    # discarding it makes a bad PCM completely silent in the
                    # journal while the service still reports active.
                    try:
                        if proc.stderr is not None:
                            err = proc.stderr.read(2048).decode("utf-8", "replace")
                            err = err.strip()
                            if err:
                                log("capture %s: %s" % (self.pcm, err.splitlines()[0]))
                    except (OSError, ValueError):
                        pass
                    _reap(proc)
            # A source that is absent in this mode (kiosk_monitor when the
            # Loopback is not configured) must not spin.
            self._stop.wait(backoff)
            # Capped at 8 s, not 30 (item 25, 2026-09-13). The cap is there so a
            # source that is absent in this mode does not spin, and 8 s costs
            # nothing for that. 30 s did cost something: after a re-enumeration
            # this service restarts, and a capture that opens on the second or
            # third attempt must be delivering levels inside the ten seconds the
            # acceptance allows for the amplifier to come back on.
            backoff = min(backoff * 2, 8.0)

    @property
    def stale(self):
        return time.monotonic() - self.updated > STALE_SECONDS

    def above(self, db):
        if not self.alive or self.stale:
            return False
        return self.dbfs > (db + self.offset_db)

    def report(self):
        if not self.alive:
            return "%s down" % self.pcm
        if self.stale:
            return "%s stalled" % self.pcm
        return "%s %.1f dBFS" % (self.pcm, self.dbfs)


def lcus2_command(channel, enabled):
    """Return the four-byte LCUS-2 command proven on the physical board."""
    if channel not in (1, 2) or not isinstance(enabled, bool):
        raise ValueError("LCUS-2 channel/state is invalid")
    operation = 1 if enabled else 0
    return bytes((0xA0, channel, operation, (0xA0 + channel + operation) & 0xff))


_LCUS2_STATUS = re.compile(r"^CH([12]): (ON|OFF)$")


def parse_lcus2_status(payload):
    """Parse the exact two-line ASCII reply observed from status command FF."""
    try:
        lines = [line.strip() for line in payload.decode("ascii").splitlines()
                 if line.strip()]
    except UnicodeDecodeError as exc:
        raise ValueError("LCUS-2 status is not ASCII") from exc
    if len(lines) != 2:
        raise ValueError("LCUS-2 status must contain exactly two channels")
    states = {}
    for line in lines:
        match = _LCUS2_STATUS.fullmatch(line)
        if match is None:
            raise ValueError("LCUS-2 status line is malformed")
        channel = int(match.group(1))
        if channel in states:
            raise ValueError("LCUS-2 status repeats a channel")
        states[channel] = match.group(2) == "ON"
    if set(states) != {1, 2}:
        raise ValueError("LCUS-2 status omits a channel")
    return states


class Lcus2SerialTransport:
    """Acknowledged LCUS-2 control over a POSIX CH340 TTY.

    The relay's state-changing command emits no acknowledgement. We therefore
    follow it with the board's 0xff status query and accept success only when
    the measured ASCII reply reports the requested state.
    """

    def __init__(self, device, baud=9600, timeout=0.75):
        if baud != 9600:
            raise ConfigurationError("LCUS-2 protocol is fixed at 9600 baud")
        self.device = device
        self.timeout = timeout
        self.fd = None
        self._termios = None
        self._open()

    def _open(self):
        # Lazy so the detector and pure protocol tests remain importable on the
        # Windows build host, which has no termios module.
        import termios

        self._termios = termios
        self.fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        # termios.error is NOT an OSError subclass (it is built with a bare
        # Exception base), so every caller's `except OSError` would miss it and
        # the daemon would die on a traceback instead of taking the designed
        # "OFF was not verified" path -- and leak this fd on the way out. A
        # device that opens but is not a TTY is a realistic misconfiguration:
        # the symlink is a config value. Convert it and close what we opened.
        try:
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0
            attrs[4] = termios.B9600
            attrs[5] = termios.B9600
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except BaseException as exc:
            self.close()
            if isinstance(exc, termios.error):
                raise OSError(
                    errno.ENOTTY,
                    "%s is not a usable 9600-baud serial port: %s"
                    % (self.device, exc)) from exc
            raise

    def _write_all(self, payload, deadline):
        view = memoryview(payload)
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("LCUS-2 write timed out")
            _, writable, _ = select.select([], [self.fd], [], remaining)
            if not writable:
                raise TimeoutError("LCUS-2 write timed out")
            written = os.write(self.fd, view)
            view = view[written:]

    def read_status(self, deadline):
        # Same termios.error conversion as _open: unplugging the CH340 mid-run
        # is exactly when this fires, and it must reach the relay's handlers.
        try:
            self._termios.tcflush(self.fd, self._termios.TCIFLUSH)
        except self._termios.error as exc:
            raise OSError(errno.ENODEV,
                          "LCUS-2 serial device went away: %s" % exc) from exc
        self._write_all(b"\xff", deadline)
        received = bytearray()
        while True:
            # Clamp: time passes between the deadline test and this call, and a
            # negative select timeout raises ValueError rather than timing out.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select([self.fd], [], [], remaining)
            if not readable:
                break
            chunk = os.read(self.fd, 128 - len(received))
            if not chunk:
                raise OSError(errno.ENODEV, "LCUS-2 serial device closed")
            received.extend(chunk)
            if len(received) >= 128:
                raise OSError(errno.EPROTO, "LCUS-2 status exceeded 127 bytes")
            try:
                return parse_lcus2_status(bytes(received))
            except ValueError:
                pass
        raise TimeoutError("LCUS-2 did not return a complete status")

    def set_state(self, channel, enabled):
        deadline = time.monotonic() + self.timeout
        self._write_all(lcus2_command(channel, enabled), deadline)
        # The physical COM4 bench used a 250 ms command settle before querying.
        # Keep the same conservative interval; switching happens only at audio
        # attack/hold boundaries, never on a latency-sensitive path.
        time.sleep(0.25)
        states = self.read_status(deadline)
        if states[channel] is not enabled:
            raise OSError(errno.EPROTO, "LCUS-2 did not apply channel %d" % channel)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Lcus2Relay:
    """Own one LCUS-2 channel and verify every state written to it."""

    def __init__(self, device, channel=1, timeout=0.75,
                 heartbeat_seconds=5.0, transport_factory=None,
                 monotonic=time.monotonic):
        self.device = device
        self.channel = channel
        self.timeout = timeout
        self.heartbeat_seconds = heartbeat_seconds
        self.transport_factory = transport_factory or Lcus2SerialTransport
        self.monotonic = monotonic
        self._transport = None
        self._running = False
        self._checked_at = -1e9

    @property
    def running(self):
        return self._running and self._transport is not None

    def _disconnect(self):
        transport, self._transport = self._transport, None
        self._running = False
        if transport is not None:
            try:
                transport.close()
            except OSError:
                pass

    def start(self):
        if self.running:
            return True
        try:
            self._transport = self.transport_factory(self.device, 9600, self.timeout)
            # A CH340 open is not proof of the relay's state. Verify OFF first,
            # then ON, and never alter the module's other channel.
            self._transport.set_state(self.channel, False)
            self._transport.set_state(self.channel, True)
            self._checked_at = self.monotonic()
            self._running = True
            return True
        except (OSError, TimeoutError, ConfigurationError, ValueError) as exc:
            log("LCUS-2 trigger failed to start: %s" % exc)
            self._disconnect()
            return False

    def ensure_off(self):
        """Command and verify OFF even when this process did not turn it on.

        The physical LCUS-2 retains its relay state after the CH340 port is
        closed.  Opening a fresh transport here is therefore intentional: a
        service start or stop must establish OFF, not infer it from local
        process state.
        """
        transport = None
        try:
            transport = self.transport_factory(self.device, 9600, self.timeout)
            transport.set_state(self.channel, False)
            return True
        except (OSError, TimeoutError, ConfigurationError, ValueError) as exc:
            log("LCUS-2 trigger OFF was not verified: %s" % exc)
            return False
        finally:
            if transport is not None:
                try:
                    transport.close()
                except OSError:
                    pass

    def maintain(self, now=None):
        if not self.running:
            return False
        now = self.monotonic() if now is None else now
        if now - self._checked_at < self.heartbeat_seconds:
            return True
        try:
            # Re-sending ON does not cycle an already-energized relay, and the
            # following status query proves the serial and MCU path stayed live.
            # MEASURED on the panel 2026-09-13, not assumed: with the relay held
            # closed and the room quiet, the Owner listening at the board heard
            # no repeating click on the 5 s beat. Had it re-actuated, this
            # heartbeat would chatter the contacts for as long as audio plays.
            self._transport.set_state(self.channel, True)
            self._checked_at = now
            return True
        except (OSError, TimeoutError, ValueError) as exc:
            log("LCUS-2 trigger verification failed: %s" % exc)
            self._disconnect()
            return False

    def stop(self):
        if self._transport is None:
            return self.ensure_off()
        verified = False
        try:
            self._transport.set_state(self.channel, False)
            verified = True
        except (OSError, TimeoutError, ValueError) as exc:
            log("LCUS-2 trigger OFF was not verified: %s" % exc)
        finally:
            self._disconnect()
        return verified


def _valid_device_path(value):
    """Accept only explicit normalized /dev paths and no control characters."""
    if not value or any(ord(char) < 32 for char in value):
        return False
    normalized = posixpath.normpath(value)
    return normalized == value and normalized.startswith("/dev/")


def build_actuator(method, lcus2_device="", lcus2_channel="1",
                   transport_factory=None):
    """Build the amplifier actuator from the configured method.

    LCUS-2 IS THE ONLY ACTUATOR (ratified 2026-09-13). Anything else -- the
    retired `audio-jack` tone included -- is refused loudly here rather than
    quietly falling back, because a silent fallback to the relay on a panel
    configured for the jack would look like it worked, and a silent fallback
    the other way would put a full-scale tone on the jack the headset leg owns.
    """
    if method == "lcus-2":
        if not _valid_device_path(lcus2_device):
            raise ConfigurationError(
                "WALL_AMP_LCUS2_DEVICE must be an explicit absolute /dev path")
        if lcus2_channel not in ("1", "2"):
            raise ConfigurationError("WALL_AMP_LCUS2_CHANNEL must be 1 or 2")
        return Lcus2Relay(
            lcus2_device,
            channel=int(lcus2_channel),
            heartbeat_seconds=_env("WALL_AMP_LCUS2_HEARTBEAT_SECONDS", 5.0,
                                   lo=1.0, hi=60.0),
            timeout=_env("WALL_AMP_LCUS2_TIMEOUT_SECONDS", 0.75,
                         lo=0.5, hi=2.0),
            transport_factory=transport_factory,
        )
    raise ConfigurationError(
        "WALL_AMP_ACTIVATOR must be exactly lcus-2; the audio-jack tone "
        "actuator was retired 2026-09-13 and the jack is now an audio output")


def required_tools(method):
    """Return only the programs used by the selected detector/actuator path."""
    if method == "lcus-2":
        return ("/usr/bin/arecord",)
    raise ConfigurationError("unknown amplifier actuator %r" % method)


# MIN_OFF ACROSS A RESTART (2026-11-09). `changed_at` is process state, and it
# used to start at 0.0, so MIN_OFF held only within one process: a restart
# while audio played could close the relay well inside ten seconds of the
# previous process opening it. That restart happens on every boot --
# `wall-audio-mode` stops and starts this unit once firstboot has run, about
# 25 s after the unit's own start -- and on every release, relay replug and
# crash. So the relay's last verified OFF is kept beside the safe-state proof,
# in /run (one boot: CLOCK_MONOTONIC is the same clock in every process of
# that boot, and a reboot clears both together).
#
# THE INVARIANT: the file holds the EARLIEST verified OFF since the relay
# could last have been energized. Every path that may energize it revokes the
# proof first (`record_safe_state(False)`), which removes this file too; a
# verified OFF writes it only when it is absent, so a stop of an idle daemon
# does not move the instant forward and charge the next process a fresh ten
# seconds of silence for an OFF that happened minutes ago.
RELAY_OFF_SINCE_NAME = "relay-off-since"


def _relay_off_since_path():
    return os.path.join(os.path.dirname(SAFE_STATE_FILE), RELAY_OFF_SINCE_NAME)


def relay_off_since(now, path=None):
    """The monotonic second of the relay's last verified OFF, or None.

    None means "not known", which the caller must read as "it may have been
    ON until now": no file (a new boot, or a process that died with the relay
    possibly energized), an unreadable one, or a value that cannot be an
    instant of this boot that has already happened.
    """
    path = _relay_off_since_path() if path is None else path
    try:
        with open(path, encoding="ascii") as handle:
            value = float(handle.read().strip())
    except (OSError, ValueError):
        return None
    if not math.isfinite(value) or value < 0.0 or value > now:
        return None
    return value


def _note_relay_off(now):
    path = _relay_off_since_path()
    if relay_off_since(now, path) is not None:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii") as handle:
        handle.write("%.3f\n" % now)
    os.replace(tmp, path)


def record_safe_state(verified, now=None):
    """Publish or revoke the artifact consumed by the S3 power path.

    The relay's off-since instant goes with it (see RELAY_OFF_SINCE_NAME):
    revoked with the proof, and kept, not refreshed, while the proof stands.
    """
    if not verified:
        # The S3 proof first and each on its own: a failure to remove the
        # ledger must never leave the proof standing over an energized relay.
        for path in (SAFE_STATE_FILE, _relay_off_since_path()):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                log("could not revoke %s: %s" % (path, exc))
        return
    try:
        tmp = SAFE_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="ascii") as handle:
            handle.write("OFF\n")
        os.replace(tmp, SAFE_STATE_FILE)
        _note_relay_off(time.monotonic() if now is None else now)
    except OSError as exc:
        log("could not update amplifier safe-state proof: %s" % exc)


def retry_beat(now, started_at):
    """How long to wait between actuator attempts, at this moment.

    One second while the re-enumeration this service was restarted for could
    still be settling, five afterwards. Pure, so the timing that the ten-second
    acceptance depends on is testable without a relay.
    """
    if now - started_at < ACTUATOR_FAST_RETRY_WINDOW:
        return ACTUATOR_FAST_RETRY_SECONDS
    return ACTUATOR_RETRY_SECONDS


def ensure_off_bounded(actuator, wait_seconds=None, monotonic=time.monotonic,
                       sleep=time.sleep):
    """ensure_off(), retried until it succeeds or the bounded wait expires.

    The retry exists for one measured case: a USB re-enumeration restarts this
    service while the relay's CH340 is still being probed, so the device node is
    briefly absent. Every attempt opens a FRESH transport, which is what makes
    this a reopen rather than a poll of a stale handle -- an ENOENT, an EIO or a
    node that has been recreated under a new minor are all the same thing here.

    Returns True on a verified OFF. It always makes at least one attempt, so a
    zero wait keeps the old behaviour exactly.
    """
    wait_seconds = RELAY_WAIT_SECONDS if wait_seconds is None else wait_seconds
    deadline = monotonic() + wait_seconds
    attempts = 0
    while True:
        attempts += 1
        if actuator.ensure_off():
            if attempts > 1:
                log("relay reached a verified OFF on attempt %d" % attempts)
            return True
        if monotonic() >= deadline:
            log("relay OFF still unverified after %.0fs; giving up so the "
                "failure is visible" % wait_seconds)
            return False
        sleep(min(RELAY_WAIT_INTERVAL, max(0.0, deadline - monotonic())))


def stop_and_record(actuator):
    verified = actuator.stop()
    record_safe_state(verified is not False)
    return verified


def relay_absent(actuator):
    """True when the relay's device node is gone, so no OFF was ever possible.

    Absence is the one condition that excuses an unverified OFF: there is no
    contact to open, and the udev re-add will re-run this daemon against the
    returned node.  A node that is PRESENT and still refused the write is a
    different thing -- the amplifier may be left energized -- and must stay
    visible.  The node is the evidence, not the failure of the write.
    """
    device = getattr(actuator, "device", None)
    if not device:
        return False
    return not os.path.exists(device)


def requested_shutdown(actuator):
    """Attempt safe shutdown after SIGTERM/SIGINT without misclassifying it.

    A dependency stop is systemd's intentional lifecycle action.  The relay
    can already be unplugged when it arrives, so an unverified best-effort OFF
    cannot turn that requested stop into a failed unit and prevent recovery on
    the adapter's next add.  Startup failures remain non-zero in main().

    TERRA 2026-09-17, major: this used to return 0 for EVERY unverified stop.
    A present relay that refused its OFF write is not the absent-device case
    this exists for, and reporting it green would hide an amplifier that may
    still be energized.  Only absence is excused.

    Implements: SR-039, LLR-941
    """
    if stop_and_record(actuator) is not False:
        return 0
    if relay_absent(actuator):
        log("relay OFF could not be verified during requested shutdown; "
            "exiting cleanly while the relay is absent")
        return 0
    log("relay OFF was not verified during requested shutdown and the device "
        "node is still present; reporting the failure")
    return 1



# ── speaker sleeps with the screen: the facts ────────────────────────────────
def _read_unsigned(path):
    """A file holding one non-negative decimal integer, or None for anything else."""
    try:
        with open(path, encoding="ascii") as handle:
            text = handle.read(32).strip()
    except (OSError, ValueError):
        return None
    return int(text) if re.fullmatch(r"[0-9]+", text) else None


def screen_lit(root=BACKLIGHT_ROOT):
    """Is the panel's screen lit? The kiosk's own rule, with one difference.

    Mirrors OfficeWallNaglight electron/display-lit.cjs: a backlight device
    counts only if `brightness` and `max_brightness` are both plain unsigned
    integers, max is above zero and brightness does not exceed it; the screen is
    lit if any counted device has brightness above zero.

    THE DIFFERENCE IS DELIBERATE: with NO counted device at all (no backlight
    class, an unreadable tree, garbage in the files) this answers LIT. The kiosk
    answers dark there because a dark answer locks the screen; here a dark
    answer silences the room, and silencing a room on missing data is the
    failure this feature must not have. Never fade on missing data.

    `root` is injectable so the tests drive a fake sysfs tree.
    Implements: SR-140, LLR-1433
    """
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return True
    counted = []
    for name in names:
        base = os.path.join(root, name)
        brightness = _read_unsigned(os.path.join(base, "brightness"))
        maximum = _read_unsigned(os.path.join(base, "max_brightness"))
        if (brightness is not None and maximum is not None
                and maximum > 0 and brightness <= maximum):
            counted.append(brightness > 0)
    return any(counted) if counted else True


def read_sleep_policy(path=AUDIO_STATE_FILE):
    """(speaker_position, sleeps_with_screen) from the applier's state file.

    Read-only. `speaker_position` is True only when the stored output is exactly
    `speaker`; a missing, unreadable or garbled file is False, so nothing ever
    fades on a state it could not read. `sleeps_with_screen` is the persisted
    preference: absent or not a boolean means True (the default, and an older
    file). Implements: SR-140, LLR-1434
    """
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.loads(handle.read(65536))
    except (OSError, ValueError):
        return False, True
    if not isinstance(raw, dict):
        return False, True
    sleeps = raw.get("speaker_sleeps_with_screen")
    return raw.get("output") == "speaker", (sleeps if isinstance(sleeps, bool) else True)


def read_call_active(path=CALL_STATE_FILE, wall_clock=time.time):
    """True while wall-bt-call says a call or a bridge is up. Implements: SR-140, LLR-1434.

    A missing, garbled, unknown-state or stale (older than CALL_MAX_AGE_SECONDS)
    document is NO call -- the same reading the broker gives it.
    """
    try:
        age = wall_clock() - os.stat(path).st_mtime
        with open(path, encoding="utf-8") as handle:
            raw = json.loads(handle.read(65536))
    except (OSError, ValueError):
        return False
    if age > CALL_MAX_AGE_SECONDS or age < -1:
        return False
    return isinstance(raw, dict) and raw.get("state") in ("call", "bridge")


def loopback_card(path=AUDIO_CARDS_FILE):
    """The Loopback card id, read the way wall-audio-output reads it."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle.read(65536).splitlines():
                line = line.strip()
                if line.startswith("WALL_AUDIO_LOOPBACK_CARD="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", value):
                        return value
    except OSError:
        pass
    return DEFAULT_LOOPBACK_CARD


class FadeStage:
    """Writes the speaker fade softvol with `amixer cset`. One writer, one control.

    The argv is exactly
        amixer -c <Loopback card> -q cset name=Speaker Fade Playback Volume N
    (the name is one argv element) and `FADE_CONTROL` is the only control name
    this class can address, so the panel's volume ("Bus Playback Volume") is
    unreachable from here by construction. Every failure is a False return and a
    rate-limited log line; nothing here raises. Implements: SR-140, LLR-1435
    """

    def __init__(self, run=subprocess.run, card_reader=loopback_card,
                 amixer=AMIXER):
        self._run = run
        self._card_reader = card_reader
        self._amixer = amixer
        self._card = None
        self._failures_logged = 0

    def argv(self, value):
        # ONE VALUE, EVERY CHANNEL. Measured on the panel 2026-11-01: `amixer -c
        # Loopback -q cset name='Bus Playback Volume' 165` set both channels of
        # that softvol (166,166 -> 165,165). The fade is a softvol of the same
        # kind on the same card, so a single value fills every channel and no
        # per-channel list is needed here.
        if self._card is None:
            self._card = self._card_reader()
        return [self._amixer, "-c", self._card, "-q", "cset",
                "name=%s" % FADE_CONTROL, str(int(value))]

    def read(self):
        """The control's current value (first channel), or None if unreadable."""
        try:
            argv = self.argv(0)[:3] + ["cget", "name=%s" % FADE_CONTROL]
            done = self._run(argv, check=False, capture_output=True, text=True,
                             timeout=FADE_WRITE_TIMEOUT_SECONDS)
            if done.returncode != 0:
                return None
            found = re.search(r"values=(\d+)", done.stdout or "")
            return int(found.group(1)) if found else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def write(self, value):
        try:
            done = self._run(self.argv(value), check=False, capture_output=True,
                             text=True, timeout=FADE_WRITE_TIMEOUT_SECONDS)
            ok = done.returncode == 0
            detail = (done.stderr or done.stdout or "").strip()[:80]
        except (OSError, subprocess.SubprocessError) as exc:
            ok, detail = False, str(exc)[:80]
        if ok:
            self._failures_logged = 0
            return True
        # Look the card up again next time: a wrong id is the likeliest cause.
        self._card = None
        if self._failures_logged < 3:
            self._failures_logged += 1
            log("speaker fade write %s failed: %s" % (value, detail or "no detail"))
        return False


class SpeakerSleep:
    """Fade the speakers out and open the relay when the screen goes dark.

    A small explicit state machine, stepped once per loop tick with the facts
    already sampled (the clock, too, is passed in, so a test owns time). The
    relay and the fade are injected: `relay_on(now)` / `relay_off(now)` return
    True when the relay reached the verified state, `fade.write(n)` returns True
    when the control took the value.

    "enabled" means: bus mode, output == speaker, preference on. "awake" means
    the screen is lit OR a call is active. With it not enabled the machine is
    transparent (phase LIT, fade at unity) and today's behaviour is untouched.

        LIT         The existing hysteresis/hold-off rule owns the relay. Fade at
                    unity. Not awake -> FADING_OUT.
        FADING_OUT  Relay still ON. dB-linear ramp to FADE_FLOOR over
                    FADE_OUT_SECONDS in FADE_STEP_SECONDS steps. Awake again ->
                    FADING_IN from where it is, at once, because the relay never
                    opened (or SETTLING for what is left of the settle, if it
                    closed less than WAKE_SETTLE_SECONDS ago). Ramp finished -> relay_off ->
                    DARK. (An unverified OFF is retried by the main loop's
                    reconcile block; it is never skipped, and a failed fade
                    write only cuts the ramp short, never delays the relay.)
        DARK        Relay OFF; the existing attack rule is suspended, so audio
                    playing cannot undo it. Awake with audio present, or a
                    call -> relay_on (retried every beat until verified) ->
                    SETTLING. Awake with no audio -> fade to unity, -> LIT, and
                    the existing attack rule may close the relay later.
        SETTLING    Relay ON, fade still at the floor, for WAKE_SETTLE_SECONDS
                    (the boards start). Then -> FADING_IN. Dark again ->
                    FADING_OUT.
        FADING_IN   Ramp to FADE_UNITY over FADE_IN_SECONDS. Done -> LIT.
                    Dark again -> FADING_OUT.

    Leaving "enabled" outside a settle (preference off, output moved, mode change)
    snaps the fade to unity and returns to LIT: the relay is then the existing
    rule's again. The fade is also set to unity by reset_unity() on start-up
    and shutdown, so a crash cannot leave the room silent.

    EVERY CLOSE GETS THE SAME SETTLE (SR-143). In the Speaker position the loop
    calls hold_for_relay() before it closes the relay itself, by the attack rule
    or by restarting a relay that stopped, and relay_closed() once it has, which
    enters SETTLING. A settle runs to the end of FADING_IN even if the machine
    stops being "enabled": it is about the amplifier starting, so the sleep
    preference has no say in it, and if the output moves away during it the ramp
    reaches a leg nobody is listening to and finishes at unity, where it would
    have been snapped to.

    Implements: SR-140, LLR-1436, SR-143, LLR-1440
    """

    LIT, FADING_OUT, DARK, SETTLING, FADING_IN = (
        "lit", "fading-out", "dark", "settling", "fading-in")

    def __init__(self, fade, relay_on, relay_off, emit=log):
        self.fade = fade
        self._relay_on = relay_on
        self._relay_off = relay_off
        self._emit = emit
        self.phase = self.LIT
        self.level = FADE_UNITY
        self.unity_known = False
        self._ramp = None
        self._fail_streak = 0
        self._settle_until = 0.0
        self._relay_hold = False
        self._last_relay_try = -1e9
        self._last_unity_try = -1e9
        self._unity_failure_logged = False
        self._waiting_logged = False

    # ── what the main loop asks ──────────────────────────────────────────
    @property
    def governing(self):
        """True while the relay belongs to this machine, not the attack rule."""
        return self.phase != self.LIT

    @property
    def ramping(self):
        return self._ramp is not None

    # ── the fade ─────────────────────────────────────────────────────────
    def _write(self, value):
        ok = self.fade.write(value)
        if ok:
            self.level = value
            self._fail_streak = 0
            self.unity_known = (value == FADE_UNITY)
        else:
            self._fail_streak += 1
            self.unity_known = False
            # The control may or may not have moved: the level is UNKNOWN, and
            # the next ramp must not start from a stale belief (see _begin_ramp).
            self.level = None
        return ok

    def reset_unity(self, now=0.0):
        """Put the fade stage at unity, once. False if the control would not take it."""
        self._ramp = None
        self._last_unity_try = now
        ok = self._write(FADE_UNITY)
        if not ok and not self._unity_failure_logged:
            self._unity_failure_logged = True
            self._emit("speaker fade stage not at unity (control not writable "
                       "yet); will retry")
        return ok

    def _begin_ramp(self, now, end, seconds):
        if self.level is None:
            # A fresh assumption: ask the control; if that fails too, assume it
            # sits at the far end from where this ramp is going, which is the
            # longest (never an abrupt) ramp.
            reader = getattr(self.fade, "read", None)
            seen = reader() if reader else None
            self.level = seen if isinstance(seen, int) else (
                FADE_UNITY if end == FADE_FLOOR else FADE_FLOOR)
        span = abs(end - self.level)
        self._fail_streak = 0
        self._ramp = (self.level, end, now,
                      seconds * span / float(FADE_UNITY - FADE_FLOOR))

    def _advance(self, now):
        """One ramp step. True when the ramp is over (finished or abandoned)."""
        start, end, began, duration = self._ramp
        progress = 1.0 if duration <= 0 else min(1.0, max(0.0, (now - began) / duration))
        value = int(round(start + (end - start) * progress))
        if value != self.level and self._fail_streak < FADE_FAIL_LIMIT:
            self._write(value)
        abandoned = self._fail_streak >= FADE_FAIL_LIMIT
        if abandoned:
            self._emit("speaker fade control not writable; cutting the ramp short")
        if abandoned or (progress >= 1.0 and self.level == end):
            self._ramp = None
            return True
        return False

    # ── the machine ──────────────────────────────────────────────────────
    def _snap_to_lit(self, now):
        self._ramp = None
        self.phase = self.LIT
        self._relay_hold = False
        self.reset_unity(now)

    def hold_for_relay(self):
        """The attack rule is about to close the relay: silence the leg first.

        A failed write only costs the fade, as it does everywhere here: the
        relay still closes, and the room gets what it gets today. It is not
        undone if the close then fails either -- the relay is open, so nothing
        is heard, and the LIT beat puts unity back.
        """
        self._ramp = None
        self._write(FADE_FLOOR)

    def relay_closed(self, now):
        """The attack rule closed the relay in the Speaker position: settle."""
        self._relay_hold = True
        self.phase = self.SETTLING
        self._settle_until = now + WAKE_SETTLE_SECONDS
        self._emit("relay ON: settling %.1fs before the fade" % WAKE_SETTLE_SECONDS)

    def step(self, now, enabled, lit, call, audio, beat=ACTUATOR_RETRY_SECONDS,
             *, relay_is_on, relay_changed_at):
        """One tick. `relay_is_on` and `relay_changed_at` are the loop's own
        record of the relay, so the sleep transitions honour the same
        MIN_ON_SECONDS / MIN_OFF_SECONDS the attack and hold-off rules do. Both are REQUIRED
        keyword-only arguments on purpose: a default would silently disable
        the dwell for a caller that forgot them."""
        # With the preference off the screen puts nothing to sleep, so the only
        # way here is an attack-rule hold, and it runs on as if awake.
        awake = bool(lit or call) or not enabled
        if not enabled and not self._relay_hold:
            if self.phase != self.LIT:
                self._emit("speaker sleep inactive; leaving %s, fade to unity"
                           % self.phase)
                self._snap_to_lit(now)
            elif not self.unity_known and now - self._last_unity_try >= UNITY_RETRY_SECONDS:
                self.reset_unity(now)
            return

        if self.phase == self.LIT:
            if awake:
                # A quiet room from a failed fade-in write is retried on the
                # beat, not every UNITY_RETRY_SECONDS.
                if not self.unity_known and now - self._last_unity_try >= beat:
                    self.reset_unity(now)
                return
            self._emit("screen dark: fading the speakers out")
            self._begin_ramp(now, FADE_FLOOR, FADE_OUT_SECONDS)
            self.phase = self.FADING_OUT

        if self.phase == self.FADING_OUT:
            if awake and relay_is_on and now < self._settle_until:
                # Lit again before the boards had their settle: the rest of it
                # still applies, however the fade got interrupted.
                self._ramp = None
                self._emit("screen lit mid-settle: finishing the settle")
                self._relay_hold = True
                self.phase = self.SETTLING
            elif awake:
                self._emit("screen lit mid-fade: fading back in")
                self._begin_ramp(now, FADE_UNITY, FADE_IN_SECONDS)
                self.phase = self.FADING_IN
            elif self._ramp is None or self._advance(now):
                # The fade is done (or abandoned) and the stage sits at the
                # floor. The relay opens whether or not the fade completed, but
                # not before it has been ON for MIN_ON_SECONDS: until then the
                # room is already silent and the relay simply waits.
                if relay_is_on and now - relay_changed_at < MIN_ON_SECONDS:
                    if not self._waiting_logged:
                        self._waiting_logged = True
                        self._emit("speakers silent; relay OFF waits out "
                                   "the minimum on-time")
                    return
                self._waiting_logged = False
                self._relay_off(now)
                self.phase = self.DARK
                self._last_relay_try = -1e9
                self._emit("speakers asleep: relay OFF")
                return

        if self.phase == self.DARK:
            if not awake:
                return
            if not (audio or call):
                self._emit("screen lit, no audio: fade to unity, relay left to "
                           "the attack rule")
                self._snap_to_lit(now)
                return
            # No wake relay-ON until the relay has been off for MIN_OFF_SECONDS
            # (contact protection, and long enough for the boards to power
            # down). The stage stays at the floor while it waits.
            if not relay_is_on and now - relay_changed_at < MIN_OFF_SECONDS:
                return
            if now - self._last_relay_try >= beat:
                self._last_relay_try = now
                if self._relay_on(now):
                    self._relay_hold = True
                    self.phase = self.SETTLING
                    self._settle_until = now + WAKE_SETTLE_SECONDS
                    self._emit("speakers waking: relay ON, settling %.1fs"
                               % WAKE_SETTLE_SECONDS)
            return

        if self.phase == self.SETTLING:
            if not awake:
                self._relay_hold = False
                self._begin_ramp(now, FADE_FLOOR, FADE_OUT_SECONDS)
                self.phase = self.FADING_OUT
            elif now >= self._settle_until:
                self._begin_ramp(now, FADE_UNITY, FADE_IN_SECONDS)
                self.phase = self.FADING_IN
            return

        if self.phase == self.FADING_IN:
            if not awake:
                self._relay_hold = False
                self._begin_ramp(now, FADE_FLOOR, FADE_OUT_SECONDS)
                self.phase = self.FADING_OUT
            # No ramp: hold_for_relay() took it for a relay restart that failed.
            elif self._ramp is None or self._advance(now):
                self.phase = self.LIT
                self._relay_hold = False
                self._emit("speakers awake")
                if not self.unity_known:
                    self.reset_unity(now)


def main():
    method = os.environ.get("WALL_AMP_ACTIVATOR", "lcus-2").strip()
    device = os.environ.get(
        "WALL_AMP_LCUS2_DEVICE", "/dev/wall-amp-relay").strip()
    channel = os.environ.get("WALL_AMP_LCUS2_CHANNEL", "1").strip()
    try:
        actuator = build_actuator(method, lcus2_device=device,
                                  lcus2_channel=channel)
        tools = required_tools(method)
    except ConfigurationError as exc:
        log("amplifier actuator configuration: %s" % exc)
        return 78

    for tool in tools:
        if not shutil.which(tool):
            log("missing %s" % tool)
            return 78

    # The measured LCUS-2 is a latching device: closing the serial port leaves
    # an energized channel energized. Establish and verify the safe state on
    # every service start, including disabled and non-trigger modes.
    safe = ensure_off_bounded(actuator)
    record_safe_state(safe)
    # When the relay last went OFF, for MIN_OFF. A previous process's verified
    # OFF survives in the ledger; anything less certain -- a new boot, a
    # predecessor that died with the relay possibly ON, an unverified OFF just
    # now -- counts the OFF as happening here, at this start. That costs at
    # most MIN_OFF of extra silence, and only when audio is already playing
    # in the first seconds after a start whose predecessor left no proof.
    started_off = time.monotonic()
    proven = relay_off_since(started_off) if safe else None
    if proven is not None:
        started_off = proven

    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())

    # An idling service has no detector loop to reconcile the relay later, so
    # for those two paths an unverified OFF is still a hard failure that the
    # RestartSec is the only cure for.
    _idle = (os.environ.get("WALL_AMP_ENABLED", "true").strip().lower()
             in ("false", "0", "no")) or current_mode() not in COMMANDING_MODES
    if _idle:
        # A daemon killed mid-fade and restarted into an idle path would leave
        # the stage at the floor. Best effort; a failure is only logged by
        # FadeStage.
        FadeStage().write(FADE_UNITY)
    if _idle and not safe:
        return 1

    if os.environ.get("WALL_AMP_ENABLED", "true").strip().lower() in ("false", "0", "no"):
        log("WALL_AMP_ENABLED is false; idling without emitting anything")
        while not stopping.wait(3600):
            pass
        return requested_shutdown(actuator)

    if current_mode() not in COMMANDING_MODES:
        log("panel mode: the amplifier is not commanded; idling")
        # Not an error and not a restart loop -- the mode script restarts this
        # unit when the mode changes back.
        while not stopping.wait(3600):
            pass
        return requested_shutdown(actuator)

    log("amplifier actuator: LCUS-2 channel %s at %s" % (channel, device))
    sources = sources_for(current_mode())
    levels = [Level(pcm, off) for pcm, off in sources]
    for lv in levels:
        lv.start()
    on = False
    above_since = None
    below_since = None
    changed_at = started_off
    last_actuator_attempt = -1e9
    last_off_attempt = -1e9
    started_at = time.monotonic()

    log("watching %s" % ", ".join(pcm for pcm, _ in sources))
    # The one source item L publishes, by NAME rather than by position: in
    # `trigger` mode the sources are different and there is no bus tap at all,
    # and publishing whichever level happened to be first would put a level from
    # somewhere else on the wall labelled as the bus.
    bus_level = next((lv for lv in levels if lv.pcm == "speaker_tap"), None)
    if bus_level is None:
        log("bus telemetry: no speaker_tap in this mode; the shell will read it "
            "as unavailable rather than as silence")
    last_telemetry = 0.0

    # ── speaker sleeps with the screen (SR-140): the relay as the machine sees it
    def sleep_relay_off(at):
        """Command and verify OFF. Unverified is `on=False, safe=False`, which
        the reconcile block below keeps retrying on the beat -- the machine
        never waits on a relay that will not answer."""
        nonlocal on, safe, changed_at, above_since, below_since
        verified = stop_and_record(actuator) is not False
        on = False
        safe = verified
        changed_at = at
        above_since = below_since = None
        if not verified:
            log("speaker sleep: relay OFF could not be verified; retrying")
        return verified

    def sleep_relay_on(at):
        nonlocal on, safe, changed_at, below_since
        record_safe_state(False)
        if actuator.start():
            on = True
            safe = False
            changed_at = at
            below_since = None
            return True
        return False

    def maintain_relay(at, beat_seconds):
        """The heartbeat and the unexpected-stop restart, for either owner."""
        nonlocal on, safe, changed_at, last_actuator_attempt
        if (on and not actuator.maintain(at)
                and at - last_actuator_attempt >= beat_seconds):
            last_actuator_attempt = at
            log("amplifier actuator stopped unexpectedly; restarting")
            # start() proves OFF before it closes, so this is a real close of the
            # relay and takes the settle like any other (SR-143).
            settle = sleep_capable and read_sleep_policy()[0]
            if settle:
                sleeper.hold_for_relay()
            if actuator.start():
                changed_at = at
                if settle:
                    sleeper.relay_closed(at)
            else:
                on = False
                # The relay is in an unknown physical state: the loop above
                # keeps commanding OFF until one is verified.
                safe = False
                changed_at = at

    sleeper = SpeakerSleep(FadeStage(), sleep_relay_on, sleep_relay_off)
    sleep_capable = current_mode() == "bus"
    if sleep_capable:
        # A crash or a kill may have left the stage down; the relay is already
        # verified OFF here, so unity cannot be heard.
        sleeper.reset_unity(time.monotonic())
    sleep_facts = {"enabled": False, "lit": True, "call": False}
    last_sample = -1e9
    while not stopping.is_set():
        now = time.monotonic()

        # Item L. Outside the decision entirely: it reads what the detector has
        # already measured and writes a file. Nothing below may depend on it.
        if now - last_telemetry >= BUS_TELEMETRY_INTERVAL:
            last_telemetry = now
            publish_bus_telemetry(bus_level, on)

        # RECONCILE A RELAY WE COULD NOT REACH AT START (terra, 2026-09-13).
        # Two things made this necessary. The relay is on the SAME USB hub as
        # the adapter, so on a port move its node can disappear BEFORE the sound
        # card does -- the stop that BindsTo triggers then cannot command OFF,
        # and the LCUS-2 latches, so the amplifier can stay physically on with
        # nothing driving it. And returning 1 here instead, as this used to,
        # cost a whole RestartSec before the capture threads even started: a
        # relay that came back at 7 s was not acted on until about 11 s, which
        # is outside the ten seconds the acceptance allows. So the detector runs
        # regardless and keeps trying to reach the safe state on the actuator
        # retry beat for as long as the amplifier is supposed to be off.
        beat = retry_beat(now, started_at)
        if not on and not safe and now - last_off_attempt >= beat:
            last_off_attempt = now
            safe = stop_and_record(actuator) is not False
            if safe:
                log("relay reconciled to a verified OFF")

        loud = any(lv.above(ON_DBFS) for lv in levels)
        quiet = all(not lv.above(OFF_DBFS) for lv in levels)

        # The three facts the sleep machine needs, re-read every half second (or
        # every ramp step while one is running). Any unreadable one reads as
        # "do nothing": see screen_lit and read_sleep_policy.
        if sleep_capable:
            if now - last_sample >= (FADE_STEP_SECONDS if sleeper.ramping
                                     else SLEEP_SAMPLE_SECONDS):
                last_sample = now
                speaker_position, sleeps = read_sleep_policy()
                sleep_facts = {"enabled": speaker_position and sleeps,
                               "lit": screen_lit(), "call": read_call_active()}
            sleeper.step(now, sleep_facts["enabled"], sleep_facts["lit"],
                         sleep_facts["call"], not quiet, beat,
                         relay_is_on=on, relay_changed_at=changed_at)

        if sleeper.governing:
            # The sleep machine owns the relay: the attack and hold-off rules are
            # suspended, so audio playing while dark cannot close it. The
            # heartbeat still serves a relay that is ON.
            above_since = below_since = None
            maintain_relay(now, beat)
        elif not on:
            if loud:
                above_since = above_since or now
                if now - above_since >= ATTACK_SECONDS and now - changed_at >= MIN_OFF_SECONDS:
                    if now - last_actuator_attempt >= beat:
                        record_safe_state(False)
                        # Read here and not from sleep_facts: those are up to
                        # half a second old, and a move to Speaker is exactly
                        # what starts the audio that brought us here. SR-143.
                        settle = sleep_capable and read_sleep_policy()[0]
                        if settle:
                            sleeper.hold_for_relay()
                        if actuator.start():
                            on = True
                            safe = False
                            changed_at = now
                            below_since = None
                            log("amplifier ON (%s)"
                                % ", ".join(lv.report() for lv in levels))
                            if settle:
                                sleeper.relay_closed(now)
                        else:
                            # Stay off and say so rather than reporting a state
                            # we could not reach; retry on a timer, not every block.
                            last_actuator_attempt = now
            else:
                above_since = None
        else:
            if quiet:
                below_since = below_since or now
                if now - below_since >= HOLD_OFF_SECONDS and now - changed_at >= MIN_ON_SECONDS:
                    if stop_and_record(actuator):
                        on = False
                        safe = True
                        changed_at = now
                        above_since = None
                        log("amplifier OFF after %.0fs idle" % HOLD_OFF_SECONDS)
                    else:
                        # The relay may still be energized. Retain the logical
                        # ON state and retry instead of publishing a false OFF.
                        below_since = now
                        log("amplifier OFF could not be verified; will retry")
            else:
                below_since = None
            maintain_relay(now, beat)

        stopping.wait(FADE_STEP_SECONDS if sleeper.ramping else BLOCK_SECONDS)

    # The level threads are stopped whatever the relay did, but the relay's
    # verdict is the unit's: discarding it here was the other half of the
    # Terra finding above.
    status = requested_shutdown(actuator)
    if sleep_capable:
        # AFTER the relay is open, so restoring unity cannot be heard; and on
        # every exit path, so a stopped daemon never leaves the stage down.
        sleeper.reset_unity(time.monotonic())
    for lv in levels:
        lv.stop()
    return status


if __name__ == "__main__":
    sys.exit(main())
