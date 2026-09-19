"""One Bluetooth device request, written where the root applier will find it.

WSN-024, and the same privilege boundary `switch_request` describes: the broker
runs as `panel` under ProtectSystem=strict with AF_UNIX as its only address
family (SR-023), so it can neither drive BlueZ nor talk to systemd. Pairing,
trusting, connecting and routing all need root -- BlueZ refuses an unprivileged
`Device1.Pair`, `Adapter1.StartDiscovery` or a write to `Trusted` -- so a
validated request is serialized here and `wall-bluetooth-device-apply.path` runs
`wall-bluetooth-device apply-request`.

WHY THE REQUEST CARRIES AN ALIAS AND NOT AN ADDRESS. The broker does not have
the address. The observer document it reads is deliberately address-free, and
the applier resolves the alias back to a MAC on its own side using the same
`bluetooth_state.assign_aliases` the document was built with. So the hardware
address never enters the broker's address space, which is a stronger statement
than "the broker does not send it".

A SECOND REQUEST FILE, NOT THE SWITCH'S. Sharing one file would mean one
applier, and the two have opposite risk profiles: a switch apply is a mixer
write that finishes in milliseconds, and a Bluetooth apply can sit on a radio
for the whole of a pairing timeout. Behind one path unit a slow pair would hold
the switch, which is the control the person at the glass is actually holding.

Implements: SR-023, SR-028, LLR-007 (WSN-024)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

# IN THE BROKER'S OWN RuntimeDirectory, beside the switch request and not in a
# directory of its own. That directory already exists, is already 0700
# panel:panel, and already carries `RuntimeDirectoryPreserve=restart` -- which
# the switch work needed because systemd removing the directory between the
# write and the applier's read would take the request with it while the broker's
# completed journal, in StateDirectory, outlived the effect it acknowledged. The
# same hazard applies here, and inheriting the fix is better than re-deriving it
# in a second unit stanza somebody would have to keep in step.
DEFAULT_PATH = Path("/run/wall-audio-router/bluetooth-request.json")

# The verbs the applier accepts. `status` and `telemetry` are absent by
# construction: they are observations, answered from the document, and a
# request file is only ever a MUTATION.
#
# `set_discoverable` is the Owner's "turn on discoverability" (2026-09-19) and
# is NOT the same verb as `discover`, which is the panel SCANNING. One makes the
# panel findable by a phone; the other makes the panel find a speaker. They were
# nearly one verb with a boolean, and keeping them apart is what stops "let me
# find my headset" from also opening the panel to anything in the room.
REQUEST_KINDS = ("discover", "cancel", "pair", "trust", "connect", "disconnect",
                 "forget", "select_input", "select_output", "set_discoverable")

# kind -> the exact key set its event carries, `kind` included.
EVENT_FIELDS = {
    "discover": {"kind", "timeoutSeconds"},
    "cancel": {"kind"},
    "pair": {"kind", "alias"},
    "trust": {"kind", "alias", "trusted"},
    "connect": {"kind", "alias"},
    "disconnect": {"kind", "alias"},
    "forget": {"kind", "alias"},
    "select_input": {"kind", "alias"},
    "select_output": {"kind", "alias"},
    "set_discoverable": {"kind", "enabled"},
}

# `pair` MAY carry the passkey the person read off the phone and compared on the
# glass. Optional rather than required, because a NoInputNoOutput device
# (most speakers) has no passkey to compare and demanding one would make them
# unpairable. See wall-bluetooth-agent's RequestConfirmation.
OPTIONAL_EVENT_FIELDS = {"pair": {"confirmation"}}

SEQ_TICKS_PER_SECOND = 1_000_000
MAX_DISCOVER_SECONDS = 120


class RequestError(ValueError):
    """A request that must not be written. Never swallowed."""


def next_seq(clock=time.time_ns):
    """A sequence that keeps increasing ACROSS an applier restart.

    Identical in mechanism and in reasoning to `switch_request.next_seq`: the
    applier persists a high-water mark, systemd.path fires on every close-write,
    and a replayed `forget` would revoke a device somebody had just re-paired.
    Microseconds, so the value stays inside JavaScript's safe integer range --
    the renderer compares it with `Number.isSafeInteger` and silently skips the
    comparison for anything larger.
    """
    return int(clock()) // (1_000_000_000 // SEQ_TICKS_PER_SECOND)


def envelope(seq, event, generation=None):
    """Build the request envelope, or refuse it.

    Contract:
      Inputs:  seq: int >= 0, strictly increasing across applier restarts
               event: {"kind": one of REQUEST_KINDS} plus that kind's fields
               generation: the applier epoch, or None to send it unscoped
      Outputs: dict ready for json.dumps
      Raises:  RequestError on an unknown kind, a bad seq, or a stray key
    """
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise RequestError("seq must be a non-negative integer")
    if not isinstance(event, dict) or event.get("kind") not in REQUEST_KINDS:
        raise RequestError("unknown event kind")
    kind = event["kind"]
    required = EVENT_FIELDS[kind]
    optional = OPTIONAL_EVENT_FIELDS.get(kind, set())
    present = set(event)
    if not required <= present or present - required - optional:
        raise RequestError("request fields are not exact")
    _check_event(event)
    built = {"version": 1, "seq": seq, "event": dict(event)}
    # Omitted rather than defaulted when unknown, for the reason
    # switch_request.envelope gives at length: guessing zero is a CLAIM about
    # which life of the applier this belongs to, and the applier would refuse
    # it, disabling device management on a panel whose only fault was an
    # unreadable document.
    if generation is not None:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise RequestError("generation must be a non-negative integer")
        built["generation"] = generation
    return built


def _check_event(event):
    """Type-check the payload the applier will act on, at the writing end.

    The broker has already run `routing.validate_action`, so this is a second
    gate on the same values. It is not redundant: this module is also what the
    applier's own tests build fixtures with, and a request file is the ONE thing
    that crosses from an unprivileged process into a root one. A boundary that
    only validates on the far side is a boundary that trusts the near one.
    """
    kind = event["kind"]
    alias = event.get("alias")
    if "alias" in EVENT_FIELDS[kind] and (not isinstance(alias, str) or not alias):
        raise RequestError("alias must be a non-empty string")
    if kind == "discover":
        seconds = event["timeoutSeconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= MAX_DISCOVER_SECONDS:
            raise RequestError("discovery timeout outside 1..%d seconds" % MAX_DISCOVER_SECONDS)
    if kind == "trust" and not isinstance(event["trusted"], bool):
        raise RequestError("trusted must be boolean")
    if kind == "set_discoverable" and not isinstance(event["enabled"], bool):
        raise RequestError("enabled must be boolean")
    confirmation = event.get("confirmation")
    if confirmation is not None and (not isinstance(confirmation, str) or
                                     not 1 <= len(confirmation) <= 16 or
                                     not confirmation.isdigit()):
        raise RequestError("pairing confirmation is invalid")


def write(seq, event, path=DEFAULT_PATH, generation=None):
    """Write one request atomically. Returns the path written."""
    payload = json.dumps(envelope(seq, event, generation), sort_keys=True) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path
