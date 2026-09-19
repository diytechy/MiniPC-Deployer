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

# A SPOOL DIRECTORY, NOT ONE MAILBOX FILE, and that is the difference between
# this and the switch's request (terra, 2026-09-19, finding 1).
#
# The switch writes ONE file that the applier reads, and that is correct there
# because every switch verb is idempotent: a request overwritten before it was
# read costs at most a position somebody is about to ask for again. Device verbs
# are not like that. Two taps in quick succession -- `forget` then `trust` --
# both return accepted, the second atomic replace destroys the first, and the
# applier advances its high-water mark past BOTH sequences, so `request_landed`
# then reports the destroyed one as landed. A revocation the person asked for
# and was told had happened simply does not happen.
#
# So each request is its own file, named by its sequence, and the applier DRAINS
# the directory in sequence order. `DirectoryNotEmpty=` is the systemd spool
# idiom and is what the path unit watches.
#
# STILL INSIDE THE BROKER'S OWN RuntimeDirectory. That directory is already 0700
# panel:panel and already carries `RuntimeDirectoryPreserve=restart` -- which the
# switch work needed because systemd removing the directory between the write
# and the applier's read would take the request with it while the broker's
# completed journal, in StateDirectory, outlived the effect it acknowledged.
DEFAULT_DIR = Path("/run/wall-audio-router/bluetooth-requests")

# Zero-padded so a lexical directory listing is a numeric ordering. A microsecond
# epoch is sixteen digits today and twenty carries it past any clock this panel
# will run under; an unpadded name would sort "9" after "10" and apply a newer
# request before an older one.
SEQ_DIGITS = 20

# How many requests one drain will perform. A bound rather than a queue policy:
# the broker holds a mutation lock and cannot produce these faster than a person
# can tap, so reaching this means something is wrong, and doing unbounded work
# inside a oneshot unit is how a wedge becomes a wedge nobody can interrupt.
MAX_DRAIN = 64

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


def request_name(seq):
    """The spool filename for one sequence."""
    return "%0*d.json" % (SEQ_DIGITS, int(seq))


def pending(directory=DEFAULT_DIR):
    """Every spooled request, oldest sequence first.

    Names that are not a sequence are IGNORED rather than refused: the applier
    writes its own temporaries into this directory, and a partially written
    `.new` must not stop the drain. It is also not the applier's business to
    police a directory only the broker writes to.
    """
    try:
        names = sorted(item.name for item in Path(directory).iterdir())
    except OSError:
        return []
    found = []
    for name in names:
        stem = name[:-len(".json")] if name.endswith(".json") else None
        if stem and stem.isdigit():
            found.append(Path(directory) / name)
    return found


def write(seq, event, directory=DEFAULT_DIR, generation=None):
    """Spool one request atomically. Returns the path written.

    One rename into place, so the applier -- which runs the instant the
    directory stops being empty -- never reads half a request.
    """
    payload = json.dumps(envelope(seq, event, generation), sort_keys=True) + "\n"
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / request_name(seq)
    # The temporary is a sibling, so the rename is within one filesystem, and it
    # is named so `pending` skips it: a `.json.new` has a non-numeric stem.
    temporary = path.with_name(path.name + ".new")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path
