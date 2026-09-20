"""The broker backend that moves the panel's own switch, and nothing else.

ONE RESPONSIBILITY: turn a validated IF-015 switch verb into one file-drop the
root applier will pick up, and answer `status` from the state file that applier
owns. It performs no device I/O of its own -- it cannot: the broker runs as
`panel` under ProtectSystem=strict with AF_UNIX as its only address family, so
it can neither run `amixer` nor talk to systemd (SR-023). The privilege boundary
is `wall-audio-apply.path` and the request file in the broker's own runtime
directory (LLR-014, LLR-015).

WHAT THIS BACKEND DELIBERATELY DOES NOT DO. It routes nothing. `inventory` is
always empty and `status.route` is always None, so `pair`, `connect`,
`select_input` and `select_output` are refused by the broker's own inventory
check before they can reach any device. WSN-024 keeps the routed-device backend
disabled until the BlueZ/PipeWire feasibility gate is settled, and shipping the
switch must not quietly re-open it. The whole surface here is the three-position
output switch, the microphone button and the level of the selected output.

THE RENDERER STILL CANNOT NAME HARDWARE. Nothing that crosses IF-015 reaches the
request file except an enum position, a boolean and a bounded integer. The
broker validates, mints the sequence number and journals; the renderer never
writes a file and never learns a card, a unit or a mixer control.

Contract:
  Inputs:  validated (method, params) pairs from AudioBroker; see routing.
  Outputs: {"accepted": bool, "seq": int} for the switch verbs -- `seq` is the
           sequence the applier will record in `request_seq`, so a client can
           correlate EXACTLY rather than by value; a status dict for `status`.
  Config:  request_path: the file wall-audio-apply.path watches
           state_path:   /etc/wall-panel/audio-state.json, read only
           applier_path: the root applier, tested for existence only
  Raises:  BrokerError("backend_unavailable") when the applier or the state
           file is not installed; BrokerError("backend_failure") otherwise.
Implements: SR-023, SR-028, LLR-015
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Mapping

import switch_request

# Imported lazily inside the methods to avoid a circular import at module load:
# audio_router imports this module to expose the backend, and this module needs
# audio_router.BrokerError to speak the broker's error vocabulary.

DEFAULT_STATE_PATH = Path("/etc/wall-panel/audio-state.json")
# -- item L: the two producers this backend READS and never runs ------------
# Neither file is opened by anything privileged here. The broker runs as
# `panel` under ProtectSystem=strict with PrivateDevices=true and AF_UNIX as
# its only address family: it cannot open a sound device, and giving it one
# would move the capture privilege to the process the renderer talks to, which
# is the boundary SR-023 exists to hold. So both producers are services that
# ALREADY have the capture they need, and the channel between them and this
# backend is a bounded document in /run.
#
#   wall-bus-visualizer  opens `bus_monitor` -- the merged, PRE-switch bus --
#                        and publishes bounded derived levels at 5 Hz. It is
#                        the ONLY producer this backend reads (SR-041).
#
# WHY THIS MOVED OFF `speaker_tap` (Owner, 2026-09-17). The old producer was
# `wall-amp-trigger`, whose capture is the post-switch tap it needs for the
# relay. That made the visualizer a function of the Speaker leg: Headset showed
# nothing at all, and no source that did not reach the speaker could ever be
# drawn. The detector KEEPS that tap -- it must, its job is the relay, and
# nothing here changes it -- and visualization moved to the bus instead.
#
# AND THE PRICE OF BEING PRE-SWITCH IS PAID HERE. `bus_monitor` carries samples
# during Mute, because the mute is downstream of it. So bus activity alone is
# NOT "playing": `_telemetry` ANDs the confirmed output selection into `active`,
# and Mute reports an inactive bus without pretending the capture is
# unavailable. A frozen or flat visualizer during Mute is the outcome that was
# ruled out.
#
# THE MICROPHONE STAYS, THE SUBSTITUTION GOES (Owner ruling, 2026-09-17, as
# read back by the coordinator). The ruling is about what the VISUALIZER may
# draw: the microphone reaches neither the speaker nor the headset, so it is not
# a visualization source and can never stand in for the bus. It was possible to
# read that as "the block leaves the document", and this file briefly did. That
# is a step too far: the `microphone` block is not a visualizer feed at all --
# it is the level indicator for the microphone button in the panel's audio
# chrome, a different consumer with a different question, and nobody asked for
# it to go away.
#
# So exactly one thing is removed, and it is the thing that was genuinely wrong:
# `_telemetry` USED TO PUBLISH `available: true` WITH A FABRICATED SILENT BUS
# WHEN ONLY THE MICROPHONE HAD TELEMETRY. That promoted a microphone reading
# into the bus's place in the reply, which is precisely the confusion the ruling
# exists to prevent. An absent bus now reports absent, whatever the microphone
# is doing.
#
#   wall-bus-visualizer  opens `bus_monitor` and publishes the bus levels.
#   wall-audio-aec       already reads the panel microphone as its near end, so
#                        the post-filter level is a by-product of a capture that
#                        is running anyway.
BUS_TELEMETRY_PATH = Path("/run/wall-bus-visualizer/bus-telemetry.json")
AEC_STATUS_PATH = Path("/run/wall-panel/aec-status.json")
# How old a document may be and still be drawn as live. Both producers publish
# several times per second; 1500 ms tolerates ordinary scheduling jitter while
# expiring a producer that has missed multiple consecutive publications.
BUS_STALE_MS = 1500
# wall-audio-aec publishes the button meter at 10 Hz. The 1.5-second threshold
# tolerates a short scheduler stall but still stops claiming a frozen level is
# live after multiple consecutive missed publications.
AEC_STALE_MS = 1500
TELEMETRY_BANDS = 8
# The exact contract of the document above. `schema` 2 is not cosmetic: a
# version-1 document is the `speaker_tap` one, which answers a different
# question, and believing it would put a post-switch level back on the wall
# under a label claiming the merged bus.
BUS_SCHEMA = 2
BUS_SOURCE = "bus_monitor"
# The three answers the producer can give. `unavailable` is the panel not being
# in bus mode, or a capture that would not open -- there is no measurement at
# all. `silent` is a measurement of a quiet bus. The shell renders them
# differently, so they may not share a representation.
BUS_PRODUCER_STATES = ("live", "silent", "unavailable")
DEFAULT_APPLIER_PATH = Path("/usr/local/sbin/wall-audio-output")
# The three positions and the two that carry a level. Duplicated from
# wall_audio_state (LLR-013) rather than imported: that module lives in the root
# applier's tree, which the broker's ProtectSystem=strict sandbox cannot read.
# The two copies are held together by test_panel_audio_switch_backend.py, which
# imports both and asserts they agree.
OUTPUTS = ("mute", "headset", "speaker")
LEVELLED_OUTPUTS = ("headset", "speaker")
# The applier's own defaults (wall_audio_state.STATE_SCHEMA). They are here so
# that a file this backend reads means exactly what the applier will make it
# mean: a state with no `volume` key is a Speaker at 60%, not a Speaker at
# silence, and reporting 0 would have drawn a switch the panel is not in.
DEFAULT_OUTPUT = "speaker"
DEFAULT_VOLUME = {"headset": 60, "speaker": 60}
VOLUME_MIN, VOLUME_MAX = 0, 100
# What an unmute from the legacy `set_mute` verb selects when the switch is
# sitting in `mute`. Speaker rather than headset because speaker is audible
# unconditionally: `headset` with the adapter absent silences every output
# (Owner ruling 7), and an unmute that produced silence would be a worse answer
# than the wrong-but-audible one.
UNMUTE_OUTPUT = "speaker"


def _broker_error(code: str, message: str):
    from audio_router import BrokerError
    return BrokerError(code, message)


class SwitchApplierBackend:
    """Move the Mute/Headset/Speaker switch through the root applier.

    Implements: SR-023, SR-028, LLR-015
    """

    def __init__(self, *, request_path=None, state_path=None, applier_path=None,
                 clock=time.time_ns):
        self.request_path = Path(request_path or switch_request.DEFAULT_PATH)
        self.state_path = Path(state_path or DEFAULT_STATE_PATH)
        self.applier_path = Path(applier_path or DEFAULT_APPLIER_PATH)
        self.clock = clock

    # ---- the Backend protocol -------------------------------------------

    # ANSWERED FROM FILES, SO THEY NEED NO PROCESS ISOLATION (2026-09-17).
    # This backend performs no device I/O of its own and structurally cannot --
    # see the module docstring: it runs as `panel` under ProtectSystem=strict
    # with AF_UNIX as its only address family, so it can neither run `amixer`
    # nor talk to systemd. `status` reads the applier's state file and
    # `telemetry` reads the visualizer and canceller documents out of /run.
    # None of them can hang on hardware, and the renderer polls `telemetry` at
    # 10 Hz whenever the display is lit: isolating it cost 64% of one core in
    # interpreter startups alone.
    #
    # `request_landed` is deliberately NOT here. It also only reads the state
    # file today, but it exists to observe a MUTATION, and a method whose job is
    # to watch hardware settle is the wrong place to save a few milliseconds.
    LOCAL_ONLY_METHODS = frozenset({"status", "telemetry"})

    def inventory(self, cancel: threading.Event) -> list:
        """No Bluetooth devices: this backend routes nothing (WSN-024)."""
        return []

    def call(self, method: str, params: Mapping[str, object], cancel: threading.Event) -> object:
        """Dispatch one validated switch verb. Implements: SR-028, LLR-015."""
        if method == "status":
            return self._status()
        if method == "telemetry":
            return self._telemetry()
        if method == "request_landed":
            return {"accepted": self._landed(params["seq"])}
        handler = getattr(self, "_" + method, None)
        if handler is None:
            # Every routing verb lands here. It is a refusal, not a crash: the
            # panel has a working switch and no device routing at all, and the
            # shell renders that difference.
            raise _broker_error("backend_unavailable", "audio routing is not configured")
        self._require_applier()
        return handler(params)

    # ---- the switch verbs -----------------------------------------------

    def _set_output(self, params: Mapping[str, object]) -> dict:
        return self._submit({"kind": "set_output", "output": params["output"]})

    def _set_input_mute(self, params: Mapping[str, object]) -> dict:
        return self._submit({"kind": "set_input_mute", "muted": params["muted"]})

    def _set_volume(self, params: Mapping[str, object]) -> dict:
        """Set the level of the SELECTED output, optionally guarded by name.

        THE WRONG-OUTPUT RACE, AND WHAT ACTUALLY CLOSES IT. The applier stores a
        level into whichever output is selected when it runs (LLR-013,
        `_store_volume`), and the request wire carries no output of its own --
        adding one would be a change to the applier, which this work does not
        own. So a level chosen while the switch read `speaker` can land in the
        headset's memory if the switch moves in between.

        A caller that knows which output it meant sends `output`, and the
        request is refused here unless the state file still agrees. That is a
        compare-and-swap against the same file the applier will read, which
        narrows the window to the gap between this read and the applier's run
        rather than to the whole life of the request. It does not eliminate it,
        and the residue is stated rather than papered over: a switch that moves
        inside that gap still takes the level. Callers that genuinely mean
        "whatever is selected" -- the rocker -- send no `output` and get the old
        behaviour.
        """
        intended = params.get("output")
        if intended is not None:
            current = self._normalized()["output"]
            if current != intended:
                raise _broker_error(
                    "switch_moved",
                    "the selected output changed before the level could be applied")
        return self._submit({"kind": "set_volume", "level": params["level"]})

    def _set_mute(self, params: Mapping[str, object]) -> dict:
        """The predecessor verb, kept accepted for one release (routing.py).

        `set_mute(true)` is exactly `set_output("mute")`. `set_mute(false)` has
        no exact equivalent, because the switch it was generalized into has two
        unmuted positions and the old verb never named one. It is therefore
        answered by the narrowest truthful rule: if the switch is not in `mute`,
        nothing is asked for and the reply is accepted with no request written
        -- the output is already not muted, which is all the caller asked. Only
        an actual unmute from `mute` moves anything, and it moves to
        UNMUTE_OUTPUT.
        """
        if params["muted"] is True:
            return self._submit({"kind": "set_output", "output": "mute"})
        applied = self._normalized()
        if applied["output"] != "mute":
            # Accepted, and honestly seq-less: no request was minted, so there
            # is nothing for a client to correlate against. The effective state
            # is still echoed, because the caller asked "is the output unmuted"
            # and this is the evidence that it is.
            return {"accepted": True, "seq": None, "generation": None,
                    "observedBefore": self._observed_before(applied)}
        return self._submit({"kind": "set_output", "output": UNMUTE_OUTPUT})

    # ---- the shell ------------------------------------------------------

    def _submit(self, event: dict) -> dict:
        """Mint a sequence and write one request. Returns {"accepted", "seq"}.

        THE SEQUENCE IS MINTED AGAINST WHAT IS ALREADY ON DISK, not against an
        in-memory counter. Two reasons, and both have bitten this design once:

        * The backend runs in a SPAWNED CHILD PROCESS per call (the broker
          isolates it, audio_router._isolated_backend_call), so any counter held
          on this object dies with the call that incremented it. A floor read
          from the state file and the pending request file is the only state
          two calls actually share.
        * `next_seq` is the wall clock in microseconds. Two requests inside the
          same tick -- or any clock step backwards, by NTP or a dead RTC --
          would produce a value the applier discards as "not newer than the last
          applied", and the switch would stop responding with no error anywhere
          on the client's side. Taking max(clock, last applied + 1, pending + 1)
          makes the sequence strictly increasing whatever the clock does.

        The broker serializes mutations under one lock, so two clients cannot be
        inside this function at once on the same broker; the floor is what keeps
        them apart across the process boundary and across a broker restart.
        """
        seq = self._next_seq()
        # THE EPOCH THE REQUEST IS SCOPED TO, and the state it is scoped
        # AGAINST, both read from the one file the applier owns (contract
        # 2026-09-14, sections 1.2 and 1.4). `_require_applier` has already
        # proved the file is readable on every path that reaches here, so an
        # unreadable file at this point is a race with a rewrite; the request
        # then goes out UNSCOPED rather than carrying a guessed epoch.
        from audio_router import BrokerError
        try:
            applied = self._normalized()
        except BrokerError:
            applied = None
        generation = applied["generation"] if applied else None
        try:
            switch_request.write(seq, event, path=self.request_path,
                                 generation=generation)
        except switch_request.RequestError as exc:
            # An event this module built itself was refused by the envelope:
            # a bug here, never a client's doing. Loud, and never as "accepted".
            raise _broker_error("backend_failure", "audio backend failed") from exc
        except OSError as exc:
            raise _broker_error("backend_failure", "audio backend failed") from exc
        return {"accepted": True, "seq": seq, "generation": generation,
                "observedBefore": self._observed_before(applied)}

    @classmethod
    def _observed_before(cls, applied) -> dict | None:
        """The applied state as it stood BEFORE this request, or None.

        NAMED FOR WHEN IT WAS TAKEN, because two independent reviews read the
        previous name -- `effective` -- as a claim about the outcome, and a
        field two readers get wrong is named wrong however carefully the
        contract explains it. The applier runs asynchronously behind a path
        unit, so NO reply can say whether this request has been applied;
        `accepted` has never meant `applied` and this field does not change
        that. The STATUS verb is where the effective applied state lives.

        What it buys the client is exactly what item I needs: a renderer whose
        request timed out can compare a freshly read `requestSeq` against this
        one to tell "it landed" from "it is still lost", without a second round
        trip to establish the baseline.
        """
        if not applied:
            return None
        return {
            "output": applied["output"],
            "inputMuted": applied["input_muted"],
            "volume": cls._volume_of(applied, applied["output"]),
            "requestSeq": applied["request_seq"],
            "generation": applied["generation"],
        }

    def _next_seq(self) -> int:
        floor = max(self._applied_seq(), self._pending_seq()) + 1
        return max(switch_request.next_seq(self.clock), floor)

    def _applied_seq(self) -> int:
        return max(self._normalized()["request_seq"], -1)

    def _pending_seq(self) -> int:
        """The sequence of a request the applier has not consumed yet.

        The applier does not delete the request file -- it deduplicates on the
        recorded high-water mark -- so a request written and not yet applied is
        invisible to `_applied_seq` and must be counted here, or a second tap
        inside the same tick would mint the same number and be discarded.
        """
        try:
            raw = json.loads(self.request_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return -1
        seq = raw.get("seq") if isinstance(raw, dict) else None
        return seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else -1

    def request_landed(self, seq: int) -> bool:
        """Declared so the broker knows this backend can answer the question.

        The broker calls it through the ordinary seam as the internal method
        `request_landed`; this attribute exists so `hasattr` can find it on a
        backend that has a volatile effect to check. Implements: LLR-015.
        """
        return self._landed(seq)

    def _landed(self, seq: object) -> bool:
        """Is the request with this sequence still going to be applied?

        True if the applier has already recorded it, or if the request file is
        still sitting there waiting to be consumed. False means the file is gone
        and the applier never saw it -- the runtime directory did not survive a
        restart -- so the acknowledgement the broker journaled is no longer true.

        THE COMPARISON IS `>=`, AND THAT IS A DECISION, NOT AN OVERSIGHT (terra
        5.1, rejected). The request file is a ONE-SLOT MAILBOX holding the
        latest intent: a second tap overwrites an unconsumed first one, on
        purpose, because the newest position is the one the Owner is asking for
        and applying the older one afterwards would move the switch away from
        it. So a sequence at or below what is sitting in the mailbox, or at or
        below what the applier has recorded, counts as settled -- the request
        was either applied or deliberately superseded by a newer request for the
        same control, which is not a lost tap.

        The residue, stated rather than papered over: replaying the completed
        reply of a SUPERSEDED request reports success for an intent that was
        overtaken. Making it exact (`== seq`) would be worse, because the redo
        would then rewrite the older command over the newer one and move the
        switch backwards; making `_submit` refuse while a request is unconsumed
        would make the second tap of a quick double-tap fail. Closing it
        properly needs a per-sequence queue in the APPLIER's protocol, which
        this work does not own; it is recorded as the owed follow-up.
        """
        from audio_router import BrokerError
        if isinstance(seq, bool) or not isinstance(seq, int):
            return False
        try:
            if self._applied_seq() >= seq:
                return True
        except BrokerError:
            # The state file cannot be read, so nothing here can say the request
            # landed. Re-doing an idempotent request is the safe answer, and the
            # re-do refuses with the same unreadable-state error.
            return False
        return self._pending_seq() >= seq

    def _require_applier(self) -> None:
        """Refuse before writing anything nobody will read.

        A request file dropped on a panel with no applier is answered by no one:
        the client would sit pending until its own timeout and be told nothing
        about why. An absent applier or an absent state file is a panel where
        the switch is not installed, and saying so is the honest answer.
        """
        if not self.applier_path.exists() or not self.state_path.exists():
            raise _broker_error("backend_unavailable", "audio routing is not configured")
        # AND THE STATE MUST BE READABLE, not merely present (terra 1.1). Every
        # mutation decision here is taken against that file: the sequence floor
        # comes from its `request_seq`, the volume guard from its `output`, and
        # the legacy unmute from whether that output is `mute`. Treating an
        # unreadable file as an empty one would mint a sequence from the clock
        # alone -- which a backward step can put below the applier's recorded
        # mark, where the applier silently discards it while this backend
        # answers "accepted" -- and would answer a legacy unmute with a cheerful
        # no-op. A file the broker cannot read is a switch it cannot move.
        self._state_strict()

    def _state(self) -> dict:
        """The applier's state file, or {} if it cannot be read.

        Never raises: every caller here treats an unreadable state as "unknown",
        and the failure modes that matter (no applier at all) are caught by
        `_require_applier` with a message of their own.
        """
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _state_strict(self) -> dict:
        """The applier's state, or refuse. For every path that MUTATES.

        The lenient `_state` is for `status` alone, where "unknown" is a real
        answer the shell renders. A mutation has no such answer: see
        `_require_applier` for the two silent failures this prevents.
        """
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _broker_error("backend_unavailable",
                                "audio routing is not configured") from exc
        if not isinstance(raw, dict):
            raise _broker_error("backend_unavailable", "audio routing is not configured")
        return raw

    # ---- item L: telemetry ------------------------------------------------

    def _read_document(self, path: Path, limit: int = 16384):
        """One bounded JSON document from /run, or None. Never raises.

        Telemetry is a decoration. A missing, truncated, oversized or
        nonsensical document is "we cannot tell you", which the shell renders as
        an absent visualizer -- never an error, and never the last thing anybody
        saw.
        """
        try:
            if path.stat().st_size > limit:
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    @staticmethod
    def _scalar(value):
        """A published 0..1 float, or None. bool is an int in Python: refuse it."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if value != value or value < 0.0 or value > 1.0:
            return None
        return value

    def _telemetry(self) -> dict:
        """The merged bus's derived levels (item L, re-sourced by SR-041).

        WHY THIS ANSWERED `{"available": false}` BEFORE ITEM L, and why that was
        a structural answer rather than a stub: this backend replaced the routed
        one, which had a capture of its own, and the switch backend performs no
        device I/O at all. It still performs none. A service that already holds
        the capture publishes what it has measured, and this reads its file.

        MUTE IS PART OF THE ANSWER, and this is the one place it can be. The
        producer reads `bus_monitor`, which is upstream of the switch: it cannot
        know whether the room is hearing what it is measuring. This backend
        already reads the confirmed output selection for `status`, so the AND
        happens here and `active` means what a viewer would mean by it --
        something is playing, to something audible.

        THE MICROPHONE MAY NOT STAND IN FOR THE BUS, and that is the whole of
        what the 2026-09-17 ruling changes here. This used to answer
        `available: true` with a fabricated silent bus whenever the microphone
        had telemetry and the bus had none -- promoting a microphone reading
        into the bus's place in the reply. An absent bus is now absent, whatever
        the microphone is doing. The block itself stays: it feeds the microphone
        button's level ring in the audio chrome, which is a different consumer
        asking a different question, and it is never a source of `bands`.

        Contract:
          Outputs: {"available": False} when the BUS publishes nothing usable --
                   regardless of the microphone -- or the IF-015 telemetry result
                   with `rms`, `peak`, `bands`, the `bus` block and an optional
                   `microphone` block. `generation` is stamped by the broker,
                   not here: the producer's own capture generation lives inside
                   its document and is never surfaced through this field.
        Implements: SR-028, SR-041, LLR-015, LLR-952
        """
        now_ms = int(time.monotonic() * 1000)
        bus = self._bus_block(now_ms)
        if bus is None:
            # Honest, and distinguishable from silence: the shell draws no
            # visualizer rather than a flat one. NOT rescued by a microphone
            # that happens to be publishing -- that was the substitution.
            return {"available": False}
        result = {
            "available": True,
            "active": bus["active"] and self._output_is_audible(),
            "rms": bus["rms"],
            "peak": bus["peak"],
            "bands": bus["bands"],
            # THE PRODUCER'S OWN OBSERVATION INSTANT, not the moment this was
            # read (terra, 2026-09-14). Both are CLOCK_MONOTONIC on the same
            # machine, so it needs no conversion -- and stamping `now` here made
            # a 1.4-second-old capture look newly observed, which is precisely
            # the freshness claim this field exists to carry.
            "observedMonotonicMs": bus["observed"],
            "bus": {"state": bus["state"], "ageMs": bus["ageMs"],
                    "valid": bus["state"] == "live", "source": BUS_SOURCE},
        }
        microphone = self._microphone_block(now_ms)
        if microphone is not None:
            result["microphone"] = microphone
        return result

    def _output_is_audible(self) -> bool:
        """Whether the CONFIRMED switch position sends the bus anywhere.

        Mute is the whole question, and Headset is deliberately audible: the
        Owner's ruling is that Headset visualizes, which is a visible change on
        this wall because the old `speaker_tap` source showed nothing there at
        all.

        An UNREADABLE state file answers False. The alternative -- assuming the
        switch is where it usually is -- would draw a moving visualizer over a
        muted room on precisely the panel that can no longer tell, and "we
        cannot confirm it is audible" is not a claim that it is.
        """
        state = self._state()
        if not state:
            return False
        output = state.get("output")
        if output not in OUTPUTS:
            # EXACTLY the applier's own repair, so this answers about the
            # position the applier would act on rather than about the bytes in
            # the file. An empty or unreadable file is a different case and was
            # refused above: repairing THAT to Speaker would be inventing a
            # position on the one panel that cannot report its own.
            output = DEFAULT_OUTPUT
        return output in LEVELLED_OUTPUTS

    def _silent_bus(self, state: str, observed: int, age_ms: int) -> dict:
        """A bus carrying nothing, with the REASON it carries nothing.

        Silence and absence are different answers and the shell renders them
        differently, so they may not share a representation (terra, 2026-09-14):
        a bus nobody is publishing must not be indistinguishable from a quiet
        room. `state` is what tells them apart.
        """
        return {"active": False, "rms": 0.0, "peak": 0.0,
                "bands": [0.0] * TELEMETRY_BANDS, "state": state,
                "observed": observed, "ageMs": age_ms}

    def _bus_block(self, now_ms: int):
        """`bus_monitor` levels from wall-bus-visualizer, or None.

        THE PRODUCER'S OWN `state` IS BELIEVED, NOT RE-DERIVED. It is the only
        thing that can tell bus mode from a failed capture from a quiet room,
        and it says which, so the three answers survive the trip. `unavailable`
        reaching the shell as `{"available": false}` is how a panel that is not
        in bus mode falls through to Frame Media instead of showing a flat
        visualizer (Owner, 2026-09-17).
        """
        raw = self._read_document(BUS_TELEMETRY_PATH)
        if not raw or raw.get("schema") != BUS_SCHEMA or raw.get("source") != BUS_SOURCE:
            return None
        state = raw.get("state")
        if state not in BUS_PRODUCER_STATES:
            return None
        if state == "unavailable":
            # NOT SILENCE. This panel is not in bus mode, or the capture would
            # not open: there is no measurement at all, and the shell must be
            # able to tell that from a quiet room.
            return None
        if state == "silent" or raw.get("valid") is not True:
            # The producer is running and is measuring a quiet bus. A quiet room
            # is a real answer and the visualizer should show one, not disappear.
            return self._silent_bus("silent", now_ms, 0)
        rms = self._scalar(raw.get("rms"))
        peak = self._scalar(raw.get("peak"))
        bands = raw.get("bands")
        if rms is None or peak is None or not isinstance(bands, list) or len(bands) != TELEMETRY_BANDS:
            return None
        clean = [self._scalar(band) for band in bands]
        if any(band is None for band in clean):
            return None
        observed = raw.get("observed_monotonic_ms")
        if not isinstance(observed, int) or isinstance(observed, bool) or observed < 0:
            return None
        age = now_ms - observed
        if age > BUS_STALE_MS or age < -BUS_STALE_MS:
            # STALE IS SILENCE, NOT THE LAST FRAME. A producer that stopped
            # would otherwise leave a still picture of a bus level on the wall
            # for as long as nobody restarted it. The age is published beside
            # it, so "stale" is a statement with evidence rather than a verdict.
            return self._silent_bus("stale", now_ms, max(0, age))
        return {"active": raw.get("active") is True, "rms": rms, "peak": peak,
                "bands": clean, "state": "live", "observed": observed,
                "ageMs": max(0, age)}

    def _microphone_block(self, now_ms: int):
        """The canceller's post-filter level, or None when there is no canceller.

        WHOSE NUMBER THIS IS, said plainly because it was briefly deleted for
        being the wrong one: it belongs to the microphone button's filled disc in
        the panel's audio chrome. It is NOT a visualizer feed. It never
        contributes a band, it never sets `active`, and since 2026-09-17 it can
        no longer stand in for an absent bus -- `_telemetry` returns
        `available: false` when the bus publishes nothing, whatever this says.

        EVERY STATE IS EXPLICIT, because the one thing this block must never do
        is let the disc be drawn live over a microphone that is muted, stale or
        not being cancelled at all. `source` is what stops raw capture ever
        being presented as post-filter.

        Implements: SR-028, LLR-015
        """
        raw = self._read_document(AEC_STATUS_PATH)
        if not raw or raw.get("schema") != 1:
            return None
        block = raw.get("microphone")
        if not isinstance(block, dict):
            return None
        level = self._scalar(block.get("level"))
        if level is None:
            return None
        source = block.get("source")
        if source not in ("aec_post_filter", "raw_capture", "none"):
            return None
        state = block.get("state")
        if state not in ("live", "muted", "stale", "unavailable"):
            return None
        observed = block.get("observed_monotonic_ms")
        if not isinstance(observed, int) or isinstance(observed, bool) or observed < 0:
            return None
        age = now_ms - observed
        if age < 0:
            # The canceller restarted, so its monotonic clock and ours no longer
            # share an origin. The age is not a number that can be defended, and
            # a level whose age cannot be defended is stale by definition.
            age = 0
            state = "stale"
        if age > AEC_STALE_MS and state == "live":
            state = "stale"
        reference = block.get("reference_dbfs")
        if isinstance(reference, bool) or not isinstance(reference, (int, float)):
            return None
        return {
            "level": level if state == "live" else 0.0,
            "source": source,
            "state": state,
            "ageMs": age,
            # Preserve the microphone producer's observation instant across
            # the broker/IPC seam.  `ageMs` is correct when this reply is
            # assembled; the renderer also needs the source instant to order
            # replies and to notice a delayed answer whose age grew in flight.
            "observedMonotonicMs": observed,
            "valid": state == "live" and block.get("valid") is True,
            "referenceDbfs": float(reference),
        }

    def _status(self) -> dict:
        """IF-015 status: no routing, plus the switch block read off the file.

        `available` is FALSE at the top level and that is not a bug: it reports
        BLUETOOTH DEVICE ROUTING, which this backend genuinely does not provide.
        The switch is reported in its own block, with `supported: true`, which is
        what lets the broker reconcile a lost `set_output`, `set_input_mute` or
        `set_volume` by observation rather than by an operator (LLR-015).

        `supported` is FALSE only when the file cannot be READ (terra 3.1), not
        when it is merely sparse. A readable file is normalized exactly as the
        applier normalizes it, so a state with fields missing reports the
        position the applier would act on -- reporting it unsupported would have
        drawn an unknown switch and, worse, refused reconciliation on a panel
        whose switch works and whose mutations this backend accepts.
        """
        from audio_router import BrokerError
        try:
            state = self._normalized()
        except BrokerError:
            # ONLY the unreadable-file refusal is caught, and it is caught here
            # rather than allowed out because `status` must always answer: the
            # shell renders "unknown" and an error would leave the chrome with
            # nothing at all to say.
            state = None
        if state is None:
            return self._status_envelope({
                "supported": False, "output": DEFAULT_OUTPUT, "inputMuted": False,
                "available": False, "reason": "state_unreadable", "volume": 0,
                # Null, not zero: an unreadable file is an epoch and a mark this
                # broker does not know, and zero is a number a client would
                # compare against (contract 2026-09-14, section 1.3).
                "generation": None, "requestSeq": None,
                # Unreadable: the coupling cannot be evaluated and the mute
                # certainly cannot be confirmed.
                "inputMuteHeld": False, "inputMutedConfirmed": False,
                # THE SHAPE IS THE SAME IN BOTH ARMS. A renderer that has to
                # ask whether a key exists before reading it will one day be
                # written by somebody who forgets, and "unknown" is a value
                # this block can carry: null means the same thing here as it
                # does in Speaker.
                "headsetVia": None, "btHeadsetPresent": False,
            }, muted=False, mute_supported=False)
        output = state["output"]
        # ── B4: Headset RESOLVES, so "is it available" is a priority ────────
        # Bluetooth wins whenever it is connected, then the USB adapter, then
        # nothing -- the Owner's ruling Q5, 2026-09-19. The same three answers
        # `wall_audio_state.headset_via` gives, mirrored here for the same
        # reason the whole of `_normalized` is: this sandbox cannot import the
        # applier's tree, and a test asserts the two agree.
        #
        # THE REASON TOKEN DOES NOT CHANGE. `headset_absent` now means "neither
        # headset resolves", and the chrome's red icon needs no second word.
        via = ("bluetooth" if state["bt_headset_present"]
               else "usb" if state["headset_present"] else None)
        unavailable = output == "mute" or (output == "headset" and via is None)
        reason = "headset_absent" if output == "headset" and via is None else None
        # ── ITEM J, reported so the chrome never has to derive the rule ────
        # `inputMuted` is the EFFECTIVE mute -- what the microphone actually is.
        # `inputMuteHeld` says an independent unmute is refused right now, which
        # is what lets the UI explain the refusal instead of just showing one.
        # `inputMutedConfirmed` is the applier's OBSERVATION: false while a mic
        # leg is (or may still be) transmitting, so a coupled mute is never
        # reported as successful while the microphone is still open.
        held = output == "mute"
        muted_effective = state["input_muted"] or held
        return self._status_envelope({
            "supported": True, "output": output, "inputMuted": muted_effective,
            "available": not unavailable, "reason": reason,
            "volume": self._volume_of(state, output),
            "generation": state["generation"], "requestSeq": state["request_seq"],
            "inputMuteHeld": held,
            "inputMutedConfirmed": muted_effective and not state["mic_legs_running"],
            # WHICH HEADSET "Headset" MEANS, so the chrome can put a Bluetooth
            # mark on the segment and the person knows before a call starts.
            # Null in any position but Headset: it is the resolution OF a
            # position, not a standing fact about the hardware.
            "headsetVia": via if output == "headset" else None,
            # The same question without the switch, which is what explains the
            # red icon ("neither") and what a future pane would offer.
            "btHeadsetPresent": state["bt_headset_present"],
        }, muted=output == "mute", mute_supported=True)

    @staticmethod
    def _status_envelope(switch: dict, *, muted: bool, mute_supported: bool) -> dict:
        return {
            "protocolVersion": 1,
            "available": False,
            "reason": "routing-disabled",
            "devices": [],
            "route": None,
            "visualizer": {"available": False},
            # The legacy block, so a shell that predates the switch still reads
            # a truthful mute: `mute` IS the muted position of the switch.
            "mute": {"supported": mute_supported, "muted": muted},
            "switch": switch,
        }

    def _normalized(self) -> dict:
        """The state as the APPLIER will read it, or refuse if unreadable.

        Mirrors `wall_audio_state.normalize` field by field, including its
        defaults, because that module lives in the root applier's tree which
        this sandbox cannot import. `test_the_backend_normalizes_like_the_applier`
        imports both and asserts they agree on a table of damaged inputs, so the
        two copies cannot drift in silence.
        """
        raw = self._state_strict()
        state = {"output": DEFAULT_OUTPUT, "input_muted": True,
                 "headset_present": False, "bt_headset_present": False,
                 "request_seq": -1, "volume_event_seq": 0,
                 "generation": 0, "mic_legs_running": True,
                 "volume": dict(DEFAULT_VOLUME)}
        # Same rule as wall_audio_state.normalize (step 4, terra rounds 2-4):
        # the microphone is MUTED unless the document explicitly carries the
        # boolean false, and a document that needed ANY repair comes back
        # muted too. Field-by-field repair is kept for everything else.
        repaired = False
        if raw.get("output") in OUTPUTS:
            state["output"] = raw["output"]
        elif "output" in raw:
            repaired = True
        if isinstance(raw.get("input_muted"), bool):
            state["input_muted"] = raw["input_muted"]
        else:
            repaired = True
        if isinstance(raw.get("headset_present"), bool):
            state["headset_present"] = raw["headset_present"]
        elif "headset_present" in raw:
            repaired = True
        if isinstance(raw.get("headset_autoswitch_armed"), bool):
            pass
        elif "headset_autoswitch_armed" in raw:
            repaired = True
        # B4: the second presence. An ABSENT key is not a repair -- a state
        # file written before B4 has neither -- for exactly the reason
        # `wall_audio_state.normalize` gives: a panel whose first status after
        # the upgrade declared the file damaged would report a muted microphone
        # nobody muted. A key that is present and the wrong type still is.
        if isinstance(raw.get("bt_headset_present"), bool):
            state["bt_headset_present"] = raw["bt_headset_present"]
        elif "bt_headset_present" in raw:
            repaired = True
        if isinstance(raw.get("bt_headset_autoswitch_armed"), bool):
            pass
        elif "bt_headset_autoswitch_armed" in raw:
            repaired = True
        seq = raw.get("request_seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and seq >= -1:
            state["request_seq"] = seq
        elif "request_seq" in raw:
            repaired = True
        # The epoch (contract 2026-09-14, section 1.1). Absent is an OLDER
        # applier's file, not damage -- the same rule wall_audio_state.normalize
        # applies -- so it does not trip the repair-mutes-the-microphone rule.
        if isinstance(raw.get("mic_legs_running"), bool):
            state["mic_legs_running"] = raw["mic_legs_running"]
        elif "mic_legs_running" in raw:
            repaired = True
        generation = raw.get("generation")
        if isinstance(generation, int) and not isinstance(generation, bool) and 0 <= generation <= 9007199254740991:
            state["generation"] = generation
        elif "generation" in raw:
            repaired = True
        volume_event = raw.get("volume_event_seq", 0)
        if isinstance(volume_event, int) and not isinstance(volume_event, bool) and 0 <= volume_event <= 9007199254740991:
            state["volume_event_seq"] = volume_event
        elif "volume_event_seq" in raw:
            repaired = True
        volume = raw.get("volume")
        if isinstance(volume, dict):
            for output in LEVELLED_OUTPUTS:
                level = volume.get(output)
                # bool is an int in Python and True would become 1%: refuse it.
                if isinstance(level, int) and not isinstance(level, bool):
                    state["volume"][output] = max(VOLUME_MIN, min(VOLUME_MAX, level))
                    if state["volume"][output] != level:
                        repaired = True
                elif output in volume:
                    repaired = True
        elif "volume" in raw:
            repaired = True
        if repaired:
            state["input_muted"] = True
        # Item J, mirrored from wall_audio_state.normalize: output Mute couples
        # the microphone, at the recovery boundary as well as at the request
        # boundary. The paired test asserts the two copies agree.
        if state["output"] == "mute":
            state["input_muted"] = True
        return state

    @staticmethod
    def _volume_of(state: Mapping[str, object], output: str) -> int:
        """The remembered level of `output`; 0 in `mute`, which has no level."""
        if output not in LEVELLED_OUTPUTS:
            return 0
        return state["volume"][output]


def backend_from_environment():
    """Build the shipped backend from WALL_AUDIO_* paths, for `serve()`.

    Config:  WALL_AUDIO_REQUEST_PATH, WALL_AUDIO_STATE_PATH,
             WALL_AUDIO_APPLIER_PATH -- all optional; the defaults are the
             installed locations. Provided so the unit file and the tests can
             point the backend at a tree without rewriting the entry point.
    """
    return SwitchApplierBackend(
        request_path=os.environ.get("WALL_AUDIO_REQUEST_PATH") or None,
        state_path=os.environ.get("WALL_AUDIO_STATE_PATH") or None,
        applier_path=os.environ.get("WALL_AUDIO_APPLIER_PATH") or None,
    )
