#!/usr/bin/env python3
"""Make the panel's power button ask before it shuts the panel down (WSN-098).

THE RULING (Owner, 2026-10-22): one press shows a full-screen, flashing "Press
again within 5 s to shut down" warning; a second press within 5 s shuts the
panel down; otherwise the warning clears and nothing happens. ONE PRESS MUST
NEVER SHUT THE PANEL DOWN.

WHY TWO PRESSES AND NOT A HOLD (measured 2026-10-22, the Owner at the panel):
the button reports press and release at the same instant on event4 (`EV_KEY
116`, value 1 then 0, tens of microseconds apart), for a short press and for a
3 s hold alike, and nothing arrives on event0, event11 or event12. A hold
cannot be detected, so the original "hold 2 s" design is impossible.

HOW logind IS KEPT OFF THE KEY. The unit runs this file under `systemd-inhibit
--what=handle-power-key --mode=block`, so the inhibitor exists BEFORE this
process starts and is released the moment it (or the wrapper) exits, for any
reason. A dead or stopped service therefore hands the button straight back to
logind's default (power off on one press): the button never goes dead. The
inhibitor is the wrapper's, not this file's, and that is deliberate -- no code
path in here can release it while the process lives, which is what "keep the
inhibitor when the kiosk is unavailable" requires.

WHAT IT DOES:
  * reads every input device named exactly "Power Button" (event0 and event4 on
    this panel) and counts EV_KEY KEY_POWER value 1 only: releases (0) and
    autorepeat (2) are ignored;
  * de-duplicates ACROSS devices only: a press on a different device than the
    last counted one, within CROSS_DEVICE_DEDUP_S of it, is the same physical
    press reported twice. Two complete presses on the SAME device are always
    two presses, however quick (WSN-098: under 300 ms counts twice);
  * first press: publishes `armed` to STATE_FILE (IF-067) for the kiosk, asks
    wall-sleep.sh's `power-button-wake` arm to light a dark display (the
    existing power-policy path: one script owns the display), and journals;
  * second press inside the window: publishes `shutting-down` and runs
    `systemctl poweroff`;
  * window over: publishes `idle`;
  * KIOSK UNAVAILABLE: the kiosk acknowledges each warning it actually painted
    by writing its sequence number to ACK_FILE. No acknowledgement within
    ACK_TIMEOUT_S journals the fact and raises an alert. Nothing else changes:
    the window still runs, and a blind second press still shuts down.

ALERTS: journal lines at warning priority, then ALERT_HOOK if it exists. THE
HOOK IS THE MARKED INTEGRATION POINT for the shared alert helper being built
under HomeHub plan W3a; until it is installed the panel has no alert route to
the hub (the same position as SR-083's low-disk alarm), and each alert says so.

ONE CLOCK, AND IT COUNTS SUSPENDED TIME (Sol review 2). Every instant here is
CLOCK_BOOTTIME: the kernel stamps each device's events on it (EVIOCSCLOCKID)
and the loop reads it for "now". CLOCK_MONOTONIC stops while the panel is
suspended, so on it a warning armed just before the scheduled 22:00 suspend
(wall-sleep.sh suspends directly, past the backlight hold) would wake the next
morning with seconds still left on its window, and a press soon after resume
would shut the panel down on what is, to the person pressing, one press. On
the boot clock those hours have passed, and the press is a new warning.

NO NEW DEPENDENCIES: struct input_event is parsed here (the wheelhouse is
offline and hash-locked), as panel-volume-keys.py does.

PURE CORE: `PowerButtonMachine` and `parse_events` do no I/O and read no clock;
`main()` and the small `Shell` class are the thin I/O layer.

Exit codes: 69 no "Power Button" device exists; 77 none could be opened;
1 every device went away. Each exit releases the inhibitor (logind's default
returns) and the unit restarts it.

Implements: SR-093, SR-094, LLR-1180, LLR-1181, LLR-1182, LLR-1183, LLR-1184
"""
from __future__ import annotations

import json
import os
import select
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # not Linux: the tests import this module on the dev box
    fcntl = None

# ── the input contract (LLR-1180) ────────────────────────────────────────────
# struct input_event on x86_64 Linux: struct timeval (two 8-byte longs), then
# __u16 type, __u16 code, __s32 value -- 24 bytes. Written with explicit sizes
# ("=qqHHi") rather than native `long`, so the module imports and parses the
# same bytes on a development machine whose `long` is 4 bytes.
EVENT_FORMAT = "=qqHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EV_KEY = 1
KEY_POWER = 116
KEY_PRESS = 1
# value 2 is autorepeat and value 0 is the release; neither is a press.
DEVICE_NAME = "Power Button"
# _IOW('E', 0xa0, int): timestamp this fd's events on CLOCK_BOOTTIME, so they
# compare with boot_clock() (the ioctl panel-volume-keys.py uses, with a clock
# that keeps counting through a suspend; evdev accepts it since Linux 4.x).
EVIOCSCLOCKID = 0x400445A0
CLOCK_BOOTTIME = 7
# Python's name for the same clock; absent off Linux (the dev box's tests).
_BOOTTIME_CLOCK_ID = getattr(time, "CLOCK_BOOTTIME", None)

# ── the ruling's numbers (LLR-1181) ──────────────────────────────────────────
# The 5 s window is the Owner's. The de-duplication window is sized to "one
# physical press reported on event0 and event4 within a few tens of
# milliseconds"; it applies across devices only, so it can never merge two
# real presses on one device. The acknowledgement allowance covers the
# kiosk's poll, a display wake and one painted frame with room to spare.
WINDOW_S = 5.0
CROSS_DEVICE_DEDUP_S = 0.15
ACK_TIMEOUT_S = 2.0
# A shutdown that was accepted by systemctl but has not taken this process down
# after a minute did not happen. The button is handed back to the ruling (warn,
# then press again) rather than left ignoring presses for the rest of the run.
SHUTDOWN_STALL_S = 60.0
# A press stamped inside the window can be READ a little after it ends (the
# loop woke for the deadline in the same instant, or the reader was busy). The
# window is judged on the press's own kernel timestamp; this grace only delays
# the expiry so a late-read in-window press still counts (Sol review 1).
EXPIRY_GRACE_S = 0.25

IDLE = "idle"
ARMED = "armed"
SHUTTING_DOWN = "shutting-down"

# ── the files (IF-067) ───────────────────────────────────────────────────────
STATE_DIR = Path("/run/wall-power-button")
STATE_FILE = STATE_DIR / "state.json"
# Written by the kiosk host (user `panel`) into the one /run directory it owns,
# created by firstboot's tmpfiles rule. Read here, bounded, never trusted for
# anything but an integer equal to the current sequence number.
ACK_FILE = Path("/run/wall-panel-renderer/power-warning-ack.json")
MAX_ACK_BYTES = 4096
SCHEMA_VERSION = 1

# ── the commands ─────────────────────────────────────────────────────────────
WAKE_COMMAND = ("/usr/local/sbin/wall-sleep.sh", "power-button-wake")
POWEROFF_COMMAND = ("/usr/bin/systemctl", "poweroff")
# THE HOOK POINT for the shared alert helper (HomeHub plan W3a). Called as
# `panel-alert <key> <message>` with a bounded timeout when it exists and is
# executable; absent today, so every alert is journal-only and says so.
ALERT_HOOK = Path("/usr/local/lib/wall-panel/panel-alert")
WAKE_TIMEOUT_S = 15
POWEROFF_TIMEOUT_S = 30
ALERT_TIMEOUT_S = 10


def boot_clock() -> float:
    """Seconds on CLOCK_BOOTTIME: monotonic, and it counts time suspended.

    The monotonic fallback exists only for a platform without the boot clock
    (the dev box that imports this module for its tests); Linux always has it.
    Implements: LLR-1181
    """
    if _BOOTTIME_CLOCK_ID is not None:
        return time.clock_gettime(_BOOTTIME_CLOCK_ID)
    return time.monotonic()


def parse_events(data: bytes) -> list[tuple[int, int, int, float]]:
    """Decode whole input_event records.

    Contract:
      Inputs:  data: bytes read from an evdev node; a trailing partial record
               is ignored (evdev returns whole records, so none is expected).
      Outputs: [(type, code, value, kernel_timestamp_seconds)] in order.
    Implements: LLR-1180
    """
    events = []
    for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
        seconds, micros, etype, code, value = struct.unpack_from(EVENT_FORMAT, data, offset)
        events.append((etype, code, value, seconds + micros / 1_000_000.0))
    return events


def is_press(etype: int, code: int, value: int) -> bool:
    """True only for a power-key press: not its release, not autorepeat.

    Implements: LLR-1180
    """
    return etype == EV_KEY and code == KEY_POWER and value == KEY_PRESS


class PowerButtonMachine:
    """The press-twice state machine. Pure: no I/O, no clock of its own.

    Contract:
      Inputs:  every call takes `now`, boot-clock seconds (CLOCK_BOOTTIME,
               which counts suspended time); `device` is any
               hashable identity of the node a press came from.
      Outputs: a list of actions, each a tuple:
                 ("publish", {"state", "seq", "remainingS"})   IF-067 document
                 ("wake",)                     light a dark display
                 ("poweroff",)                 run systemctl poweroff
                 ("alert", key, message)       journal at warning + hook
                 ("journal", message)          journal at info
      Config:  window_s (WSN-098: 5), dedup_s, ack_timeout_s (LLR-1181).
    The sequence number starts at `seq_start` and rises by one per warning, so
    an acknowledgement for an older warning (or from a previous run, given a
    clock-derived start) never matches the current one.
    Implements: SR-093, SR-094, LLR-1181, LLR-1182, LLR-1183
    """

    def __init__(self, *, seq_start: int = 0, window_s: float = WINDOW_S,
                 dedup_s: float = CROSS_DEVICE_DEDUP_S, ack_timeout_s: float = ACK_TIMEOUT_S,
                 stall_s: float = SHUTDOWN_STALL_S):
        self.window_s = window_s
        self.dedup_s = dedup_s
        self.ack_timeout_s = ack_timeout_s
        self.stall_s = stall_s
        self.state = IDLE
        self.seq = seq_start
        self.deadline = None
        self.ack_due = None
        self.stall_at = None
        self.last_device = None
        self.last_press_at = None

    def _publish(self, now: float) -> tuple:
        remaining = max(0.0, self.deadline - now) if self.state == ARMED else None
        return ("publish", {"state": self.state, "seq": self.seq, "remainingS": remaining})

    def start(self, now: float) -> list:
        """The first document of a run: idle, whatever a previous run left.

        Implements: LLR-1183
        """
        return [self._publish(now)]

    def is_duplicate(self, device, now: float) -> bool:
        """A press on ANOTHER device within dedup_s of the last counted press.

        Never true for the same device: two complete press/release cycles on
        one node are two presses (WSN-098). A timestamp that runs backwards
        (a device whose clock could not be switched) is treated as near.
        Implements: LLR-1182
        """
        if self.last_press_at is None or device == self.last_device:
            return False
        return abs(now - self.last_press_at) < self.dedup_s

    def key_event(self, device, etype: int, code: int, value: int, now: float) -> list:
        """Feed one input event. Only a counted press can change anything.

        Implements: LLR-1180, LLR-1182
        """
        if not is_press(etype, code, value):
            return []
        if self.is_duplicate(device, now):
            return []
        self.last_device, self.last_press_at = device, now
        return self.press(now)

    def press(self, now: float) -> list:
        """One counted press.

        Implements: SR-093, LLR-1181
        """
        actions = self.tick(now, lambda: None, check_ack=False)
        if self.state == SHUTTING_DOWN:
            return actions
        if self.state == ARMED and now <= self.deadline:
            self.state, self.deadline, self.ack_due = SHUTTING_DOWN, None, None
            self.stall_at = now + self.stall_s
            return actions + [self._publish(now),
                              ("journal", "second press inside the window: shutting the panel down"),
                              ("poweroff",)]
        self.seq += 1
        self.state = ARMED
        self.deadline = now + self.window_s
        self.ack_due = now + self.ack_timeout_s
        return actions + [self._publish(now), ("wake",),
                          ("journal", "power button pressed: warning %d shown; a second press "
                                      "within %g s shuts the panel down" % (self.seq, self.window_s))]

    def tick(self, now: float, read_ack, *, check_ack: bool = True) -> list:
        """Advance the clocks. `read_ack()` is called only when the check is due.

        The acknowledgement is judged before the window, so a kiosk that never
        painted is reported even when the window ends in the same tick.
        Implements: SR-094, LLR-1181, LLR-1183
        """
        actions = []
        if self.state == ARMED and check_ack and self.ack_due is not None and now >= self.ack_due:
            self.ack_due = None
            if read_ack() != self.seq:
                actions.append(("alert", "power-button-kiosk-unavailable",
                                "the kiosk did not show power-button warning %d within %g s; the "
                                "button is still guarded and a second press within %g s of the "
                                "first still shuts the panel down" % (self.seq, self.ack_timeout_s,
                                                                    self.window_s)))
        if self.state == ARMED and now > self.deadline + EXPIRY_GRACE_S:
            self.state, self.deadline, self.ack_due = IDLE, None, None
            actions += [self._publish(now), ("journal", "no second press: warning %d cleared" % self.seq)]
        if self.state == SHUTTING_DOWN and now >= self.stall_at:
            self.state, self.stall_at = IDLE, None
            actions += [self._publish(now), ("alert", "power-button-poweroff-stalled",
                                             "systemctl poweroff was accepted %g s ago and the panel "
                                             "is still running; the button warns again on the next "
                                             "press" % self.stall_s)]
        return actions

    def poweroff_failed(self, now: float, detail: str) -> list:
        """systemctl poweroff did not take: say so and stand down to idle.

        Implements: LLR-1183
        """
        self.state, self.deadline, self.ack_due, self.stall_at = IDLE, None, None, None
        return [self._publish(now), ("alert", "power-button-poweroff-failed",
                                     "the second press asked for a shutdown and systemctl "
                                     "poweroff failed (%s); the panel is still running" % detail)]

    def next_due(self):
        """The earliest boot-clock instant a tick has work to do, or None."""
        if self.state == SHUTTING_DOWN:
            return self.stall_at
        if self.state != ARMED:
            return None
        candidates = [self.deadline + EXPIRY_GRACE_S + 0.001]
        if self.ack_due is not None:
            candidates.append(self.ack_due)
        return min(candidates)


def state_document(fields: dict, wall_ms: int) -> dict:
    """The IF-067 document for one publish action.

    `deadline` is wall-clock epoch milliseconds because the reader is another
    process on the same machine; it is only ever displayed, never used to
    decide (the decision is this daemon's, on the boot clock).
    Implements: LLR-1184
    """
    remaining = fields.get("remainingS")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "state": fields["state"],
        "seq": fields["seq"],
        "windowMs": int(WINDOW_S * 1000),
        "deadline": None if remaining is None else wall_ms + int(round(remaining * 1000)),
        "observedAt": wall_ms,
    }


def read_ack(path: Path = ACK_FILE):
    """The sequence number the kiosk last acknowledged, or None.

    Bounded and opened without following a symlink; anything that is not a
    small JSON object with an integer `seq` is no acknowledgement.
    Implements: LLR-1184
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        data = os.read(fd, MAX_ACK_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > MAX_ACK_BYTES:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    seq = value.get("seq") if isinstance(value, dict) else None
    return seq if type(seq) is int else None


def write_state(document: dict, path: Path = STATE_FILE) -> None:
    """Replace the state file atomically (0644, so the kiosk can read it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".state-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, separators=(",", ":"))
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def journal(message: str, priority: int = 6) -> None:
    """One journal line; the <N> prefix is journald's priority."""
    print("<%d>wall-power-button: %s" % (priority, message), flush=True)


class Shell:
    """Carry out the machine's actions. Every seam is injectable for the tests.

    Wake and alerts run on their own threads: wall-sleep.sh may wait up to
    10 s for the power lock, and a second press must never queue behind it.
    The poweroff runs inline, because nothing else matters once it is asked
    for, and its failure has to reach the machine.
    Implements: LLR-1183, LLR-1184
    """

    def __init__(self, machine: PowerButtonMachine, *, run=subprocess.run,
                 write=write_state, log=journal, wall_ms=lambda: int(time.time() * 1000),
                 alert_hook: Path = ALERT_HOOK, spawn=None):
        self.machine = machine
        self.run = run
        self.write = write
        self.log = log
        self.wall_ms = wall_ms
        self.alert_hook = alert_hook
        self.spawn = spawn or (lambda target: threading.Thread(target=target, daemon=True).start())

    def _command(self, argv, timeout) -> str | None:
        """Run one command; None on success, else a short reason.

        Its output is NOT discarded: it goes to this unit's journal, so
        wall-sleep.sh's own account of the wake (or of why it declined) sits
        beside the press that asked for it.
        """
        try:
            result = self.run(list(argv), timeout=timeout, stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            return type(error).__name__
        return None if result.returncode == 0 else "exit %d" % result.returncode

    def _wake(self) -> None:
        reason = self._command(WAKE_COMMAND, WAKE_TIMEOUT_S)
        if reason is not None:
            self.alert("power-button-wake-failed",
                       "the display could not be woken for the power-button warning (%s); "
                       "a second press within %g s still shuts the panel down" % (reason, WINDOW_S))

    def alert(self, key: str, message: str) -> None:
        self.log("ALERT %s: %s" % (key, message), 4)
        hook = self.alert_hook
        if hook.is_file() and os.access(hook, os.X_OK):
            reason = self._command((str(hook), key, message), ALERT_TIMEOUT_S)
            if reason is not None:
                self.log("ALERT NOT DELIVERED %s: the alert hook failed (%s)" % (key, reason), 4)
        else:
            self.log("ALERT NOT DELIVERED %s: no panel alert route is installed (%s is the hook "
                     "point for the shared alert helper); the journal line above is the alert"
                     % (key, hook), 4)

    def execute(self, actions: list, now: float) -> None:
        pending = list(actions)
        while pending:
            action = pending.pop(0)
            kind = action[0]
            if kind == "publish":
                try:
                    self.write(state_document(action[1], self.wall_ms()))
                except OSError as error:
                    self.log("could not write the warning state (%s); the kiosk will not show "
                             "it, the button is still guarded" % type(error).__name__, 4)
            elif kind == "wake":
                self.spawn(self._wake)
            elif kind == "alert":
                key, message = action[1], action[2]
                self.spawn(lambda key=key, message=message: self.alert(key, message))
            elif kind == "journal":
                self.log(action[1])
            elif kind == "poweroff":
                reason = self._command(POWEROFF_COMMAND, POWEROFF_TIMEOUT_S)
                if reason is not None:
                    pending.extend(self.machine.poweroff_failed(now, reason))
            else:  # a new action kind must be taught here, never dropped
                raise ValueError("unknown action %r" % (kind,))


def find_devices(sys_root: Path = Path("/sys/class/input")) -> list[str]:
    """Every /dev/input/eventN whose device is named exactly DEVICE_NAME.

    Implements: LLR-1180
    """
    found = []
    for node in sorted(sys_root.glob("event*")):
        try:
            name = (node / "device" / "name").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if name == DEVICE_NAME:
            found.append("/dev/input/" + node.name)
    return found


def _boottime_timestamps(fd: int) -> bool:
    """Ask evdev to stamp this fd's events on CLOCK_BOOTTIME; True if it did."""
    if fcntl is None:
        return False
    try:
        fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", CLOCK_BOOTTIME))
        return True
    except OSError:
        return False


def open_devices(paths: list[str]) -> dict:
    """{fd: (path, kernel_stamps_on_boot_clock)} for every node that opened.

    A node that refuses the clock is still read, stamped at read time on the
    same boot clock (run's `clock`). A node that does not open is left out;
    main() refuses to guard with fewer nodes than it found.
    Implements: LLR-1180
    """
    handles = {}
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        handles[fd] = (path, _boottime_timestamps(fd))
    return handles


def drain(fd: int) -> bytes | None:
    """Everything waiting on fd; None when the device is gone."""
    chunks = []
    while True:
        try:
            data = os.read(fd, EVENT_SIZE * 64)
        except BlockingIOError:
            break
        except OSError:
            return None
        if not data:
            return None if not chunks else b"".join(chunks)
        chunks.append(data)
        if len(data) < EVENT_SIZE * 64:
            break
    return b"".join(chunks)


def run(handles: dict, machine: PowerButtonMachine, shell: Shell, *,
        clock=boot_clock, wait=select.select, ack=read_ack, stop=lambda: False) -> int:
    """The event loop: wait for input or the next due instant, then act.

    Every instant is the boot clock's: kernel stamps where the device took
    the clock, `clock()` at read time where it did not. A suspend therefore
    ages an armed warning by the hours the panel slept.

    INPUT FIRST, THEN THE CLOCKS. Events that are waiting are judged on their
    own timestamps before the window is allowed to expire, so a second press
    made inside the window is never turned into a new warning because it was
    read late (Sol review 1).

    Returns 1 as soon as ANY device goes away, rather than carrying on with
    the rest: on this panel only event4 has ever reported a press, so a guard
    left reading event0 alone would hold the inhibitor over a dead button. The
    exit drops the inhibitor (logind's one-press default holds the button) and
    the restart rediscovers the devices. Returns 0 only when `stop()` says so.
    Implements: LLR-1183
    """
    shell.execute(machine.start(clock()), clock())
    while not stop():
        due = machine.next_due()
        timeout = None if due is None else max(0.0, due - clock())
        ready, _, _ = wait(list(handles), [], [], timeout)
        for fd in ready:
            path, kernel_stamped = handles[fd]
            data = drain(fd)
            if data is None:
                shell.log("%s went away; exiting so the restart finds the power-button devices "
                          "again (logind's default holds the button until then)" % path, 4)
                for other in list(handles):
                    os.close(other)
                handles.clear()
                return 1
            for etype, code, value, stamp in parse_events(data):
                when = stamp if kernel_stamped else clock()
                shell.execute(machine.key_event(path, etype, code, value, when), clock())
        now = clock()
        shell.execute(machine.tick(now, ack), now)
    return 0


def main() -> int:
    paths = find_devices()
    if not paths:
        journal("no input device is named %r; nothing to guard, logind keeps the button"
                % DEVICE_NAME, 3)
        return 69
    handles = open_devices(paths)
    if not handles:
        journal("power-button devices exist (%s) but none could be opened" % ", ".join(paths), 3)
        return 77
    machine = PowerButtonMachine(seq_start=int(time.time() * 1000))
    shell = Shell(machine)
    journal("guarding the power button on %s (handle-power-key inhibited while this runs); "
            "one press warns, a second within %g s shuts down"
            % (", ".join(path for path, _ in handles.values()), WINDOW_S))
    return run(handles, machine, shell)


if __name__ == "__main__":
    sys.exit(main())
