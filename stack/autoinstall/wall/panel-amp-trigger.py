#!/usr/bin/env python3
"""Power the speaker amplifier while audio is playing.

The level detector is independent of the actuator. The shipped default emits a
continuous tone on the ALC255 headphone jack for a far-end rectifier. The
alternative commands one channel of the measured LCUS-2 USB relay through its
CH340 serial interface. Software decides in one place; configuration chooses
how that decision reaches the amplifier.

WHY A TONE AND NOT A DETECTOR ON THE AMPLIFIER FEED (measured 2026-09-12):
the feed is downstream of the volume control, so an analog detector's
sensitivity would track the rocker and quiet listening would never switch the
amplifier on. And the ALC255, rejected for audio at 31 dB noisier than the USB
adapter, is the right part for this: a rectifier does not care about noise and
does care about volts, and it measured 2.6 V bridged against the adapter's 0.29.

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
wheelhouse, so capture and playback are done by driving arecord and aplay rather
than importing a binding.

Exit codes: 78 configuration unusable.
"""

import errno
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

def log(message):
    print(message, file=sys.stderr, flush=True)


CARDS_FILE = "/etc/wall-panel/audio-cards.env"


def _card(key, default):
    """Read one card id from the generated card map.

    Card ids live in exactly one generated file so that swapping the USB adapter
    is a knob rather than an edit across the ALSA configs, the mode script and
    both daemons. The default is a fallback for a panel whose file predates it.
    """
    try:
        with open(CARDS_FILE) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value
    except OSError:
        pass
    return default


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

# The trigger output.
TRIGGER_CARD = _card("WALL_AUDIO_BUILTIN_CARD", "PCH")
TRIGGER_PCM = "trigger_out"
# 0 Hz is all-zero samples: aplay stays alive and the relay never closes.
TRIGGER_FREQ = _env("WALL_AMP_TONE_HZ", 1000.0, lo=50.0, hi=20000.0)
TRIGGER_AMPLITUDE = 0.99
# 'Front Headphone Jack' -- a cable that has fallen out otherwise presents as
# "the amplifier stopped working" with nothing anywhere to say why. The internal
# speaker is muted and auto-mute disabled independently of this, so this is
# diagnostics rather than safety.
JACK_CONTROL = "Front Headphone Jack"
JACK_POLL_SECONDS = 5.0
# Do not reopen a failing aplay or serial device on every 100 ms block.
ACTUATOR_RETRY_SECONDS = 5.0
# ...but five seconds is too slow for the first few, and terra found exactly why
# (2026-09-13): after a port move this service restarts, and if the CH340 is
# still missing when the startup wait gives up, the next attempt on a five
# second beat lands at about eleven seconds -- outside the ten the acceptance
# allows -- for a relay that was actually back at seven. So for a bounded spell
# after start, retry once a second. Bounded, because a relay that is genuinely
# absent must not be probed once a second for the life of the panel, and
# restricted to the relay because respawning aplay that fast is a different and
# worse thing to do.
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


def amixer(card, *args):
    try:
        return subprocess.run(["/usr/bin/amixer", "-c", card, *args],
                              check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        log("amixer %s %s: %s" % (card, " ".join(args), exc))
        return None


def current_mode():
    """trigger or panel. A missing file means trigger, the normal configuration."""
    try:
        with open(MODE_FILE) as fh:
            value = fh.read().strip()
    except OSError:
        return "trigger"
    return value if value in ("trigger", "panel") else "trigger"


def jack_present():
    """True if a cable is in the headphone jack, or if detection is unavailable.

    Defaults to True deliberately: if the control cannot be read, refusing to
    emit the tone would silently disable the amplifier, which is worse than
    emitting it into a muted speaker.
    """
    got = amixer(TRIGGER_CARD, "cget", "name=" + JACK_CONTROL)
    if got is None or got.returncode != 0:
        return True
    for line in got.stdout.splitlines():
        line = line.strip()
        if line.startswith(": values="):
            return line.endswith("on")
    return True


def assert_trigger_output():
    """Put the trigger path where the measured 2.6 V assumes it is.

    Not inherited from alsactl: a silent reset would halve the trigger voltage
    and the amplifier would simply stop switching on, with nothing in any log.

    Failures are REPORTED, not swallowed. A muted Headphone or a renamed control
    means this daemon can log "amplifier ON" while no trigger voltage exists at
    all; a failure to mute Speaker can put a full-scale tone through the panel's
    own speaker. Both are invisible unless said out loud.
    """
    ok = True
    for args in (("sset", "Master", "100%", "unmute"),
                 ("sset", "Headphone", "100%", "unmute"),
                 # The internal speaker stays muted and auto-mute disabled in
                 # both modes, re-asserted here so a stray mixer change cannot
                 # put a full-scale tone on the panel's own speaker.
                 ("sset", "Speaker", "mute"),
                 ("sset", "Auto-Mute Mode", "Disabled")):
        got = amixer(TRIGGER_CARD, "-q", *args)
        if got is None or got.returncode != 0:
            ok = False
            detail = ""
            if got is not None:
                detail = (got.stderr or got.stdout or "").strip()
            first = detail.splitlines()[0] if detail else "no detail"
            log("TRIGGER PATH NOT ASSERTED: amixer %s failed: %s"
                % (" ".join(args), first))
    return ok


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
                    self.updated = time.monotonic()
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    log("capture %s: %s" % (self.pcm, exc))
            finally:
                self.alive = False
                self.dbfs = -120.0
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


class Tone:
    """The trigger tone, as a child aplay fed continuously.

    Generated rather than looped from a file so there is no gap where a file
    ends and restarts -- a restarting player is also what appeared to wedge the
    shared dmix slave during bench work.
    """

    def __init__(self):
        self._proc = None
        self._thread = None
        self._stop = threading.Event()

    @property
    def running(self):
        """Alive means aplay is up AND the feeder is still delivering.

        Checking only the process leaves a hole: if the feeder thread dies
        while aplay sits waiting on its pipe, this reads true forever and the
        restart path never fires, so the relay opens with nothing saying why.
        """
        if self._proc is None or self._proc.poll() is not None:
            return False
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        """True only if the tone is actually playing.

        Popen succeeding proves nothing: an ALSA open failure happens inside
        the child, so a missing trigger_out or a busy card returned success
        here and the caller logged 'amplifier ON' with no voltage anywhere.
        """
        if self.running:
            return True
        self._stop.clear()
        try:
            self._proc = subprocess.Popen(
                ["/usr/bin/aplay", "-D", TRIGGER_PCM, "-f", "S16_LE",
                 "-r", str(RATE), "-c", str(CHANNELS), "-q", "-"],
                stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            log("cannot start tone: %s" % exc)
            self._proc = None
            return False
        self._thread = threading.Thread(target=self._feed, daemon=True)
        self._thread.start()
        # Give the child long enough to fail an ALSA open, then look.
        try:
            self._proc.wait(timeout=0.4)
        except subprocess.TimeoutExpired:
            return True
        err = ""
        try:
            if self._proc.stderr is not None:
                err = self._proc.stderr.read(2048).decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            pass
        log("TONE FAILED TO START on %s: %s"
            % (TRIGGER_PCM, err.splitlines()[0] if err else "aplay exited immediately"))
        self._proc = None
        return False

    def _feed(self):
        phase = 0
        chunk = BLOCK_FRAMES
        while not self._stop.is_set() and self.running:
            frames = []
            for _ in range(chunk):
                v = int(32767 * TRIGGER_AMPLITUDE
                        * math.sin(2 * math.pi * TRIGGER_FREQ * phase / RATE))
                # Anti-phase across the pair: taken across tip and ring this is
                # double either one to ground, the +6 dB that puts the rectified
                # level clear of the relay's threshold.
                frames.append(v)
                frames.append(-v)
                phase = (phase + 1) % RATE
            try:
                self._proc.stdin.write(struct.pack("<%dh" % len(frames), *frames))
                self._proc.stdin.flush()
            except (OSError, ValueError, AttributeError):
                return

    def stop(self):
        self._stop.set()
        proc, self._proc = self._proc, None
        if proc is None:
            return True
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        _reap(proc)
        return True

    def maintain(self, _now=None):
        """Return whether the already-started tone is still being delivered."""
        return self.running


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
    """Build exactly one amplifier actuator from the configured method."""
    if method == "audio-jack":
        return Tone()
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
        "WALL_AMP_ACTIVATOR must be exactly audio-jack or lcus-2")


def required_tools(method):
    """Return only the programs used by the selected detector/actuator path."""
    common = ("/usr/bin/arecord",)
    if method == "audio-jack":
        return common + ("/usr/bin/aplay", "/usr/bin/amixer")
    if method == "lcus-2":
        return common
    raise ConfigurationError("unknown amplifier actuator %r" % method)


def record_safe_state(verified):
    """Publish or revoke the artifact consumed by the S3 power path."""
    try:
        if not verified:
            try:
                os.unlink(SAFE_STATE_FILE)
            except FileNotFoundError:
                pass
            return
        tmp = SAFE_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="ascii") as handle:
            handle.write("OFF\n")
        os.replace(tmp, SAFE_STATE_FILE)
    except OSError as exc:
        log("could not update amplifier safe-state proof: %s" % exc)


def retry_beat(now, started_at, jack_actuator):
    """How long to wait between actuator attempts, at this moment.

    One second while the re-enumeration this service was restarted for could
    still be settling, five afterwards. Pure, so the timing that the ten-second
    acceptance depends on is testable without a relay.
    """
    if jack_actuator:
        return ACTUATOR_RETRY_SECONDS
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
    safe = True
    if method == "lcus-2":
        safe = ensure_off_bounded(actuator)
    record_safe_state(safe)

    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())

    # An idling service has no detector loop to reconcile the relay later, so
    # for those two paths an unverified OFF is still a hard failure that the
    # RestartSec is the only cure for.
    _idle = (os.environ.get("WALL_AMP_ENABLED", "true").strip().lower()
             in ("false", "0", "no")) or current_mode() != "trigger"
    if _idle and not safe:
        return 1

    if os.environ.get("WALL_AMP_ENABLED", "true").strip().lower() in ("false", "0", "no"):
        log("WALL_AMP_ENABLED is false; idling without emitting anything")
        while not stopping.wait(3600):
            pass
        return 0 if stop_and_record(actuator) is not False else 1

    if current_mode() != "trigger":
        log("panel mode: the amplifier is not commanded; idling")
        # Not an error and not a restart loop -- the mode script restarts this
        # unit when the mode changes back.
        while not stopping.wait(3600):
            pass
        return 0 if stop_and_record(actuator) is not False else 1

    jack_actuator = method == "audio-jack"
    if jack_actuator:
        assert_trigger_output()
    else:
        log("amplifier actuator: LCUS-2 channel %s at %s" % (channel, device))
    levels = [Level(pcm, off) for pcm, off in SOURCES]
    for lv in levels:
        lv.start()
    on = False
    above_since = None
    below_since = None
    changed_at = 0.0
    last_jack_poll = 0.0
    last_actuator_attempt = -1e9
    last_off_attempt = -1e9
    jack = True
    started_at = time.monotonic()
    last_reassert = started_at

    log("watching %s" % ", ".join(pcm for pcm, _ in SOURCES))
    while not stopping.is_set():
        now = time.monotonic()

        if jack_actuator and now - last_jack_poll >= JACK_POLL_SECONDS:
            last_jack_poll = now
            present = jack_present()
            if present != jack:
                log("trigger cable %s" % ("connected" if present else "REMOVED"))
            jack = present

        # Re-assert periodically. A wedged daemon that stops doing this is also
        # one whose tone should not be trusted to stop; this is the cheap half
        # of the watchdog.
        if now - last_reassert >= 60.0:
            last_reassert = now
            if jack_actuator:
                assert_trigger_output()

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
        beat = retry_beat(now, started_at, jack_actuator)
        if not on and not safe and now - last_off_attempt >= beat:
            last_off_attempt = now
            safe = stop_and_record(actuator) is not False
            if safe:
                log("relay reconciled to a verified OFF")

        loud = any(lv.above(ON_DBFS) for lv in levels)
        quiet = all(not lv.above(OFF_DBFS) for lv in levels)

        if not on:
            if loud and jack:
                above_since = above_since or now
                if now - above_since >= ATTACK_SECONDS and now - changed_at >= MIN_OFF_SECONDS:
                    if now - last_actuator_attempt >= beat:
                        record_safe_state(False)
                        if actuator.start():
                            on = True
                            safe = False
                            changed_at = now
                            below_since = None
                            log("amplifier ON (%s)"
                                % ", ".join(lv.report() for lv in levels))
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
            if jack_actuator and not jack and actuator.running:
                log("trigger cable removed; stopping tone")
                safe = stop_and_record(actuator) is not False
                on = False
                changed_at = now
            elif (on and not actuator.maintain(now)
                  and now - last_actuator_attempt >= beat):
                last_actuator_attempt = now
                log("amplifier actuator stopped unexpectedly; restarting")
                if not actuator.start():
                    on = False
                    # The relay is in an unknown physical state: the loop above
                    # keeps commanding OFF until one is verified.
                    safe = False
                    changed_at = now

        stopping.wait(BLOCK_SECONDS)

    stopped = stop_and_record(actuator)
    for lv in levels:
        lv.stop()
    return 0 if stopped is not False else 1


if __name__ == "__main__":
    sys.exit(main())
