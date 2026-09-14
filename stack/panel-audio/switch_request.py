"""One audio-switch request, written where the root applier will find it.

WHY THIS IS A FILE AND NOT A CALL. The broker runs as `panel` under
ProtectSystem=strict with AF_UNIX as its only address family (SR-023): it can
neither write /etc nor talk to systemd, and nothing in this design asks for
that to change. So a validated request is serialized here, into the broker's own
runtime directory, and wall-audio-apply.path runs the root applier
(`wall-audio-output`) to answer it. The renderer still cannot name a unit, a
card or a mixer control -- the request carries a sequence number and one of four
event kinds, and both ends validate it.

Pure-ish by design: `envelope` is a pure function the tests exercise
exhaustively; `write` is the four-line shell around it.

Implements: SR-028, LLR-015
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

DEFAULT_PATH = Path("/run/wall-audio-router/request.json")


# The wall clock in MICROSECONDS. Nanoseconds were the first answer and they
# were wrong in a way nothing on this side could see: a nanosecond epoch is
# about 1.8e18, well past JavaScript's 2^53-1 safe integer, so the renderer --
# which compares this number against the applier's high-water mark with
# Number.isSafeInteger -- would have silently skipped the comparison it depends
# on for confirming its own request. Microseconds stay safe past the year 2200
# and are still finer than any rate a finger or a rocker can produce.
SEQ_TICKS_PER_SECOND = 1_000_000


def next_seq(clock=time.time_ns):
    """A sequence number that keeps increasing ACROSS A BROKER RESTART.

    A counter starting from zero in each broker lifetime is wrong here and the
    failure is silent: the applier persists the high-water mark in the state
    file, so after a restart every request below the previous lifetime's last
    value is discarded and the switch simply stops responding. The wall clock in
    nanoseconds is monotonic enough for a deduplication key and needs no state
    of its own.

    The residual case -- a clock stepped backwards, by NTP or by a dead RTC --
    is bounded and loud rather than silent: the applier journals each refusal,
    and the next request after the step forward is accepted.
    """
    # Truncating division, so a clock that only moves forward can only produce
    # a value that only moves forward. The caller is still responsible for
    # keeping it above the applier's recorded mark (switch_backend._next_seq):
    # two requests inside one microsecond, or a clock step backwards, would
    # otherwise be discarded by the applier's deduplication in silence.
    return int(clock()) // (1_000_000_000 // SEQ_TICKS_PER_SECOND)

# The events the applier accepts from a request. `headset` is deliberately NOT
# among them: adapter presence is the kernel's fact, reported by udev, and a
# renderer that could assert it could talk the panel into believing a headset is
# plugged in and then silence every output (Owner ruling 7).
REQUEST_KINDS = ("set_output", "set_input_mute", "set_volume", "nudge_volume")


class RequestError(ValueError):
    """A request that must not be written. Never swallowed."""


def envelope(seq, event):
    """Build the request envelope, or refuse it.

    Contract:
      Inputs:  seq: int >= 0, strictly increasing ACROSS broker restarts --
                    use next_seq(). The applier deduplicates on it, because
                    systemd.path fires on every close-write and a replayed
                    rocker press would walk the room's volume down on its own.
               event: {"kind": one of REQUEST_KINDS, plus that kind's key}
      Outputs: dict ready for json.dumps
      Raises:  RequestError on an unknown kind, a bad seq, or a stray key
    Implements: SR-028, LLR-015
    """
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise RequestError("seq must be a non-negative integer")
    if not isinstance(event, dict) or event.get("kind") not in REQUEST_KINDS:
        raise RequestError("unknown event kind")
    expected = {"set_output": {"kind", "output"}, "set_input_mute": {"kind", "muted"},
                "set_volume": {"kind", "level"}, "nudge_volume": {"kind", "louder"}}
    if set(event) != expected[event["kind"]]:
        raise RequestError("request fields are not exact")
    return {"version": 1, "seq": seq, "event": dict(event)}


def write(seq, event, path=DEFAULT_PATH):
    """Write one request atomically. Returns the path written.

    One rename, so the applier never reads half a request -- it runs the instant
    the file is closed, and a torn read would be a switch position nobody chose.
    """
    payload = json.dumps(envelope(seq, event), sort_keys=True) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path
