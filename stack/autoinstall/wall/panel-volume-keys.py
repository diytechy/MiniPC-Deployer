#!/usr/bin/env python3
"""Drive the amplifier output volume from the panel's side rocker.

WHY THIS IS A DAEMON AND NOT A COMPOSITOR BINDING (2026-09-12):
the rocker already emits standard keycodes — `Intel Virtual Buttons` declares
KEY_VOLUMEUP and KEY_VOLUMEDOWN — but `wall-kiosk.sh` runs Electron under cage
from a tty autologin session, not from a unit, and cage has no configurable key
bindings. Reading evdev directly is compositor-independent and keeps working if
the shell is restarted, which `pkill -f runtime/electron` does routinely.

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

# struct input_event on 64-bit Linux: struct timeval (two longs) then
# __u16 type, __u16 code, __s32 value.
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
assert EVENT_SIZE == 24

EV_KEY = 0x01
KEY_MUTE = 113
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115

# Press and autorepeat both act; release does not. Holding the rocker ramps.
ACTING_VALUES = (1, 2)

# THE PANEL IS MOUNTED ROTATED relative to the way the rocker was labelled, so
# the button that sits physically uppermost on the wall is the one reporting
# KEY_VOLUMEDOWN. Reaching up to make it quieter is wrong in the only way a
# volume control can be wrong, so the two keycodes are swapped here. This is a
# property of how the panel hangs, not of the hardware: if it is ever remounted
# the right way up, set this back to False rather than rewiring anything.
SWAP_FOR_PANEL_ORIENTATION = True

# 3% per event is a ramp that reaches either end in about a second of holding
# without being twitchy. The adapter's `Speaker` control spans a wide dB range
# (20% is already -29.6 dB), so percent steps, not absolute steps, are right.
STEP_FRACTION = 0.03
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

# Devices are matched by NAME, not by event number: `Intel Virtual Buttons` is a
# WMI device with no stable /dev/input/by-path symlink, and its event number
# moves when USB devices come and go. Any device declaring the volume keys is
# watched, so an attached keyboard works too.
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


VOLUME_SOCKET = "/run/wall-volume-request.sock"


def nudge_bus(louder):
    """Request one relative bus step through the fixed-direction root socket.

    Return success only after the applier completes. A failure is journaled;
    bus mode must never fall back to a physical mixer and disrupt its graph.
    Implements: SR-028, LLR-014.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(24)
            connection.connect(VOLUME_SOCKET)
            connection.sendall(b"up\n" if louder else b"down\n")
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


def nudge(louder):
    """Move the front pair only, on whichever card the current mode is using."""
    if current_mode() == "bus":
        nudge_bus(louder)
        return
    card, control, kcontrol = target()
    state = _read_volume(card, kcontrol)
    if state is None:
        return
    values, maximum = state
    step = max(1, int(round(maximum * STEP_FRACTION)))
    for i in FRONT_CHANNELS:
        if i < len(values):
            values[i] = min(maximum, max(0, values[i] + (step if louder else -step)))
    _run(card, ["cset", "name=" + kcontrol, ",".join(str(v) for v in values)])
    # The mute switch is pswitch-joined, so this is one switch for every channel.
    _run(card, ["-q", "sset", control, "unmute"])


def main():
    paths = find_devices()
    if not paths:
        print("no input device declares volume keys", file=sys.stderr)
        return 69

    handles = {}
    for path in paths:
        try:
            handles[os.open(path, os.O_RDONLY)] = path
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

    while True:
        for fd, flag in poller.poll():
            # A device that disappeared (adapter or keyboard unplugged) is
            # dropped rather than allowed to spin the poll loop.
            if flag & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                poller.unregister(fd)
                os.close(fd)
                del handles[fd]
                if not handles:
                    return 69
                continue
            try:
                data = os.read(fd, EVENT_SIZE * 64)
            except OSError:
                continue
            for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _, _, etype, code, value = struct.unpack_from(
                    EVENT_FORMAT, data, offset
                )
                if etype != EV_KEY or value not in ACTING_VALUES:
                    continue
                if code in (KEY_VOLUMEUP, KEY_VOLUMEDOWN):
                    louder = (code == KEY_VOLUMEUP)
                    if SWAP_FOR_PANEL_ORIENTATION:
                        louder = not louder
                    # Unmuting rides along inside nudge(), so a volume press
                    # always produces sound -- what someone reaching for the
                    # rocker means.
                    nudge(louder)
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


if __name__ == "__main__":
    sys.exit(main())
