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
(`setInterval(pollAudioTelemetry, 250)` at the time), each poll starting an interpreter to
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


# ---------------------------------------------------------------------------
# The backend has to SURVIVE the isolation, not just deserve it (2026-09-19)
# ---------------------------------------------------------------------------
#
# Everything above this line asks WHICH calls are isolated. Nothing asked
# whether the isolated call works, and the answer on the shipped panel was no.
#
# `spawn` hands the child its work by PICKLING it, backend object included.
# WSN-024's `RoutedDeviceBackend` holds a `threading.Lock`; a `_thread.lock`
# cannot be pickled; `process.start()` raised a bare `TypeError` that
# `handle()`'s `except Exception` turned into `backend_failure` with nothing in
# the journal. Every mutation the broker accepts died there -- the Bluetooth
# verbs AND, because the routed backend wraps the switch one, the panel's own
# Mute/Headset/Speaker switch. The pane kept rendering correct live state,
# because `status` and `telemetry` are LOCAL_ONLY and pickle nothing, so the
# wall looked like a panel whose touch had died and was first reported as that.
#
# WHY THE SUITE WAS GREEN THROUGH ALL OF IT. Every other test of this seam uses
# the in-process path -- white-box tests opt into it deliberately -- so nothing
# in this repository had ever pickled a real backend. A test asserting that
# `inventory()` returns two devices passed that day and would have passed every
# day. The test therefore has to BE the spawn.

import json  # noqa: E402
import multiprocessing  # noqa: E402

import bluetooth_request  # noqa: E402
import routed_backend  # noqa: E402
from routing import device_authorization  # noqa: E402


def _shipped_document(now):
    """The observer document, in the shape `_document` requires."""
    return {
        "version": 1, "publishedAt": now, "reason": "ready", "adapter": True,
        "powered": True, "discoverable": False, "pairable": False,
        "devices": [{"alias": "pixel", "name": "Pixel", "kind": "input",
                     "trusted": True, "connected": True, "battery": None}],
        "route": {"input": "pixel", "output": None},
        "pairing": {"active": False, "discoverable": False, "passkey": None,
                    "device": None, "endsInSeconds": None},
        "generation": 3, "requestSeq": 100,
    }


def _shipped_backend(tmp_path):
    """The object `serve()` builds, assembled the way `serve()` assembles it.

    Built through both `backend_from_environment` functions rather than by
    calling the constructors, because the defect was in what the SHIPPED
    composition holds -- a hand-built backend is free to omit the field that
    breaks.
    """
    import os
    state = tmp_path / "bluetooth-state.json"
    state.write_text(json.dumps(_shipped_document(time.time())), encoding="utf-8")
    applier = tmp_path / "wall-bluetooth-device"
    applier.write_text("#!/bin/sh\n", encoding="utf-8")
    os.environ["WALL_AUDIO_REQUEST_PATH"] = str(tmp_path / "request.json")
    os.environ["WALL_AUDIO_STATE_PATH"] = str(tmp_path / "audio-state.json")
    os.environ["WALL_AUDIO_APPLIER_PATH"] = str(applier)
    os.environ["WALL_BLUETOOTH_STATE_PATH"] = str(state)
    os.environ["WALL_BLUETOOTH_REQUEST_PATH"] = str(tmp_path / "spool")
    os.environ["WALL_BLUETOOTH_APPLIER_PATH"] = str(applier)
    try:
        import switch_backend as _switch
        return routed_backend.backend_from_environment(_switch.backend_from_environment())
    finally:
        for name in ("WALL_AUDIO_REQUEST_PATH", "WALL_AUDIO_STATE_PATH",
                     "WALL_AUDIO_APPLIER_PATH", "WALL_BLUETOOTH_STATE_PATH",
                     "WALL_BLUETOOTH_REQUEST_PATH", "WALL_BLUETOOTH_APPLIER_PATH"):
            os.environ.pop(name, None)


import time  # noqa: E402


def test_the_shipped_backend_survives_the_spawn_boundary(tmp_path):
    """The exact transport `_isolated_backend_call` uses, on the real object.

    `multiprocessing.get_context("spawn")` rather than `pickle.dumps`: the
    context is what the broker uses, it may reduce differently, and a pickle
    assertion would pass on an object the child could not rebuild.
    """
    backend = _shipped_backend(tmp_path)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_echo_aliases, args=(child, backend), daemon=True)
    # A TypeError HERE, out of start(), is the shipped defect -- the child never
    # ran, so no assertion about its answer could have caught it.
    process.start()
    child.close()
    try:
        assert parent.poll(30), "the spawned child never answered"
        assert parent.recv() == ["pixel"], "the child rebuilt a backend that cannot see the document"
    finally:
        parent.close()
        process.join(10)


def _echo_aliases(pipe, backend):
    """Runs in the spawned child: prove the rebuilt object is usable, not just built.

    `inventory` is the call in the traceback -- `AudioBroker._mutation` makes it
    before EVERY mutation -- so it is the one that has to come back.
    """
    try:
        pipe.send(sorted(device.alias for device in backend.inventory(threading.Event())))
    finally:
        pipe.close()


def test_a_mutation_reaches_the_spool_through_the_isolated_path(tmp_path):
    """End to end, isolation ON: the verb the wall could not perform.

    This is the assertion that fails on the shipped tree. `isolate_backend` is
    left at its default so the broker chooses the spawn itself.
    """
    backend = _shipped_backend(tmp_path)
    broker = _CountingBroker(backend, authorize=device_authorization,
                             isolate_backend=True,
                             state_path=str(tmp_path / "broker-state.json"))
    result = broker._backend_call("call", "set_discoverable", {"enabled": True})
    assert broker.spawns == ["set_discoverable"], "the mutation must have been isolated"
    assert result["accepted"] is True
    spooled = bluetooth_request.pending(tmp_path / "spool")
    assert len(spooled) == 1, "the request never reached the applier's spool"
    assert json.loads(spooled[0].read_text(encoding="utf-8"))["event"]["kind"] == "set_discoverable"


def test_a_child_cannot_reuse_a_sequence_still_in_the_spool(tmp_path):
    """The guard `spawn` deleted, restored from the one place both children see.

    `_last_seq` lives in the child's COPY and dies with it, so two mutations in
    the same microsecond would mint the same sequence -- and a request file is
    NAMED after its sequence, so the second would overwrite a request the
    applier had not drained. The spool itself is the shared high-water mark.
    """
    backend = _shipped_backend(tmp_path)
    spool = tmp_path / "spool"
    # A request already waiting, at a sequence far above the clock's. Written
    # as a file rather than through `write`, because what `_highest_spooled`
    # reads is the NAME -- the applier has not drained it, and that is all
    # the next sequence has to clear.
    spool.mkdir(parents=True, exist_ok=True)
    (spool / bluetooth_request.request_name(10 ** 17)).write_text("{}\n", encoding="utf-8")
    fresh = routed_backend.RoutedDeviceBackend.__new__(routed_backend.RoutedDeviceBackend)
    fresh.__setstate__(backend.__getstate__())
    assert fresh._last_seq == -1, "a child must not inherit a high-water mark it cannot refresh"
    assert fresh._next_seq(None) > 10 ** 17, "a fresh child reused a spooled sequence"


def test_a_child_clears_the_mark_the_applier_has_already_committed_to(tmp_path):
    """The window the spool does not cover (terra, 2026-09-19).

    `wall-bluetooth-device` writes `mark.json` and unlinks the spool file
    BEFORE it performs the action, and republishes the document only when the
    action finishes -- up to forty seconds later for a `pair`. In between, the
    spool is empty and the document still names the PREVIOUS request, so a
    clock that steps backwards mints a sequence the applier will silently
    discard while the broker answers `accepted`.

    Both other floors are deliberately left saying "nothing here", which is
    exactly the state that window produces.
    """
    backend = _shipped_backend(tmp_path)
    assert not list(bluetooth_request.pending(tmp_path / "spool")), "the spool must be empty"
    (tmp_path / "mark.json").write_text(
        json.dumps({"generation": 3, "requestSeq": 10 ** 17}), encoding="utf-8")
    assert backend._next_seq(None) > 10 ** 17, (
        "a sequence at or below the applier's committed mark is discarded in silence")


@pytest.mark.parametrize("damage", ["", "not json", '{"requestSeq": true}',
                                    '{"requestSeq": -1}', '{"requestSeq": "8"}', '{}', '[]'])
def test_an_unreadable_mark_is_no_floor_rather_than_a_guessed_one(tmp_path, damage):
    """A floor is raised past; inventing one from a file that did not parse
    would hold back a request that was perfectly fine. Every shape answers
    None and lets the clock decide, and none of them raises."""
    backend = _shipped_backend(tmp_path)
    (tmp_path / "mark.json").write_text(damage, encoding="utf-8")
    assert backend._applier_mark() is None
    assert backend._next_seq(None) > 0


def test_a_sequence_taken_behind_the_backs_floors_is_refused_not_clobbered(tmp_path):
    """The reservation, which is a different guarantee from the floor.

    The floors are READ-THEN-WRITE. In a sequential test they look sufficient
    -- `_next_seq` re-reads the spool and steps past what is there -- which is
    exactly why a sequential test proves nothing here. Every mutation is its
    own spawned child, so two of them can both read the floors BEFORE either
    writes, and then both compute the same sequence. `os.replace` cannot
    refuse, so one request silently replaced the other while both callers were
    told `accepted`: a tap that did nothing.

    Forced by taking the sequence after `_next_seq` has chosen it and before
    `_submit` writes it, which is precisely the interleaving a second child
    produces.
    """
    backend = _shipped_backend(tmp_path)
    spool = tmp_path / "spool"
    chosen = backend._next_seq(None)

    original = routed_backend.RoutedDeviceBackend._next_seq
    calls = []

    def racing(self, document=None):
        seq = original(self, document)
        if not calls:
            calls.append(seq)
            # The other child wins the sequence in the gap.
            spool.mkdir(parents=True, exist_ok=True)
            (spool / bluetooth_request.request_name(seq)).write_text(
                json.dumps({"seq": seq, "event": {"kind": "discover"}}), encoding="utf-8")
        return seq

    routed_backend.RoutedDeviceBackend._next_seq = racing
    try:
        result = backend._submit({"kind": "cancel"})
    finally:
        routed_backend.RoutedDeviceBackend._next_seq = original

    assert result["seq"] != calls[0], "the loser must take a different sequence"
    spooled = bluetooth_request.pending(spool)
    assert len(spooled) == 2, "the other child's request was overwritten"
    kinds = sorted(json.loads(path.read_text(encoding="utf-8"))["event"]["kind"]
                   for path in spooled)
    assert kinds == ["cancel", "discover"], kinds
    # The acknowledged sequence must be the one on disk, or the renderer
    # correlates its reply to a request that is not there.
    assert (spool / bluetooth_request.request_name(result["seq"])).exists()
    assert chosen > 0


def test_the_exclusive_write_refuses_an_existing_sequence(tmp_path):
    """The primitive the reservation rests on, held on its own.

    `os.replace` overwrites and cannot be asked not to; `os.link` refuses. If
    this ever silently becomes a replace again, every test above it still
    passes, because they exercise the loop that this makes possible.
    """
    spool = tmp_path / "spool"
    bluetooth_request.write(41, {"kind": "discover", "timeoutSeconds": 30}, spool, exclusive=True)
    with pytest.raises(bluetooth_request.SequenceTaken):
        bluetooth_request.write(41, {"kind": "cancel"}, spool, exclusive=True)
    kept = json.loads((spool / bluetooth_request.request_name(41)).read_text(encoding="utf-8"))
    assert kept["event"]["kind"] == "discover", "the refused write still overwrote"
    # Without the flag the historic behaviour is untouched: the applier's own
    # tests deliberately rewrite a sequence to prove a REPLAY is refused
    # downstream, which is a different question from this one.
    bluetooth_request.write(41, {"kind": "cancel"}, spool)
    replaced = json.loads((spool / bluetooth_request.request_name(41)).read_text(encoding="utf-8"))
    assert replaced["event"]["kind"] == "cancel"


def test_no_temporary_is_left_in_the_appliers_spool(tmp_path):
    """The exclusive path LINKS rather than renames, so the temporary is not
    consumed and has to be removed deliberately. `pending` would skip it, but
    the applier's directory is not a scratch space and this leaks one file per
    tap."""
    spool = tmp_path / "spool"
    for seq in (7, 8, 9):
        bluetooth_request.write(seq, {"kind": "cancel"}, spool, exclusive=True)
    names = sorted(path.name for path in spool.iterdir())
    assert all(name.endswith(".json") and name[:-5].isdigit() for name in names), names


def test_a_sequence_that_can_never_be_reserved_refuses_rather_than_lying(monkeypatch, tmp_path):
    """The bound on the retry loop, and the direction it fails in.

    In ordinary use the loop cannot exhaust: `_next_seq` clears the HIGHEST
    spooled sequence, so a lost race hands the next attempt a free one. The
    bound is there because a loop in the verb path has to terminate on its own
    rather than on the argument that it cannot spin -- so the test forces the
    only state that spins, a writer that always says the sequence is taken.

    Being told `accepted` for a request that was never written is the one
    answer this path must never give, so exhaustion is a refusal.
    """
    backend = _shipped_backend(tmp_path)
    attempts = []

    def always_taken(seq, event, directory=None, generation=None, exclusive=False):
        attempts.append(seq)
        raise bluetooth_request.SequenceTaken("taken")

    monkeypatch.setattr(bluetooth_request, "write", always_taken)
    import audio_router
    with pytest.raises(audio_router.BrokerError) as caught:
        backend._submit({"kind": "cancel"})
    assert caught.value.code == "backend_failure"
    assert len(attempts) == routed_backend.RoutedDeviceBackend.MAX_SEQ_ATTEMPTS, (
        "the loop must be bounded by MAX_SEQ_ATTEMPTS and nothing else")
    assert not bluetooth_request.pending(tmp_path / "spool"), "a refusal must spool nothing"


def test_a_failed_write_leaves_nothing_behind_in_the_spool(tmp_path, monkeypatch):
    """terra, round 3. The cleanup has to cover the WRITE, not only the link.

    A full tmpfs or an fsync error used to leave a partial `.new` behind --
    one per failure, on a panel nobody logs in to, in a filesystem that is
    RAM. `pending` skips a non-numeric stem, so nothing would ever have said
    so.

    Failed at `fsync` rather than at `open`: that is the realistic error (the
    write lands in the page cache and the flush is what discovers the full
    disk), and it unwinds through the `with`, so the handle is closed before
    the cleanup runs. An injection that leaves the handle open would be
    testing the platform's unlink semantics instead.
    """
    spool = tmp_path / "spool"

    def full_disk(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bluetooth_request.os, "fsync", full_disk)
    with pytest.raises(OSError):
        bluetooth_request.write(3, {"kind": "cancel"}, spool, exclusive=True)
    assert list(spool.iterdir()) == [], "a partial temporary was left in the applier's spool"
