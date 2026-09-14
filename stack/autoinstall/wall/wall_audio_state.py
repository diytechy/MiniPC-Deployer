"""Pure state core for the panel's output switch (item 23, steps 1-2).

One responsibility: decide what the audio state becomes, given the state that
was on disk and one event. It performs NO I/O of its own -- every function here
takes plain data and returns plain data -- so the whole switch, including the
one-shot headset auto-switch and the per-output volume memory, is exhaustively
unit-testable without a sound card, a udev event or root.

The thin shell that reads files, runs amixer/systemctl and journals is
`wall-audio-output`; the only thing it is allowed to decide is when to call in
here. Reading order: STATE_SCHEMA, then `apply_event`, then `plan`.

Contract:
  Inputs:  state: dict as loaded from /etc/wall-panel/audio-state.json
           event: dict {"kind": ..., plus per-kind keys} (see apply_event)
  Outputs: (state, [journal lines]) -- always a NEW dict, inputs untouched
  Config:  none read here; the shell owns every path
  Raises:  StateError on a request that policy refuses
Implements: SR-028, LLR-013
"""

from __future__ import annotations

# The three switch positions, in the order they appear on the glass
# (upper-left: input mute, then Mute / Headset / Speaker).
OUTPUTS = ("mute", "headset", "speaker")
# Outputs that carry audio and therefore remember a level. `mute` does not:
# there is nothing to set a level on, and a remembered "mute volume" would be a
# second mute with no control of its own (Owner ruling F).
LEVELLED_OUTPUTS = ("headset", "speaker")
VOLUME_MIN, VOLUME_MAX = 0, 100
# One rocker press. The bus level is a PERCENTAGE rather than the adapter's raw
# 0..197 steps, so that it means the same thing on the headset card, whose own
# control has 38 (both measured on the panel 2026-09-13).
VOLUME_STEP = 4

STATE_VERSION = 1
STATE_SCHEMA = {
    # The switch position the Owner last chose. Persists across reboot and
    # sleep (Owner ruling G); nothing but an explicit request and the one-shot
    # headset event below may change it.
    "output": "speaker",
    # The mic mute, which is a SEPARATE button from the output switch (Owner
    # ruling E): `mute` silences the room, not the microphone.
    "input_muted": False,
    # Per-output level memory (Owner ruling F): switching back to an output
    # restores the level it had, rather than shouting at the last one's setting.
    "volume": {"headset": 60, "speaker": 60},
    # Whether the headset adapter (0d8c:0014) is enumerated right now. False
    # with output == "headset" is the silence-everywhere case of ruling 7.
    "headset_present": False,
    # The one-shot latch behind D5-as-amended. Armed while no adapter is
    # present; spent by the add event that flips speaker -> headset. Without it
    # every udev `change` on an already-plugged adapter would drag the Owner
    # back to the headset they had deliberately switched away from.
    "headset_autoswitch_armed": True,
    # The last broker request applied, for deduplication. systemd.path fires on
    # every close-write, so an unchanged request file re-read after a restart
    # would otherwise replay a rocker press. It lives in the state rather than
    # in /run because the applier must be able to answer "have I done this one"
    # before /run has been written even once.
    "request_seq": -1,
    "version": STATE_VERSION,
}


class StateError(ValueError):
    """A request or a stored state that policy refuses. Never swallowed."""


def default_state():
    """A fresh state: speaker, unmuted, mid level, no headset seen yet."""
    state = dict(STATE_SCHEMA)
    state["volume"] = dict(STATE_SCHEMA["volume"])
    return state


def normalize(raw):
    """Coerce a loaded state file into a valid state, field by field.

    A corrupt or partial file must not leave the panel with no audio policy at
    all, so every key falls back INDEPENDENTLY to its default rather than the
    whole file being discarded. The shell journals when it had to replace
    something; nothing here guesses silently.

    Inputs:  raw: whatever json.load returned (any type)
    Outputs: a dict satisfying STATE_SCHEMA's shape
    Implements: SR-028, LLR-013
    """
    state = default_state()
    if not isinstance(raw, dict):
        return state
    if raw.get("output") in OUTPUTS:
        state["output"] = raw["output"]
    if isinstance(raw.get("input_muted"), bool):
        state["input_muted"] = raw["input_muted"]
    if isinstance(raw.get("headset_present"), bool):
        state["headset_present"] = raw["headset_present"]
    if isinstance(raw.get("headset_autoswitch_armed"), bool):
        state["headset_autoswitch_armed"] = raw["headset_autoswitch_armed"]
    seq = raw.get("request_seq")
    if isinstance(seq, int) and not isinstance(seq, bool) and seq >= -1:
        state["request_seq"] = seq
    volume = raw.get("volume")
    if isinstance(volume, dict):
        for output in LEVELLED_OUTPUTS:
            level = volume.get(output)
            # bool is an int in Python and True would become 1%: refuse it.
            if isinstance(level, int) and not isinstance(level, bool):
                state["volume"][output] = clamp_volume(level)
    return state


def clamp_volume(level):
    """Hold a level inside 0..100 rather than refusing it.

    A rocker held down past the end is not an error, and a state file written
    with a wider range must not make the panel silent.
    """
    return max(VOLUME_MIN, min(VOLUME_MAX, int(level)))


def volume_of(state, output=None):
    """The remembered level of `output` (default: the selected one).

    `mute` has no level of its own; asking for it answers 0, which is what the
    hardware is actually set to in that position.
    """
    output = output or state["output"]
    if output not in LEVELLED_OUTPUTS:
        return 0
    return clamp_volume(state["volume"].get(output, STATE_SCHEMA["volume"][output]))


def audible(state):
    """Whether ANY output should be carrying audio right now.

    False in `mute`, and false for `headset` while the adapter is absent --
    Owner ruling 7: silence on every output, nothing tunnelled, nothing to the
    speakers, until the user presses the switch. The red icon is the UI's
    rendering of exactly this being false while the output is `headset`.
    """
    if state["output"] == "mute":
        return False
    if state["output"] == "headset":
        return bool(state["headset_present"])
    return True


def unavailable_reason(state):
    """Why the selected output is silent, for the UI's red icon, or None."""
    if state["output"] == "headset" and not state["headset_present"]:
        return "headset_absent"
    return None


def apply_event(state, event):
    """Fold one event into the state; return the new state and journal lines.

    Every accepted event returns at least one line: a change nobody can see in
    the journal is a change nobody can debug (coordinator rule 4, evidence).
    A no-op event returns a line saying so, and an unchanged state.

    Inputs:  state: any loaded state (normalized here)
             event: {"kind": one of
                 "set_output"     {"output": one of OUTPUTS}
                 "set_input_mute" {"muted": bool}
                 "set_volume"     {"level": int}    -- absolute percent, clamped
                 "nudge_volume"   {"louder": bool}  -- one rocker press
                 "headset"        {"present": bool}}
    Outputs: (new_state, lines)
    Raises:  StateError if the event is unknown or malformed
    Implements: SR-028, LLR-013
    """
    kind = event.get("kind")
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise StateError("unknown event kind")
    return handler(normalize(state), event)


def _set_output(state, event):
    output = event.get("output")
    if output not in OUTPUTS:
        raise StateError("output must be one of %s" % (", ".join(OUTPUTS),))
    if state["output"] == output:
        return state, ["output already %s" % output]
    previous = state["output"]
    state["output"] = output
    lines = ["output %s -> %s (level %d%%)" % (previous, output, volume_of(state))]
    if unavailable_reason(state):
        # Said out loud because the panel is about to be silent ON PURPOSE, and
        # a silent panel with no journal line looks exactly like a broken one.
        lines.append("headset selected but the adapter is absent: every output silent")
    return state, lines


def _set_input_mute(state, event):
    muted = event.get("muted")
    # Required, not defaulted -- the same reasoning as routing.set_mute: a
    # request that forgot to say which way would silently toggle the mic.
    if not isinstance(muted, bool):
        raise StateError("muted must be boolean")
    if state["input_muted"] == muted:
        return state, ["input mute already %s" % ("on" if muted else "off")]
    state["input_muted"] = muted
    return state, ["input mute %s" % ("on" if muted else "off")]


def _set_volume(state, event):
    level = event.get("level")
    if not isinstance(level, int) or isinstance(level, bool):
        raise StateError("level must be an integer percentage")
    return _store_volume(state, clamp_volume(level))


def _nudge_volume(state, event):
    louder = event.get("louder")
    if not isinstance(louder, bool):
        raise StateError("louder must be boolean")
    step = VOLUME_STEP if louder else -VOLUME_STEP
    return _store_volume(state, clamp_volume(volume_of(state) + step))


def _store_volume(state, level):
    """Write a level into the SELECTED output's memory (Owner ruling F).

    In `mute` there is no output to remember a level for. The rocker is not
    inert there -- it is simply not a level control until a position that has
    one is chosen -- so the press is journaled and dropped rather than written
    into some other output's memory, which would move a level the Owner cannot
    hear changing.
    """
    output = state["output"]
    if output not in LEVELLED_OUTPUTS:
        return state, ["volume press ignored in %s" % output]
    previous = state["volume"].get(output)
    if previous == level:
        return state, ["%s volume already %d%%" % (output, level)]
    state["volume"][output] = level
    return state, ["%s volume %s%% -> %d%%" % (output, previous, level)]


def _headset(state, event):
    """The USB adapter appeared or went away (D5, amended by review finding 1).

    The event is the ADAPTER enumerating, never a cable: 0d8c:0014 is a USB
    audio-class device with no jack detection at all, so "a headset is plugged
    in" is not a fact this panel can observe. Measured on the panel
    2026-09-13: its mixer carries Speaker, Mic and Auto Gain Control, and
    nothing else.

    The auto-switch fires ONCE per plug. The latch is re-armed by the remove
    event, so plug / listen / unplug / plug switches twice, while a udev
    `change` storm on a stationary adapter switches nothing.
    """
    present = event.get("present")
    if not isinstance(present, bool):
        raise StateError("present must be boolean")
    lines = []
    if state["headset_present"] != present:
        lines.append("headset adapter %s" % ("present" if present else "absent"))
    state["headset_present"] = present
    if not present:
        state["headset_autoswitch_armed"] = True
        if state["output"] == "headset":
            lines.append("headset still selected: every output silent until the "
                         "switch is moved")
        return state, lines or ["headset adapter absent (no change)"]
    armed = state["headset_autoswitch_armed"]
    state["headset_autoswitch_armed"] = False
    if armed and state["output"] == "speaker":
        state["output"] = "headset"
        lines.append("auto-switch speaker -> headset (one-shot, level %d%%)"
                     % volume_of(state))
    else:
        # Spent latch, or the Owner is in mute/headset already: say why nothing
        # moved, so an Owner who expected a switch can see the reason.
        lines.append("no auto-switch (output %s, latch %s)"
                     % (state["output"], "armed" if armed else "spent"))
    return state, lines


_HANDLERS = {
    "set_output": _set_output,
    "set_input_mute": _set_input_mute,
    "set_volume": _set_volume,
    "nudge_volume": _nudge_volume,
    "headset": _headset,
}


# The forwarder units the switch owns. Named once, here, because the plan, the
# applier and the tests must agree on the set and its order (stop before start).
SPEAKER_LEGS = ("wall-bus-speaker.service", "wall-speaker-out.service")
HEADSET_LEGS = ("wall-bus-headset.service",)
ALL_LEGS = SPEAKER_LEGS + HEADSET_LEGS


def plan(state):
    """Translate a state into the things the shell must make true.

    Kept here, beside the state, so "what speaker mode means" has ONE
    definition the tests can read, rather than being spread through a shell
    script's branches.

    Outputs: {"legs": {unit name: should be running},
              "volume": int percent for the bus softvol,
              "adapter_muted": bool, "headset_muted": bool,
              "input_muted": bool, "reason": str | None}
    Implements: SR-028, LLR-013
    """
    live = audible(state)
    speaker = live and state["output"] == "speaker"
    headset = live and state["output"] == "headset"
    legs = {unit: speaker for unit in SPEAKER_LEGS}
    legs.update({unit: headset for unit in HEADSET_LEGS})
    return {
        # The speaker leg is TWO units on purpose: bus -> tap, then
        # tap -> adapter. The detector reads the tap, which is what makes it
        # post-switch (review finding 2) and pre-volume, so that a quiet room
        # does not drop the amplifier.
        "legs": legs,
        "volume": volume_of(state),
        # Hardware mutes are belt to the leg's braces: stopping a forwarder
        # stops new audio, but an amplifier fed by an unmuted DAC still passes
        # whatever that card's own mixer lets through.
        "adapter_muted": not speaker,
        "headset_muted": not headset,
        "input_muted": bool(state["input_muted"]),
        "reason": unavailable_reason(state),
    }
