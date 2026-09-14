#!/usr/bin/env python3
"""Re-enumerate the amplifier-side USB hub when it comes back empty.

ITEM 25 ADDENDUM, measured on the panel 2026-09-13 21:52. The Owner moved the
hub that carries the USB audio adapter and the LCUS-2 relay to another port. The
hub itself enumerated -- and then none of its downstream ports did:

    hub 1-2:1.0: hub_ext_port_status failed (err = -71)

so the adapter and the CH340 were both absent, `dev-wall_audio_adapter.device`
never came back, and the three units that BindsTo= it stayed stopped exactly as
designed. Item 25's recovery is correct and did nothing, because it depends on
the hub cooperating and the hub had not. A software re-enumeration fixed it:

    echo 0 > /sys/bus/usb/devices/1-2/authorized
    echo 1 > /sys/bus/usb/devices/1-2/authorized

and both devices came back with the units restarting themselves via BindsTo=.

THIS IS DELIBERATELY NOT A HEALTH DAEMON. It is started by a udev add event on
the hub, it watches for a bounded number of seconds, it acts at most twice, and
it exits. Three separate reasons:

* A toggle of `authorized` is a hardware reset of everything downstream. Doing
  that on a timer would mean an unattended panel could reset the amplifier's
  relay repeatedly, and the LCUS-2 LATCHES -- the amplifier's power state
  survives its serial port closing. Bounded action beats continuous authority.
* The interesting window is exactly the one after a re-plug. A hub that has been
  working for a week does not need watching.
* A long-running unit would need its own restart, failure and watchdog policy,
  and would be a second thing that can wedge. A oneshot cannot wedge for longer
  than its own TimeoutStartSec.

WHY TWO CONDITIONS AND NOT ONE. Acting on "the adapter's device unit is not
active" alone would reset the hub every time somebody unplugs the adapter on
purpose, which is a hardware reset of the amplifier's relay as a side effect of
tidying a desk. The measured fault has a second, sharper signature: the hub has
NO CHILDREN AT ALL. A hub with the relay attached but no adapter is a working
hub and an absent adapter, and this exits without touching it.

Exit codes: 0 nothing to do, recovered, or acted and said so. 1 only for a
configuration or sysfs problem worth a failed unit.
"""

import argparse
import os
import re
import subprocess
import sys
import time

SYSFS_USB = "/sys/bus/usb/devices"
ADAPTER_UNIT = "dev-wall_audio_adapter.device"
# The measured Atmel 4-port hub on the amplifier side.
HUB_VENDOR = "03eb"
HUB_PRODUCT = "0902"
# How long the adapter is given to arrive by itself before this intervenes. The
# panel's own item 25 acceptance is ten seconds end to end when the hub
# cooperates, so anything shorter than that would race a recovery that was
# already working.
DEFAULT_WAIT_SECONDS = 15.0
POLL_SECONDS = 1.0
# At most this many toggles for one hub add event, and never more than this many
# inside the cooling window however many events arrive. Re-authorizing a hub can
# itself produce udev events, so the in-process counter alone is not a bound.
MAX_ATTEMPTS = 2
COOL_OFF_SECONDS = 600.0
STATE_DIR = "/run/wall-usb-hub-reset"
# A bus id: 1-2, 1-2.4, 3-1.1.2. Nothing else may be joined to a sysfs path.
# The anchors are deliberate: a plain dollar also matches before a TRAILING
# NEWLINE, so an id with one would be accepted and joined into a path that does
# not exist -- or, with anything after that newline, into one that does.
BUS_ID = re.compile(r"\A\d+-\d+(\.\d+)*\Z")


def log(message):
    print("[wall-usb-hub-reset] %s" % message, file=sys.stderr, flush=True)


def read_attr(path):
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def hub_path(bus_id):
    """The sysfs directory for a bus id, refusing anything that is not one.

    This argument arrives from a udev substitution, so it is not attacker input
    in any ordinary sense -- but it is joined into a path that is then WRITTEN
    to, and `authorized` exists on every USB device on the machine. An
    unvalidated id is how this ends up deauthorizing the root hub, the keyboard
    or the panel's own touchscreen.
    """
    if not BUS_ID.match(bus_id or ""):
        return None
    return os.path.join(SYSFS_USB, bus_id)


def is_the_hub(path):
    """True only for the measured Atmel hub, by VID:PID."""
    return (read_attr(os.path.join(path, "idVendor")).lower() == HUB_VENDOR
            and read_attr(os.path.join(path, "idProduct")).lower() == HUB_PRODUCT)


def children(bus_id, listdir=os.listdir):
    """Downstream devices of this hub, by sysfs naming.

    A child of 1-2 is 1-2.1 .. 1-2.4; 1-2:1.0 is the hub's own INTERFACE and is
    not a child, which is exactly the distinction the fault turns on -- the hub
    presented its interface and no ports.
    """
    prefix = bus_id + "."
    try:
        names = listdir(SYSFS_USB)
    except OSError:
        return []
    return sorted(name for name in names
                  if name.startswith(prefix) and ":" not in name)


def unit_is_active(unit, run=subprocess.run):
    """True active, False inactive, None COULD NOT TELL.

    The third answer is not pedantry. "systemctl did not answer" is not
    evidence that the adapter is absent, and treating it as such is how a hub
    -- and with it the latching amplifier relay -- gets reset on no evidence at
    all. Returning False here was terra's finding; the caller now refuses to
    act on None instead.
    """
    try:
        got = run(["/usr/bin/systemctl", "is-active", "--quiet", unit],
                  check=False, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        log("could not ask systemd about %s: %s" % (unit, exc))
        return None
    if got.returncode == 0:
        return True
    # systemctl exits 3 for "inactive/failed", which is the answer being asked
    # for. Anything else -- a usage error, a dead bus, a killed child -- is not
    # an answer and must not be read as one.
    if got.returncode in (1, 3):
        return False
    log("systemctl is-active %s exited %d; treating that as no answer"
        % (unit, got.returncode))
    return None


def wait_for_adapter(unit, wait_seconds, monotonic=time.monotonic,
                     sleep=time.sleep, run=subprocess.run):
    """True active, False inactive for the whole window, None could not tell.

    A single unanswerable probe ends the wait. Waiting the window out on
    unanswerable probes would produce exactly the "inactive for 15 s" verdict
    that authorizes a hardware reset, on no evidence.
    """
    deadline = monotonic() + wait_seconds
    while True:
        state = unit_is_active(unit, run=run)
        if state is not False:
            return state
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleep(min(POLL_SECONDS, remaining))


def attempts_so_far(bus_id, now=None, state_dir=STATE_DIR):
    """Toggles recorded for this hub inside the cooling window.

    In /run, so a reboot clears it: a panel that has just booted has not been
    fighting with this hub, whatever the file used to say.
    """
    now = time.time() if now is None else now
    stamps = []
    try:
        with open(os.path.join(state_dir, bus_id), encoding="ascii") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        stamps.append(float(line))
                    except ValueError:
                        pass
    except OSError:
        return []
    return [stamp for stamp in stamps if now - stamp < COOL_OFF_SECONDS]


def record_attempt(bus_id, now=None, state_dir=STATE_DIR):
    """Reserve an attempt BEFORE it is made. False means it was not reserved.

    Fail CLOSED, which was terra's finding: the /run file is the only bound
    that survives process exit, and re-authorizing a hub can itself produce the
    add events that start the next process. If the reservation cannot be
    written, a hub that keeps coming back empty would get two fresh toggles per
    event forever. Not toggling at all is the safe side of that.
    """
    now = time.time() if now is None else now
    kept = attempts_so_far(bus_id, now=now, state_dir=state_dir) + [now]
    try:
        os.makedirs(state_dir, exist_ok=True)
        tmp = os.path.join(state_dir, bus_id + ".tmp")
        with open(tmp, "w", encoding="ascii") as handle:
            handle.write("".join("%.3f\n" % stamp for stamp in kept))
        os.replace(tmp, os.path.join(state_dir, bus_id))
        return True
    except OSError as exc:
        log("could not record the attempt for %s: %s -- NOT resetting the hub. "
            "The only bound that survives this process lives in that file, so "
            "without it a hub that keeps coming back empty would be reset "
            "again on every event." % (bus_id, exc))
        return False


def toggle_authorized(path, sleep=time.sleep, settle=1.0):
    """0 then 1 on the hub's `authorized`, which re-enumerates everything below.

    Both writes are reported. A failed 0 leaves the hub exactly as it was; a
    failed 1 leaves it DEAUTHORIZED, which is strictly worse than the fault
    being recovered from, so that one is said loudly with the cure in the line.
    """
    control = os.path.join(path, "authorized")
    try:
        with open(control, "w", encoding="ascii") as handle:
            handle.write("0\n")
    except OSError as exc:
        log("could not deauthorize %s: %s" % (control, exc))
        return False
    sleep(settle)
    try:
        with open(control, "w", encoding="ascii") as handle:
            handle.write("1\n")
    except OSError as exc:
        log("DEAUTHORIZED %s AND COULD NOT RE-AUTHORIZE IT: %s -- every device "
            "on this hub is now off the bus. Recover with: echo 1 > %s"
            % (control, exc, control))
        return False
    return True


def main(argv=None, sleep=time.sleep, monotonic=time.monotonic,
         run=subprocess.run, state_dir=STATE_DIR):
    parser = argparse.ArgumentParser(
        description="Reset the amplifier-side USB hub if it enumerates empty.")
    parser.add_argument("bus_id", help="the hub's sysfs bus id, e.g. 1-2")
    parser.add_argument("--unit", default=ADAPTER_UNIT)
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS)
    args = parser.parse_args(argv)

    path = hub_path(args.bus_id)
    if path is None:
        log("refusing %r: not a USB bus id" % args.bus_id)
        return 1
    if not os.path.isdir(path):
        # The hub left again while this was starting. Not an error.
        log("%s is gone; nothing to do" % args.bus_id)
        return 0
    if not is_the_hub(path):
        log("%s is not the %s:%s hub; nothing to do"
            % (args.bus_id, HUB_VENDOR, HUB_PRODUCT))
        return 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        state = wait_for_adapter(args.unit, args.wait, monotonic=monotonic,
                                 sleep=sleep, run=run)
        if state is True:
            if attempt > 1:
                log("%s is active after %d hub reset(s)"
                    % (args.unit, attempt - 1))
            return 0
        if state is None:
            log("could not establish whether %s is active. NOT resetting %s: "
                "resetting this hub is a hardware reset of the amplifier's "
                "latching relay and needs evidence, not a timeout."
                % (args.unit, args.bus_id))
            return 1

        kids = children(args.bus_id)
        if kids:
            log("%s is inactive but the hub has children (%s); the adapter is "
                "absent, not the hub. Not resetting."
                % (args.unit, ", ".join(kids)))
            return 0

        recorded = attempts_so_far(args.bus_id, state_dir=state_dir)
        if len(recorded) >= MAX_ATTEMPTS:
            log("%s answered with no children again, but %d reset(s) were "
                "already made in the last %.0f s. NOT resetting: this needs a "
                "person, not another toggle."
                % (args.bus_id, len(recorded), COOL_OFF_SECONDS))
            return 0

        log("%s: hub present, no downstream ports, %s inactive for %.0f s. "
            "Toggling authorized (attempt %d of %d)."
            % (args.bus_id, args.unit, args.wait, attempt, MAX_ATTEMPTS))
        if not record_attempt(args.bus_id, state_dir=state_dir):
            return 1
        if not toggle_authorized(path, sleep=sleep):
            return 1

    if wait_for_adapter(args.unit, args.wait, monotonic=monotonic,
                        sleep=sleep, run=run) is True:
        log("%s is active after %d hub reset(s)" % (args.unit, MAX_ATTEMPTS))
        return 0
    log("%s is still inactive after %d hub reset(s). Giving up so the failure "
        "is visible rather than repeated." % (args.unit, MAX_ATTEMPTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
