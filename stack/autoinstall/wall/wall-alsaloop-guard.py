#!/usr/bin/env python3
"""Run alsaloop with a BOUNDED recover loop.

WHY THIS EXISTS (measured on the panel 2026-09-13, Owner item 25): the USB
hub was moved to another port. The kernel re-enumerated the ICUSBAUDIO7D with
the same card id, udev recreated /dev/wall-amp-relay and wall-line-in came
back by itself -- but the long-running alsaloop in wall-kiosk-loop kept its
handle on the OLD device instance and printed

    unable to prepare slave

forever. alsaloop's recover path never gives up and never exits, so
`Restart=always` never fires: from systemd's point of view the unit is
perfectly healthy while no audio moves at all. Only a hand-run
`systemctl restart` recovered it.

This wrapper turns that silent wedge into an exit. It runs alsaloop as a
child, mirrors every line the child writes to the journal unchanged, and
counts the lines that mean "the stream is broken". It kills the child and
exits non-zero -- which is what `Restart=always` is waiting for -- on either
of two bounds: more than --max-errors of those lines inside a --window second
sliding window (the flood), or errors arriving with no clean window-long gap
for --sustain seconds (the slow wedge, which no rate cap can see, because an
alsaloop that is broken forever but complains once a second keeps any
sensible rate under the cap while carrying no audio at all).

It is deliberately dumb: no parsing of ALSA state, no reopen logic of its
own. The unit's Restart= is the recovery mechanism, and the device binding
(BindsTo= on the adapter's udev alias, see 90-wall-audio-adapter.rules) is
what handles the case where the adapter is gone rather than merely confused.

Exit codes: the child's own exit code when it exits by itself; 1 when the
error cap tripped; 2 for a usage error.

--respawn (opt-in, wall-spdif-in only, 2026-10-16) moves the restart INTO the
guard. Measured that evening: systemd's restart counter never resets after a
healthy run -- only a manual start or a reboot clears it -- so the unit's
2 s -> 120 s ramp, meant for a sleeping desktop, climbed once and then charged
every later glitch the full ceiling. A crash at 20:22:16 after seventeen clean
minutes sat silent for 104 s. In respawn mode the guard relaunches the child
itself: --respawn-delays[0] after any run that lasted --healthy-after seconds,
and walks up the schedule only while relaunches keep failing fast. It exits
only when systemd stops it (0) or the child cannot be executed at all (1), so
the unit's own Restart= stays the backstop for a guard that cannot work.
"""

import argparse
import collections
import re
import signal
import subprocess
import sys
import threading
import time

# Every one of these means "this stream is not carrying audio any more".
# `unable to prepare slave` is the measured one; the rest are the neighbouring
# messages alsaloop emits from the same recover path, included so that a
# different flavour of the same wedge is not missed.
ERROR_PATTERN = re.compile(
    r"unable to prepare|prepare slave|read/write error|xrun|"
    r"poll failed|no such device|input/output error|broken pipe|"
    r"device or resource busy|resource temporarily unavailable|"
    r"unable to (start|recover|open)|error:",
    re.IGNORECASE)

DEFAULT_MAX_ERRORS = 20
DEFAULT_WINDOW = 10.0
DEFAULT_SUSTAIN = 30.0
DEFAULT_RESPAWN_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
DEFAULT_HEALTHY_AFTER = 20.0


class ErrorWindow:
    """A sliding window of error timestamps, with a rate cap AND a duration cap.

    `record` returns True when either bound is exceeded:

    * more than `max_errors` error lines inside the last `window` seconds --
      the flood, which is what the measured wedge looks like; or
    * errors arriving without a clean `window`-second gap for longer than
      `sustain` seconds -- the SLOW wedge. A rate cap alone cannot see that
      one: an alsaloop broken forever but printing once a second keeps the
      window under any sensible cap and runs silent indefinitely, which is
      precisely the active-but-no-audio state this file exists to end.

    A clean gap of `window` seconds is what resets the streak, so ordinary
    isolated xruns never accumulate towards either bound.
    """

    def __init__(self, max_errors=DEFAULT_MAX_ERRORS, window=DEFAULT_WINDOW,
                 sustain=DEFAULT_SUSTAIN, monotonic=time.monotonic):
        if max_errors < 1:
            raise ValueError("max_errors must be at least 1")
        if window <= 0:
            raise ValueError("window must be positive")
        if sustain <= 0:
            raise ValueError("sustain must be positive")
        self.max_errors = max_errors
        self.window = window
        self.sustain = sustain
        self.monotonic = monotonic
        self._times = collections.deque()
        self._streak_start = None
        self.reason = ""

    def __len__(self):
        return len(self._times)

    def record(self, now=None):
        now = self.monotonic() if now is None else now
        # Prune BEFORE appending, so an empty deque here really does mean "no
        # error for a whole window" and the streak can be restarted.
        # >= , not >: the contract says a clean gap of `window` seconds resets
        # the streak, and an exactly-window gap is a clean gap (terra).
        while self._times and now - self._times[0] >= self.window:
            self._times.popleft()
        if not self._times:
            self._streak_start = now
        self._times.append(now)
        if len(self._times) > self.max_errors:
            self.reason = ("%d stream errors in %.0fs"
                           % (len(self._times), self.window))
            return True
        if now - self._streak_start >= self.sustain:
            self.reason = ("stream errors unbroken for %.0fs"
                           % (now - self._streak_start))
            return True
        return False


class Backoff:
    """The respawn schedule: ramp on fast failures, reset after a healthy run.

    `next_delay(ran_for)` is called once per child exit with how long that
    child ran. A run of at least `healthy_after` seconds puts the schedule back
    at its first step -- the property systemd's RestartSteps lacks -- so a
    glitch after a long clean stretch costs delays[0], however many failures
    came before it. Consecutive short runs walk up the schedule and stay on its
    last step, which is the patience a sleeping desktop needs.
    """

    def __init__(self, delays=DEFAULT_RESPAWN_DELAYS,
                 healthy_after=DEFAULT_HEALTHY_AFTER):
        delays = tuple(float(d) for d in delays)
        if not delays:
            raise ValueError("respawn delays must not be empty")
        if any(d < 0 for d in delays):
            raise ValueError("respawn delays must not be negative")
        if healthy_after <= 0:
            raise ValueError("healthy-after must be positive")
        self.delays = delays
        self.healthy_after = healthy_after
        self._step = 0

    def next_delay(self, ran_for):
        if ran_for >= self.healthy_after:
            self._step = 0
        delay = self.delays[self._step]
        self._step = min(self._step + 1, len(self.delays) - 1)
        return delay


def parse_delays(text):
    try:
        return tuple(float(part) for part in text.split(",") if part.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            "expected comma-separated seconds, got %r" % text)


def is_error_line(line):
    return bool(ERROR_PATTERN.search(line))


def log(message):
    sys.stderr.write("wall-alsaloop-guard: %s\n" % message)
    sys.stderr.flush()


def watch(lines, window, echo=None):
    """Consume child output; return True if the error cap tripped.

    `lines` is any iterable of str. Kept separate from process handling so the
    cap is testable without ALSA, without a child and without a clock.
    """
    echo = echo or (lambda text: None)
    for line in lines:
        line = line.rstrip("\n")
        echo(line)
        if is_error_line(line) and window.record():
            log("alsaloop: %s -- the recover loop is wedged; exiting so the "
                "unit restarts" % window.reason)
            return True
    return False


class CannotStart(Exception):
    """The child could not be executed at all -- not a stream fault."""


def echo_line(text):
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def run_child(command, window, holder, stop=None):
    """Run one child to completion under `window`; return the guard's code.

    `holder` is a one-element list the signal handler reads, so a SIGTERM
    reaches whichever child is current. Raises CannotStart when the command
    cannot be executed.
    """
    try:
        # stderr is merged into stdout so a message is caught wherever alsaloop
        # chose to print it; both are line-consumed here and re-emitted, so the
        # journal reads exactly as it did before this wrapper existed.
        child = subprocess.Popen(command, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
    except OSError as exc:
        raise CannotStart("cannot start %s: %s" % (command[0], exc))
    holder[0] = child
    try:
        # A stop that landed between Popen and the holder assignment found no
        # child to forward to; deliver it now rather than run a full stream.
        if stop is not None and stop.is_set():
            child.terminate()

        stream = (line.decode("utf-8", "replace") for line in child.stdout)
        tripped = watch(stream, window, echo_line)

        if tripped:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            return 1

        code = child.wait()
        if code < 0:
            log("alsaloop was killed by signal %d" % -code)
            return 128 + -code
        if code != 0:
            log("alsaloop exited %d" % code)
        return code
    finally:
        holder[0] = None


def supervise(run_once, backoff, stop, monotonic=time.monotonic):
    """Relaunch `run_once` until `stop` is set; return 0 when it is.

    `run_once()` runs one child and returns its code; `stop` is a
    threading.Event whose wait() doubles as the interruptible sleep, so a
    `systemctl stop` during a backoff ends the guard at once. Kept free of
    processes and ALSA so the schedule is testable with fakes.
    """
    while not stop.is_set():
        started = monotonic()
        code = run_once()
        ran_for = monotonic() - started
        if stop.is_set():
            break
        delay = backoff.next_delay(ran_for)
        log("alsaloop ended (code %s) after %.1fs; relaunching in %gs"
            % (code, ran_for, delay))
        if stop.wait(delay):
            break
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="run alsaloop with a bounded recover loop")
    parser.add_argument("--max-errors", type=int, default=DEFAULT_MAX_ERRORS)
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW)
    # The second bound: errors with no clean window-long gap for this long.
    parser.add_argument("--sustain", type=float, default=DEFAULT_SUSTAIN)
    # Opt-in: relaunch the child here instead of exiting to systemd.
    parser.add_argument("--respawn", action="store_true")
    parser.add_argument("--respawn-delays", type=parse_delays,
                        default=DEFAULT_RESPAWN_DELAYS)
    parser.add_argument("--healthy-after", type=float,
                        default=DEFAULT_HEALTHY_AFTER)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no command given")
    try:
        ErrorWindow(args.max_errors, args.window, args.sustain)
        backoff = Backoff(args.respawn_delays, args.healthy_after)
    except ValueError as exc:
        parser.error(str(exc))

    def new_window():
        return ErrorWindow(args.max_errors, args.window, args.sustain)

    holder = [None]
    stop = threading.Event()

    # A SIGTERM from systemd must reach alsaloop, not just this wrapper:
    # otherwise `systemctl stop` leaves the child holding the PCM until the
    # stop timeout kills the cgroup, which is exactly the stale-handle problem
    # this file is about. In respawn mode it also ends the relaunch loop.
    def forward(signum, _frame):
        stop.set()
        child = holder[0]
        if child is not None:
            try:
                child.send_signal(signum)
            except OSError:
                pass

    # SIGHUP by name, because the Windows build host used for the unit tests has
    # no such signal and importing this file must not depend on the platform.
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, forward)
            except (ValueError, OSError):
                pass

    try:
        if not args.respawn:
            return run_child(command, new_window(), holder)
        # A fresh window per child: the previous child's errors say nothing
        # about the new one's stream.
        return supervise(lambda: run_child(command, new_window(), holder, stop),
                         backoff, stop)
    except CannotStart as exc:
        log(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
