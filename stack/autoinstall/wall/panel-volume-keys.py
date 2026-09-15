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

NO NEW DEPENDENCIES: the panel's installer is an offline, hash-locked
wheelhouse, so this parses struct input_event itself rather than importing
python-evdev.

Exit codes: 69 no readable volume-key device, 77 not permitted to read one.
"""

import glob
import os
import select
import socket
import struct
import subprocess
import sys
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
# source TEXT rather than the behaviour. RampPlanner below is the ramp contract
# and is worth testing properly off-panel, so the guard is scoped to where it
# is meaningful rather than dropped.
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
#    is why RampPlanner ends a hold on a QUIET GAP rather than on a release.
#
# The original bug: the daemon applied one volume change per press event, inline,
# while one apply costs ~520 ms (measured: 498-551 ms, because Accept=yes spawns
# a transient unit and a fresh python3 per step). Presses arrived 5x faster than
# they could be applied, the surplus queued in the evdev buffer, and release was
# not handled at all -- so a 3 s hold spent ~15 s draining and the level kept
# falling long after the Owner let go. The journal caught it mid-drain:
# "4% -> 0%" and then "volume already 0%" four times over. That is the "stuck
# slider"; the on-glass overlay is read-only and was faithfully showing a queue.

# How long a gap with NO press means the finger is really off. Must comfortably
# exceed the ~100 ms repeat period, or a hold would be chopped into taps; every
# millisecond beyond that is pure detection latency, so it is not generous.
# It costs no VOLUME, because the ramp accrues against the last contact rather
# than against the clock -- see RampPlanner.due.
HOLD_GAP_S = 0.250

# THE PANEL IS MOUNTED ROTATED relative to the way the rocker was labelled, so
# the button that sits physically uppermost on the wall is the one reporting
# KEY_VOLUMEDOWN. Reaching up to make it quieter is wrong in the only way a
# volume control can be wrong, so the two keycodes are swapped here. This is a
# property of how the panel hangs, not of the hardware: if it is ever remounted
# the right way up, set this back to False rather than rewiring anything.
SWAP_FOR_PANEL_ORIENTATION = True

# The Owner's specification for the rocker, 2026-09-15:
#   a press and release bumps 5%
#   holding it past 600 ms ramps at 25% per second
#   releasing stops it immediately
#
# The adapter's `Speaker` control spans a wide dB range (20% is already
# -29.6 dB), so percent steps, not absolute steps, are right.
TAP_PERCENT = 5
HOLD_THRESHOLD_S = 0.600
RAMP_PERCENT_PER_S = 25.0
# Where the level comes to REST once the rocker is released (Owner, 2026-09-15:
# "I would like the volume to land at / round to 5% increments after a
# release"). Only the resting place is rounded -- the ramp itself stays exact,
# because quantising every step would err by up to half an increment twice a
# second and drift the ramp well off its 25%/s promise. One extra apply per
# gesture, after the finger is already off, where nothing is waiting on it.
SNAP_PERCENT = 5
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


class RampPlanner:
    """Decide what the rocker owes, from TIME HELD rather than events seen.

    This is the whole fix for the queued-volume bug, so the invariants are worth
    stating plainly:

        every percent this emits is a percent the finger was DOWN for, and
        nothing is ever owed for time after it came up.

    The hardware makes this less obvious than it sounds: a held rocker sends
    repeating PRESS/RELEASE pairs at ~10 Hz (see the capture above), so a single
    RELEASE proves nothing. A hold is therefore a BURST -- it begins at the
    first press and ends when no press has arrived for HOLD_GAP_S.

    The trick that keeps "release stops it" honest despite that gap: the ramp
    accrues against `contact`, the timestamp of the last event the DEVICE sent,
    never against the wall clock. While the finger is down, contact is refreshed
    every ~100 ms and the ramp runs. The moment the finger lifts, contact stops
    advancing, so the ramp stops accruing THEN -- not HOLD_GAP_S later when the
    burst is finally declared over. The gap costs detection latency; it costs no
    volume.

    A slow applier cannot make this drift: `due` hands back the ramp accrued
    since the last step it issued and advances its anchor by exactly the time
    that step represents, so a late call yields one LARGER step rather than a
    growing pile of small ones.

    Kept free of evdev, sockets and the clock so the behaviour can be tested
    directly; `now` is always passed in.
    """

    def __init__(self, tap=TAP_PERCENT, threshold=HOLD_THRESHOLD_S,
                 rate=RAMP_PERCENT_PER_S, gap=HOLD_GAP_S):
        self.tap = tap
        self.threshold = threshold
        self.rate = rate
        self.gap = gap
        self.louder = None      # None when no burst is in progress
        self.contact = None     # last event from the device: the finger was down
        self.ramp_from = None   # the instant un-issued ramp starts accruing

    def press(self, louder, now):
        """Take a press. Returns the signed percent owed IMMEDIATELY (0 or tap).

        Only the press that OPENS a burst is a tap. The ~10 Hz repeats inside a
        hold are not taps and owe nothing -- charging 5% for each would be 50%/s
        and would put the queue straight back. Pressing the other way ends the
        current burst and opens a new one.
        """
        if self.louder is not None and self.louder == louder:
            self.contact = now
            return 0
        self.louder = louder
        self.contact = now
        self.ramp_from = now + self.threshold
        return self.tap if louder else -self.tap

    def release(self, now):
        """Take a release. Does NOT end the hold -- a repeat may follow in ~100 ms.

        It does mark contact: this is the instant the finger was last known to
        be down, and the ramp will accrue no further than here.
        """
        if self.louder is not None:
            self.contact = now

    def settle(self, now):
        """End the burst once the device is quiet AND nothing is still owed.

        Returns True on the pass that actually closes it, so the caller can
        settle the level onto a round number exactly once per gesture.

        THE DEBT OUTLIVES THE HOLD, and must. Each apply blocks for ~520 ms, so
        when the gap finally expires there is normally still a step's worth of
        ramp earned but not yet paid -- the finger WAS down for it. Closing the
        burst on the gap alone silently threw that away: measured on the panel,
        a 1.0 s hold moved 10% where 15% was owed. So the burst stays open until
        the arrears fall below one percent, and `due` pays them out meanwhile.

        This does not let the level run on past the finger: `due` accrues only
        to `contact`, so what is left to pay is bounded by the time actually
        held, and it is paid within one apply of the release.
        """
        if self.louder is None or now - self.contact <= self.gap:
            return False
        if int((self.contact - self.ramp_from) * self.rate) >= 1:
            return False
        self.cancel()
        return True

    def cancel(self):
        """Drop any hold outright, owing nothing further.

        For the cases where there is no doubt the finger is off -- the device
        disappeared -- rather than the timed judgement `settle` makes.
        """
        self.louder = None
        self.contact = None
        self.ramp_from = None

    def held(self):
        return self.louder is not None

    def due(self, now):
        """The ramp step owed now, as (louder, percent), or None.

        Accrues to `contact`, never to `now`: time after the finger lifted is
        not owed. Returns None until a whole percent has built up, so the
        applier is never woken for a step too small to hear.
        """
        if self.louder is None:
            return None
        edge = min(now, self.contact)
        if edge < self.ramp_from:
            return None
        step = int((edge - self.ramp_from) * self.rate)
        if step < 1:
            return None
        step = min(step, 100)
        self.ramp_from += step / self.rate
        return self.louder, step

    def wait(self, now):
        """Seconds to poll for before the next decision; None to block.

        Blocking outright when nothing is held is what keeps this daemon at zero
        CPU on an idle wall. While a burst is open it must wake often enough to
        both pace the ramp and notice the quiet gap that ends it.
        """
        if self.louder is None:
            return None
        settle_at = self.contact + self.gap
        if now < self.ramp_from:
            return max(0.0, min(self.ramp_from, settle_at) - now)
        return max(0.0, min(1.0 / self.rate, settle_at - now))


VOLUME_SOCKET = "/run/wall-volume-request.sock"


def request_bus(payload):
    """Send one request to the root socket and wait for its verdict.

    Return success only after the applier completes. A failure is journaled;
    bus mode must never fall back to a physical mixer and disrupt its graph.
    Implements: SR-028, LLR-014.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(24)
            connection.connect(VOLUME_SOCKET)
            connection.sendall(payload + b"\n")
            connection.shutdown(socket.SHUT_WR)
            answer = bytearray()
            while len(answer) < 16:
                part = connection.recv(16 - len(answer))
                if not part:
                    break
                answer.extend(part)
            if answer != b"ok\n":
                print("bus volume apply refused or failed", file=sys.stderr, flush=True)
                return False
            return True
    except OSError as exc:
        print("bus volume: %s" % exc, file=sys.stderr, flush=True)
        return False


def nudge_bus(louder, percent):
    """Request one relative bus step of `percent`.

    ONE REQUEST PER MOVEMENT, whatever its size. Sending `percent` instead of
    repeating the default-sized `up`/`down` is what stops a fast ramp becoming
    a queue of slow applies -- see RampPlanner.
    """
    percent = max(1, min(100, int(percent)))
    return request_bus(b"%s:%d" % (b"up" if louder else b"down", percent))


def snap(increment):
    """Round the level to the nearest multiple of `increment` and settle there.

    Sent ONCE, when the rocker has been released and every percent it earned has
    been applied -- never during a ramp. In bus mode the applier owns the level
    and does the arithmetic; on a physical card this daemon does it, because
    there the mixer IS the state.
    """
    if current_mode() == "bus":
        request_bus(b"snap:%d" % max(1, min(100, int(increment))))
        return
    card, control, kcontrol = target()
    state = _read_volume(card, kcontrol)
    if state is None:
        return
    values, maximum = state
    grain = max(1, int(round(maximum * (max(1, min(100, int(increment))) / 100.0))))
    for i in FRONT_CHANNELS:
        if i < len(values):
            values[i] = min(maximum, max(0, int(round(values[i] / float(grain))) * grain))
    _run(card, ["cset", "name=" + kcontrol, ",".join(str(v) for v in values)])


def nudge(louder, percent):
    """Move the front pair by `percent`, on whichever card the mode is using."""
    if current_mode() == "bus":
        nudge_bus(louder, percent)
        return
    card, control, kcontrol = target()
    state = _read_volume(card, kcontrol)
    if state is None:
        return
    values, maximum = state
    step = max(1, int(round(maximum * (max(1, min(100, int(percent))) / 100.0))))
    for i in FRONT_CHANNELS:
        if i < len(values):
            values[i] = min(maximum, max(0, values[i] + (step if louder else -step)))
    _run(card, ["cset", "name=" + kcontrol, ",".join(str(v) for v in values)])
    # The mute switch is pswitch-joined, so this is one switch for every channel.
    _run(card, ["-q", "sset", control, "unmute"])


# _IOW('E', 0xa0, int) -- ask the kernel to timestamp events on CLOCK_MONOTONIC
# instead of CLOCK_REALTIME, so they can be compared with time.monotonic().
EVIOCSCLOCKID = 0x400445A0
CLOCK_MONOTONIC = 1


def _use_monotonic_timestamps(fd):
    """True if this fd will now timestamp events on CLOCK_MONOTONIC.

    WHY THE KERNEL'S TIMESTAMP AND NOT time.monotonic(): events are read AFTER
    the applier returns, and one apply blocks for ~520 ms. Stamping them at
    drain time therefore dates them up to half a second late, which drags the
    "last contact" past the instant the finger actually came up -- and the ramp,
    which accrues to that contact, spends volume for time nobody was holding.
    Measured before this was fixed: a 1.0 s hold moved 21% where 15% was owed.
    The kernel stamps each event when it HAPPENS, which is the only honest
    answer to "how long was it held".
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


def main():
    paths = find_devices()
    if not paths:
        print("no input device declares volume keys", file=sys.stderr)
        return 69

    handles = {}
    monotonic = {}
    for path in paths:
        try:
            # NON-BLOCKING is load-bearing, not tidiness. Every wake drains its
            # device until the reads run dry, so that a burst of autorepeat can
            # never be left sitting in the evdev buffer to be acted on later. A
            # blocking fd would stall the drain on the read after the last event.
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            # None means "the kernel is stamping these on CLOCK_MONOTONIC";
            # anything else is the fallback marker for a device that refused.
            handles[fd] = path
            monotonic[fd] = None if _use_monotonic_timestamps(fd) else True
        except PermissionError:
            continue
        except OSError:
            continue
    if not handles:
        print("volume-key devices exist but none is readable", file=sys.stderr)
        return 77

    poller = select.poll()
    for fd in handles:
        poller.register(fd, select.POLLIN)

    planner = RampPlanner()

    # THE SHAPE OF THIS LOOP IS THE FIX. Each pass: work out how long we may
    # sleep, drain EVERY pending event without acting on them, and only then
    # perform AT MOST ONE apply. Because the drain always completes before the
    # apply, a release that has already arrived is always seen first and the
    # ramp is cancelled before another step can be issued. The old loop applied
    # inside the event walk, which is what let a backlog of autorepeats keep
    # spending volume long after the Owner let go.
    while True:
        wait = planner.wait(time.monotonic())
        ready = poller.poll(None if wait is None else max(0.0, wait) * 1000.0)

        # Taps are NETTED rather than queued. Two presses inside one drain owe
        # 10%, but they owe it as one request -- never as two half-second
        # applies, which is the queue coming back in miniature.
        tap_net = 0
        for fd, flag in ready:
            # A device that disappeared (adapter or keyboard unplugged) is
            # dropped rather than allowed to spin the poll loop.
            if flag & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                poller.unregister(fd)
                os.close(fd)
                del handles[fd]
                monotonic.pop(fd, None)
                if not handles:
                    return 69
                # A rocker cannot still be held on a device that just vanished.
                planner.cancel()
                continue
            for code, value, when in _drain(fd, monotonic[fd]):
                if code in (KEY_VOLUMEUP, KEY_VOLUMEDOWN):
                    louder = (code == KEY_VOLUMEUP)
                    if SWAP_FOR_PANEL_ORIENTATION:
                        louder = not louder
                    if value == KEY_PRESS:
                        tap_net += planner.press(louder, when)
                    elif value == KEY_RELEASE:
                        # NOT the end of a hold -- this rocker sends a release
                        # every ~100 ms while it is held down. It only records
                        # that the finger was still down at this instant.
                        planner.release(when)
                    # KEY_AUTOREPEAT is accepted and ignored. This rocker never
                    # sends one, but an attached USB keyboard would, and its
                    # repeats must not be charged as taps either.
                elif value != KEY_PRESS:
                    continue
                elif code == KEY_MUTE and current_mode() == "bus":
                    # DELIBERATELY INERT IN BUS MODE, and said out loud. Mute is
                    # a position of the on-glass switch now, and a one-way
                    # `set mute` from a key that cannot un-mute would strand the
                    # panel silent for anyone not standing at it. Step 5 gives
                    # the state a previous-output memory and this key a toggle.
                    print("mute key ignored in bus mode: use the on-glass switch",
                          file=sys.stderr, flush=True)
                elif code == KEY_MUTE:
                    # NOTE: the switch is joined across all channels, so this
                    # silences the trigger line too and the amplifier will power
                    # down with it. That may be wanted; it is not a choice this
                    # daemon gets to make separately.
                    card, control, _ = target()
                    _run(card, ["-q", "sset", control, "toggle"])

        # Close the burst if the rocker has gone quiet. Done AFTER the drain and
        # BEFORE the apply, so a finger that came up is known to be up before
        # anything else is spent.
        if planner.settle(time.monotonic()):
            # The gesture is over and fully paid. Land on a round number.
            snap(SNAP_PERCENT)
            continue

        # One apply per pass, tap first. Unmuting rides along inside nudge(), so
        # a volume press always produces sound -- what someone reaching for the
        # rocker means.
        if tap_net:
            nudge(tap_net > 0, abs(tap_net))
            continue
        step = planner.due(time.monotonic())
        if step is not None:
            nudge(step[0], step[1])


if __name__ == "__main__":
    sys.exit(main())
