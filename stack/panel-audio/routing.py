"""Pure policy for the panel audio broker; performs no device or process I/O.

The policy intentionally models only aliases provisioned in host-private state.
Hardware addresses never cross IF-015, and no implicit capture source is valid.
Implements: SR-023, LLR-007.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping


ALIAS = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
MUTATING_METHODS = frozenset(
    {"discover", "cancel", "pair", "connect", "disconnect", "forget",
     "select_input", "select_output", "set_visualizer", "set_mute",
     "set_output", "set_input_mute", "set_volume",
     # WSN-024, added with the routed-device backend (Owner, 2026-09-19:
     # "need bluetooth to have a method to trust a device, might need a method
     # to ... turn on discoverability").
     #
     # `trust` IS A SEPARATE DECISION FROM `pair`, and it has to be, because
     # BlueZ separates them: a bond makes a device able to connect, and
     # `Trusted` makes the panel accept a connection the device starts on its
     # own. A phone paired without trust works perfectly until you walk out of
     # the room, and then never reconnects -- which reads as "Bluetooth is
     # broken" and has no control anywhere to fix it. Pairing sets it, and this
     # verb is how it is set or revoked afterwards.
     #
     # `set_discoverable` IS NOT `discover`. See bluetooth_request.REQUEST_KINDS.
     "trust", "set_discoverable"}
)

# THE TWO NAMES THAT LOOK ALIKE, AND ARE NOT (item 23 step 2). `select_output`
# routes to a trusted BLUETOOTH DEVICE by alias and predates this work.
# `set_output` moves the panel's own Mute / Headset / Speaker switch and takes
# no alias at all -- the renderer still cannot name a card, a unit or a mixer.
# They were nearly merged during the redesign; keeping them apart is what stops
# a device alias from ever reaching the host switch, and vice versa.
#
# `set_mute` is the generalization's predecessor: it was the OUTPUT mute, and
# `set_output` with output="mute" is exactly that request with a wider range. It
# stays accepted for one release so a panel running an older shell keeps its
# mute button, and is removed with the chrome work (step 5).
SWITCH_OUTPUTS = ("mute", "headset", "speaker")
# The positions that carry a level. `mute` does not: there is nothing to set a
# level on, and a remembered "mute volume" would be a second mute with no
# control of its own (Owner ruling F, and wall_audio_state.LEVELLED_OUTPUTS).
LEVELLED_OUTPUTS = ("headset", "speaker")
# A PERCENTAGE, not the adapter's raw steps. The headset card has 38 of them and
# the bus softvol 197, so a raw number would mean two different loudnesses on
# the two outputs; the applier is the only thing that knows either scale.
VOLUME_MIN, VOLUME_MAX = 0, 100
METHODS = MUTATING_METHODS | {"status", "telemetry"}

# SELF-RECONCILING MUTATIONS: journaled, but never sticky.
#
# Every other mutation leaves the world genuinely unknown if it dies mid-flight
# -- a half-finished pairing is why `pending` is sticky and why an operator has
# to reconcile it by hand. Mute is not like that. It is idempotent, it is one
# boolean, and the truthful answer can be read straight back off the mixer
# control. A broker that refused every later routing change because one mute tap
# timed out would be trading a real capability for no information at all.
#
# So a pending entry for one of these is resolved by OBSERVING the device rather
# than by trusting the journal or an operator. See AudioRouter._reconcile.
SELF_RECONCILING_METHODS = frozenset(
    {"set_mute", "set_output", "set_input_mute", "set_volume"})
HARDWARE_ADDRESS = re.compile(
    r"(?i)(?<![0-9a-f])(?:(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|"
    r"(?:[0-9a-f]{2}_){5}[0-9a-f]{2}|(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}|"
    r"[0-9a-f]{12})(?![0-9a-f])"
)


# Every verb that names a device. Derived once and used by both the parameter
# check and the inventory check, because they used to carry two hand-written
# copies of this list and a verb added to one was silently unchecked by the
# other.
ALIAS_METHODS = frozenset({"pair", "trust", "connect", "disconnect", "forget",
                           "select_input", "select_output"})

# The verbs that manage DEVICES, as opposed to the panel's own switch. This is
# the set `device_authorization` opens, and it is the whole of WSN-024's
# surface.
DEVICE_METHODS = ALIAS_METHODS | {"discover", "cancel", "set_discoverable"}


# B2. The capability vocabulary, duplicated from bluetooth_state deliberately:
# this module is the POLICY and must not import the observer's module to know
# what a legal value is -- the applier does not import this one either, for the
# same reason the alias length lives in both. The paired test asserts they
# agree, which is the cheap half of the trade.
CAPABILITIES = ("sink", "source", "hf", "ag")
# WHICH CAPABILITY EACH ROUTE NEEDS, and this is the whole of B2's widening.
#
# `select_output` wants somewhere to PLAY, so the device must be able to
# receive: a sink. `select_input` wants somewhere to LISTEN to, so the device
# must be able to send -- and there are three ways to be able to send. A phone
# playing music is a `source`; a phone on a call is an `ag` and its far end is
# what arrives; a HEADSET is an `hf` and its microphone is what arrives.
#
# THE THIRD ONE IS THE POINT. A headset advertises sink and hf, so it is
# headlined `output` and, before this, `select_input` refused it because its
# kind was not `input`. That made "use my Bluetooth headset's microphone"
# unexpressible, which is the residual `classify` used to document and B2
# exists to close.
ROUTE_CAPABILITIES = {
    "select_output": ("sink",),
    "select_input": ("source", "ag", "hf"),
}


class PolicyError(ValueError):
    """A request violates the fixed route/device policy."""


@dataclass(frozen=True)
class Device:
    """Sanitized device facts used by policy; alias is not a hardware address."""

    alias: str
    kind: str
    trusted: bool = False
    connected: bool = False
    # B2. WHAT THE DEVICE CAN DO, as opposed to which column its card is in.
    # A TUPLE because this dataclass is frozen and hashed elsewhere; a list
    # field would make Device unhashable and the failure would surface
    # somewhere unrelated. Defaults to empty so every existing construction --
    # including the tests that build a Device by hand -- keeps working and
    # simply has no capability to offer.
    capabilities: tuple = ()

    def __post_init__(self):
        if not ALIAS.fullmatch(self.alias) or HARDWARE_ADDRESS.search(self.alias):
            raise PolicyError("invalid device alias")
        if self.kind not in {"input", "output"}:
            raise PolicyError("invalid device kind")
        if not isinstance(self.capabilities, tuple) or \
                not all(capability in CAPABILITIES for capability in self.capabilities):
            raise PolicyError("invalid device capabilities")


def validate_action(method: str, params: Mapping[str, object]) -> None:
    """Validate one IF-015 method before a backend can observe it.

    Inputs: method from METHODS; params JSON object.
    Raises: PolicyError for unknown keys, aliases or implicit capture selection.
    Implements: SR-023, LLR-007.
    """
    if method not in METHODS:
        raise PolicyError("unknown method")
    allowed = {
        "status": set(), "telemetry": set(), "discover": {"timeoutSeconds"}, "cancel": set(),
        "pair": {"alias", "confirmation"}, "connect": {"alias"},
        "disconnect": {"alias"}, "forget": {"alias"},
        "select_input": {"alias", "explicit"}, "select_output": {"alias"},
        "trust": {"alias", "trusted"},
        "set_discoverable": {"enabled"},
        "set_visualizer": {"enabled"},
        "set_mute": {"muted"},
        "set_output": {"output"},
        "set_input_mute": {"muted"},
        "set_volume": {"level", "output"},
    }[method]
    if set(params) - allowed:
        raise PolicyError("unknown parameter")
    alias = params.get("alias")
    if method in ALIAS_METHODS and alias is None:
        raise PolicyError("device alias is required")
    if alias is not None and (not isinstance(alias, str) or not ALIAS.fullmatch(alias) or
                              HARDWARE_ADDRESS.search(alias)):
        raise PolicyError("invalid device alias")
    if method == "select_input" and params.get("explicit") is not True:
        raise PolicyError("input selection must be explicit")
    if method == "discover":
        timeout = params.get("timeoutSeconds", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
            raise PolicyError("discovery timeout outside 1..120 seconds")
    if method in ("set_visualizer", "set_discoverable") and not isinstance(params.get("enabled"), bool):
        raise PolicyError("enabled must be boolean")
    # Required rather than defaulted, for the same reason `muted` is: a trust
    # request that forgot to say which way is a caller bug, and guessing it
    # would silently grant or revoke a device's standing invitation.
    if method == "trust" and not isinstance(params.get("trusted"), bool):
        raise PolicyError("trusted must be boolean")
    # `muted` is required, not defaulted: a mute request that forgot to say which
    # way is a caller bug, and guessing it would silently toggle the room.
    if method in ("set_mute", "set_input_mute") and not isinstance(params.get("muted"), bool):
        raise PolicyError("muted must be boolean")
    # An enum, not a free string: the host applier turns this into units and
    # mixer settings, so an unknown position must die here rather than at a
    # shell that has already stopped the leg that was playing.
    if method == "set_output" and params.get("output") not in SWITCH_OUTPUTS:
        raise PolicyError("output must be one of %s" % ", ".join(SWITCH_OUTPUTS))
    # A LEVEL IS A PERCENTAGE AND THE RANGE IS CLOSED HERE, not at the applier.
    # The applier clamps, because a rocker held past the end is not an error;
    # a broker request is different -- a client that asked for 5000 has a bug,
    # and clamping it to 100 would hide the bug behind a room at full volume.
    if method == "set_volume":
        level = params.get("level")
        if (isinstance(level, bool) or not isinstance(level, int) or
                not VOLUME_MIN <= level <= VOLUME_MAX):
            raise PolicyError("level must be an integer percentage in %d..%d"
                              % (VOLUME_MIN, VOLUME_MAX))
        # OPTIONAL, and it is a GUARD rather than a destination: the wire to the
        # applier carries no output for a level, so naming one here means "apply
        # this only if that is still the selected position". `mute` is refused
        # because it has no level to set, so guarding on it could only ever
        # refuse. Omitting it means "whatever is selected", which is what the
        # rocker means.
        if "output" in params and params["output"] not in LEVELLED_OUTPUTS:
            raise PolicyError("volume output must be one of %s"
                              % ", ".join(LEVELLED_OUTPUTS))
    if method == "pair" and "confirmation" in params:
        confirmation = params["confirmation"]
        if (not isinstance(confirmation, str) or not 1 <= len(confirmation) <= 16 or
                HARDWARE_ADDRESS.search(confirmation)):
            raise PolicyError("pairing confirmation is invalid")


# The verbs the panel's own switch is made of. They carry no alias, so none of
# them can reach a Bluetooth device, and none of them is `select_output`.
SWITCH_METHODS = frozenset({"set_output", "set_input_mute", "set_volume", "set_mute"})


def switch_only_authorization(method: str, params: Mapping[str, object]) -> bool:
    """Authorize the host switch and nothing else. Implements: SR-023, SR-028.

    WHY THIS EXISTS AND WHY IT IS THIS NARROW. The broker's authorization
    callback defaults to deny because the image has never decided how panel
    authentication maps onto BLUETOOTH authority: pairing a phone, trusting it
    and routing audio to it are decisions with a person and a policy behind
    them, and guessing at that was the wrong risk to take (SR-023, README).

    The switch is a different question with a different answer. It is the
    physical control of the wall in front of whoever is standing at it: it names
    no device, reaches no device, and does nothing a person at the glass could
    not do with a knob. The socket is already peer-UID checked and 0660, so the
    caller is the panel's own session. Denying it would mean shipping a switch
    nobody can move, which is what the shell showed on the glass before this.

    Everything else still defaults to deny, so WSN-024's routed-device backend
    stays shut: `pair`, `connect`, `select_output` and the rest are refused here
    before a backend can observe them at all.
    """
    return method in SWITCH_METHODS


# The two verbs that may name a device the panel does not trust yet. Everything
# else needs the trust to exist first -- which is what makes `trust` the way in
# rather than a convenience beside it.
UNTRUSTED_METHODS = frozenset({"pair", "trust"})


def device_authorization(method: str, params: Mapping[str, object]) -> bool:
    """Authorize the host switch AND Bluetooth device management. WSN-024.

    WHAT CHANGED, AND WHY IT IS NOT A LOOSENING OF SR-023. The docstring on
    `switch_only_authorization` says device management stayed shut because "the
    image has never decided how panel authentication maps onto BLUETOOTH
    authority". That decision now exists and is made one layer up, not here:
    `audio-ipc.cjs` admits only `set_output` and `set_input_mute` without an
    authenticated session (PUBLIC_CONTROLS, Owner 2026-09-14, "local audio
    switches are public, device management is not"), and every verb in
    DEVICE_METHODS arrives having passed that check or not at all.

    So this callback is not the authority; it is the broker's statement of what
    a backend is ALLOWED to be asked. Keeping the deny-by-default for everything
    outside these two sets is what still holds: an unknown verb, or a verb some
    future backend invents, is refused here before any device I/O.

    `switch_only_authorization` is KEPT and is still what a panel without the
    routed-device backend installs. A panel whose applier is absent must refuse
    at the gate rather than accept a pairing request nothing will ever answer.
    """
    return method in SWITCH_METHODS or method in DEVICE_METHODS


def validate_inventory_action(
    method: str, params: Mapping[str, object], devices: Iterable[Device]
) -> None:
    """Refuse device actions whose alias, kind, or trust is not current.

    Pair and trust may target a discovered untrusted device -- `trust` above
    all, because a device that is already trusted is the one case where that
    verb has nothing to do. Connect/disconnect/forget and route selection
    require a trusted device; input/output selection also requires the matching
    kind. Implements: SR-023, LLR-007.
    """
    alias = params.get("alias")
    if alias is None:
        return
    matches = [device for device in devices if device.alias == alias]
    if len(matches) != 1:
        raise PolicyError("device alias is not in the current inventory")
    device = matches[0]
    if method not in UNTRUSTED_METHODS and not device.trusted:
        raise PolicyError("device is not trusted")
    # B2: the ROUTE GATE READS CAPABILITIES, not the headline kind.
    #
    # It used to compare `device.kind` against a fixed expectation, which is
    # why a headset -- headlined `output` because it is mostly a thing you
    # play to -- could never be selected as the microphone source. The check
    # is not loosened by this: a device still has to be able to do the thing
    # being asked of it, and now that is asked of the facts rather than of a
    # summary that had to pick one.
    #
    # A DEVICE THAT DECLARES NOTHING IS STILL REFUSED. An empty capability set
    # satisfies no route, which is the honest answer for a device advertising
    # no audio UUID, and is the same answer the old check gave by a different
    # argument. The fallback to `kind` below is for a document written by an
    # applier that predates B2, where absent is "not stated" and not "none".
    needed = ROUTE_CAPABILITIES.get(method)
    if needed:
        if device.capabilities:
            if not any(capability in device.capabilities for capability in needed):
                raise PolicyError("device cannot carry that route")
        else:
            expected_kind = {"select_input": "input", "select_output": "output"}[method]
            if device.kind != expected_kind:
                raise PolicyError("device kind does not match route")


def choose_restored_route(
    devices: Iterable[Device], preferred_alias: str | None, fallback_aliases: Iterable[str]
) -> str | None:
    """Choose a trusted output preference or the first trusted fallback.

    Untrusted and input devices are never auto-restored. None means the backend
    must retain its safe local/default output rather than guessing.
    Implements: SR-023, LLR-007.
    """
    eligible = {d.alias for d in devices if d.kind == "output" and d.trusted}
    candidates = ([preferred_alias] if preferred_alias else []) + list(fallback_aliases)
    return next((alias for alias in candidates if alias in eligible), None)
