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
VOLUME_STEP = 5
# What the level lands on when the rocker is released (Owner, 2026-09-15). The
# Rocker movements are grid steps; RampPlanner carries fractional time debt.
VOLUME_SNAP = 5

# ── the mic legs (step 4, item 23 C / D3 / D4) ─────────────────────────────
# The two capture devices this panel can select between, named as the ALSA PCMs
# they resolve to. There is no third: the 5.1 adapter's ONE capture stream is
# spent on the desktop's S/PDIF (measured 2026-09-13), so a wired mic cannot
# share it.
MIC_SOURCE_PANEL = "mic_panel"      # ALC255 Analog, card PCH -- D4's "panel mic"
MIC_SOURCE_HEADSET = "mic_headset"  # 0d8c:0014's mono mic -- D3's headset mic
# The cancelled panel microphone (step 6). It is the SAME capsule as
# MIC_SOURCE_PANEL with `wall-audio-aec` in front of it, so it is only
# meaningful in the positions where the panel mic is selected -- and only worth
# selecting in Speaker, which is the one position where the room's own music is
# playing into it. It is chosen by the shell, not here: whether the canceller is
# installed and enabled is a fact about the machine, and this module decides
# policy from state alone.
MIC_SOURCE_CLEAN = "mic_clean"
# THE BLUETOOTH HEADSET'S MICROPHONE (B3, 2026-09-19). It is NOT a BlueALSA PCM
# opened here: an SCO capture PCM cannot be wrapped in `plug` (measured
# 2026-09-19: `Poll FD initialization failed`) and it exists only while the AG
# link is up, so nothing downstream could open it by name and survive. It is a
# LOOPBACK, written by `wall-bt-call`, in exactly the shape `mic_clean` already
# has -- which is what lets the mic legs keep opening one name and nothing
# else in the graph learn what a radio is.
MIC_SOURCE_BT = "mic_bt"
MIC_SOURCES = (MIC_SOURCE_PANEL, MIC_SOURCE_HEADSET, MIC_SOURCE_CLEAN,
               MIC_SOURCE_BT)

# ── which thing the Headset position resolves to (B4, Owner ruling Q5) ──────
# "Bluetooth is automatic and has priority over USB, even if USB is plugged
# back in. When the bluetooth headset is not connected, always attempt to
# resolve to USB. The headset icon would be red if both the USB and bluetooth
# are not connected."  (Owner, 2026-09-19.)
#
# So Headset is no longer a synonym for "the USB adapter". It is a POSITION
# that resolves, and these are the three answers. The switch still has three
# positions and the glass is unchanged -- WSN-024's closing line holds, because
# a Bluetooth headset is a resolution of Headset and never a fourth pill.
HEADSET_VIA_BLUETOOTH = "bluetooth"
HEADSET_VIA_USB = "usb"
HEADSET_VIA_NONE = None

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
    # ── the Bluetooth headset, as a SECOND presence (B4/B5) ─────────────────
    # Whether a trusted Bluetooth device that can be a headset -- it advertises
    # the hands-free UNIT role, `bluetooth_state.CAPABILITY_HF` -- is connected
    # right now. It is a separate field from `headset_present` and not a widened
    # one, because the two are independently true: the Owner's ruling is a
    # PRIORITY between them, and a priority needs both terms.
    #
    # `wall-bluetooth-device observe` is what reports it, on the same ten-second
    # cadence it already keeps the inventory fresh on, through the same
    # `wall-audio-output headset` verb the USB adapter's udev unit uses.
    "bt_headset_present": False,
    # The one-shot latch for the Bluetooth side, with the same life as the USB
    # one and kept SEPARATE for the same reason the presences are: a headset
    # connecting must be able to flip Speaker -> Headset once even if a USB plug
    # already spent its own latch this boot, and vice versa. Sharing one latch
    # would make the second arrival of the day silent.
    "bt_headset_autoswitch_armed": True,
    # The last broker request applied, for deduplication. systemd.path fires on
    # every close-write, so an unchanged request file re-read after a restart
    # would otherwise replay a rocker press. It lives in the state rather than
    # in /run because the applier must be able to answer "have I done this one"
    # before /run has been written even once.
    "request_seq": -1,
    # Physical rocker readout event, including repeated presses at the bounds.
    "volume_event_seq": 0,
    # THE COMPARE-AND-SET TOKEN for a guarded absolute volume target
    # (PANEL_VOLUME_ROCKER_RESPONSIVENESS_PLAN_2026-09-18). None of the three
    # counters already here is this token, and the plan says so field by field:
    # `generation` is a backend-life epoch and does not move within a life,
    # `request_seq` counts BROKER requests and so misses the rocker, a udev add
    # and the root CLI, and `volume_event_seq` counts rocker readouts and so
    # misses an output change. A rocker gesture that captured a baseline and
    # then applied half a second later has to be able to ask "is the audible
    # state still the one I measured" -- and speaker -> headset -> speaker is
    # the case where every visible value agrees and the answer is still no.
    #
    # SO IT COVERS EXACTLY THE AUDIBLE POLICY (see _policy_fingerprint): the
    # selected output, the input mute, the per-output level memory and the
    # adapter's presence. Deliberately NOT request_seq, generation,
    # volume_event_seq or mic_legs_running: those move for bookkeeping and for
    # observations, and a gesture refused because the applier re-recorded an
    # observation would be a rocker that silently stops working under load.
    "state_revision": 0,
    # THE BACKEND EPOCH (contract 2026-09-14, section 1.1). `request_seq` orders
    # requests INSIDE one life of the applier; it cannot order across a restart,
    # because a state file that is replaced, repaired or rolled back can move the
    # high-water mark BACKWARDS -- and then a request the applier already refused
    # becomes indistinguishable from a fresh one, in silence.
    #
    # So the applier also stamps an epoch. It lives HERE, in the durable state,
    # so it is monotonic across reboots; it is advanced by exactly one thing,
    # `apply-state` finding no epoch marker in /run (which is tmpfs, so: once per
    # boot). Nothing compares a `request_seq` across a change in this number.
    "generation": 0,
    # ITEM J'S OBSERVATION. Written by the applier AFTER a pass, not before:
    # every other field here is an intention, and this one is evidence. True
    # means a mic leg is (or may still be) transmitting. It defaults to True
    # because "we have never looked" must not read as "we checked and it is
    # silent" -- the UI's `inputMutedConfirmed` is derived from it, and an
    # unconfirmed mute shown as confirmed is the one error in this design that
    # nobody in the room can hear.
    "mic_legs_running": True,
    "version": STATE_VERSION,
}


class StateError(ValueError):
    """A request or a stored state that policy refuses. Never swallowed."""


# The four refusal reasons a guarded apply may report, and the only four. The
# preview protocol's `refused` terminal carries one of these verbatim, and the
# renderer accepts no other spelling, so they are named once here rather than
# written out at each boundary.
REFUSE_STATE_CHANGED = "state-changed"
REFUSE_APPLY_FAILED = "apply-failed"
REFUSE_UNAVAILABLE = "unavailable"
REFUSE_AMBIGUOUS = "ambiguous"
REFUSAL_REASONS = (REFUSE_STATE_CHANGED, REFUSE_APPLY_FAILED,
                   REFUSE_UNAVAILABLE, REFUSE_AMBIGUOUS)


class GuardMismatch(StateError):
    """A guarded target whose compare-and-set did not hold.

    A StateError subclass so that every existing `except StateError` keeps
    refusing it, and a distinct type so the one caller that must report WHICH
    guard failed can ask instead of parsing a sentence.
    """

    def __init__(self, message, reason=REFUSE_STATE_CHANGED):
        super().__init__(message)
        self.reason = reason


def _policy_fingerprint(state):
    """The part of the state `state_revision` is a revision OF.

    Everything audible and nothing else: which output is selected, whether the
    microphone is muted, what level each levelled output remembers, and whether
    the headset adapter is there to be selected. See STATE_SCHEMA's note on
    `state_revision` for why the counters are excluded.
    """
    # THE BLUETOOTH PRESENCE IS IN HERE FOR THE SAME REASON THE USB ONE IS.
    # A rocker gesture that captured a baseline in Headset-over-USB and applied
    # after a Bluetooth headset connected is aimed at a different card with a
    # different level; every other visible value agrees and the answer is still
    # no. Leaving it out would be the speaker -> headset -> speaker hole again,
    # in the one costume B4 adds.
    return (state["output"], state["input_muted"], state["headset_present"],
            state["bt_headset_present"],
            tuple(state["volume"].get(output) for output in LEVELLED_OUTPUTS))


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

    ONE FIELD IS NOT LIKE THE OTHERS, AND IT HAS ITS OWN RULE. Landing on the
    default for `output`, the volume or the latch is harmless. Landing on the
    default for `input_muted` -- False -- means a damaged or truncated file has
    turned a privacy control from muted to live, with nothing in the room to
    hear it happen.

    So: **the microphone is muted unless this document explicitly carries the
    boolean `false`.** An absent key is NOT treated as a fresh file taking the
    schema default, because a truncated write is indistinguishable from one; the
    earlier version of this rule made that mistake and review found it. A
    document that needed any OTHER repair comes back muted too, which is what
    catches a file that says `false` while being damaged elsewhere.

    The cost is one button press after a state file is damaged. The alternative
    is a microphone opened by a partial write.

    Inputs:  raw: whatever json.load returned (any type)
    Outputs: a dict satisfying STATE_SCHEMA's shape
    Implements: SR-028, LLR-013
    """
    state = default_state()
    if not isinstance(raw, dict):
        # Not even a document. Fall back to the whole default EXCEPT the one
        # field whose default is not the safe answer: see the rule below.
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
    # THE ONE RULE, AFTER FOUR REVIEW ROUNDS FOUND FOUR WAYS ROUND THE LAST
    # ONE: the microphone is MUTED unless this document explicitly says the
    # boolean false. Not "unless it says true"; not "unless a field was
    # repaired" -- both of those left holes, because a TRUNCATED write and an
    # unreadable file are indistinguishable from a fresh one if absence is
    # treated as consent. `{}` and `{"output": "speaker"}` are now muted.
    #
    # Round 3's rule (any repair mutes) is kept as well: it is what catches a
    # document that says `false` while being damaged elsewhere.
    state["input_muted"] = True
    if isinstance(raw.get("input_muted"), bool):
        state["input_muted"] = raw["input_muted"]
    else:
        repaired = True
    if isinstance(raw.get("headset_present"), bool):
        state["headset_present"] = raw["headset_present"]
    elif "headset_present" in raw:
        repaired = True
    if isinstance(raw.get("headset_autoswitch_armed"), bool):
        state["headset_autoswitch_armed"] = raw["headset_autoswitch_armed"]
    elif "headset_autoswitch_armed" in raw:
        repaired = True
    # THE BLUETOOTH PRESENCE DEFAULTS TO FALSE AND AN ABSENT KEY IS NOT A
    # REPAIR. A state file written before B4 has neither key, and a panel whose
    # first apply after the upgrade declared its state damaged would come back
    # with the microphone muted for no reason anybody could see. Absent means
    # "this file predates the field", which is a true and harmless answer: the
    # observer republishes the real presence within ten seconds, and False in
    # the meantime resolves Headset to USB, which is what the panel did
    # yesterday. A key that is PRESENT and the wrong type is still a repair.
    if isinstance(raw.get("bt_headset_present"), bool):
        state["bt_headset_present"] = raw["bt_headset_present"]
    elif "bt_headset_present" in raw:
        repaired = True
    if isinstance(raw.get("bt_headset_autoswitch_armed"), bool):
        state["bt_headset_autoswitch_armed"] = raw["bt_headset_autoswitch_armed"]
    elif "bt_headset_autoswitch_armed" in raw:
        repaired = True
    seq = raw.get("request_seq")
    if isinstance(seq, int) and not isinstance(seq, bool) and seq >= -1:
        state["request_seq"] = seq
    elif "request_seq" in raw:
        repaired = True
    # The epoch. A file written by an applier that predates the contract has no
    # `generation` at all, and that is NOT a repair: it is an older-but-honest
    # document, and treating it as damage would mute the microphone on every
    # panel the first time the new applier reads the old file. An epoch that is
    # PRESENT and malformed is damage like any other.
    # The observation. Absent is an older file, not damage -- but it still reads
    # as True, because the default IS the cautious answer here and an absent
    # observation is exactly "we have never looked".
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
    # The CAS token. ABSENT IS NOT DAMAGE, for the same reason `generation`
    # absent is not: a state file written by an applier that predates this
    # field is older-but-honest, and treating it as damage would mute the
    # microphone on every panel the first time the new applier reads it. It
    # reads as 0, and the first accepted mutation moves it off 0 -- so a
    # gesture whose baseline was taken against the absent field is still
    # refused the moment anything actually changes.
    revision = raw.get("state_revision", 0)
    if isinstance(revision, int) and not isinstance(revision, bool) and 0 <= revision <= 9007199254740991:
        state["state_revision"] = revision
    elif "state_revision" in raw:
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
    # ITEM J AT THE RECOVERY BOUNDARY. Every caller normalizes before it decides
    # anything -- `apply_event` does it, the applier does it on load, the broker
    # mirrors it -- so this one line is what makes the coupling hold for the
    # paths that are not requests at all: boot, resume, a udev event, a rolled
    # back state file, a document written by an applier that predates item J.
    # Without it a panel could come up in Mute with a live microphone and no
    # request would ever be made to notice.
    if state["output"] == "mute":
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


def headset_via(state):
    """What the Headset position resolves to RIGHT NOW: bluetooth, usb or None.

    THE OWNER'S RULING IS A PRIORITY AND THIS IS THE WHOLE OF IT (Q5,
    2026-09-19): "Bluetooth is automatic and has priority over USB, even if USB
    is plugged back in. When the bluetooth headset is not connected, always
    attempt to resolve to USB. The headset icon would be red if both the USB
    and bluetooth are not connected."

    So Bluetooth wins whenever it is there -- unconditionally, with no latch and
    no memory of which arrived first. That is deliberately simpler than the USB
    auto-switch, which needs a latch because it MOVES the switch; this only
    decides what the switch already points at, and a rule with a memory in it
    would make "which headset am I on" unanswerable from the panel's face.

    None is the red-icon case, and it is the only one: Headset with neither.

    ONE FUNCTION, called by `audible`, `mic_source`, `plan` and the shell. The
    priority must not be spelled twice -- an applier that resolved output one
    way and the microphone another would put the room's music in one headset
    and somebody's voice in the other, which is the precise failure `mic_source`
    already documents for the USB case.
    """
    if state["bt_headset_present"]:
        return HEADSET_VIA_BLUETOOTH
    if state["headset_present"]:
        return HEADSET_VIA_USB
    return HEADSET_VIA_NONE


def audible(state):
    """Whether ANY output should be carrying audio right now.

    False in `mute`, and false for `headset` while the position resolves to
    NEITHER a Bluetooth headset nor the USB adapter -- Owner ruling 7 as
    amended by Q5: silence on every output, nothing tunnelled, nothing to the
    speakers, until the user presses the switch. The red icon is the UI's
    rendering of exactly this being false while the output is `headset`.
    """
    if state["output"] == "mute":
        return False
    if state["output"] == "headset":
        return headset_via(state) is not None
    return True


def unavailable_reason(state):
    """Why the selected output is silent, for the UI's red icon, or None.

    THE TOKEN IS UNCHANGED ON PURPOSE. `headset_absent` is what the chrome,
    the broker and three test files already read, and its MEANING widens from
    "the USB adapter is not plugged in" to "neither headset resolves" without
    any of them having to learn a second word. The Owner's ruling asks for one
    red icon, not two.
    """
    if state["output"] == "headset" and headset_via(state) is None:
        return "headset_absent"
    return None


def mic_source(state, aec_available=False):
    """Which capture device the mic legs read. Follows the OUTPUT position.

    Item 23 D3 (Headset) routes "that jack's mic back to the mic input"; D4
    (Speaker) routes "the panel's internal mic". So the selection is a
    consequence of the output switch and is not a control of its own -- there is
    no fourth button on the glass, and the Owner never asked for one.

    In `mute` the panel mic is NAMED, and it is never opened: item J couples the
    output Mute position to the input mute, so `mic_live` is false there and no
    leg is started at all. The name still has to be a valid one, because it is
    published into /run for the legs' `@func getenv` whether or not they run,
    and `mute` names no output device to take a headset mic from.

    ITEM J SUPERSEDES OWNER RULING E HERE, and the supersession is the whole
    point of the item: ruling E said the output Mute position does NOT mute the
    microphone ("the mic has its own mute"), and the Owner's 2026-09-14 ruling
    replaces that. The mic button still exists and is still separate; what has
    changed is that selecting output Mute now also mutes the input.
    """
    # THE HEADSET POSITION TAKES THE MICROPHONE OF WHICHEVER HEADSET IT
    # RESOLVED TO (B3/B4). It has to be the same resolution `plan` sends the
    # OUTPUT to, or the far end of whatever is listening hears a different
    # room from the one the music is in.
    #
    # `mic_bt` IS SILENT UNLESS A BRIDGE IS UP, AND THAT IS NOT A DEFECT. A
    # Bluetooth headset cannot carry A2DP and SCO at once -- the profile, not
    # this panel -- so taking its microphone would drop the room's music to
    # 8 kHz mono for as long as the mic was open. `wall-bt-call` therefore
    # raises the AG link only while it is bridging a gateway's call, and feeds
    # `mic_bt` silence the rest of the time so that nothing downstream stalls
    # on a loopback with no writer. What reaches the desktop's line input in
    # the Headset-over-Bluetooth position is therefore silence unless a call
    # is up; naming `mic_panel` there instead would tunnel a microphone the
    # Owner believes is switched away from, which is the one failure in this
    # design nobody could hear happening (ruling 7).
    via = headset_via(state)
    if state["output"] == "headset" and via == HEADSET_VIA_BLUETOOTH:
        return MIC_SOURCE_BT
    if state["output"] == "headset" and via == HEADSET_VIA_USB:
        return MIC_SOURCE_HEADSET
    # THE CANCELLER IS ONLY WORTH INSERTING IN SPEAKER. It removes the ROOM's
    # own music from the microphone, and the room only has music in Speaker --
    # in Mute nothing is playing (and the mic is coupled off anyway, item J),
    # and in Headset the sound is in somebody's ears, not in the air. Putting a
    # canceller in the path there would be a filter with a silent reference,
    # which is a slightly worse microphone for no benefit at all.
    if aec_available and state["output"] == "speaker":
        return MIC_SOURCE_CLEAN
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
    # ITEM J. Selecting output Mute mutes the microphone too. In practice this
    # line is belt to the braces of `normalize` and `_set_output`, which both
    # latch `input_muted` true in the mute position -- but it is the line that
    # makes the coupling true of a state dict from ANY source, including one a
    # future caller builds by hand, and it costs nothing.
    if state["output"] == "mute":
        return False
    if state["output"] == "headset" and headset_via(state) is None:
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
                 "nudge_volume"   {"louder": bool, "step": int optional}
                                  -- a rocker movement; "step" is its size in
                                  percent, defaulting to VOLUME_STEP (one press)
                 "snap_volume"    {"to": int optional} -- round the level to the
                                  nearest multiple of "to" (default VOLUME_SNAP);
                                  sent once when the rocker is released
                 "set_volume_guarded"
                                  {"target": int, "expect_output": str,
                                   "expect_level": int, "expect_revision": int}
                                  -- one absolute level, applied only while the
                                  state it was computed on still holds
                 "headset"        {"present": bool}}
    Outputs: (new_state, lines)
    Raises:  StateError if the event is unknown or malformed
    Implements: SR-028, LLR-013
    """
    kind = event.get("kind")
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise StateError("unknown event kind")
    working = normalize(state)
    # SNAPSHOT, NOT A SECOND REFERENCE. Every handler mutates the dict it is
    # given and returns that same object, so keeping `working` around and
    # diffing it afterwards would compare a state with itself -- which is
    # always equal, and the revision would never move. The fingerprint is a
    # tuple of scalars, so taking it now is the whole snapshot.
    before = _policy_fingerprint(working)
    result, lines = handler(working, event)
    # THE REVISION IS BUMPED HERE AND NOWHERE ELSE, and that is what makes it
    # impossible to forget. Every handler above is written as "change the
    # fields"; none of them has to remember to also move a counter, and a
    # handler added next year gets the bump for free. The diff is against the
    # NORMALIZED input, so a repair applied on the way in counts as the change
    # it is -- a state file that came back rolled back must not let a gesture
    # whose baseline predates the rollback compare equal.
    if _policy_fingerprint(result) != before:
        result["state_revision"] = (result["state_revision"] + 1) % 9007199254740992
    return result, lines


def _set_output(state, event):
    output = event.get("output")
    if output not in OUTPUTS:
        raise StateError("output must be one of %s" % (", ".join(OUTPUTS),))
    if state["output"] == output:
        # ALREADY THERE IS STILL A COUPLING POINT. A state that says `mute` with
        # a stored `input_muted: false` should not exist -- `normalize` latches
        # it -- but a deduplicated request must not be the one path that leaves
        # it standing if it ever does.
        if output == "mute" and not state["input_muted"]:
            state["input_muted"] = True
            return state, ["output already mute; input mute re-asserted (item J)"]
        return state, ["output already %s" % output]
    previous = state["output"]
    state["output"] = output
    lines = ["output %s -> %s (level %d%%)" % (previous, output, volume_of(state))]
    # ── ITEM J: SELECTING MUTE LATCHES THE INPUT MUTE ─────────────────────────
    # The Owner's 2026-09-14 ruling has two halves and they are only consistent
    # if the coupling is a LATCH on the stored flag rather than a mask over it:
    #
    #   (a) "Selecting speaker/output Mute also mutes microphone transmission."
    #   (b) "No inferred requirement to unmute the microphone when leaving
    #        output Mute: default to retaining its muted state until an explicit
    #        microphone-unmute action."
    #
    # A mask -- leaving `input_muted` alone and hiding it while in mute -- gives
    # (a) and breaks (b): leaving mute would restore the unmuted flag
    # underneath and the microphone would come back live without anyone asking
    # for it, which is the one failure in this design nobody in the room can
    # hear. So entering mute WRITES the flag, and leaving mute writes nothing.
    #
    # THE CONSEQUENCE, STATED PLAINLY BECAUSE IT IS A REAL COST: a person who
    # was on Speaker with the microphone live, taps Mute, then taps Speaker
    # again, comes back with the microphone MUTED and must press the mic button
    # to get it back. That is what (b) asks for. It is an implementation
    # interpretation recorded for Owner review, not a previously ratified rule.
    if output == "mute" and not state["input_muted"]:
        state["input_muted"] = True
        lines.append("input mute on (coupled to the output Mute position, item J)")
    elif previous == "mute" and state["input_muted"]:
        lines.append("input stays muted after leaving Mute until an explicit "
                     "unmute (item J)")
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
    # ITEM J: AN INDEPENDENT UNMUTE IS HELD WHILE OUTPUT MUTE IS SELECTED.
    # REFUSED, not silently dropped: the plan requires the coupling to be
    # unbypassable, and a request that changed nothing while answering
    # "accepted" would leave the chrome painting a live microphone over a dead
    # one. A StateError reaches the caller as a refusal the UI can say out loud,
    # and `input_mute_held` in the plan is what lets it say WHY.
    if muted is False and state["output"] == "mute":
        raise StateError("input unmute is held while the output switch is in "
                         "mute; move the output switch first")
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
    # OPTIONAL MAGNITUDE, defaulting to the historic single press. The rocker
    # daemon needs this because ONE APPLY COSTS ABOUT HALF A SECOND end to end
    # (measured 2026-09-15: 498-551 ms for a socket round trip through the
    # Accept=yes unit). A ramp built from fixed VOLUME_STEP presses therefore
    # cannot exceed ~8%/s no matter how fast the key repeats, and every press
    # beyond that rate just queues -- which is exactly the bug this parameter
    # exists to fix (the level kept falling for tens of seconds after release
    # while the backlog drained). Sizing ONE request by the time actually held
    # keeps the ramp on its 25%/s promise with ~2 applies a second.
    step = event.get("step", VOLUME_STEP)
    if isinstance(step, bool) or not isinstance(step, int):
        raise StateError("step must be an integer percentage")
    if not 1 <= step <= VOLUME_MAX:
        raise StateError("step must be 1..%d" % VOLUME_MAX)
    level = volume_of(state)
    # WSN-062: the opening movement repairs an old off-grid value in the
    # direction travelled. Later planner requests are whole increments, with
    # their fractional time debt carried upstream rather than rounded away.
    if level % VOLUME_SNAP:
        target = ((level + VOLUME_SNAP - 1) // VOLUME_SNAP) * VOLUME_SNAP if louder \
            else (level // VOLUME_SNAP) * VOLUME_SNAP
    else:
        target = level + (step if louder else -step)
    state["volume_event_seq"] = (state["volume_event_seq"] + 1) % 9007199254740992
    return _store_volume(state, clamp_volume(target))


def _set_volume_guarded(state, event):
    """One ABSOLUTE level, applied only if the state it was computed on still holds.

    THE REASON THIS IS NOT `set_volume`. The rocker daemon computes its target
    from a baseline it read up to half a second ago -- one privileged apply
    costs 498-551 ms -- and in that window the Owner can have moved the switch,
    a headset can have enumerated, and the on-glass slider can have set a level
    of its own. An unguarded absolute write would then land the OLD gesture's
    arithmetic on the NEW world: the plan's speaker -> headset -> speaker round
    trip, where every visible value agrees again and the level is still wrong.
    So the caller states the world it measured and this refuses if it moved.

    THE GUARD IS THE REVISION PLUS THE VALUES, not either alone. The revision
    catches a round trip that ends where it started; the output and level
    catch a state file that was replaced or rolled back under a revision that
    happens to match. Requiring both costs nothing and neither is redundant.

    Inputs:  target:          0..100, the absolute level to land on
             expect_output:   the output the caller measured
             expect_level:    that output's level when the caller measured it
             expect_revision: `state_revision` when the caller measured it
    Raises:  GuardMismatch (reason `state-changed`) if any guard moved;
             GuardMismatch (reason `unavailable`) if the measured output has
             no level to set -- `mute`, or a headset with no adapter.
    """
    target = event.get("target")
    if not isinstance(target, int) or isinstance(target, bool):
        raise StateError("target must be an integer percentage")
    if not VOLUME_MIN <= target <= VOLUME_MAX:
        raise StateError("target must be %d..%d" % (VOLUME_MIN, VOLUME_MAX))
    expect_output = event.get("expect_output")
    if expect_output not in OUTPUTS:
        raise StateError("expect_output must be one of %s" % (", ".join(OUTPUTS),))
    expect_level = event.get("expect_level")
    if not isinstance(expect_level, int) or isinstance(expect_level, bool):
        raise StateError("expect_level must be an integer percentage")
    expect_revision = event.get("expect_revision")
    if not isinstance(expect_revision, int) or isinstance(expect_revision, bool) \
            or expect_revision < 0:
        raise StateError("expect_revision must be a non-negative integer")
    if state["state_revision"] != expect_revision:
        raise GuardMismatch(
            "guard failed: state_revision is %d, not the %d this target was "
            "computed on" % (state["state_revision"], expect_revision))
    if state["output"] != expect_output:
        raise GuardMismatch(
            "guard failed: output is %s, not the %s this target was computed on"
            % (state["output"], expect_output))
    # `mute` HAS NO LEVEL TO GUARD OR TO SET, and that is a different answer
    # from "the world moved": nothing changed, the position simply is not a
    # level control. It is `unavailable` so the overlay retires to the
    # confirmed truth instead of reporting a race that did not happen.
    if expect_output not in LEVELLED_OUTPUTS:
        raise GuardMismatch("output %s has no level" % expect_output,
                            reason=REFUSE_UNAVAILABLE)
    if volume_of(state) != clamp_volume(expect_level):
        raise GuardMismatch(
            "guard failed: %s is at %d%%, not the %d%% this target was computed on"
            % (expect_output, volume_of(state), clamp_volume(expect_level)))
    # THE READOUT COUNTER STILL MOVES AT A BOUND. A gesture pressed against
    # 0% or 100% changes no level, and the wall must still acknowledge the
    # press -- that is what `volume_event_seq` has always been for, and the
    # guarded path must not be the one that stops feeding it.
    state["volume_event_seq"] = (state["volume_event_seq"] + 1) % 9007199254740992
    return _store_volume(state, target)


def _snap_volume(state, event):
    """Safety-net an abnormal old level onto the grid; normal gestures need it not.

    The rocker sent one of these when the Owner let go (Owner ruling,
    2026-09-15: "I'd like the volume to land at / round to 5% increments after
    a release"). It runs ONCE per gesture, at the end -- NOT on every ramp step.
    It was right that independently rounding 13%-ish time samples loses up to
    2.5% twice a second. That is not this design: RampPlanner carries the
    fractional time debt and releases only complete 5% increments, so no error
    is discarded. A normal gesture is already on-grid and this is a no-op.

    Nearest, not "further in the direction travelled": after the first gesture
    the level is already on the grid, so every later tap moves exactly one
    increment and the choice stops mattering. Nearest only shows up once, when
    coming from an off-grid level, and there it is the smaller surprise.
    """
    to = event.get("to", VOLUME_SNAP)
    if isinstance(to, bool) or not isinstance(to, int):
        raise StateError("snap increment must be an integer")
    if not 1 <= to <= VOLUME_MAX:
        raise StateError("snap increment must be 1..%d" % VOLUME_MAX)
    level = volume_of(state)
    target = clamp_volume(int(round(level / float(to))) * to)
    if target != level:
        state["volume_event_seq"] = (state["volume_event_seq"] + 1) % 9007199254740992
    return _store_volume(state, target)


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
    # WHICH HEADSET THIS REPORT IS ABOUT (B5). Absent means `usb`, because every
    # caller that predates B5 -- the udev presence unit, `apply-state`'s
    # coldplug report, a bare CLI `headset add` -- is talking about the USB
    # adapter, and a default that silently retargeted them at the radio would
    # be the worst kind of upgrade.
    via = event.get("via", HEADSET_VIA_USB)
    if via not in (HEADSET_VIA_USB, HEADSET_VIA_BLUETOOTH):
        raise StateError("via must be usb or bluetooth")
    bluetooth = via == HEADSET_VIA_BLUETOOTH
    presence_key = "bt_headset_present" if bluetooth else "headset_present"
    latch_key = ("bt_headset_autoswitch_armed" if bluetooth
                 else "headset_autoswitch_armed")
    # THE NOUN, so a journal a week later says which radio or cable moved. The
    # two reports are otherwise indistinguishable in the log, and they now have
    # genuinely different consequences.
    noun = "bluetooth headset" if bluetooth else "headset adapter"
    lines = []
    changed_presence = state[presence_key] != present
    if boot:
        state[presence_key] = present
        # Present across the boot => no pending event; absent => the next add
        # is a real one. Either way the OUTPUT is not touched: ruling G wins.
        state[latch_key] = not present
        lines.append("%s %s at boot: recorded, no auto-switch "
                     "(output stays %s)"
                     % (noun, "present" if present else "absent", state["output"]))
        if changed_presence and not present and state["output"] == "headset":
            lines.append(_headset_left_selected(state))
        return state, lines
    if changed_presence:
        lines.append("%s %s" % (noun, "present" if present else "absent"))
    state[presence_key] = present
    if not present:
        state[latch_key] = True
        if state["output"] == "headset":
            lines.append(_headset_left_selected(state))
        return state, lines or ["%s absent (no change)" % noun]
    if not changed_presence:
        # NOTHING ARRIVED. The applier already believed this adapter was there,
        # so whatever produced this report -- a `udevadm trigger`, the presence
        # unit being restarted, a re-assert after resume, the Bluetooth
        # observer's ten-second republish -- is not a plug, and the ruling is
        # that only a plug may move the switch. The latch is left exactly as it
        # was, because spending it here would eat the next real plug.
        # (Review, 2026-09-13.)
        #
        # THIS LINE IS WHAT MAKES THE BLUETOOTH OBSERVER SAFE TO RUN EVERY TEN
        # SECONDS. It reports presence, not arrival -- it has no way to tell
        # them apart -- so all but the first report of a connected headset lands
        # here and does nothing at all.
        return state, lines + ["%s already present: no arrival to act on" % noun]
    armed = state[latch_key]
    state[latch_key] = False
    if armed and state["output"] == "speaker":
        state["output"] = "headset"
        lines.append("auto-switch speaker -> headset (one-shot, %s, level %d%%)"
                     % (via, volume_of(state)))
    else:
        # Spent latch, or the Owner is in mute/headset already: say why nothing
        # moved, so an Owner who expected a switch can see the reason.
        lines.append("no auto-switch (output %s, latch %s)"
                     % (state["output"], "armed" if armed else "spent"))
    return state, lines


def _headset_left_selected(state):
    """The line for a presence report that left Headset pointing at something.

    ONE OF TWO SENTENCES, AND THEY ARE DIFFERENT FACTS (B4). Before the
    Bluetooth resolution existed, a headset going away while Headset was
    selected always meant silence; now it usually means the OTHER headset
    takes over, and a journal that still said "every output silent" would send
    the next person looking for a fault that is not there.
    """
    via = headset_via(state)
    if via is None:
        return ("headset still selected: every output silent until the "
                "switch is moved")
    return "headset still selected: resolved to %s" % via


_HANDLERS = {
    "set_output": _set_output,
    "set_input_mute": _set_input_mute,
    "set_volume": _set_volume,
    "nudge_volume": _nudge_volume,
    "snap_volume": _snap_volume,
    "set_volume_guarded": _set_volume_guarded,
    "headset": _headset,
}


# The forwarder units the switch owns. Named once, here, because the plan, the
# applier and the tests must agree on the set and its order (stop before start).
SPEAKER_LEGS = ("wall-bus-speaker.service", "wall-speaker-out.service")
# THE HEADSET POSITION IS TWO LEGS AND AT MOST ONE OF THEM RUNS (B4). Which one
# is `headset_via`'s answer; the name `HEADSET_LEGS` is kept for the set of
# units the position owns, because the applier, the tests and `ALL_LEGS` all
# need "everything Headset may start" as one tuple.
HEADSET_USB_LEGS = ("wall-bus-headset.service",)
# A UNIT OF ITS OWN, NOT `wall-bus-bluetooth.service`, AND THE PLAN SAID
# OTHERWISE. §13 B4 proposed reusing the WSN-024 route leg, and that would give
# two owners to one unit and one environment file: `wall-bluetooth-device`
# restarts it on every `select_output`, and the applier would start and stop it
# on every switch move. Every out-of-sync defect in this panel's access chain so
# far has been an overloaded name rather than a race, so the two facts get two
# units. The cost is stated rather than discovered: selecting a Bluetooth
# SPEAKER as an explicit route while Headset resolves to a Bluetooth HEADSET
# runs both legs, which is exactly what WSN-024 already says a route leg does
# ("it runs BESIDE whichever position the switch is in").
HEADSET_BT_LEGS = ("wall-bus-bt-headset.service",)
HEADSET_LEGS = HEADSET_USB_LEGS + HEADSET_BT_LEGS
# The mic legs (step 4). They are listed SEPARATELY from the output legs because
# they are governed by a different control -- `input_muted`, Owner ruling E --
# and because one of them is a supervisor rather than a forwarder:
#
#   wall-mic-rear   mic_selected -> the adapter's REAR pair, which item 23
#                   revision 2 wires to the desktop's audio input.
MIC_LEGS = ("wall-mic-rear.service",)
# THE CALL SUPERVISOR IS NOT A MIC LEG, and it stopped being one when it grew
# the far-end direction (A3, 2026-09-19). It was in MIC_LEGS, which keys on
# `mic_live`, so muting the microphone stopped the whole unit -- and the unit
# now also carries the OTHER person's voice onto the bus. Muting your
# microphone must not silence the person you are listening to; that is what a
# muted headset does, and it is WSN-027 read with §20a of the call plan.
#
# So it keys on `audible` instead: output Mute (or Headset with no adapter)
# means the panel is silent and there is nothing for either direction to do,
# while Speaker and Headset keep the supervisor up. The INPUT mute is then
# enforced inside the process, which re-reads the switch state on every tick
# and stops the microphone leg alone. That is the tighter guarantee of the
# two -- a unit stop can be outrun by an operator restart, and a per-tick
# check cannot -- and it is what `wall-bt-call.service` argues at length.
CALL_LEGS = ("wall-bt-call.service",)
ALL_LEGS = SPEAKER_LEGS + HEADSET_LEGS + MIC_LEGS + CALL_LEGS


def plan(state, aec_available=False):
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
    via = headset_via(state)
    mic = mic_live(state)
    legs = {unit: speaker for unit in SPEAKER_LEGS}
    # EXACTLY ONE OF THE TWO, AND BOTH ARE NAMED EVERY TIME. Writing only the
    # winner would leave the loser's previous value in place -- the applier
    # stops what the plan says is False -- so a move from USB to Bluetooth would
    # leave the USB leg running and the room's music in two headsets.
    legs.update({unit: headset and via == HEADSET_VIA_USB
                 for unit in HEADSET_USB_LEGS})
    legs.update({unit: headset and via == HEADSET_VIA_BLUETOOTH
                 for unit in HEADSET_BT_LEGS})
    legs.update({unit: mic for unit in MIC_LEGS})
    # Not `mic`: see CALL_LEGS. The supervisor owns the microphone half of its
    # own behaviour, and stopping it for a mute would take the far end with it.
    legs.update({unit: live for unit in CALL_LEGS})
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
        # THE USB CARD'S OWN MUTE, AND IT FOLLOWS THE RESOLUTION AND NOT THE
        # POSITION (B4). Headset resolving to Bluetooth leaves the USB adapter
        # plugged in with no leg feeding it, so it is muted exactly as it is in
        # Speaker -- otherwise a card left unmuted would pass whatever alsactl
        # last restored into a headset hanging on the desk.
        "headset_muted": not (headset and via == HEADSET_VIA_USB),
        "input_muted": bool(state["input_muted"]),
        # Which capture PCM the mic legs open, published to them through
        # /run/wall-panel/audio-mic-source.env and read by `@func getenv`.
        "mic_source": mic_source(state, aec_available),
        "mic_live": mic,
        # WHAT HEADSET MEANS RIGHT NOW (B4), published so that nothing else has
        # to re-derive the priority: the applier writes it into
        # /run/wall-panel/audio-headset.env for the legs and for `wall-bt-call`,
        # and the broker carries it to the chrome so the Headset segment can
        # wear a Bluetooth mark. None in any position but Headset -- it is the
        # resolution OF a position, not a standing fact about the hardware.
        "headset_via": via if state["output"] == "headset" else None,
        # The same question asked without the switch: is a Bluetooth headset
        # there at all. The chrome needs this to explain the red icon, and
        # `wall-bt-call` needs it to know whether a bridge is even possible.
        "bt_headset_present": bool(state["bt_headset_present"]),
        # ── ITEM J, published so the UI never has to derive it ──────────────
        # What the microphone ACTUALLY is, which is the only thing the chrome
        # may draw. Identical to `input_muted` today, because the coupling is a
        # latch rather than a mask; it is published as its own name so that a
        # consumer reads the fact rather than reconstructing the rule, and so
        # that a future change of mechanism cannot silently change what the UI
        # shows.
        "input_muted_effective": bool(state["input_muted"]) or state["output"] == "mute",
        # WHY it is muted, for the refusal the UI has to explain. True means an
        # independent unmute is refused right now and the person must move the
        # output switch first.
        "input_mute_held": state["output"] == "mute",
        "reason": unavailable_reason(state),
    }
