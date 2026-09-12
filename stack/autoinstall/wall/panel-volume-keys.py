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
STEP = "3%"
CARD = "ICUSBAUDIO7D"
CONTROL = "Speaker"

# Devices are matched by NAME, not by event number: `Intel Virtual Buttons` is a
# WMI device with no stable /dev/input/by-path symlink, and its event number
# moves when USB devices come and go. Any device declaring the volume keys is
# watched, so an attached keyboard works too.
WANTED_KEYS = {KEY_VOLUMEUP, KEY_VOLUMEDOWN, KEY_MUTE}


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


def amixer(*args):
    """Best-effort mixer change. A failed volume nudge must never kill the daemon."""
    try:
        subprocess.run(
            ["/usr/bin/amixer", "-q", "-c", CARD, "sset", CONTROL, *args],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


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
                    # `unmute` rides along so a volume press always produces
                    # sound, which is what someone reaching for the rocker means.
                    amixer(STEP + ("+" if louder else "-"), "unmute")
                elif code == KEY_MUTE:
                    amixer("toggle")


if __name__ == "__main__":
    sys.exit(main())
