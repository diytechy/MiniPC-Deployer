#!/usr/bin/env python3
"""Power the speaker amplifier while audio is playing.

The panel emits a continuous tone on the ALC255 headphone jack whenever it sees
audio; a rectifier on the far end turns that into a DC level that holds the
amplifier's relay closed. Software decides, analog carries.

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


def _env(name, default):
    """Tunable from /etc/wall-panel/amp-trigger.env without editing this file.

    Every one of these is a number someone will want to change after living with
    it for a week, and a bad value must not stop the amplifier working -- so an
    unparseable override falls back to the default rather than failing to start.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log("ignoring unparseable %s=%r" % (name, raw))
        return default


# Thresholds on the DC-stripped RMS of the louder channel, relative to each
# source's nominal full scale. The measured floor is -83 dBFS, so -60 sits 23 dB
# clear of it. Hysteresis, because a single threshold chatters at the boundary.
ON_DBFS = _env("WALL_AMP_ON_DBFS", -60.0)
OFF_DBFS = _env("WALL_AMP_OFF_DBFS", -68.0)

# A click or a pop must not cycle the relay.
ATTACK_SECONDS = _env("WALL_AMP_ATTACK_SECONDS", 0.3)
# Gaps between tracks, dialogue pauses and quiet passages all dip below any
# usable threshold, so the hold-off is minutes rather than seconds.
HOLD_OFF_SECONDS = _env("WALL_AMP_HOLD_OFF_SECONDS", 240.0)
# Protect the relay contacts and the amplifier from rapid cycling whatever the
# audio does.
MIN_ON_SECONDS = _env("WALL_AMP_MIN_ON_SECONDS", 30.0)
MIN_OFF_SECONDS = _env("WALL_AMP_MIN_OFF_SECONDS", 10.0)

# The trigger output.
TRIGGER_CARD = "PCH"
TRIGGER_PCM = "trigger_out"
TRIGGER_FREQ = _env("WALL_AMP_TONE_HZ", 1000.0)
TRIGGER_AMPLITUDE = 0.99
# 'Front Headphone Jack' -- a cable that has fallen out otherwise presents as
# "the amplifier stopped working" with nothing anywhere to say why. The internal
# speaker is muted and auto-mute disabled independently of this, so this is
# diagnostics rather than safety.
JACK_CONTROL = "Front Headphone Jack"
JACK_POLL_SECONDS = 5.0

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
    except (OSError, subprocess.SubprocessError):
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
    """
    amixer(TRIGGER_CARD, "-q", "sset", "Master", "100%", "unmute")
    amixer(TRIGGER_CARD, "-q", "sset", "Headphone", "100%", "unmute")
    # The internal speaker stays muted and auto-mute disabled in both modes;
    # re-asserted here so a stray mixer change cannot put a full-scale tone on
    # the panel's own speaker.
    amixer(TRIGGER_CARD, "-q", "sset", "Speaker", "mute")
    amixer(TRIGGER_CARD, "-q", "sset", "Auto-Mute Mode", "Disabled")


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
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.alive = True
                backoff = 1.0
                while not self._stop.is_set():
                    raw = proc.stdout.read(BLOCK_BYTES)
                    if not raw or len(raw) < BLOCK_BYTES:
                        break
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
                    try:
                        proc.kill(); proc.wait(timeout=2)
                    except (OSError, subprocess.SubprocessError):
                        pass
            # A source that is absent in this mode (kiosk_monitor when the
            # Loopback is not configured) must not spin.
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

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
        return self._proc is not None and self._proc.poll() is None

    def start(self):
        if self.running:
            return
        self._stop.clear()
        try:
            self._proc = subprocess.Popen(
                ["/usr/bin/aplay", "-D", TRIGGER_PCM, "-f", "S16_LE",
                 "-r", str(RATE), "-c", str(CHANNELS), "-q", "-"],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log("cannot start tone: %s" % exc)
            self._proc = None
            return
        self._thread = threading.Thread(target=self._feed, daemon=True)
        self._thread.start()

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
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate(); proc.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass


def main():
    for tool in ("/usr/bin/arecord", "/usr/bin/aplay", "/usr/bin/amixer"):
        if not shutil.which(tool):
            log("missing %s" % tool)
            return 78

    if current_mode() != "trigger":
        log("panel mode: the amplifier is not commanded; idling")
        # Not an error and not a restart loop -- the mode script restarts this
        # unit when the mode changes back.
        while True:
            time.sleep(3600)

    assert_trigger_output()
    levels = [Level(pcm, off) for pcm, off in SOURCES]
    for lv in levels:
        lv.start()
    tone = Tone()

    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())

    on = False
    above_since = None
    below_since = None
    changed_at = 0.0
    last_jack_poll = 0.0
    jack = True
    last_reassert = time.monotonic()

    log("watching %s" % ", ".join(pcm for pcm, _ in SOURCES))
    while not stopping.is_set():
        now = time.monotonic()

        if now - last_jack_poll >= JACK_POLL_SECONDS:
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
            assert_trigger_output()

        loud = any(lv.above(ON_DBFS) for lv in levels)
        quiet = all(not lv.above(OFF_DBFS) for lv in levels)

        if not on:
            if loud and jack:
                above_since = above_since or now
                if now - above_since >= ATTACK_SECONDS and now - changed_at >= MIN_OFF_SECONDS:
                    on = True
                    changed_at = now
                    below_since = None
                    tone.start()
                    log("amplifier ON (%s)" % ", ".join(lv.report() for lv in levels))
            else:
                above_since = None
        else:
            if quiet:
                below_since = below_since or now
                if now - below_since >= HOLD_OFF_SECONDS and now - changed_at >= MIN_ON_SECONDS:
                    on = False
                    changed_at = now
                    above_since = None
                    tone.stop()
                    log("amplifier OFF after %.0fs idle" % HOLD_OFF_SECONDS)
            else:
                below_since = None
            if not jack and tone.running:
                log("trigger cable removed; stopping tone")
                tone.stop()
                on = False
                changed_at = now
            elif on and not tone.running:
                log("tone died; restarting")
                tone.start()

        stopping.wait(BLOCK_SECONDS)

    tone.stop()
    for lv in levels:
        lv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
