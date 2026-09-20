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

# THE TWO HFP UUIDS ARE OPPOSITE ROLES AND WERE TREATED AS ONE (terra,
# 2026-09-19, finding 4). `111e` is Handsfree -- the HF role, which is what a
# HEADSET advertises -- and `111f` is HandsfreeAudioGateway, which is what a
# PHONE advertises. Lumping both under "a headset, both directions" classified
# every phone as an output, and `select_input` refuses a device whose kind is
# not `input`, so the panel could not select the phone whose microphone
# `wall-bt-call` exists to carry. The primary use of the verb was unreachable.
HANDSFREE_UNIT_UUID = "0000111e"     # the far end is a headset  -> panel output
HANDSFREE_GATEWAY_UUID = "0000111f"  # the far end is a phone    -> panel input

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


def assign_aliases(devices, previous=None):
    """[(address, name)] -> {address: alias}, stable and collision-free.

    A DEVICE KEEPS THE ALIAS IT WAS LAST PUBLISHED UNDER, and that is the whole
    of the stability guarantee. Ordering by address is not enough and shipped
    once as if it were: it is stable for a FIXED set of devices and not
    otherwise, so a second phone also called "Pixel", with a LOWER address,
    took the bare `pixel` and pushed the first one to `pixel-2`. A tap on a row
    painted a moment earlier then paired, trusted, forgot or routed the wrong
    phone (terra, 2026-09-19, finding 3).

    `previous` is the applier's last published map. A device in it keeps its
    alias UNLESS the alias it would derive from its CURRENT name has a different
    base -- somebody renaming their phone should see the new name, and nothing
    is holding a row from before the rename. Everything else is assigned as
    before: sorted by address, bare slug to the first claimant, `-2`, `-3` after.

    Still deterministic: (previous, devices) in, one map out, no clock and no
    randomness.
    """
    previous = {str(key).upper(): value for key, value in (previous or {}).items()}
    ordered = sorted(devices, key=lambda item: str(item[0]).upper())
    bases = {}
    for index, (address, name) in enumerate(ordered, 1):
        bases[str(address)] = slug(name) or fallback_alias(index)

    assigned: dict[str, str] = {}
    used: set[str] = set()
    # Pass one: honour the previous assignment, where it still matches the name.
    for address, base in bases.items():
        held = previous.get(address.upper())
        if held and held not in used and (held == base or held.startswith(base + "-")):
            assigned[address] = held
            used.add(held)
    # AN ALIAS A DEVICE HELD A MOMENT AGO IS NOT HANDED STRAIGHT TO ANOTHER ONE
    # (terra round 2, finding 2). Rename device A from "Pixel" to "Other" while a
    # NEW device B called "Pixel" appears, and without this A's `pixel` falls
    # free in pass two and B takes it -- so a row painted before the rename sends
    # `pixel` and the broker resolves it to B. Reserving it costs B a suffix.
    #
    # ONLY FOR A DEVICE THAT IS STILL HERE. An alias belonging to a device that
    # has GONE is not reserved: nothing can be painted for a device the document
    # no longer carries, and reserving those would make the map grow for the life
    # of the boot.
    for address in bases:
        held = previous.get(address.upper())
        if held and address not in assigned:
            used.add(held)
    # Pass two: everything left, in address order, taking the first free suffix.
    for address, base in bases.items():
        if address in assigned:
            continue
        alias = base
        count = 1
        # Bounded rather than clever: `used` only grows, so this terminates, and
        # it also steps over a device literally named "thing-2".
        while alias in used:
            count += 1
            alias = "%s-%d" % (base[:ALIAS_MAX - 3], count)
        assigned[address] = alias
        used.add(alias)
    return assigned


# WHAT A DEVICE CAN DO, AS FOUR INDEPENDENT FACTS (B2, 2026-09-19).
#
# `kind` answers "which column does this card go in", which is a presentation
# question and has to have one answer. These answer "may the panel ask this
# device for X", which is a policy question and genuinely has more than one
# answer for the same device -- a headset both sinks the room's audio and
# sources a microphone, and collapsing that into one word is what stopped a
# headset ever being the mic source.
#
# They are named for what the FAR END is, exactly as the UUID constants above
# are, because the alternative -- naming them for the panel's side -- inverts
# every time somebody reads it quickly.
CAPABILITY_SINK = "sink"      # the far end can RECEIVE audio: a speaker, a headset
CAPABILITY_SOURCE = "source"  # the far end can SEND audio: a phone playing music
CAPABILITY_HF = "hf"          # the far end is a hands-free UNIT: a headset
CAPABILITY_AG = "ag"          # the far end is a hands-free GATEWAY: a phone, a laptop
CAPABILITIES = (CAPABILITY_SINK, CAPABILITY_SOURCE, CAPABILITY_HF, CAPABILITY_AG)


def capabilities(uuids) -> list:
    """Every capability the device advertises, sorted. Never raises.

    SORTED because this reaches a document the renderer compares, and an
    arbitrary order would make an unchanged device look changed on every
    republish. Empty is a legitimate answer -- a device advertising no audio
    UUID at all can be neither a source nor a sink -- and the route gates
    refuse it on those grounds rather than on a guess.
    """
    prefixes = {str(uuid).lower()[:8] for uuid in uuids or ()}
    found = []
    if AUDIO_SINK_UUID in prefixes:
        found.append(CAPABILITY_SINK)
    if AUDIO_SOURCE_UUID in prefixes:
        found.append(CAPABILITY_SOURCE)
    if HANDSFREE_UNIT_UUID in prefixes:
        found.append(CAPABILITY_HF)
    if HANDSFREE_GATEWAY_UUID in prefixes:
        found.append(CAPABILITY_AG)
    return sorted(found)


def classify(uuids) -> str:
    """The panel-relative kind of a device, from its service UUIDs.

    ONE KIND PER DEVICE, because `kind` is IF-015's shape and is validated in
    five places; a device that is genuinely both gets the one the Owner is more
    likely to mean by it. A HEADSET sinks the room's audio and sources a
    microphone, and "send the music there" is the choice somebody makes about
    it, so a sink wins: its microphone reaches the panel through the mic-source
    selection, which is `select_input` against a different device.

    THAT RESIDUAL IS CLOSED NOW, and this function is no longer the whole
    answer. `kind` remains exactly what it was -- the card's headline, one
    value, chosen by the precedence below -- but `capabilities` beside it
    carries the facts without collapsing them, and the route gates read that.
    A headset is still headlined `output` and can now also be chosen as the
    microphone source, which is what B2 was for. Do not widen the precedence
    here to compensate: two places deciding the same thing differently is the
    defect this pair exists to avoid.
    """
    prefixes = {str(uuid).lower()[:8] for uuid in uuids or ()}
    if AUDIO_SINK_UUID in prefixes or HANDSFREE_UNIT_UUID in prefixes:
        return "output"
    if AUDIO_SOURCE_UUID in prefixes or HANDSFREE_GATEWAY_UUID in prefixes:
        return "input"
    return DEFAULT_KIND
