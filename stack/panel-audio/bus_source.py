"""The pure half of the merged-bus visualizer source.

ONE RESPONSIBILITY: everything `panel-bus-visualizer.py` decides, with none of
what it opens. Block arithmetic, the published document's exact shape, the
depth-one backpressure rule and the peer-authorization policy live here so that
all four can be tested without an ALSA device, a socket or a service.

WHAT THIS SOURCE IS, AND WHY IT IS NOT THE AMPLIFIER DETECTOR'S (SR-041).
`wall-amp-trigger` reads `speaker_tap`, which is POST-switch: it sees audio only
while the switch is on Speaker, deliberately, because its job is the relay. That
makes it the wrong signal for a visualizer twice over -- Headset shows nothing
at all, and the Owner asked for every source that reaches the merged bus. This
source reads `bus_monitor`, which is PRE-switch and carries Library, Pandora,
Bluetooth and the S/PDIF input together. The cost of being pre-switch is that
bus activity alone is NOT "playing": Mute carries samples, so the confirmed
output selection is part of the decision and is applied by the broker, not here.

RAW AUDIO. Nothing in this module retains a sample. `mono_window` returns a
derived window and the caller drops the block; `LatestBlockQueue` holds at most
ONE unsent block and overwrites it rather than growing. Raw audio is not
retained or emitted unless explicitly permitted: the one permission is
ProjectM's, it is local and memory-only, it lasts only while ProjectM is the
selected and visible fullscreen owner, and the samples are never written to
disk, journaled, put in diagnostics or telemetry, uploaded, or kept after
ProjectM yields.

Implements: SR-041, LLR-950, LLR-951, LLR-955, LLR-954.
"""

from __future__ import annotations

import math
import struct
from typing import Sequence

# The bus, exactly as `asound-bus-mode.conf` declares it. These are not
# preferences: `bus_monitor` is a dsnoop whose slave fixes all four, and a
# capture that asked for anything else would be resampled behind our back.
BUS_RATE = 48000
BUS_CHANNELS = 2
BUS_PERIOD_FRAMES = 1024
BUS_SAMPLE_BYTES = 2
BUS_BLOCK_BYTES = BUS_PERIOD_FRAMES * BUS_CHANNELS * BUS_SAMPLE_BYTES

# The analysis window, and why it is two periods rather than one. A 1024-frame
# period is 21.3 ms, which is barely more than one cycle of the 60 Hz band --
# a single-frequency probe over one cycle is mostly noise. Two periods is
# 42.7 ms and two and a half cycles, which is the cheapest window that lets the
# lowest band mean anything.
WINDOW_FRAMES = 2 * BUS_PERIOD_FRAMES
# Decimate by four with a four-tap boxcar, giving 512 mono samples at 12 kHz.
# The boxcar's first null sits exactly at the decimated Nyquist, so it is the
# anti-alias filter this decimation needs rather than merely a smoothing of it,
# and nothing audible folds into a displayed band. The same argument, and the
# same two constants, as the amplifier detector's own readout.
WINDOW_DECIMATE = 4
WINDOW_BOXCAR = 4
WINDOW_SAMPLES = WINDOW_FRAMES // WINDOW_DECIMATE
WINDOW_RATE = BUS_RATE / WINDOW_DECIMATE

# Band edges in Hz, geometric from 60 Hz, and the SAME EIGHT the panel has shown
# since 2026-09-14. The wall's appearance is not what this work package is
# changing; its source is.
BUS_BANDS_HZ = (60.0, 120.0, 240.0, 480.0, 960.0, 1900.0, 3400.0, 5200.0)
# The dB window the published 0..1 scalars are mapped onto. Linear in DECIBELS,
# because a value linear in AMPLITUDE sits at zero until somebody shouts.
BUS_FLOOR_DBFS = -60.0
BUS_REFERENCE_DBFS = -12.0

# `schema` 2, and the bump is not cosmetic: a consumer that believes a version-1
# document is believing `speaker_tap`, which answers a different question. The
# broker refuses anything that is not exactly (2, "bus_monitor").
DOCUMENT_SCHEMA = 2
DOCUMENT_SOURCE = "bus_monitor"

# The three things the producer can be, and they are three answers rather than
# two. `unavailable` is the panel not being in bus mode, or the capture refusing
# to open -- there is no measurement at all; `silent` is a measurement of a quiet
# bus. A visualizer renders those differently (absent versus a quiet room), so
# they may not share a representation.
STATE_LIVE = "live"
STATE_SILENT = "silent"
STATE_UNAVAILABLE = "unavailable"
DOCUMENT_STATES = (STATE_LIVE, STATE_SILENT, STATE_UNAVAILABLE)


def mono_window(block: bytes, *, channels: int = BUS_CHANNELS,
                decimate: int = WINDOW_DECIMATE, boxcar: int = WINDOW_BOXCAR,
                limit: int = WINDOW_SAMPLES) -> list[float]:
    """One interleaved S16_LE block as decimated mono samples in [-1,1].

    Contract:
      Inputs:  block: interleaved signed 16-bit little-endian bytes, a whole
                      number of frames
               channels/decimate/boxcar: the shape and the filter
               limit: the most samples to return; later frames are discarded
      Outputs: a list of at most `limit` floats in [-1,1]
      Raises:  ValueError for a partial frame or a nonsensical shape.
    Implements: SR-041, LLR-950.
    """
    if not isinstance(block, (bytes, bytearray, memoryview)):
        raise ValueError("block must be bytes")
    block = bytes(block)
    for name, value, low, high in (("channels", channels, 1, 8),
                                   ("decimate", decimate, 1, 64),
                                   ("boxcar", boxcar, 1, 64),
                                   ("limit", limit, 1, 2048)):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError("%s outside its supported range" % name)
    frame_bytes = channels * BUS_SAMPLE_BYTES
    if len(block) % frame_bytes:
        raise ValueError("block is not a whole number of frames")
    frames = len(block) // frame_bytes
    samples = struct.unpack("<%dh" % (frames * channels), block)
    step = decimate * channels
    span = boxcar * channels
    divisor = float(channels * boxcar * 32768)
    window: list[float] = []
    for base in range(0, frames * channels - span + 1, step):
        if len(window) == limit:
            break
        total = 0.0
        for tap in range(boxcar):
            offset = base + tap * channels
            for channel in range(channels):
                total += samples[offset + channel]
        # The divisor already covers the full-scale sum of every tap and
        # channel, so the result cannot leave [-1,1] and the transform
        # downstream never has to clamp a measurement it was handed.
        window.append(total / divisor)
    return window


def scalar_from_amplitude(amplitude: float, *, floor_dbfs: float = BUS_FLOOR_DBFS,
                          reference_dbfs: float = BUS_REFERENCE_DBFS) -> float:
    """An amplitude in [0,1] as a bounded 0..1 scalar, linear in DECIBELS.

    DUPLICATED, DELIBERATELY, from `panel-amp-trigger.scalar_from_amplitude`,
    and for the same reason `switch_backend` duplicates the applier's output
    list: the detector runs in a different process with a different privilege
    and must not acquire an import from this tree, and it is the one module in
    the audio path this work package is forbidden to disturb. The two copies are
    held together by `tests/test_panel_bus_visualizer.py`, which imports both
    and asserts they agree across the whole curve.

    Implements: SR-041, LLR-951.
    """
    if isinstance(amplitude, bool) or not isinstance(amplitude, (int, float)):
        raise ValueError("amplitude must be numeric and not boolean")
    amplitude = float(amplitude)
    if not math.isfinite(amplitude) or amplitude < 0.0:
        raise ValueError("amplitude must be finite and non-negative")
    if not amplitude > 0.0:
        return 0.0
    dbfs = 20.0 * math.log10(amplitude)
    span = reference_dbfs - floor_dbfs
    return max(0.0, min(1.0, (dbfs - floor_dbfs) / span))


def build_document(telemetry, *, state: str, generation: int,
                   observed_monotonic_ms: int,
                   bands: int = len(BUS_BANDS_HZ)) -> dict[str, object]:
    """The exact document `/run/wall-bus-visualizer/bus-telemetry.json` carries.

    THE dB MAPPING HAPPENS HERE, not in the transform. `analyze_samples`
    measures and returns linear magnitudes, which is the honest thing for a
    measurement to be; the curve that makes a magnitude legible on a wall is a
    presentation choice belonging to this producer, and keeping the two apart is
    what lets the transform stay shared with anything else that ever measures.

    Contract:
      Inputs:  telemetry: an `analyze_samples`/`VisualizerTelemetry` result, or
                          None when there is no measurement at all
               state: one of DOCUMENT_STATES
               generation: the capture generation, bumped on every reopen
               observed_monotonic_ms: when the window was captured
      Outputs: the document, ready to be serialized
      Raises:  ValueError for an unknown state or a malformed measurement.
    Implements: SR-041, LLR-951.
    """
    if state not in DOCUMENT_STATES:
        raise ValueError("unknown producer state")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("generation must be a non-negative integer")
    if (isinstance(observed_monotonic_ms, bool)
            or not isinstance(observed_monotonic_ms, int) or observed_monotonic_ms < 0):
        raise ValueError("observation time must be a non-negative integer")
    if state != STATE_LIVE or telemetry is None:
        # A PRODUCER THAT IS NOT MEASURING PUBLISHES ZEROS, NEVER THE LAST THING
        # IT SAW. A frozen visualizer is indistinguishable from a quiet room,
        # which is exactly the confusion `state` exists to prevent.
        return _document(state, generation, observed_monotonic_ms,
                         0.0, 0.0, [0.0] * bands)
    measured_bands = telemetry.get("bands")
    if not isinstance(measured_bands, list) or len(measured_bands) != bands:
        raise ValueError("measurement does not carry the expected bands")
    return _document(
        state, generation, observed_monotonic_ms,
        scalar_from_amplitude(_measured(telemetry, "rms")),
        scalar_from_amplitude(_measured(telemetry, "peak")),
        [scalar_from_amplitude(value) for value in measured_bands],
        active=telemetry.get("active") is True,
    )


def _document(state, generation, observed, rms, peak, bands, active=False):
    return {
        "schema": DOCUMENT_SCHEMA,
        # NAMED, so a consumer can never present a level from somewhere else as
        # the merged bus. `bus_monitor` is PRE-switch: it carries what every
        # source is sending, not what the room is hearing.
        "source": DOCUMENT_SOURCE,
        "state": state,
        "valid": state == STATE_LIVE,
        "active": bool(active),
        "generation": generation,
        "rms": round(rms, 6),
        "peak": round(peak, 6),
        "bands": [round(value, 6) for value in bands],
        "observed_monotonic_ms": observed,
        "reference_dbfs": BUS_REFERENCE_DBFS,
    }


def _measured(telemetry, key):
    value = telemetry.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("measurement %s is not numeric" % key)
    return float(value)


class LatestBlockQueue:
    """A one-deep mailbox: the newest block wins and the old one is dropped.

    THE DEPTH IS ONE AND THAT IS THE WHOLE DESIGN (LLR-955). A consumer that
    falls behind must not cause a queue to grow, because a growing queue of
    audio is a recording held in memory -- which the privacy rule forbids
    outright -- and because stale audio is worthless to a visualizer anyway. So
    there is no buffer to bound, no high-water mark to tune and no drain policy:
    there is one slot, `put` overwrites it, and `drops` counts how often that
    happened so a lifecycle counter can say so without ever naming a sample.
    """

    __slots__ = ("_block", "drops", "accepted")

    def __init__(self):
        self._block = None
        self.drops = 0
        self.accepted = 0

    def put(self, block) -> bool:
        """Offer a block. Returns False when it displaced an unconsumed one."""
        replaced = self._block is not None
        self._block = block
        self.accepted += 1
        if replaced:
            self.drops += 1
        return not replaced

    def take(self):
        """Return and clear the pending block, or None."""
        block, self._block = self._block, None
        return block

    def clear(self) -> None:
        """Drop the pending block. Called on every teardown path."""
        self._block = None

    @property
    def pending(self) -> bool:
        return self._block is not None


def peer_allowed(uid, *, allowed_uids: Sequence[int]) -> bool:
    """Whether a connected peer's uid may receive PCM.

    THE THREAT MODEL IS THE OWNER'S, STATED PLAINLY: this design exists to make
    checking items off NagLight easy while keeping children from checking things
    off for fun. The peer credential the kernel supplies, plus the socket's own
    0660 group permission, is therefore the right-sized authorization for a
    local, single-user kiosk -- two independent facts the kernel vouches for,
    neither of which a curious person at the glass can forge. It is NOT a
    defence against a compromised host or a hostile local process, it does not
    pretend to be, and a reviewer shall not read it as one.

    Implements: SR-041, LLR-954.
    """
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        return False
    clean = [value for value in allowed_uids
             if not isinstance(value, bool) and isinstance(value, int) and value >= 0]
    # An EMPTY allow-list denies. The alternative -- treating "nobody was
    # configured" as "anybody may" -- is the failure mode that turns a missing
    # line in a unit file into an open socket.
    return bool(clean) and uid in clean


def band_centres(limit_hz: float = WINDOW_RATE / 2.0) -> tuple[float, ...]:
    """The published band centres, with any above the window's Nyquist dropped.

    Returned rather than assumed so the count in the document and the count the
    transform is asked for can never disagree: the document's band list is
    exactly as long as this tuple.
    """
    return tuple(centre for centre in BUS_BANDS_HZ if centre < limit_hz)


def telemetry_state(*, mode_is_bus: bool, capture_alive: bool,
                    measurement: object) -> str:
    """Which of the three producer states this moment is in.

    Implements: SR-041, LLR-950.
    """
    if not mode_is_bus or not capture_alive:
        return STATE_UNAVAILABLE
    return STATE_LIVE if measurement is not None else STATE_SILENT


def split_frames(buffer: bytes, decode_header, header_bytes: int):
    """Split a growing byte buffer into whole frames and a remainder.

    Contract:
      Inputs:  buffer: everything read so far and not yet consumed
               decode_header: the frame codec's validating header decoder
               header_bytes: that codec's fixed header length
      Outputs: (list of whole frames, the bytes left over)
      Raises:  whatever `decode_header` raises. It is NOT swallowed: a stream
               that has lost frame alignment cannot be resynchronised by
               guessing, so the only correct response is to drop the connection.
    Implements: SR-041, LLR-953.
    """
    frames: list[bytes] = []
    while len(buffer) >= header_bytes:
        fields = decode_header(buffer[:header_bytes])
        total = header_bytes + fields["payloadBytes"]
        if len(buffer) < total:
            break
        frames.append(buffer[:total])
        buffer = buffer[total:]
    return frames, buffer
