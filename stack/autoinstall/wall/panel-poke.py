#!/usr/bin/env python3
"""panel-poke.py - synthesize ONE input event on the wall panel's seat.

WHY THIS EXISTS, and it is the whole answer to "why did virtual testing not
catch the broken music player" (2026-09-06):

    The panel's lower area - the tabs and the music player - EXISTS ONLY IN
    SPLIT, and SPLIT exists only while an interaction is recent. Every
    screenshot verify-panel.sh has ever taken was taken with nobody touching
    the panel, so the panel was in FULL, where the lower area is
    `--lower-h: 0%` and `opacity: 0`. The player was not rendered wrong in
    those captures; it was not IN them. No amount of looking at that PNG could
    have found it.

    So a checker that wants to see the player has to be the interaction. This
    is that interaction, and nothing more.

MECHANISM. `cage` is a Wayland compositor and Wayland has no injection API for
clients - by design, and correctly. The seat is fed by libinput reading
/dev/input/event*, so the honest way in is one level down: create a virtual
input device with /dev/uinput, emit one event through it, destroy it. libinput
picks the device up like any other USB peripheral and the compositor cannot
tell (and should not care) that it was not a finger.

/dev/uinput is 0600 root:root, so this needs root. On the panel `sudo -n` is
NOPASSWD for the kiosk user, so it does not need a human.

No third-party module: `evdev` is not installed on the panel image and this is
not worth an apt dependency on a kiosk. Raw ioctls and two structs.

    sudo -n python3 panel-poke.py key           # a Shift press - counts as
                                                # input, types nothing
    sudo -n python3 panel-poke.py tap 960 540   # a tap at absolute x,y
    sudo -n python3 panel-poke.py park          # shove the pointer into the
                                                # corner named by
                                                # WALL_CURSOR_PARK_CORNER
    sudo -n python3 panel-poke.py park top-left # ...or an explicit one

`tap` is what opens the check-off list (the ambient NagLight layer turns a
click into the drill). `key` is enough to bring the panel out of FULL.

`park` IS NOT A TEST HELPER - it is the fix for the arrow in the middle of the
wall, and it belongs at session start. Measured 2026-09-06 with `grim -c`:

  * `cage` draws its OWN default cursor at the centre of the output from the
    moment the session starts, and it keeps drawing it. A capture taken after
    40 s with nobody touching anything still had an arrow in the middle.
  * the page's `cursor: none` could not remove it, because Chromium only sets a
    cursor image for a pointer that has ENTERED its surface, and on a
    touch-only panel no pointer ever moves. The CSS was correct and inert.
  * ONE relative motion fixes both halves at once: it moves the pointer out of
    the middle AND hands it to Chromium, after which `cursor: none` finally
    applies. After a park, a full-screen `grim -c` shows no cursor anywhere -
    including immediately after a touch.

Wayland has no pointer-warp for applications, so this cannot be done from
inside the shell. It is a seat-level action, which is why it lives here.

SAFETY. `key` sends KEY_LEFTSHIFT, which is inert: the shell listens for
`keydown` and does not read which key. `tap` DOES press the UI underneath it,
so a tap at the wrong coordinate can check an item off. Aim it at the ambient
layer (the top half) and it only ever opens the drill.
"""

import fcntl
import os
import struct
import sys
import time

# ioctls from <linux/uinput.h>: _IOW('U', n, int) and _IO('U', n).
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_SET_RELBIT = 0x40045566
UI_SET_PROPBIT = 0x4004556E
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

EV_SYN, EV_KEY, EV_ABS, EV_REL = 0x00, 0x01, 0x03, 0x02
SYN_REPORT = 0
KEY_LEFTSHIFT = 42
BTN_TOUCH = 0x14A
ABS_X, ABS_Y = 0x00, 0x01
REL_X, REL_Y = 0x00, 0x01
BTN_LEFT = 0x110

# INPUT_PROP_DIRECT is what makes this a TOUCHSCREEN rather than a tablet or an
# absolute mouse, and it changes the answer the shell gets. Without it libinput
# classifies the device as a pointer, Chromium reports `pointerType: "mouse"`
# for the events, and the shell's cursor policy — which deliberately reveals
# the arrow for a mouse and never for a finger — does the wrong thing. A probe
# that does not present itself as the hardware it stands in for measures its own
# disguise. (Found by doing exactly that, 2026-09-06.)
INPUT_PROP_DIRECT = 0x01

# A device is only "settled" once libinput has seen the udev event and the
# compositor has added it to the seat. Emitting before that goes nowhere and
# looks exactly like a bug in the shell, so it is worth a full second.
SETTLE_S = 1.0

# struct uinput_user_dev: name[80] + input_id(8) + ff_effects_max(4)
# + absmax/absmin/absfuzz/absflat, 64 int32 each.
UDEV_SIZE = 80 + 8 + 4 + 4 * 64 * 4
ABSMAX_OFF = 92
ABSMIN_OFF = ABSMAX_OFF + 64 * 4

# The virtual screen this device reports. Absolute coordinates are scaled by
# the compositor onto the real output, so these need not match the panel's
# resolution - but matching it makes "tap 960 540" mean pixels, which is what
# anybody calling this will assume.
ABS_RANGE = (0, 4095)


def _open():
    return os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)


def _create(fd, name, absolute=False):
    dev = bytearray(UDEV_SIZE)
    raw = name.encode()[:79]
    dev[0 : len(raw)] = raw
    struct.pack_into("<HHHH", dev, 80, 0x03, 0x1234, 0x5678, 1)  # BUS_USB
    if absolute:
        for axis in (ABS_X, ABS_Y):
            struct.pack_into("<i", dev, ABSMAX_OFF + axis * 4, ABS_RANGE[1])
            struct.pack_into("<i", dev, ABSMIN_OFF + axis * 4, ABS_RANGE[0])
    os.write(fd, bytes(dev))
    fcntl.ioctl(fd, UI_DEV_CREATE)
    time.sleep(SETTLE_S)


def _emit(fd, etype, code, value):
    # input_event on a 64-bit kernel: two __kernel_ulong_t (8 bytes each) for
    # the timeval, then u16 type, u16 code, s32 value = 24 bytes. Getting this
    # wrong is an EINVAL from write(2) that says nothing about the cause.
    os.write(fd, struct.pack("<qqHHi", 0, 0, etype, code, value))


def _syn(fd):
    _emit(fd, EV_SYN, SYN_REPORT, 0)


def poke_key(hold=0.05):
    fd = _open()
    try:
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_KEYBIT, KEY_LEFTSHIFT)
        _create(fd, "wall-probe-key")
        _emit(fd, EV_KEY, KEY_LEFTSHIFT, 1)
        _syn(fd)
        time.sleep(hold)
        _emit(fd, EV_KEY, KEY_LEFTSHIFT, 0)
        _syn(fd)
        time.sleep(0.2)
        fcntl.ioctl(fd, UI_DEV_DESTROY)
    finally:
        os.close(fd)


def poke_tap(x, y, width, height, hold=0.12):
    """One tap at pixel (x, y) on a width x height output."""
    ax = int(round(x / max(1, width - 1) * ABS_RANGE[1]))
    ay = int(round(y / max(1, height - 1) * ABS_RANGE[1]))
    fd = _open()
    try:
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
        fcntl.ioctl(fd, UI_SET_ABSBIT, ABS_X)
        fcntl.ioctl(fd, UI_SET_ABSBIT, ABS_Y)
        fcntl.ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)
        _create(fd, "wall-probe-touch", absolute=True)
        # Position BEFORE the press: a press at a stale position is a tap
        # somewhere else, and on this UI "somewhere else" can be a checkbox.
        _emit(fd, EV_ABS, ABS_X, ax)
        _emit(fd, EV_ABS, ABS_Y, ay)
        _syn(fd)
        _emit(fd, EV_KEY, BTN_TOUCH, 1)
        _syn(fd)
        time.sleep(hold)
        _emit(fd, EV_KEY, BTN_TOUCH, 0)
        _syn(fd)
        time.sleep(0.2)
        fcntl.ioctl(fd, UI_DEV_DESTROY)
    finally:
        os.close(fd)


CORNERS = {
    "top-left": (-1, -1),
    "top-right": (1, -1),
    "bottom-left": (-1, 1),
    "bottom-right": (1, 1),
}


def poke_park(corner="bottom-right", steps=40, step=500):
    """Drive the pointer hard into a corner of the output.

    Relative motion, not absolute: the compositor clamps at the output edge, so
    "further than the screen is wide" lands exactly in the corner without this
    needing to know the resolution, and the corner is only the SIGN of the two
    deltas. BTN_LEFT is declared but NEVER emitted - a device with no buttons at
    all is not necessarily treated as a pointer, and a stray click on a kiosk
    can check an item off.
    """
    if corner not in CORNERS:
        raise ValueError(f"unknown corner '{corner}' (expected one of {', '.join(sorted(CORNERS))})")
    sx, sy = CORNERS[corner]
    fd = _open()
    try:
        for code in (EV_KEY, EV_REL):
            fcntl.ioctl(fd, UI_SET_EVBIT, code)
        fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_LEFT)
        fcntl.ioctl(fd, UI_SET_RELBIT, REL_X)
        fcntl.ioctl(fd, UI_SET_RELBIT, REL_Y)
        _create(fd, "wall-park")
        # Many small steps rather than one huge one: a single enormous delta can
        # be discarded as implausible by pointer-acceleration filters.
        for _ in range(steps):
            _emit(fd, EV_REL, REL_X, sx * step)
            _emit(fd, EV_REL, REL_Y, sy * step)
            _syn(fd)
            time.sleep(0.005)
        time.sleep(0.3)
        fcntl.ioctl(fd, UI_DEV_DESTROY)
    finally:
        os.close(fd)


def main(argv):
    what = argv[1] if len(argv) > 1 else "key"
    if what == "key":
        poke_key()
        print("poked: key")
        return 0
    if what == "tap":
        if len(argv) < 4:
            print("usage: panel-poke.py tap X Y [WIDTH HEIGHT]", file=sys.stderr)
            return 2
        x, y = int(argv[2]), int(argv[3])
        w = int(argv[4]) if len(argv) > 4 else 1920
        h = int(argv[5]) if len(argv) > 5 else 1080
        poke_tap(x, y, w, h)
        print(f"poked: tap {x},{y} of {w}x{h}")
        return 0
    if what == "park":
        # The corner comes from WALL_CURSOR_PARK_CORNER when it is not given on
        # the command line, so the unit needs no argument and the knob is the
        # single place the answer lives (wall.env -> kiosk.env -> here).
        corner = argv[2] if len(argv) > 2 else os.environ.get(
            "WALL_CURSOR_PARK_CORNER", "bottom-right")
        try:
            poke_park(corner)
        except ValueError as err:
            print(f"panel-poke: {err}", file=sys.stderr)
            return 2
        print(f"poked: park (pointer driven to the {corner} corner)")
        return 0
    print(f"unknown action '{what}' (expected 'key', 'tap' or 'park')", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except PermissionError:
        print("panel-poke: /dev/uinput needs root - run under sudo -n", file=sys.stderr)
        sys.exit(77)
    except FileNotFoundError:
        print("panel-poke: no /dev/uinput - the uinput module is not loaded", file=sys.stderr)
        sys.exit(69)
