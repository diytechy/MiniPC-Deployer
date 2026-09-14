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

# ── the mic legs (step 4, item 23 C / D3 / D4) ─────────────────────────────
# The two capture devices this panel can select between, named as the ALSA PCMs
# they resolve to. There is no third: the 5.1 adapter's ONE capture stream is
# spent on the desktop's S/PDIF (measured 2026-09-13), so a wired mic cannot
# share it.
MIC_SOURCE_PANEL = "mic_panel"      # ALC255 Analog, card PCH -- D4's "panel mic"
MIC_SOURCE_HEADSET = "mic_headset"  # 0d8c:0014's mono mic -- D3's headset mic
MIC_SOURCES = (MIC_SOURCE_PANEL, MIC_SOURCE_HEADSET)

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
    # back to the headset they had deliberately switched away from. It is also
    # SET from what coldplug sees (armed if the adapter was absent across the
    # boot, spent if it was present), which is how a power cycle with the
    # adapter plugged in comes back in the position the Owner left it in.
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

    ONE FIELD IS NOT LIKE THE OTHERS. Landing on the default for `output`, the
    volume or the latch is harmless. Landing on the default for `input_muted` --
    False -- means a damaged file has turned a privacy control from muted to
    live, with nothing in the room to hear it happen. So a document that needed
    any repair at all comes back MUTED, and a document that is not a document
    comes back muted too. A key that is simply ABSENT is not damage: that is a
    file written before the field existed, and it takes the schema default.

    Inputs:  raw: whatever json.load returned (any type)
    Outputs: a dict satisfying STATE_SCHEMA's shape
    Implements: SR-028, LLR-013
    """
    state = default_state()
    if not isinstance(raw, dict):
        # Not even a document. Fall back to the whole default EXCEPT the one
        # field whose default is not the safe answer: see `repaired` below.
        state["input_muted"] = True
        return state
    # WHETHER ANYTHING HAD TO BE REPLACED, AND WHY THAT DECIDES THE MICROPHONE.
    # Every other field's default is harmless to land on: `speaker` plays music,
    # 60% is a level, an armed latch switches once. `input_muted`'s default is
    # False, and landing on THAT means a damaged file has turned a privacy
    # control from muted to live -- silently, and with nothing in the room to
    # hear. Review (terra, 2026-09-14, round 2) was right to refuse the earlier
    # answer that the applier and the Bluetooth supervisor agreeing made it safe:
    # agreeing to open a microphone nobody asked for is not safety.
    #
    # So repair is still field by field -- a typo must not cost the panel its
    # whole audio policy -- but a document that needed ANY repair comes back
    # with the microphone MUTED. The Owner presses one button; the alternative
    # is a microphone opened by a truncated write.
    repaired = False
    if raw.get("output") in OUTPUTS:
        state["output"] = raw["output"]
    elif "output" in raw:
        repaired = True
    if isinstance(raw.get("input_muted"), bool):
        state["input_muted"] = raw["input_muted"]
    elif "input_muted" in raw:
        repaired = True
    if isinstance(raw.get("headset_present"), bool):
        state["headset_present"] = raw["headset_present"]
    elif "headset_present" in raw:
        repaired = True
    if isinstance(raw.get("headset_autoswitch_armed"), bool):
        state["headset_autoswitch_armed"] = raw["headset_autoswitch_armed"]
    elif "headset_autoswitch_armed" in raw:
        repaired = True
    seq = raw.get("request_seq")
    if isinstance(seq, int) and not isinstance(seq, bool) and seq >= -1:
        state["request_seq"] = seq
    elif "request_seq" in raw:
        repaired = True
    volume = raw.get("volume")
    if isinstance(volume, dict):
        for output in LEVELLED_OUTPUTS:
            level = volume.get(output)
            # bool is an int in Python and True would become 1%: refuse it.
            if isinstance(level, int) and not isinstance(level, bool):
                state["volume"][output] = clamp_volume(level)
                # A CLAMP IS A REPAIR (terra, round 3). `{"speaker": 101}` came
                # back as 100 and stayed UNMUTED, which is the rule this
                # function claims to follow failing on its own boundary -- and
                # the applier journals exactly this case as "repaired", so the
                # two halves were already disagreeing in the log.
                if state["volume"][output] != level:
                    repaired = True
            elif output in volume:
                repaired = True
    elif "volume" in raw:
        repaired = True
    if repaired:
        state["input_muted"] = True
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


def mic_source(state):
    """Which capture device the mic legs read. Follows the OUTPUT position.

    Item 23 D3 (Headset) routes "that jack's mic back to the mic input"; D4
    (Speaker) routes "the panel's internal mic". So the selection is a
    consequence of the output switch and is not a control of its own -- there is
    no fourth button on the glass, and the Owner never asked for one.

    In `mute` the panel mic is used, because Owner ruling E is explicit that the
    output Mute position does NOT mute the microphone ("the mic has its own
    mute"). `mute` names no output device, so there is no headset to take the
    mic from and the built-in one is the only defensible answer.
    """
    if state["output"] == "headset" and state["headset_present"]:
        return MIC_SOURCE_HEADSET
    return MIC_SOURCE_PANEL


def mic_live(state):
    """Whether ANY mic leg should be carrying audio right now.

    Two things stop it, and they are different things on purpose:

    * `input_muted` -- Owner ruling E's separate input-mute button. It is
      applied as a REAL mute: the forwarders are stopped, so not one sample
      leaves this panel, and the capture switch is closed on every card that has
      one. It is deliberately not a flag the UI draws over a running mic.
    * `headset` selected with no adapter -- Owner ruling 7: "silence on every
      output, NO MIC TUNNELLED, nothing to the speakers until the user presses
      the switch". Falling back to the panel mic here would tunnel a microphone
      the Owner believes is switched away from, which is the one failure in this
      whole design that nobody could hear happening.
    """
    if state["input_muted"]:
        return False
    if state["output"] == "headset" and not state["headset_present"]:
        return False
    return True


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

    `boot` IS THE OWNER'S 2026-09-13 RULING, AND IT IS THE WHOLE REASON THIS
    FUNCTION HAS A SECOND MODE. The position must survive a power cycle
    "regardless of whether the adapter is plugged in over the boot; the dynamic
    switch on plug-in is an EVENT, and over a boot we do not know when that
    event occurred or whether it is still relevant." So a presence report that
    comes from COLDPLUG -- udev enumerating a device that was already there
    when the kernel started, or `apply-state` looking at what is plugged in --
    is a FACT to be recorded and never an event to be acted on. It updates
    `headset_present` and nothing else about the output.

    It also SETS the latch rather than spending it, because coldplug is the one
    moment the panel gets a free, trustworthy look at the world: an adapter
    present across the boot has no pending plug event behind it (latch spent),
    and an adapter absent across the boot means the next arrival really is a
    fresh plug (latch armed). That is what lets acceptance check 8 still pass
    on a panel that booted with nothing plugged in.

    Measured failure this replaces (panel, 2026-09-13 23:18): a power cycle
    with the adapter already plugged in logged "headset adapter present" then
    "auto-switch speaker -> headset (one-shot)", and the panel came back on
    Headset although it was left on Speaker.
    """
    present = event.get("present")
    if not isinstance(present, bool):
        raise StateError("present must be boolean")
    boot = event.get("boot", False)
    if not isinstance(boot, bool):
        raise StateError("boot must be boolean")
    lines = []
    changed_presence = state["headset_present"] != present
    if boot:
        state["headset_present"] = present
        # Present across the boot => no pending event; absent => the next add
        # is a real one. Either way the OUTPUT is not touched: ruling G wins.
        state["headset_autoswitch_armed"] = not present
        lines.append("headset adapter %s at boot: recorded, no auto-switch "
                     "(output stays %s)"
                     % ("present" if present else "absent", state["output"]))
        if changed_presence and not present and state["output"] == "headset":
            lines.append("headset still selected: every output silent until the "
                         "switch is moved")
        return state, lines
    if changed_presence:
        lines.append("headset adapter %s" % ("present" if present else "absent"))
    state["headset_present"] = present
    if not present:
        state["headset_autoswitch_armed"] = True
        if state["output"] == "headset":
            lines.append("headset still selected: every output silent until the "
                         "switch is moved")
        return state, lines or ["headset adapter absent (no change)"]
    if not changed_presence:
        # NOTHING ARRIVED. The applier already believed this adapter was there,
        # so whatever produced this report -- a `udevadm trigger`, the presence
        # unit being restarted, a re-assert after resume -- is not a plug, and
        # the ruling is that only a plug may move the switch. The latch is left
        # exactly as it was, because spending it here would eat the next real
        # plug. (Review, 2026-09-13.)
        return state, lines + ["headset adapter already present: no arrival to act on"]
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
# The mic legs (step 4). They are listed SEPARATELY from the output legs because
# they are governed by a different control -- `input_muted`, Owner ruling E --
# and because one of them is a supervisor rather than a forwarder:
#
#   wall-mic-rear   mic_selected -> the adapter's REAR pair, which item 23
#                   revision 2 wires to the desktop's audio input.
#   wall-bt-mic     mic_selected -> the connected phone's HFP SCO sink, started
#                   and stopped by its own bounded poll because an SCO PCM only
#                   exists while a call is up.
MIC_LEGS = ("wall-mic-rear.service", "wall-bt-mic.service")
ALL_LEGS = SPEAKER_LEGS + HEADSET_LEGS + MIC_LEGS


def plan(state):
    """Translate a state into the things the shell must make true.

    Kept here, beside the state, so "what speaker mode means" has ONE
    definition the tests can read, rather than being spread through a shell
    script's branches.

    Outputs: {"legs": {unit name: should be running},
              "volume": int percent for the bus softvol,
              "adapter_muted": bool, "headset_muted": bool,
              "input_muted": bool, "mic_live": bool,
              "mic_source": one of MIC_SOURCES, "reason": str | None}
    Implements: SR-028, LLR-013
    """
    live = audible(state)
    speaker = live and state["output"] == "speaker"
    headset = live and state["output"] == "headset"
    mic = mic_live(state)
    legs = {unit: speaker for unit in SPEAKER_LEGS}
    legs.update({unit: headset for unit in HEADSET_LEGS})
    legs.update({unit: mic for unit in MIC_LEGS})
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
        # THE ADAPTER'S MUTE IS NOT `not speaker` ANY MORE, AND THIS IS THE ONE
        # PLACE STEP 4 WEAKENS A BELT-AND-BRACES. MEASURED on the panel
        # 2026-09-14: `Speaker Playback Switch` (numid 7) on the ICUSBAUDIO7D is
        # a SINGLE boolean over all eight channels -- there is no per-channel
        # mute switch -- while `Speaker Playback Volume` (numid 8) is eight
        # values the Owner's bench session set to 66,66,24,24,0,0,24,24.
        #
        # The mic leg's destination is the REAR pair (channels 4/5) of that same
        # eight-channel stream, so muting the card to silence the front output
        # would also silence the mic return to the desktop -- which is exactly
        # what item 23 D3 asks for in Headset position. The two cannot both be
        # had from one switch. So the card is muted only when NOTHING at all is
        # meant to leave it, and what keeps the room's music out of the desktop
        # and the microphone out of the speakers is the pair of SOFTWARE route
        # tables, each of which writes EXPLICIT ZEROS into every channel it does
        # not own (`ttable.*.4..7` in audio-trim.conf, `ttable.0.0..3,6,7` in
        # audio-mic.conf). Both halves are asserted by tests, and by the ALSA
        # parse test that makes alsa-lib itself read them.
        #
        # The remaining hardware guarantee is the one that always did the real
        # work: in Mute and Headset nothing feeds `speaker_tap`, so the amp
        # detector's 240 s hold-off expires and the LCUS-2 relay physically
        # removes power from the amplifier.
        "adapter_muted": not (speaker or mic),
        "headset_muted": not headset,
        "input_muted": bool(state["input_muted"]),
        # Which capture PCM the mic legs open, published to them through
        # /run/wall-panel/audio-mic-source.env and read by `@func getenv`.
        "mic_source": mic_source(state),
        "mic_live": mic,
        "reason": unavailable_reason(state),
    }
