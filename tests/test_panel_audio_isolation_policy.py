"""The broker isolates what can hang, and stops paying for what cannot.

WHY THIS FILE EXISTS. `_isolated_backend_call` spawns a whole Python interpreter
per backend call, because a backend that talks to a device can hang and Python
cannot kill a stuck thread. Right for hardware; expensive for an observation the
backend answers by reading a file.

Measured on the wall panel 2026-09-17: `wall-audio-router` held 64% of one core
-- about a sixth of that four-core box -- continuously. `strace -c` showed 32
`execve` of `/usr/bin/python3 --multiprocessing-fork` in eight seconds, with
4201 `openat` and 2334 `mmap` behind them, all module imports. The cause was the
renderer polling `telemetry` at 4 Hz whenever the display is lit
(`setInterval(pollAudioTelemetry, 250)`), each poll starting an interpreter to
read two JSON files out of /run.

The shipped backend CANNOT hang on hardware. Its own module docstring says so:
it runs as `panel` under ProtectSystem=strict with AF_UNIX as its only address
family, so it can neither run `amixer` nor talk to systemd.

THE SHAPE OF THE FIX IS WHAT THESE TESTS PIN. The exemption is DECLARED BY THE
BACKEND and defaults to off, so a future routed-device backend (WSN-024) that
answers `status` from BlueZ inherits isolation by saying nothing at all. A
blanket "telemetry is cheap" rule inside the broker would have gone silently
wrong the day that backend landed.
"""

from pathlib import Path
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stack/panel-audio"))
from audio_router import (AudioBroker, UnavailableBackend, backend_answers_locally)
from switch_backend import SwitchApplierBackend
from routing import switch_only_authorization


class _CountingBroker(AudioBroker):
    """A broker that records every time it would start an interpreter."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spawns = []

    def _isolated_backend_call(self, operation, method, params):
        self.spawns.append(method)
        return super()._isolated_backend_call(operation, method, params)


class _DeclaresNothing:
    """A backend that says nothing about its methods: the default must isolate."""

    def call(self, method, params, cancel):
        return {"available": False}

    def inventory(self, cancel):
        return []


def test_backend_answers_locally_defaults_to_false():
    """Saying nothing must mean "isolate me". Silence is not a cheap answer."""
    backend = _DeclaresNothing()
    for method in ("status", "telemetry", "select_output", "request_landed", None):
        assert backend_answers_locally(backend, method) is False


def test_backend_answers_locally_reads_the_declaration():
    backend = UnavailableBackend()
    assert backend_answers_locally(backend, "status") is True
    assert backend_answers_locally(backend, "telemetry") is True
    # Declared or not, a mutation is never local.
    assert backend_answers_locally(backend, "select_output") is False
    # A method that observes a MUTATION settling stays isolated deliberately.
    assert backend_answers_locally(backend, "request_landed") is False


def test_a_malformed_declaration_is_treated_as_no_declaration():
    """A backend whose declaration cannot be tested against must not be trusted.

    Fail toward isolation: the expensive answer is the safe one.
    """
    class Broken:
        LOCAL_ONLY_METHODS = 7  # not a container

        def call(self, method, params, cancel):
            return None

        def inventory(self, cancel):
            return []

    assert backend_answers_locally(Broken(), "telemetry") is False


def _broker(backend, tmp_path):
    return _CountingBroker(backend, authorize=switch_only_authorization,
                           isolate_backend=True,
                           state_path=str(tmp_path / "state.json"))


def test_telemetry_does_not_start_an_interpreter(tmp_path):
    """The 4 Hz poll that cost 64% of a core must spawn nothing."""
    broker = _broker(UnavailableBackend(), tmp_path)
    for _ in range(5):
        broker._backend_call("call", "telemetry", {})
    assert broker.spawns == [], "a file-answered observation must not fork a process"


def test_an_undeclared_method_still_starts_one(tmp_path):
    """The protection that matters is untouched, and this proves it by counting."""
    broker = _broker(_DeclaresNothing(), tmp_path)
    broker._backend_call("call", "telemetry", {})
    assert broker.spawns == ["telemetry"], "an undeclared backend must still be isolated"


def test_the_shipped_switch_backend_declares_only_its_file_reads():
    """The real backend's declaration, held to exactly what it can answer locally."""
    assert SwitchApplierBackend.LOCAL_ONLY_METHODS == frozenset({"status", "telemetry"})


# ---------------------------------------------------------------------------
# The activity hold (Owner, 2026-09-17)
# ---------------------------------------------------------------------------

from visualizer import VisualizerTelemetry  # noqa: E402


def _tone(level):
    """One window at a steady level, as a real capture would deliver it."""
    return [level, -level] * 64


def test_the_shipped_activity_defaults_are_the_owner_s_numbers():
    """Pinned, because the producer takes the DEFAULTS and passes neither.

    `panel-bus-visualizer.py` constructs `VisualizerTelemetry` with a band count,
    a sample rate, the band centres and a publish interval -- and says nothing
    about the floor or the hold. So these defaults ARE the panel's behaviour, and
    a change to them is a change to the wall.
    """
    core = VisualizerTelemetry()
    assert core.silence_hold_ms == 5000, "the Owner asked for about five seconds"
    assert core.silence_floor == 0.004, "-48 dBFS; 0.01 called quiet passages silence"


def test_a_gap_shorter_than_the_hold_stays_active():
    """A pause between tracks must not send the wall to Frame Media."""
    core = VisualizerTelemetry(minimum_interval_ms=50)
    assert core.process(_tone(0.5), generation=1, observed_monotonic_ms=0)["active"] is True
    # Silence 4 seconds later: inside the hold.
    held = core.process(_tone(0.0), generation=1, observed_monotonic_ms=4000)
    assert held["active"] is True, "4 s of quiet is a gap, not the end of playback"


def test_a_gap_longer_than_the_hold_goes_inactive():
    """The hold extends activity; it must not assert it forever."""
    core = VisualizerTelemetry(minimum_interval_ms=50)
    core.process(_tone(0.5), generation=1, observed_monotonic_ms=0)
    done = core.process(_tone(0.0), generation=1, observed_monotonic_ms=5001)
    assert done["active"] is False, "past the hold, silence is silence"


def test_the_hold_cannot_invent_activity_that_never_happened():
    """A bus that has never carried audio is never held active."""
    core = VisualizerTelemetry(minimum_interval_ms=50)
    first = core.process(_tone(0.0), generation=1, observed_monotonic_ms=0)
    assert first["active"] is False
    later = core.process(_tone(0.0), generation=1, observed_monotonic_ms=1000)
    assert later["active"] is False


def test_reset_drops_the_hold():
    """Suspend, disconnect and disable must not resume into a stale hold."""
    core = VisualizerTelemetry(minimum_interval_ms=50)
    core.process(_tone(0.5), generation=1, observed_monotonic_ms=0)
    core.reset()
    resumed = core.process(_tone(0.0), generation=2, observed_monotonic_ms=100)
    assert resumed["active"] is False


def test_a_level_between_the_old_floor_and_the_new_one_is_audio():
    """The floor change is load-bearing, not cosmetic.

    An rms around -44 dBFS read as SILENCE before and reads as audio now, which
    is the half of the fix a longer hold alone would not have delivered.
    """
    quiet = _tone(0.006)
    assert VisualizerTelemetry(minimum_interval_ms=50).process(
        quiet, generation=1, observed_monotonic_ms=0)["active"] is True
    assert VisualizerTelemetry(silence_floor=0.01, minimum_interval_ms=50).process(
        quiet, generation=1, observed_monotonic_ms=0)["active"] is False


# ---------------------------------------------------------------------------
# The two defects terra found in the isolation change (2026-09-17)
# ---------------------------------------------------------------------------

def test_a_string_declaration_is_refused():
    """A string answers membership by SUBSTRING and is not a set of methods.

    `LOCAL_ONLY_METHODS = "telemetry"` would otherwise exempt "tele" and "etry"
    too. A declaration that is not a collection of names is a mistake, not
    permission.
    """
    class Stringly:
        LOCAL_ONLY_METHODS = "telemetry"

        def call(self, method, params, cancel):
            return None

        def inventory(self, cancel):
            return []

    backend = Stringly()
    assert backend_answers_locally(backend, "telemetry") is False
    assert backend_answers_locally(backend, "tele") is False


def test_a_mapping_declaration_is_refused():
    """A mapping would opt in by its keys, which nobody wrote down as intent."""
    class Mapped:
        LOCAL_ONLY_METHODS = {"telemetry": True}

        def call(self, method, params, cancel):
            return None

        def inventory(self, cancel):
            return []

    assert backend_answers_locally(Mapped(), "telemetry") is False


def test_a_declaration_of_non_strings_is_refused():
    class Weird:
        LOCAL_ONLY_METHODS = frozenset({"telemetry", 7})

        def call(self, method, params, cancel):
            return None

        def inventory(self, cancel):
            return []

    assert backend_answers_locally(Weird(), "telemetry") is False


def test_a_declaration_that_explodes_when_read_is_refused():
    """Fail toward isolation on ANY exception, not just TypeError."""
    class Exploding:
        @property
        def LOCAL_ONLY_METHODS(self):
            raise RuntimeError("no")

        def call(self, method, params, cancel):
            return None

        def inventory(self, cancel):
            return []

    assert backend_answers_locally(Exploding(), "telemetry") is False


def test_a_worker_that_cannot_start_does_not_leak_its_slot(tmp_path, monkeypatch):
    """The slot is released by the WORKER, so a worker that never runs leaks it.

    `start()` raises when the process cannot create another thread. The
    semaphore is acquired before that line, so each failure used to cost one
    permanent slot and MAX_BACKEND_WORKERS of them would leave the broker
    answering `backend_busy` for the life of the process. Latent while only
    tests reached this seam; live from the moment production did.
    """
    import audio_router

    broker = _broker(UnavailableBackend(), tmp_path)

    class _Refusing:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(audio_router.threading, "Thread", _Refusing)
    for _ in range(audio_router.MAX_BACKEND_WORKERS + 2):
        with pytest.raises(RuntimeError):
            broker._backend_call("call", "telemetry", {})

    # The capacity must be intact: every slot was handed back.
    monkeypatch.undo()
    assert broker._backend_call("call", "telemetry", {}) == {"available": False}, (
        "the broker still had capacity after repeated thread-start failures")
