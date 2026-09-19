#!/usr/bin/env python3
"""Drive the amplifier output volume from the panel's side rocker.

WHY THIS IS A DAEMON AND NOT A COMPOSITOR BINDING (2026-09-12):
the rocker already emits standard keycodes, but `wall-kiosk.sh` runs Electron
under cage from a tty autologin session, not from a unit, and cage has no
configurable key bindings. Reading evdev directly is compositor-independent and
keeps working if the shell is restarted, which `pkill -f runtime/electron` does
routinely.

WHICH DEVICE, corrected 2026-09-15: this docstring used to name `Intel Virtual
Buttons` as the source. It is not. That device (event11) declares KEY_VOLUMEUP
and KEY_VOLUMEDOWN and NEVER FIRES THEM; the side rocker is wired through the
keyboard controller and arrives on event5, `AT Translated Set 2 keyboard`.
Watching every device that declares the keys is what made this work regardless,
and is why the mistake went unnoticed -- but it sent a later reader (me) hunting
the wrong device's capabilities. See the event capture further down.

WHY IT CONTROLS THE ADAPTER AND NOT THE BUILT-IN CODEC: the amplifier is fed
from the adapter's line output, because the ALC255 headphone jack measured
31 dB noisier (see asound.conf). `Speaker` on the adapter is the last stage
before that output, so it is the only control that is a true overall volume for
the speakers. The adapter's *capture* gain must not be used for this — that
would degrade the signal before the passthrough rather than after it.

HOW IT WORKS SINCE 2026-09-19 (strategy B of
docs/PANEL_VOLUME_ROCKER_RESPONSIVENESS_PLAN_2026-09-18.md, Owner selected):

  * Every firmware BREAK pulse is exactly one 5% step. Not time held -- the
    firmware repeats make/break pairs at ~106 ms and there is no autorepeat to
    integrate, so the pulse COUNT is the specification. See PulsePlanner.
  * The input loop owns evdev and the preview and never blocks. One apply
    costs ~0.5 s, so the applies happen on ApplyActor's thread, bounded to one
    request in flight plus one replaceable absolute target. That is what lets
    the overlay advance at ~10 Hz while the room's volume moves in coalesced
    jumps at the backend's sustainable ~2 Hz.
  * What is applied is an ABSOLUTE target plus the mode, output, level and
    state revision it was computed on. The applier performs it as a
    compare-and-set under its own lock and answers with what landed, so a
    switch moved mid-gesture refuses the stale target rather than dragging
    whichever output is selected when the apply finally runs.
  * The overlay reads a schema-2 preview document this daemon publishes on the
    opening press and on every pulse, with an exact terminal (settled,
    refused or cancelled). See GesturePreview.

NO NEW DEPENDENCIES: the panel's installer is an offline, hash-locked
wheelhouse, so this parses struct input_event itself rather than importing
python-evdev.

Exit codes: 69 no readable volume-key device, 77 not permitted to read one.
"""

import glob
import json
import os
import queue
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

try:
    import fcntl
except ImportError:  # not Linux: the tests run on the dev box, the panel does not
    fcntl = None

# struct input_event on 64-bit Linux: struct timeval (two longs) then
# __u16 type, __u16 code, __s32 value.
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
# ASSERTED ON LINUX ONLY, and deliberately so. On the panel (x86_64 Linux) a
# struct timeval is two 8-byte longs and the whole record is 24 bytes; a wrong
# size there would silently misparse every event, so it stays a hard failure.
# But this assertion also made the module IMPOSSIBLE TO IMPORT on a developer
# machine whose `long` is 4 bytes, which forced the tests to assert against the
# source TEXT rather than the behaviour. PulsePlanner and VirtualTarget below
# are the gesture contract and are worth testing properly off-panel, so the
# guard is scoped to where it is meaningful rather than dropped.
if sys.platform.startswith("linux"):
    assert EVENT_SIZE == 24

EV_KEY = 0x01
KEY_MUTE = 113
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115

KEY_RELEASE, KEY_PRESS, KEY_AUTOREPEAT = 0, 1, 2

# WHAT THIS ROCKER ACTUALLY EMITS -- captured from the hardware 2026-09-15,
# because guessing it wrong is what produced two different wrong designs:
#
#   8.585  VOLUMEDOWN PRESS          <- a tap is ONE pair...
#   8.592  VOLUMEDOWN RELEASE  +7ms     ...and then silence
#  10.624  VOLUMEDOWN PRESS          <- a hold is the SAME pair,
#  10.632  VOLUMEDOWN RELEASE  +8ms     repeating every ~100 ms
#  10.731  VOLUMEDOWN PRESS   +99ms
#  10.738  VOLUMEDOWN RELEASE  +7ms
#   ... 28 pairs over a 2.8 s hold ...
#
# Three things follow, and all three are counter-intuitive:
#
# 1. THE ROCKER IS event5, the `AT Translated Set 2 keyboard` -- the side button
#    is wired through the keyboard controller. `Intel Virtual Buttons` (event11)
#    declares the keycodes but never fires them. Watching every device that
#    declares volume keys is what makes this work regardless.
# 2. THERE IS NO AUTOREPEAT. event5 sets EV_REP, but the driver does not use it:
#    no value-2 event is ever delivered. The repeat is whole press/release pairs
#    at about 10 Hz.
# 3. A RELEASE THEREFORE DOES NOT MEAN "LET GO". It means "let go, or about to
#    repeat in ~100 ms". Nothing can be concluded from one release alone, which
#    is why PulsePlanner ends a gesture on a QUIET GAP rather than on a
#    release -- while still charging each release, which IS a complete pulse.
#
# The original bug: the daemon applied one volume change per press event, inline,
# while one apply costs ~520 ms (measured: 498-551 ms, because Accept=yes spawns
# a transient unit and a fresh python3 per step). Presses arrived 5x faster than
# they could be applied, the surplus queued in the evdev buffer, and release was
# not handled at all -- so a 3 s hold spent ~15 s draining and the level kept
# falling long after the Owner let go. The journal caught it mid-drain:
# "4% -> 0%" and then "volume already 0%" four times over. That is the "stuck
# slider"; the on-glass overlay is read-only and was faithfully showing a queue.

# How long a gap with NO break pulse means the finger is really off. Must
# comfortably exceed the ~106-107 ms repeat period measured on 2026-09-18, or
# one hold would be chopped into several taps; every millisecond beyond that is
# pure detection latency, so it is not generous.
#
# IT COSTS NO VOLUME, and under the 2026-09-18 pulse rule that is a stronger
# statement than it used to be: movement is earned by BREAK PULSES, never by
# elapsed time, so the gap cannot add or subtract a single percent. It decides
# only when the gesture is declared over. Reducing it needs a longer jitter
# capture across both directions, load and resume (see the plan's "Quiet-gap
# choice"), so 250 ms is retained for this implementation.
HOLD_GAP_S = 0.250

# THE PANEL IS MOUNTED ROTATED relative to the way the rocker was labelled, so
# the button that sits physically uppermost on the wall is the one reporting
# KEY_VOLUMEDOWN. Reaching up to make it quieter is wrong in the only way a
# volume control can be wrong, so the two keycodes are swapped here. This is a
# property of how the panel hangs, not of the hardware: if it is ever remounted
# the right way up, set this back to False rather than rewiring anything.
SWAP_FOR_PANEL_ORIENTATION = True

# THE OWNER'S SPECIFICATION, AS AMENDED 2026-09-18 (strategy B):
#
#   every real firmware BREAK pulse is exactly one five-percent step.
#
# WHAT THAT REPLACED, AND WHY THE OLD SHAPE IS GONE RATHER THAN TUNED. Until
# 2026-09-18 this file carried three numbers -- a 5% tap, a 600 ms hold
# threshold and a 25%/s ramp -- and a RampPlanner that turned TIME HELD into
# percent. The 2026-09-18 kprobe capture at `serio_interrupt` showed why that
# was the wrong model: below atkbd, below evdev, the FIRMWARE ITSELF repeats
# make/break pairs every ~106-107 ms. There is no such thing as a continuously
# held rocker to integrate over -- there is a train of discrete pulses, and the
# only honest unit of "how much did the Owner ask for" is how many of them
# arrived. Counting pulses also makes the quiet gap free: extrapolated time
# after the last pulse is never spent, so the detection delay cannot move the
# volume.
#
# A tap shorter than the firmware's repeat delay carries one pair and moves 5%.
# A press held across that delay carries two or more and INTENTIONALLY moves
# 10% or more. There is no tap classifier and no threshold: the count is the
# specification. A sustained hold measured 9.3-10 pulses per second, so roughly
# 46-50 percentage points per second.
#
# The adapter's `Speaker` control spans a wide dB range (20% is already
# -29.6 dB), so percent steps, not absolute steps, are right.
PULSE_PERCENT = 5
# How often the input loop wakes while a gesture is open. The plan asks for
# 40-80 ms: often enough to notice the quiet gap promptly and to drain worker
# verdicts, and far enough below the ~106 ms pulse period that it never becomes
# the thing that paces the overlay. NEW EVIDENCE STILL ONLY ARRIVES ON A PULSE,
# so waking faster would publish the same number more times and nothing else.
LOOP_WAKE_S = 0.050
# The old `snap` is NOT in this file any more, and its absence is deliberate.
# It rounded the level to a 5% multiple on release, which under the pulse rule
# can move the volume with no break pulse behind it -- exactly what "every
# percent is a percent the Owner asked for" forbids. An off-grid starting level
# is repaired by the first pulse instead, which travels in the direction the
# Owner is pressing. See `VirtualTarget.step`.
# THE ROCKER'S TARGET FOLLOWS THE OUTPUT MODE. In trigger mode the amplifier is
# fed from the USB adapter; in panel mode the panel's own speaker is playing.
# Adjusting the wrong card is silent -- the rocker appears dead while actually
# moving a control nobody is listening to -- so the mode is read on every press
# rather than captured at startup, because `wall-audio-mode` does not restart
# this unit.
MODE_FILE = "/etc/wall-panel/audio-mode"
TARGETS = {
    "trigger": ("WALL_AUDIO_ADAPTER_CARD", "ICUSBAUDIO7D"),
    "panel": ("WALL_AUDIO_BUILTIN_CARD", "PCH"),
}
CONTROL = "Speaker"
VOLUME_KCONTROL = "Speaker Playback Volume"
# The volume control carries ONE VALUE PER CHANNEL, and `amixer sset` writes all
# of them. That is wrong here (measured 2026-09-12): the rear pair is pinned at
# 0 dB as the amplifier's trigger line, and a single press of the rocker dragged
# it from 197 back down to 24 with the front pair, silently disarming the
# trigger. Only the front pair may move, so this reads the control, changes
# indices 0 and 1, and writes every channel back.
FRONT_CHANNELS = (0, 1)


def current_mode():
    """trigger | panel | bus, read on every press.

    Not captured at startup: `wall-audio-mode` does not restart this unit, and
    adjusting the wrong card is silent -- the rocker appears dead while moving a
    control nobody is listening to.
    """
    try:
        with open(MODE_FILE) as fh:
            mode = fh.read().strip()
    except OSError:
        return "trigger"
    return mode if mode in ("trigger", "panel", "bus") else "trigger"


def target():
    """(card, simple control, kcontrol name) for the output currently in use."""
    mode = current_mode()
    key, fallback = TARGETS.get(mode, TARGETS["trigger"])
    return _card(key, fallback), CONTROL, VOLUME_KCONTROL

# Devices are matched by CAPABILITY, not by event number or name: there is no
# stable /dev/input/by-path symlink for these, and event numbers move when USB
# devices come and go. Any device declaring the volume keys is watched, so both
# the side rocker (event5) and an attached keyboard work.
WANTED_KEYS = {KEY_VOLUMEUP, KEY_VOLUMEDOWN, KEY_MUTE}


CARDS_FILE = "/etc/wall-panel/audio-cards.env"


def _card(key, default):
    """Read one card id from the generated card map.

    Card ids live in exactly one generated file so that swapping the USB adapter
    is a knob rather than an edit across the ALSA configs, the mode script and
    both daemons. The default is a fallback for a panel whose file predates it.
    """
    try:
        with open(CARDS_FILE) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value
    except OSError:
        pass
    return default


def _declares_volume_keys(sysfs_dir):
    """True if this input device's KEY capability bitmap has a volume key.

    The bitmap in sysfs is space-separated 64-bit hex words, most significant
    first, so it is reversed before indexing.
    """
    try:
        with open(os.path.join(sysfs_dir, "device", "capabilities", "key")) as fh:
            words = [int(w, 16) for w in reversed(fh.read().split())]
    except OSError:
        return False
    for bit in WANTED_KEYS:
        word, pos = divmod(bit, 64)
        if word < len(words) and (words[word] >> pos) & 1:
            return True
    return False


def find_devices():
    found = []
    for sysfs_dir in sorted(glob.glob("/sys/class/input/event*")):
        if not _declares_volume_keys(sysfs_dir):
            continue
        found.append("/dev/input/" + os.path.basename(sysfs_dir))
    return found


def _run(card, args):
    """Best-effort amixer call. A failed volume nudge must never kill the daemon."""
    try:
        return subprocess.run(
            ["/usr/bin/amixer", "-c", card, *args],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _read_volume(card, kcontrol):
    """Return (values, maximum) for the per-channel volume control, or None."""
    got = _run(card, ["cget", "name=" + kcontrol])
    if got is None or got.returncode != 0:
        return None
    values = maximum = None
    for line in got.stdout.splitlines():
        line = line.strip()
        if line.startswith(": values="):
            try:
                values = [int(v) for v in line[len(": values="):].split(",")]
            except ValueError:
                return None
        elif "max=" in line:
            for field in line.replace("|", ",").split(","):
                field = field.strip()
                if field.startswith("max=") and field[4:].isdigit():
                    maximum = int(field[4:])
    if not values or not maximum:
        return None
    return values, maximum


class PulsePlanner:
    """Turn the rocker's firmware pulse train into gesture lifecycle + steps.

    THE WHOLE CONTRACT, IN THREE SENTENCES:

        a BREAK pulse is one five-percent step in its own direction;
        a quiet gap ends the gesture and adds nothing;
        nothing else moves the volume, ever.

    The hardware makes the first sentence less obvious than it sounds. A held
    rocker does not autorepeat: the firmware re-sends whole make/break pairs
    about every 106 ms (captured below `atkbd` with a kprobe on
    `serio_interrupt`, 2026-09-18). So a single RELEASE proves nothing about
    the finger -- it means "let go, or about to repeat in ~106 ms" -- and the
    gesture can only be declared over by a QUIET INTERVAL in which the next
    pulse did not arrive.

    That is why the step is charged on the BREAK rather than on the make: the
    break is the end of one complete, observed contact pulse, so every step
    this hands out corresponds to a pulse the hardware really produced. The
    later gap expiry charges nothing, which is what makes the gap's length a
    pure detection latency and not a volume knob.

    DIRECTION REVERSAL STAYS INSIDE ONE GESTURE. Each break carries its own
    direction and is applied to the one virtual target in event order; an
    Owner correcting themselves quickly gets one gesture that went up and came
    back, not two gestures racing two apply queues. `VirtualTarget` is what
    keeps that arithmetic right at a boundary.

    Kept free of evdev, sockets, files and the clock so the behaviour can be
    tested directly; `now` is always passed in.
    """

    def __init__(self, gap=HOLD_GAP_S):
        self.gap = gap
        self.open = False       # is a gesture in progress
        self.contact = None     # the last pulse edge the DEVICE sent
        self.pulses = 0         # break pulses counted in this gesture

    def press(self, now):
        """Take a make. Opens a gesture if none is open; earns nothing.

        Returns True when this make OPENED a gesture, which is the caller's
        cue to capture a baseline and publish the first preview. The ~10 Hz
        repeats inside a hold return False: they are the same gesture.
        """
        self.contact = now
        if self.open:
            return False
        self.open = True
        self.pulses = 0
        return True

    def release(self, louder, now):
        """Take a break: one five-percent step in `louder`'s direction.

        Returns the signed percent, or 0 if no gesture is open -- which
        happens exactly once per daemon life if the rocker was already down
        when this process started, and must not become a free step.
        """
        if not self.open:
            return 0
        self.contact = now
        self.pulses += 1
        return PULSE_PERCENT if louder else -PULSE_PERCENT

    def settle(self, now):
        """True on the one pass that declares the gesture over.

        THE GAP RUNS FROM THE LAST PULSE THE DEVICE SENT, not from the wall
        clock and not from when this loop got round to looking. `contact` is
        the kernel's own monotonic timestamp for the event (see
        `_use_monotonic_timestamps`), so a slow apply that delayed the drain
        cannot stretch the gesture past the moment the finger actually left.

        UNLIKE ITS PREDECESSOR IT WAITS FOR NOTHING ELSE. RampPlanner.settle
        had to hold the gesture open while un-issued fractional ramp debt was
        paid off, and getting that predicate to agree with the payer's took
        three attempts. There is no debt here: every step was handed out whole
        at the moment its pulse arrived, so quiet means over.
        """
        if not self.open or now - self.contact <= self.gap:
            return False
        self.open = False
        return True

    def cancel(self):
        """Drop the gesture outright: the device vanished, so there is no doubt."""
        self.open = False
        self.contact = None

    def held(self):
        return self.open

    def wait(self, now):
        """Seconds until the next decision is due; None to block.

        Blocking outright when nothing is held is what keeps this daemon at
        zero CPU on an idle wall. While a gesture is open it wakes on the
        plan's 40-80 ms cadence -- to notice the gap, and to drain the apply
        worker's verdicts -- never faster than that, because no new evidence
        exists between pulses.
        """
        if not self.open:
            return None
        return max(0.0, min(LOOP_WAKE_S, self.contact + self.gap - now))


class VirtualTarget:
    """The one absolute level a gesture is asking for, in pulse order.

    A SIGNED ACCUMULATOR IS WRONG HERE AND THE BOUNDARY IS WHERE IT SHOWS.
    From 98%, one up pulse and then one down pulse must land on 95: the up
    clamps to 100, and the down comes off 100. Netting +5 and -5 to zero would
    leave 98, and netting them to "no movement" would leave 100. So each pulse
    is applied to the running absolute value, in order, with the clamp applied
    at each step -- which is also the only order the Owner could have meant,
    since they are pressing the buttons one at a time.

    THE FIRST PULSE REPAIRS AN OFF-GRID LEVEL rather than stepping past it. A
    level of 62 with an up pulse goes to 65, not 67: it lands on the grid in
    the direction travelled. That is WSN-062's rule, and keeping it here is
    what let the quiet-gap `snap` be deleted -- the grid is reached by a pulse
    the Owner actually sent, not by a rounding nobody asked for.
    """

    def __init__(self, level, grid=PULSE_PERCENT):
        self.grid = max(1, int(grid))
        self.level = self._clamp(level)

    @staticmethod
    def _clamp(level):
        return max(0, min(100, int(level)))

    def step(self, percent):
        """Apply one signed pulse and return the new absolute target."""
        louder = percent > 0
        if self.level % self.grid:
            self.level = self._clamp(
                ((self.level + self.grid - 1) // self.grid) * self.grid if louder
                else (self.level // self.grid) * self.grid)
        else:
            self.level = self._clamp(self.level + percent)
        return self.level

    def rebase(self, level):
        """Adopt a confirmed level as the new absolute truth.

        Called when an apply comes back with what actually landed. The target
        does NOT keep its own arithmetic in preference to the confirmed value:
        a clamp the applier performed, a level another writer set, or a
        refusal reconciled from fresh state are all the world being right and
        this being out of date.
        """
        self.level = self._clamp(level)
        return self.level


VOLUME_SOCKET = "/run/wall-volume-request.sock"
# THE PUBLIC AUDIO STATE, READ AND NEVER WRITTEN. It is the confirmed authority
# for what the level and the output are; this daemon reads it to capture a
# gesture's baseline and to reconcile after a refusal. 0644 on the panel, and
# this process has no privilege that could change that.
PUBLIC_STATE = "/etc/wall-panel/audio-state.json"

# THE PREVIEW DOCUMENT'S OWN DIRECTORY, AND WHY IT IS NOT /run/wall-panel.
# Measured on the panel 2026-09-19, in the journal of a unit that had been
# running since 16:22:
#
#   volume gesture stamp: [Errno 13] Permission denied:
#       '/run/wall-panel/volume-gesture.json'
#
# repeated for EVERY press since the feature shipped. /run/wall-panel is
# created and owned by root units (the applier, the canceller) at mode 0755,
# and this daemon is DynamicUser -- so it could never create a file there, and
# WSN-063's press-raises-the-overlay path has never once run on the real
# panel. The whole preview protocol below would have inherited that.
#
# It is NOT fixed by pointing RuntimeDirectory= at wall-panel: systemd chowns
# a RuntimeDirectory to the unit's user on every start, which would hand a
# dynamic uid the directory root writes the audio state into. So this daemon
# gets its OWN runtime directory, declared in wall-volume-keys.service, and
# publishes there. The renderer reads it; nothing writes it but this process.
GESTURE_DIR = "/run/wall-volume-gesture"
GESTURE_FILE = GESTURE_DIR + "/volume-gesture.json"
# Schema 2. Schema 1 was `{"pressedAtMs": N}` -- the bare fact of a press, with
# no level in it, because the level came from the state file a slow apply had
# not written yet. Strategy B needs the level DURING the hold, so the document
# now carries the absolute virtual target and the CAS scope it was computed on.
PREVIEW_VERSION = 2
# How long a terminal document stays before it is removed. Five of the host's
# 100 ms fallback polls, so a renderer whose directory watch coalesced or
# missed the notification still observes the terminal rather than inferring it
# from the file vanishing.
TERMINAL_HOLD_S = 0.5
# Phases and reasons, spelled once. The host accepts these and nothing else.
PHASE_ACTIVE = "active"
PHASE_SETTLED = "settled"
PHASE_REFUSED = "refused"
PHASE_CANCELLED = "cancelled"
REFUSE_REASONS = ("state-changed", "apply-failed", "unavailable", "ambiguous")
CANCEL_REASONS = ("device-removed", "mode-changed", "daemon-stopping", "timeout")
# How many times one gesture may reconcile after its pulses have stopped. A
# reconciliation is launched only when the confirmed level still differs from
# the target, so the normal case is zero or one; the bound exists so that a
# backend refusing forever cannot become a spin.
MAX_RECONCILE = 3
# How long one guarded apply may take before the reply is treated as lost. The
# measured round trip is 498-551 ms through the Accept=yes unit; ten seconds is
# a backend that has stopped answering, not one that is slow.
APPLY_TIMEOUT_S = 10
# The whole gesture's bound. Past this a gesture is cancelled with `timeout`
# whatever the actor is doing, so nothing can pin the overlay up.
GESTURE_TIMEOUT_S = 30


def read_public_state(path=PUBLIC_STATE):
    """(output, level, state_revision, generation) from the applier's file.

    None when the file cannot be read or does not carry the four facts. A
    gesture that cannot capture a baseline does not start an apply -- it still
    raises the overlay, because the Owner pressed a button and is owed the
    acknowledgement, but it has nothing honest to preview a level with.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    output = raw.get("output")
    if output not in ("mute", "headset", "speaker"):
        return None
    revision = raw.get("state_revision", 0)
    generation = raw.get("generation", 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        return None
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        generation = 0
    volumes = raw.get("volume")
    level = volumes.get(output) if isinstance(volumes, dict) else None
    if output not in ("headset", "speaker"):
        level = 0
    if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 100:
        return None
    return output, level, revision, generation


class GesturePreview:
    """The root-owned runtime document the overlay reads, schema 2.

    NOT A CONTROL SURFACE, and every field here is chosen so that it cannot
    become one. There is no path, no device, no card, no unit and no command
    in it: the most a forged copy could do is draw a wrong number on the glass
    for a bounded moment, and it cannot even do that, because this process is
    the only writer of a directory nothing else may write.

    ATOMICALLY REPLACED, NEVER TRUNCATED IN PLACE. The reader is watching the
    directory and may read at any instant; a truncate-then-write hands it half
    a document, which it would discard -- so the overlay would flicker between
    the preview and the confirmed level at ~10 Hz for the whole hold.

    ORDERING IS (gestureId, pulseSeq) AND NEVER THE CLOCK. `updatedAtMs` is
    wall time and is there only so the host can bound staleness; a clock
    correction mid-gesture must not be able to reverse the sequence.
    """

    def __init__(self, path=GESTURE_FILE):
        self.path = path
        self.directory = os.path.dirname(path)
        self.present = False

    def _write(self, document):
        temporary = self.path + ".new"
        try:
            os.makedirs(self.directory, exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(document, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, self.path)
            self.present = True
        except OSError as exc:
            print("volume gesture preview: %s" % exc, file=sys.stderr, flush=True)
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def active(self, gesture_id, pulse_seq, output, revision, target):
        self._write({
            "version": PREVIEW_VERSION,
            "gestureId": gesture_id,
            "pulseSeq": pulse_seq,
            "updatedAtMs": int(time.time() * 1000),
            "output": output,
            "stateRevision": revision,
            "targetLevel": target,
            "phase": PHASE_ACTIVE,
        })

    def settled(self, gesture_id, pulse_seq, output, revision, target,
                confirmed_revision, confirmed_level):
        self._write({
            "version": PREVIEW_VERSION,
            "gestureId": gesture_id,
            "pulseSeq": pulse_seq,
            "updatedAtMs": int(time.time() * 1000),
            "output": output,
            "stateRevision": revision,
            "targetLevel": target,
            "phase": PHASE_SETTLED,
            "confirmedStateRevision": confirmed_revision,
            "confirmedLevel": confirmed_level,
        })

    def terminal(self, phase, reason, gesture_id, pulse_seq, output, revision, target):
        self._write({
            "version": PREVIEW_VERSION,
            "gestureId": gesture_id,
            "pulseSeq": pulse_seq,
            "updatedAtMs": int(time.time() * 1000),
            "output": output,
            "stateRevision": revision,
            "targetLevel": target,
            "phase": phase,
            "reason": reason,
        })

    def clear(self):
        self.present = False
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print("volume gesture clear: %s" % exc, file=sys.stderr, flush=True)


class BusBackend:
    """Bus mode: one guarded absolute target over the restricted root socket.

    The request names a target and the world it was computed on; the applier
    performs the compare-and-set under its own apply lock and answers with
    what actually landed. This process holds no privilege of its own and sends
    no path, device or command -- see panel-volume-request.py.
    """

    mode_name = "bus"

    def __init__(self, socket_path=VOLUME_SOCKET, timeout=APPLY_TIMEOUT_S):
        self.socket_path = socket_path
        self.timeout = timeout

    def scope(self):
        return read_public_state()

    def apply(self, target, mode, output, expect_level, expect_revision):
        """Return ("ok", output, level, revision) or ("refused", reason)."""
        payload = ("set:%d:%s:%s:%d:%d" % (target, mode, output, expect_level,
                                           expect_revision)).encode("ascii")
        answer = self._round_trip(payload)
        if answer is None:
            # A LOST REPLY IS `ambiguous` AND NOT A FAILURE. The apply may well
            # have happened; claiming it did not would let the caller replay
            # arithmetic that has already landed. Ambiguous means "re-read the
            # truth", which is always safe.
            return ("refused", "ambiguous")
        parts = answer.strip().split(b":")
        if len(parts) == 2 and parts[0] == b"refused":
            reason = parts[1].decode("ascii", "replace")
            return ("refused", reason if reason in REFUSE_REASONS else "ambiguous")
        if len(parts) == 4 and parts[0] == b"ok":
            try:
                return ("ok", parts[1].decode("ascii"), int(parts[2]), int(parts[3]))
            except (ValueError, UnicodeDecodeError):
                return ("refused", "ambiguous")
        return ("refused", "ambiguous")

    def _round_trip(self, payload):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.socket_path)
                connection.sendall(payload + b"\n")
                connection.shutdown(socket.SHUT_WR)
                answer = bytearray()
                while len(answer) < 64:
                    part = connection.recv(64 - len(answer))
                    if not part:
                        break
                    answer.extend(part)
                    if answer.endswith(b"\n"):
                        break
                return bytes(answer) if answer else None
        except OSError as exc:
            print("bus volume: %s" % exc, file=sys.stderr, flush=True)
            return None


class PhysicalBackend:
    """trigger / panel mode: the card's own mixer IS the state.

    There is no applier, no state file authority and no revision to compare
    against on this path -- the mixer is the only truth -- so the guard
    degenerates to "the level is still where we last saw it", which is the
    most this arm can honestly offer. It is not the deployed configuration
    (the panel runs bus mode) and exists so that a rollback with
    `wall-audio-mode trigger` still has a working rocker.

    The target is ABSOLUTE here too, so the one thing that used to make this
    arm drift -- repeated relative nudges against a grain that is not exactly
    5% of full scale -- cannot happen: every write lands on the percentage the
    pulse count asked for.
    """

    def __init__(self, mode):
        self.mode_name = mode

    def _state(self):
        card, _, kcontrol = target()
        state = _read_volume(card, kcontrol)
        if state is None:
            return None
        values, maximum = state
        readable = [values[i] for i in FRONT_CHANNELS if i < len(values)]
        if not readable or not maximum:
            return None
        return card, kcontrol, values, maximum, int(round(
            100.0 * (sum(readable) / float(len(readable))) / maximum))

    def scope(self):
        found = self._state()
        if found is None:
            return None
        # `speaker` and revision 0: this arm has no switch position and no CAS
        # token. Naming the position `speaker` keeps one vocabulary in the
        # preview document rather than inventing a fourth value the host would
        # have to learn.
        return ("speaker", found[4], 0, 0)

    def apply(self, level, mode, output, expect_level, expect_revision):
        found = self._state()
        if found is None:
            return ("refused", "unavailable")
        card, kcontrol, values, maximum, current = found
        if current != expect_level:
            return ("refused", "state-changed")
        wanted = max(0, min(maximum, int(round(maximum * level / 100.0))))
        for index in FRONT_CHANNELS:
            if index < len(values):
                values[index] = wanted
        if _run(card, ["cset", "name=" + kcontrol,
                       ",".join(str(value) for value in values)]) is None:
            return ("refused", "apply-failed")
        # The mute switch is pswitch-joined, so this is one switch for every
        # channel. Unmuting rides along with a volume press because that is
        # what someone reaching for the rocker means.
        _run(card, ["-q", "sset", CONTROL, "unmute"])
        settled = self._state()
        if settled is None:
            return ("refused", "ambiguous")
        return ("ok", "speaker", settled[4], 0)


class ApplyActor:
    """One worker thread, one active request, one replaceable desired target.

    WHY A THREAD AT ALL, stated plainly because it is the cost of strategy B.
    One privileged apply blocks for about half a second, and the firmware
    delivers a pulse every ~106 ms. The old single-threaded loop waited for
    each reply inline, so during an apply it drained no events and published
    no previews -- the overlay froze and the pulses piled up in the evdev
    buffer. The input loop must never block on the applier again, so the
    applier moved off it.

    QUEUE DEPTH IS TWO AND CANNOT GROW: one request in flight plus one
    replaceable absolute target. That bound is the entire fix for the original
    stuck-slider bug, and it is a bound on this structure rather than a
    promise about timing -- a target submitted while one is already waiting
    REPLACES it, because it is a newer statement of the same thing. Sending
    one request per pulse is what created the queue in the first place and is
    forbidden.

    THE ACTOR OUTLIVES ONE GESTURE. A new gesture that starts while an apply
    or a desired target is still outstanding inherits this actor's confirmed
    scope instead of re-reading a state file the outstanding work has not
    landed in yet -- which is how two quick taps used to compute the same
    baseline twice and lose one of them.
    """

    def __init__(self, backend):
        self.backend = backend
        self.condition = threading.Condition()
        self.desired = None        # the one replaceable absolute target
        self.inflight = None       # the target currently being applied
        self.active = False        # is a request in flight
        self.confirmed = None      # (output, level, revision) as last confirmed
        self.results = queue.Queue()
        self.running = True
        self.thread = threading.Thread(target=self._serve, name="volume-apply",
                                       daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        with self.condition:
            self.running = False
            self.desired = None
            self.condition.notify_all()

    def busy(self):
        """True while anything is outstanding: in flight, or waiting to go."""
        with self.condition:
            return self.active or self.desired is not None

    def adopt(self, scope):
        """Record a confirmed scope read from outside (a gesture's baseline)."""
        with self.condition:
            self.confirmed = scope

    def known(self):
        with self.condition:
            return self.confirmed

    def intent(self):
        """The level this actor is HEADING FOR, or None if it knows nothing.

        NOT the confirmed level, and the difference is the whole reason this
        method exists (terra, 2026-09-19). `confirmed` is the last level an
        apply came back with; between the request and that reply -- about half
        a second -- there is a target in flight, and possibly a queued one
        behind it, that `confirmed` does not know about.

        A new gesture that started in that window and took `confirmed` as its
        baseline would compute against a world its own predecessor had already
        left. Measured against the contract: from 60, one up pulse (65 in
        flight), then past the 250 ms gap but inside the 500 ms apply, one down
        pulse -- the new gesture baselines at 60, targets 55, and the 65 that
        was in flight lands and is then reconciled to 55. Event order says 60.
        Two quick up taps end at 65 instead of 70 the same way. That is the
        pulse conservation the plan requires, lost exactly when the actor is
        busy, which is exactly when a person taps twice.

        The newest statement wins: a queued target is newer than one in flight,
        which is newer than the last confirmation.
        """
        with self.condition:
            if self.desired is not None:
                return self.desired
            if self.inflight is not None:
                return self.inflight
            return self.confirmed[1] if self.confirmed else None

    def request(self, target):
        """Ask for one absolute level. Replaces any target not yet started."""
        with self.condition:
            self.desired = int(target)
            self.condition.notify_all()

    def _serve(self):
        while True:
            with self.condition:
                while self.running and self.desired is None:
                    self.condition.wait()
                if not self.running:
                    return
                target = self.desired
                self.desired = None
                self.inflight = target
                self.active = True
                scope = self.confirmed
            # THE GUARD IS BUILT AT DEQUEUE, FROM THE NEWEST CONFIRMED SCOPE,
            # and that is deliberate: the compare-and-set exists to catch
            # changes made by SOMEBODY ELSE, not to refuse this actor's own
            # previous apply. Guarding on a scope captured before our own last
            # reply landed would refuse every second pulse of every hold.
            if scope is None:
                scope = self.backend.scope()
            # Every scope in this file is the same four-tuple -- (output,
            # level, state_revision, generation) -- whichever backend produced
            # it, so nothing downstream has to ask which shape it got.
            if scope is None:
                outcome = ("refused", "unavailable")
            elif scope[1] == target:
                # ALREADY THERE, ON EVIDENCE. `confirmed` is only ever set from
                # an apply's own reply or a fresh read, so this is not a guess
                # -- and spending half a second to write a level the applier
                # would answer "volume already N%" to is half a second the next
                # real target waits for. The normal end of a hold lands here,
                # because the last queued target is usually the one the
                # in-flight apply had already reached.
                outcome = ("ok", scope[0], scope[1], scope[2])
            else:
                output, level, revision, _generation = scope
                outcome = self.backend.apply(target, self.backend.mode_name,
                                             output, level, revision)
            with self.condition:
                self.active = False
                self.inflight = None
                if outcome[0] == "ok":
                    self.confirmed = (outcome[1], outcome[2], outcome[3],
                                      scope[3] if scope else 0)
                else:
                    # A REFUSAL IS RECONCILED FROM FRESH TRUTH, never from what
                    # we hoped. Re-reading here is what makes the next guarded
                    # attempt able to succeed instead of failing identically.
                    self.confirmed = self.backend.scope()
                    # AND IT CANCELS THE SUCCESSOR, which is the half that is
                    # easy to miss and was caught in the scenario run of
                    # 2026-09-19. The queued target was computed on the world
                    # that just turned out to be gone -- somebody moved the
                    # switch -- so applying it against the NEW world is
                    # precisely the "overwrite a concurrent volume choice" the
                    # compare-and-set exists to prevent: the guard refuses the
                    # target in flight and then the queue lands the same
                    # arithmetic one apply later. The caller rebases on the
                    # fresh scope and may ask again from there; nothing here
                    # replays it on the caller's behalf.
                    self.desired = None
                self.results.put((target, outcome, self.confirmed))
                self.condition.notify_all()


# _IOW('E', 0xa0, int) -- ask the kernel to timestamp events on CLOCK_MONOTONIC
# instead of CLOCK_REALTIME, so they can be compared with time.monotonic().
EVIOCSCLOCKID = 0x400445A0
CLOCK_MONOTONIC = 1


def _use_monotonic_timestamps(fd):
    """True if this fd will now timestamp events on CLOCK_MONOTONIC.

    WHY THE KERNEL'S TIMESTAMP AND NOT time.monotonic(): events are read AFTER
    the applier returns, and one apply blocks for ~520 ms. Stamping them at
    drain time therefore dates them up to half a second late, which drags the
    "last contact" past the instant the finger actually came up -- and the
    quiet gap, which is measured from that contact, would then declare the
    gesture over later than it really ended. Under the pulse rule that costs
    no volume (a late gap adds no step), but it does decide when the terminal
    preview is published and when a new gesture may capture a fresh baseline,
    so the honest timestamp still matters. It mattered more under the
    withdrawn ramp: a 1.0 s hold moved 21% where 15% was owed.
    """
    if fcntl is None:
        return False
    try:
        fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", CLOCK_MONOTONIC))
        return True
    except (OSError, AttributeError):
        return False


def _drain(fd, fallback):
    """Yield every (code, value, when) key event waiting on `fd`, until dry.

    Reads until EAGAIN rather than taking one bufferful, so no event is ever
    left behind to be acted on after the press that produced it is over. Only
    EV_KEY is yielded; EV_SYN and the rest are not this daemon's business.

    `when` is the kernel's own monotonic timestamp. `fallback` is used instead
    for a device whose clock could not be switched -- less accurate, but the
    daemon must still work there.
    """
    while True:
        try:
            data = os.read(fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return
        except OSError:
            return
        if not data:
            return
        for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            seconds, micros, etype, code, value = struct.unpack_from(
                EVENT_FORMAT, data, offset)
            if etype == EV_KEY:
                when = seconds + micros / 1000000.0 if fallback is None \
                    else time.monotonic()
                yield code, value, when
        if len(data) < EVENT_SIZE * 64:
            return



def make_backend():
    """The apply backend for whichever mode the panel is in right now.

    Read per gesture rather than at startup: `wall-audio-mode` does not
    restart this unit, and adjusting the wrong card is silent -- the rocker
    appears dead while moving a control nobody is listening to.
    """
    mode = current_mode()
    return BusBackend() if mode == "bus" else PhysicalBackend(mode)


class Gesture:
    """One rocker gesture's identity, preview bookkeeping and virtual target.

    It exists so that the main loop can say what it means -- "another pulse",
    "the finger is off", "the apply came back" -- without also spelling out
    the preview document's key sets at each of those points.
    """

    def __init__(self, preview, generation, started_ns, scope, backend_mode):
        self.preview = preview
        self.identity = "%d:%d" % (generation, started_ns)
        self.pulse_seq = 0
        self.backend_mode = backend_mode
        self.started = time.monotonic()
        self.closed = False        # the quiet gap has declared it over
        self.reconciled = 0
        self.terminal_at = None    # monotonic instant the terminal was published
        if scope is None:
            # NO BASELINE IS STILL A GESTURE. The Owner pressed a physical
            # button and is owed the acknowledgement, so the overlay rises;
            # there is simply no honest level to preview or to target, which
            # `unavailable` says out loud.
            self.output, self.revision = "speaker", 0
            self.target = None
            self.preview.terminal(PHASE_REFUSED, "unavailable", self.identity,
                                  0, self.output, self.revision, 0)
            self.terminal_at = time.monotonic()
            self.closed = True
            return
        self.output, level, self.revision, _generation = scope
        self.target = VirtualTarget(level)
        self.preview.active(self.identity, 0, self.output, self.revision,
                            self.target.level)

    def live(self):
        return self.target is not None and self.terminal_at is None

    def pulse(self, percent):
        """One break pulse: move the virtual target and publish the preview."""
        if not self.live():
            return None
        self.pulse_seq += 1
        level = self.target.step(percent)
        self.preview.active(self.identity, self.pulse_seq, self.output,
                            self.revision, level)
        return level

    def settle(self, confirmed_revision, confirmed_level):
        if self.terminal_at is not None:
            return
        self.preview.settled(self.identity, self.pulse_seq, self.output,
                             self.revision, self.target.level,
                             confirmed_revision, confirmed_level)
        self.terminal_at = time.monotonic()

    def finish(self, phase, reason):
        if self.terminal_at is not None:
            return
        self.preview.terminal(phase, reason, self.identity, self.pulse_seq,
                              self.output, self.revision,
                              self.target.level if self.target else 0)
        self.terminal_at = time.monotonic()

    def expired(self, now):
        """True once the terminal has stood long enough to be removed."""
        return self.terminal_at is not None and now - self.terminal_at >= TERMINAL_HOLD_S

    def overdue(self, now):
        return self.terminal_at is None and now - self.started > GESTURE_TIMEOUT_S


def _open_devices(paths):
    """Every readable volume-key device, with its timestamp fallback marker."""
    handles, monotonic = {}, {}
    for path in paths:
        try:
            # NON-BLOCKING is load-bearing, not tidiness. Every wake drains its
            # device until the reads run dry, so that a burst of pulses can
            # never be left sitting in the evdev buffer to be acted on later. A
            # blocking fd would stall the drain on the read after the last event.
            handle = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except (PermissionError, OSError):
            continue
        handles[handle] = path
        # None means "the kernel is stamping these on CLOCK_MONOTONIC";
        # anything else is the fallback marker for a device that refused.
        monotonic[handle] = None if _use_monotonic_timestamps(handle) else True
    return handles, monotonic


def main():
    paths = find_devices()
    if not paths:
        print("no input device declares volume keys", file=sys.stderr)
        return 69

    handles, monotonic = _open_devices(paths)
    if not handles:
        print("volume-key devices exist but none is readable", file=sys.stderr)
        return 77

    poller = select.poll()
    for handle in handles:
        poller.register(handle, select.POLLIN)

    planner = PulsePlanner()
    preview = GesturePreview()
    # ANY DOCUMENT HERE AT STARTUP IS NOT OURS, and it has to go (terra,
    # 2026-09-19). RuntimeDirectoryPreserve=yes keeps the directory across a
    # restart, so a SIGKILL mid-hold leaves an `active` document behind with
    # no writer -- and an `active` document is precisely the one the renderer
    # lets lead the confirmed state. Removing it is the one honest thing this
    # process can say about a gesture it did not make.
    preview.clear()
    actor = None
    gesture = None

    # SIGTERM PUBLISHES A TERMINAL AND THEN LETS GO. `systemctl restart` during
    # a hold would otherwise leave the last `active` document on disk with a
    # fresh timestamp, and the overlay would sit on a preview whose writer no
    # longer exists until it aged out. A flag rather than work in the handler:
    # the preview write is I/O and belongs on the loop.
    stopping = {"now": False}

    def _stop(_signal, _frame):
        stopping["now"] = True

    for number in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(number, _stop)
        except (ValueError, OSError):  # pragma: no cover - not the main thread
            pass

    # A SELF-PIPE, BECAUSE THE FLAG ALONE CANNOT WAKE poll() (terra,
    # 2026-09-19). With nothing held this loop blocks in `poller.poll(None)`,
    # which is the whole reason it costs no CPU on an idle wall -- and under
    # PEP 475 Python RETRIES an interrupted syscall once a signal handler has
    # returned normally. This handler only sets a flag, so SIGTERM on an idle
    # panel left the process blocked in poll() until an unrelated key arrived
    # or systemd's stop timeout expired and killed it. Every `systemctl
    # restart wall-volume-keys` paid that, and the terminal this loop is
    # supposed to publish on the way out was never reached.
    #
    # `set_wakeup_fd` makes the C-level handler write a byte, which is
    # something poll() can see. It is the documented way to join signals to a
    # poll loop and needs no change to the handler.
    wake_read, wake_write = os.pipe()
    for handle in (wake_read, wake_write):
        os.set_blocking(handle, False)
    try:
        signal.set_wakeup_fd(wake_write)
    except (ValueError, OSError):  # pragma: no cover - not the main thread
        pass
    poller.register(wake_read, select.POLLIN)
    # AND THE WINDOW BEFORE THE PIPE EXISTED (terra, 2026-09-19). A SIGTERM
    # between installing the handler and installing the wakeup fd sets the
    # flag and writes no byte, so the very first `poller.poll(None)` would
    # block on it forever -- the same hang, moved earlier by a few
    # microseconds. Asked once, here, where the answer is finally knowable.
    if stopping["now"]:
        _close_wakeup(poller, wake_read, wake_write)
        return 0

    # THE SHAPE OF THIS LOOP IS THE FIX, AS IT WAS BEFORE -- but the thing it
    # must never do has moved. Each pass: work out how long we may sleep, drain
    # EVERY pending event, fold each into the planner and the virtual target,
    # publish the preview, then hand AT MOST ONE absolute target to the actor.
    # The apply itself happens on the actor's thread, so this loop never waits
    # half a second for a socket and the ~106 ms pulses keep being drained and
    # previewed while an apply is running. That is the whole of strategy B.
    while True:
        now = time.monotonic()
        wait = planner.wait(now)
        if gesture is not None:
            # Something is still owed: a verdict to drain, a terminal to
            # publish, or a terminal to take back down. Bounded waking rather
            # than blocking, so none of those can be left hanging.
            wait = LOOP_WAKE_S if wait is None else min(wait, LOOP_WAKE_S)
        ready = poller.poll(None if wait is None else max(0.0, wait) * 1000.0)

        if stopping["now"]:
            if gesture is not None:
                gesture.finish(PHASE_CANCELLED, "daemon-stopping")
                _retire(preview, gesture)
            if actor is not None:
                actor.stop()
            _close_wakeup(poller, wake_read, wake_write)
            return 0

        for handle, flag in ready:
            if handle == wake_read:
                # The signal byte. It carries nothing; `stopping` above is the
                # message, and this only had to make poll() return.
                try:
                    os.read(wake_read, 64)
                except OSError:
                    pass
                continue
            # A device that disappeared (adapter or keyboard unplugged) is
            # dropped rather than allowed to spin the poll loop.
            if flag & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                poller.unregister(handle)
                os.close(handle)
                del handles[handle]
                monotonic.pop(handle, None)
                # A rocker cannot still be held on a device that just vanished.
                planner.cancel()
                if gesture is not None and gesture.live():
                    gesture.finish(PHASE_CANCELLED, "device-removed")
                if not handles:
                    _retire(preview, gesture)
                    if actor is not None:
                        actor.stop()
                    _close_wakeup(poller, wake_read, wake_write)
                    return 69
                continue
            for code, value, when in _drain(handle, monotonic[handle]):
                if code in (KEY_VOLUMEUP, KEY_VOLUMEDOWN):
                    louder = (code == KEY_VOLUMEUP)
                    if SWAP_FOR_PANEL_ORIENTATION:
                        louder = not louder
                    if value == KEY_PRESS:
                        if planner.press(when):
                            gesture, actor = _begin(preview, actor, gesture)
                    elif value == KEY_RELEASE:
                        # THE BREAK IS THE STEP. This rocker sends a release
                        # every ~106 ms while it is held, and each one is a
                        # complete contact pulse the firmware really produced
                        # -- so each one is 5%, including the first. It is NOT
                        # "the finger came up"; only the quiet gap says that.
                        percent = planner.release(louder, when)
                        if percent and gesture is not None:
                            level = gesture.pulse(percent)
                            if level is not None and actor is not None:
                                actor.request(level)
                    # KEY_AUTOREPEAT is accepted and ignored. This rocker never
                    # sends one, but an attached USB keyboard would, and its
                    # repeats must not be charged as steps either.
                elif value != KEY_PRESS:
                    continue
                elif code == KEY_MUTE and current_mode() == "bus":
                    # DELIBERATELY INERT IN BUS MODE, and said out loud. Mute is
                    # a position of the on-glass switch now, and a one-way
                    # `set mute` from a key that cannot un-mute would strand the
                    # panel silent for anyone not standing at it.
                    print("mute key ignored in bus mode: use the on-glass switch",
                          file=sys.stderr, flush=True)
                elif code == KEY_MUTE:
                    # NOTE: the switch is joined across all channels, so this
                    # silences the trigger line too and the amplifier will power
                    # down with it. That may be wanted; it is not a choice this
                    # daemon gets to make separately.
                    card, control, _ = target()
                    _run(card, ["-q", "sset", control, "toggle"])

        # The quiet gap. Done AFTER the drain, so a pulse that has already
        # arrived is always counted before the gesture is declared over, and
        # it adds no step of its own.
        planner.settle(time.monotonic())
        if gesture is not None:
            gesture, actor = _reap(gesture, actor, planner, preview)


def _close_wakeup(poller, wake_read, wake_write):
    """Unhook the signal self-pipe. Every exit takes this path."""
    try:
        signal.set_wakeup_fd(-1)
    except (ValueError, OSError):  # pragma: no cover - not the main thread
        pass
    try:
        poller.unregister(wake_read)
    except (KeyError, OSError):
        pass
    for handle in (wake_read, wake_write):
        try:
            os.close(handle)
        except OSError:
            pass


def _retire(preview, gesture):
    """Let a just-published terminal stand its hold, then remove it.

    THE HOLD IS THE CONTRACT AND BOTH EXITS USED TO SKIP IT (terra,
    2026-09-19). Publishing `cancelled` and unlinking it in the same breath is
    indistinguishable, from the reader's side, from never publishing it: the
    renderer's directory watch coalesces, its fallback polls at 100 ms, and it
    would see only the file vanish -- which is not evidence of anything, so it
    would go on drawing the preview as the truth. Unplugging the keyboard
    mid-hold and `systemctl restart` mid-hold are both ordinary events here.

    Half a second is the whole cost, it is paid only on a path that is already
    ending, and it is far inside systemd's stop timeout.
    """
    if gesture is not None and gesture.terminal_at is not None:
        remaining = TERMINAL_HOLD_S - (time.monotonic() - gesture.terminal_at)
        if remaining > 0:
            time.sleep(min(remaining, TERMINAL_HOLD_S))
    preview.clear()


def _begin(preview, actor, previous):
    """Open a gesture, reusing the actor and its scope when work is outstanding.

    INHERITING THE ACTOR'S SCOPE IS THE POINT. A second tap arriving during
    the first one's ~0.5 s apply must not re-read the public state: that file
    still says what the panel was before the first tap landed, so both taps
    would compute from the same baseline and one of them would be lost. If
    anything is outstanding the new gesture continues from the actor's virtual
    truth; only a genuinely idle actor reads the file again.
    """
    backend = make_backend()
    if actor is not None and (actor.busy() or (previous is not None
                                               and previous.live())):
        scope = actor.known() or backend.scope()
        # THE BASELINE IS THE ACTOR'S INTENT, NOT ITS LAST CONFIRMATION, and
        # the guard scope is still the confirmation. Those are two different
        # questions and conflating them lost a pulse (terra, 2026-09-19): the
        # CAS has to be compared against the world as last observed, while the
        # arithmetic has to continue from where the outstanding work is
        # heading. From 60, up (65 in flight), gap, down must reach 60 -- not
        # 55, which is what baselining on the stale confirmation produced.
        intent = actor.intent()
        if scope is not None and intent is not None:
            scope = (scope[0], intent, scope[2], scope[3])
    else:
        if actor is not None:
            actor.stop()
        actor = ApplyActor(backend).start()
        scope = backend.scope()
        actor.adopt(scope)
    if previous is not None and previous.live():
        # The previous gesture never reached a terminal because this press
        # arrived first. Retire it rather than leaving the host holding an
        # `active` document that will never be superseded by its own terminal.
        previous.finish(PHASE_CANCELLED, "timeout")
    return Gesture(preview, scope[3] if scope else 0, time.monotonic_ns(),
                   scope, backend.mode_name), actor


def _reap(gesture, actor, planner, preview):
    """Drain verdicts, reconcile, and retire a finished gesture's preview."""
    now = time.monotonic()
    confirmed = None
    while actor is not None:
        try:
            _target, outcome, confirmed_scope = actor.results.get_nowait()
        except queue.Empty:
            break
        confirmed = (outcome, confirmed_scope)

    if confirmed is not None and gesture.live():
        outcome, scope = confirmed
        if outcome[0] != "ok" and not planner.held():
            # THE GESTURE IS OVER AND ITS TARGET WAS REFUSED. It is retired
            # with the level it ASKED for, not with the fresh truth: the
            # renderer's rule for a `refused` terminal is to re-read the
            # confirmed state for itself, so overwriting the target here would
            # publish a number that is neither what was wanted nor evidence of
            # anything.
            gesture.finish(PHASE_REFUSED, outcome[1])
        elif outcome[0] != "ok" and scope is not None:
            # STILL HELD, so the Owner is mid-gesture and the remaining pulses
            # must move from the world as it now is. Rebasing the virtual
            # target on the fresh scope is what lets the next pulse succeed
            # instead of re-sending arithmetic the guard has already refused.
            gesture.target.rebase(scope[1])
            gesture.revision = scope[2]
            gesture.output = scope[0]
        elif outcome[0] == "ok" and scope is not None \
                and scope[1] != gesture.target.level \
                and gesture.reconciled < MAX_RECONCILE:
            # ONE RECONCILIATION, NOT A RETRY LOOP. The confirmed level and
            # the virtual target disagree, which after a successful apply
            # means more pulses arrived while it was in flight; the actor is
            # idle now, so exactly one more absolute target closes the gap.
            gesture.reconciled += 1
            actor.request(gesture.target.level)

    if gesture.live() and not planner.held() and not actor.busy():
        scope = actor.known()
        if scope is not None and scope[1] == gesture.target.level:
            gesture.settle(scope[2], scope[1])
        elif gesture.reconciled >= MAX_RECONCILE:
            gesture.finish(PHASE_REFUSED, "apply-failed")

    if gesture.overdue(now):
        gesture.finish(PHASE_CANCELLED, "timeout")
    if gesture.expired(now):
        preview.clear()
        return None, actor
    return gesture, actor


if __name__ == "__main__":
    sys.exit(main())
