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
counts the lines that mean "the stream is broken". When more than
--max-errors of them arrive inside a --window second sliding window it kills
the child and exits non-zero, which is what `Restart=always` is waiting for.

It is deliberately dumb: no parsing of ALSA state, no reopen logic of its
own. The unit's Restart= is the recovery mechanism, and the device binding
(BindsTo= on the adapter's udev alias, see 90-wall-audio-adapter.rules) is
what handles the case where the adapter is gone rather than merely confused.

Exit codes: the child's own exit code when it exits by itself; 1 when the
error cap tripped; 2 for a usage error.
"""

import argparse
import collections
import os
import re
import signal
import subprocess
import sys
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


class ErrorWindow:
    """A sliding window of error timestamps with a cap.

    `record` returns True when the cap has been exceeded, i.e. when more than
    `max_errors` error lines arrived within the last `window` seconds. The
    window is what keeps a panel that xruns once an hour running, and the cap
    is what stops the endless recover loop.
    """

    def __init__(self, max_errors=DEFAULT_MAX_ERRORS, window=DEFAULT_WINDOW,
                 monotonic=time.monotonic):
        if max_errors < 1:
            raise ValueError("max_errors must be at least 1")
        if window <= 0:
            raise ValueError("window must be positive")
        self.max_errors = max_errors
        self.window = window
        self.monotonic = monotonic
        self._times = collections.deque()

    def __len__(self):
        return len(self._times)

    def record(self, now=None):
        now = self.monotonic() if now is None else now
        self._times.append(now)
        while self._times and now - self._times[0] > self.window:
            self._times.popleft()
        return len(self._times) > self.max_errors


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
            log("alsaloop reported %d stream errors in %.0fs -- the recover "
                "loop is wedged; exiting so the unit restarts"
                % (len(window), window.window))
            return True
    return False


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="run alsaloop with a bounded recover loop")
    parser.add_argument("--max-errors", type=int, default=DEFAULT_MAX_ERRORS)
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no command given")
    try:
        window = ErrorWindow(args.max_errors, args.window)
    except ValueError as exc:
        parser.error(str(exc))

    try:
        # stderr is merged into stdout so a message is caught wherever alsaloop
        # chose to print it; both are line-consumed here and re-emitted, so the
        # journal reads exactly as it did before this wrapper existed.
        child = subprocess.Popen(command, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
    except OSError as exc:
        log("cannot start %s: %s" % (command[0], exc))
        return 1

    # A SIGTERM from systemd must reach alsaloop, not just this wrapper:
    # otherwise `systemctl stop` leaves the child holding the PCM until the
    # stop timeout kills the cgroup, which is exactly the stale-handle problem
    # this file is about.
    def forward(signum, _frame):
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

    def echo(text):
        sys.stderr.write(text + "\n")
        sys.stderr.flush()

    stream = (line.decode("utf-8", "replace") for line in child.stdout)
    tripped = watch(stream, window, echo)

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


if __name__ == "__main__":
    sys.exit(main())
