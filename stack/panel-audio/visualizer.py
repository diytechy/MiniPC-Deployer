"""Bounded, stateless audio-to-telemetry transform; raw samples never escape.

This core is backend-independent. A later, physically proven PipeWire adapter
may feed it monitor samples, but the image currently ships no capture adapter.
Implements: SR-023, LLR-007.
"""

from __future__ import annotations

import math
import time
from typing import Iterable


MAX_SAMPLES = 2048
MAX_BANDS = 16
JS_SAFE_INTEGER = 9_007_199_254_740_991


def analyze_samples(
    samples: Iterable[float], *, generation: int, band_count: int = 8,
    silence_floor: float = 0.01, observed_monotonic_ms: int | None = None,
    max_samples: int = MAX_SAMPLES,
) -> dict[str, object]:
    """Return finite normalized RMS/peak/bands and discard the input buffer.

    Inputs: mono normalized samples in [-1,1], at most MAX_SAMPLES; generation
      non-negative; band_count 1..MAX_BANDS; silence_floor 0..1.
    Raises: ValueError for non-finite/out-of-range or oversized input.
    Implements: SR-023, LLR-007.
    """
    if (isinstance(generation, bool) or not isinstance(generation, int) or
            not 0 <= generation <= JS_SAFE_INTEGER):
        raise ValueError("generation must be a non-negative integer")
    if (isinstance(band_count, bool) or not isinstance(band_count, int) or
            isinstance(max_samples, bool) or not isinstance(max_samples, int) or
            isinstance(silence_floor, bool) or not isinstance(silence_floor, (int, float)) or
            not math.isfinite(silence_floor) or not 1 <= band_count <= MAX_BANDS or
            not 1 <= max_samples <= MAX_SAMPLES or not 0 <= silence_floor <= 1):
        raise ValueError("telemetry bounds invalid")
    if observed_monotonic_ms is not None and (
        isinstance(observed_monotonic_ms, bool) or
        not isinstance(observed_monotonic_ms, int) or
        not 0 <= observed_monotonic_ms <= JS_SAFE_INTEGER
    ):
        raise ValueError("observation time must be a non-negative integer")
    values: list[float] = []
    for sample in samples:
        if len(values) == max_samples:
            raise ValueError("sample window too large")
        if isinstance(sample, bool) or not isinstance(sample, (int, float)):
            raise ValueError("sample must be numeric and not boolean")
        value = float(sample)
        if not math.isfinite(value) or abs(value) > 1:
            raise ValueError("sample outside normalized finite range")
        values.append(value)
    if not values:
        values = [0.0]
    rms = math.sqrt(sum(v * v for v in values) / len(values))
    peak = max(abs(v) for v in values)
    # A small direct transform avoids a runtime dependency before the image's
    # package/probe gate. Only bounded representative bins are calculated.
    bands = []
    n = len(values)
    for band in range(band_count):
        k = 1 + (band * max(1, (n // 2) - 1) // band_count)
        real = sum(v * math.cos(2 * math.pi * k * i / n) for i, v in enumerate(values))
        imag = -sum(v * math.sin(2 * math.pi * k * i / n) for i, v in enumerate(values))
        bands.append(min(1.0, math.hypot(real, imag) * 2 / n))
    return {
        "generation": generation,
        "observedMonotonicMs": (
            int(time.monotonic() * 1000) if observed_monotonic_ms is None
            else observed_monotonic_ms
        ),
        "active": rms > silence_floor,
        "rms": round(min(1.0, rms), 6),
        "peak": round(min(1.0, peak), 6),
        "bands": [round(value, 6) for value in bands],
    }


class VisualizerTelemetry:
    """Apply silence hold, cadence and generation reset without sample retention.

    Only timestamps and the current generation survive a call. `process`
    returns None when the cadence cap suppresses an update, allowing a capture
    adapter to drop the whole window immediately. Implements: SR-023, LLR-007.
    """

    def __init__(self, *, band_count: int = 8, max_samples: int = MAX_SAMPLES,
                 silence_floor: float = 0.01, silence_hold_ms: int = 1500,
                 minimum_interval_ms: int = 50):
        if (isinstance(silence_hold_ms, bool) or not isinstance(silence_hold_ms, int)
                or not 0 <= silence_hold_ms <= 10_000):
            raise ValueError("silence hold outside 0..10000 ms")
        if (isinstance(minimum_interval_ms, bool) or not isinstance(minimum_interval_ms, int)
                or not 20 <= minimum_interval_ms <= 1000):
            raise ValueError("telemetry cadence outside 20..1000 ms")
        self.band_count = band_count
        self.max_samples = max_samples
        self.silence_floor = silence_floor
        self.silence_hold_ms = silence_hold_ms
        self.minimum_interval_ms = minimum_interval_ms
        self._generation: int | None = None
        self._last_observed_ms: int | None = None
        self._last_emitted_ms: int | None = None
        self._last_signal_ms: int | None = None

    def process(self, samples: Iterable[float], *, generation: int,
                observed_monotonic_ms: int) -> dict[str, object] | None:
        if (isinstance(generation, bool) or not isinstance(generation, int) or
                not 0 <= generation <= JS_SAFE_INTEGER):
            raise ValueError("generation must be a non-negative integer")
        if (isinstance(observed_monotonic_ms, bool) or
                not isinstance(observed_monotonic_ms, int) or
                not 0 <= observed_monotonic_ms <= JS_SAFE_INTEGER):
            raise ValueError("observation time must be a non-negative integer")
        if self._last_observed_ms is not None and observed_monotonic_ms < self._last_observed_ms:
            raise ValueError("monotonic observation time moved backwards")
        if generation != self._generation:
            self._generation = generation
            self._last_emitted_ms = None
            self._last_signal_ms = None
        self._last_observed_ms = observed_monotonic_ms
        if (self._last_emitted_ms is not None and
                observed_monotonic_ms - self._last_emitted_ms < self.minimum_interval_ms):
            return None
        telemetry = analyze_samples(
            samples, generation=generation, band_count=self.band_count,
            silence_floor=self.silence_floor, observed_monotonic_ms=observed_monotonic_ms,
            max_samples=self.max_samples,
        )
        if telemetry["active"]:
            self._last_signal_ms = observed_monotonic_ms
        elif self._last_signal_ms is not None:
            telemetry["active"] = observed_monotonic_ms - self._last_signal_ms <= self.silence_hold_ms
        self._last_emitted_ms = observed_monotonic_ms
        return telemetry

    def reset(self) -> None:
        """Discard derived activity state on suspend/disconnect/disable."""
        self._generation = self._last_observed_ms = None
        self._last_emitted_ms = self._last_signal_ms = None
