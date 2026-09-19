"""Pure Bluetooth device facts: aliases, kinds, and the observer document.

WSN-024. This is the ONE definition of how a BlueZ device becomes something the
panel may name, and both sides of the privilege boundary import it: the root
observer (`wall-bluetooth-device`) builds the document, and the broker backend
(`routed_backend`) reads it. A second spelling of the alias rule is how the
renderer ends up naming a device the applier cannot find, which is a refusal
nobody can act on.

NO HARDWARE ADDRESS EVER CROSSES IF-015, and this file is where that is made
true rather than merely promised. The document the broker reads carries no MAC
at all: the applier keeps the alias-to-address map on its own side of the
boundary and re-derives it from BlueZ, deterministically, on every request. So
the broker cannot leak an address because it never has one, and a renderer
cannot name one because `routing.ALIAS` refuses anything that looks like one.

THE ALIAS IS DERIVED, NOT STORED, and it has to be STABLE: it is what the
renderer holds between painting a row and the finger landing on it. It is a slug
of the device's own BlueZ name, which changes only when the person renames their
phone, plus a deterministic suffix when two devices slug the same. Ordering is
by ADDRESS and not by discovery order, so the same two devices get the same two
aliases on every poll, on every boot, in any order BlueZ happens to list them.

Implements: SR-023, LLR-007 (WSN-024)
"""

from __future__ import annotations

import re
import unicodedata

# Kept in step with routing.ALIAS; imported there rather than re-spelled where
# this module can help it, but the applier does not import `routing`, so the
# length is repeated as a constant and `test_the_alias_rule_has_one_spelling`
# asserts the two agree.
ALIAS_MAX = 48

# The A2DP/HFP service UUIDs that say which DIRECTION a device is, from the
# PANEL's point of view. A phone that plays music to the panel is an AudioSource
# (the panel is the sink), and the panel's `kind` for it is "input": it is an
# input to the panel's bus. A Bluetooth speaker or headset is an AudioSink and
# is an "output". Getting this backwards is the kind of mistake that reads as
# "the route selector will not accept my speaker".
AUDIO_SOURCE_UUID = "0000110a"   # the far end SOURCES audio -> panel input
AUDIO_SINK_UUID = "0000110b"     # the far end SINKS audio   -> panel output
HANDSFREE_UUIDS = ("0000111e", "0000111f")  # HFP: a headset, both directions

# What a device is when its UUIDs say nothing useful. A device BlueZ has not
# resolved services for yet is still worth showing -- it is the one you are
# trying to pair -- and "input" is the safer default on this panel: the panel is
# an A2DP SINK, so a phone is what it mostly meets, and `select_output` refuses
# a device of the wrong kind rather than routing the room into a phone.
DEFAULT_KIND = "input"

# REMOVED, NOT SEPARATED. An apostrophe sits INSIDE a word -- "Peter's Pixel"
# is two words, not three -- and treating it as a separator produced
# `peter-s-pixel`, which is both uglier and one character further from what the
# phone is actually called. Both spellings, because a phone types the curly one.
_SLUG_DROP = re.compile(r"['’ʼ]")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_HARDWARE = re.compile(
    r"(?i)(?<![0-9a-f])(?:(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|"
    r"(?:[0-9a-f]{2}_){5}[0-9a-f]{2}|(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}|"
    r"[0-9a-f]{12})(?![0-9a-f])"
)


def slug(name: str) -> str:
    """One device name to one alias candidate, or "" when nothing survives.

    Unicode is FOLDED and not dropped, because a phone called "Pétur’s iPhone"
    should become `peturs-iphone` and not `s-iphone`. Anything that does not
    fold to ASCII is then removed.

    A candidate that still LOOKS LIKE AN ADDRESS is refused outright rather than
    repaired. `routing.ALIAS` would accept `a0b1c2d3e4f5` -- it starts with a
    letter and is all lowercase alphanumerics -- and the hardware-address guard
    beside it is what actually catches it, so a device whose name is its own MAC
    (which is exactly what BlueZ calls an unresolved one) must not be handed a
    name-shaped alias. `fallback_alias` gives it a neutral one instead.
    """
    if not isinstance(name, str):
        return ""
    folded = unicodedata.normalize("NFKD", _SLUG_DROP.sub("", name)).encode("ascii", "ignore").decode("ascii")
    candidate = _SLUG_STRIP.sub("-", folded.lower()).strip("-")
    # Must begin with a letter: routing.ALIAS requires it, and a device called
    # "5.1 Speakers" would otherwise produce `5-1-speakers`, which is refused at
    # the far end with a message about an invalid alias rather than about a name.
    candidate = candidate.lstrip("-0123456789")
    candidate = candidate[:ALIAS_MAX].strip("-")
    if not candidate or _HARDWARE.search(candidate):
        return ""
    return candidate


def fallback_alias(index: int) -> str:
    """The alias for a device whose own name produces nothing usable."""
    return "device-%d" % index


def assign_aliases(devices):
    """[(address, name)] -> {address: alias}, stable and collision-free.

    STABLE MEANS ORDERED BY ADDRESS. The renderer holds an alias between the
    paint and the tap, and the applier resolves it back to an address on the
    other side of the boundary, so two devices that slug the same must get the
    same two aliases every single time. BlueZ's own listing order is not a
    promise, so it is not used: the addresses are sorted first and the suffix
    falls out of that.

    The suffix is `-2`, `-3`, ... on the SECOND and later claimants, so the
    common case -- one device with a distinct name -- never grows one, and an
    alias never changes because an unrelated device appeared.
    """
    taken: dict[str, int] = {}
    assigned: dict[str, str] = {}
    for index, (address, name) in enumerate(sorted(devices, key=lambda item: str(item[0]).upper()), 1):
        base = slug(name) or fallback_alias(index)
        count = taken.get(base, 0) + 1
        taken[base] = count
        alias = base if count == 1 else "%s-%d" % (base[:ALIAS_MAX - 3], count)
        # The suffix can still collide with a device literally named "thing-2".
        # Bounded rather than clever: keep appending until it is free, which
        # terminates because `taken` only grows.
        while alias in assigned.values():
            count += 1
            taken[base] = count
            alias = "%s-%d" % (base[:ALIAS_MAX - 3], count)
        assigned[str(address)] = alias
    return assigned


def classify(uuids) -> str:
    """The panel-relative kind of a device, from its service UUIDs.

    A device that is BOTH (a headset: it sinks the room's audio and sources a
    microphone) is an OUTPUT, because that is the choice the Owner makes about
    it -- "send the music there". Its microphone reaches the panel through the
    separate mic-source selection, not through this kind.
    """
    prefixes = {str(uuid).lower()[:8] for uuid in uuids or ()}
    if AUDIO_SINK_UUID in prefixes or prefixes & set(HANDSFREE_UUIDS):
        return "output"
    if AUDIO_SOURCE_UUID in prefixes:
        return "input"
    return DEFAULT_KIND
