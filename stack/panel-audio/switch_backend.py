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
DEFAULT_APPLIER_PATH = Path("/usr/local/sbin/wall-audio-output")
# The three positions and the two that carry a level. Duplicated from
# wall_audio_state (LLR-013) rather than imported: that module lives in the root
# applier's tree, which the broker's ProtectSystem=strict sandbox cannot read.
# The two copies are held together by test_panel_audio_switch_backend.py, which
# imports both and asserts they agree.
OUTPUTS = ("mute", "headset", "speaker")
LEVELLED_OUTPUTS = ("headset", "speaker")
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

    def inventory(self, cancel: threading.Event) -> list:
        """No Bluetooth devices: this backend routes nothing (WSN-024)."""
        return []

    def call(self, method: str, params: Mapping[str, object], cancel: threading.Event) -> object:
        """Dispatch one validated switch verb. Implements: SR-028, LLR-015."""
        if method == "status":
            return self._status()
        if method == "telemetry":
            return {"available": False}
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
            current = self._state_strict().get("output")
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
        if self._state_strict().get("output") != "mute":
            # Accepted, and honestly seq-less: no request was minted, so there
            # is nothing for a client to correlate against.
            return {"accepted": True}
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
        try:
            switch_request.write(seq, event, path=self.request_path)
        except switch_request.RequestError as exc:
            # An event this module built itself was refused by the envelope:
            # a bug here, never a client's doing. Loud, and never as "accepted".
            raise _broker_error("backend_failure", "audio backend failed") from exc
        except OSError as exc:
            raise _broker_error("backend_failure", "audio backend failed") from exc
        return {"accepted": True, "seq": seq}

    def _next_seq(self) -> int:
        floor = max(self._applied_seq(), self._pending_seq()) + 1
        return max(switch_request.next_seq(self.clock), floor)

    def _applied_seq(self) -> int:
        seq = self._state_strict().get("request_seq")
        return seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else -1

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

    def _status(self) -> dict:
        """IF-015 status: no routing, plus the switch block read off the file.

        `available` is FALSE at the top level and that is not a bug: it reports
        BLUETOOTH DEVICE ROUTING, which this backend genuinely does not provide.
        The switch is reported in its own block, with `supported: true`, which is
        what lets the broker reconcile a lost `set_output`, `set_input_mute` or
        `set_volume` by observation rather than by an operator (LLR-015).
        """
        state = self._state()
        output = state.get("output")
        if output not in OUTPUTS:
            output = "speaker"
        input_muted = state.get("input_muted")
        headset_present = bool(state.get("headset_present"))
        volume = self._volume_of(state, output)
        unavailable = output == "mute" or (output == "headset" and not headset_present)
        reason = "headset_absent" if output == "headset" and not headset_present else None
        readable = bool(state) and self.state_path.exists()
        return {
            "protocolVersion": 1,
            "available": False,
            "reason": "routing-disabled",
            "devices": [],
            "route": None,
            "visualizer": {"available": False},
            # The legacy block, so a shell that predates the switch still reads
            # a truthful mute: `mute` IS the muted position of the switch.
            "mute": {"supported": readable, "muted": output == "mute"},
            "switch": {
                "supported": readable,
                "output": output,
                "inputMuted": bool(input_muted),
                "available": not unavailable,
                "reason": reason,
                "volume": volume,
            },
        }

    @staticmethod
    def _volume_of(state: Mapping[str, object], output: str) -> int:
        """The remembered level of `output`; 0 in `mute`, which has no level."""
        if output not in LEVELLED_OUTPUTS:
            return 0
        volume = state.get("volume")
        level = volume.get(output) if isinstance(volume, dict) else None
        if isinstance(level, bool) or not isinstance(level, int):
            return 0
        return max(0, min(100, level))


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
