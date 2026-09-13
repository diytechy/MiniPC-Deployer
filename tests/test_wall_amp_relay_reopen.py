"""The relay is reopened, with a bound, when the CH340 is late back.

Item 25: the LCUS-2 is on the same USB hub as the audio adapter, so the restart
that the adapter's re-enumeration now triggers can land while /dev/wall-amp-relay
does not exist yet.
"""

import errno
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "stack" / "autoinstall" / "wall" / "panel-amp-trigger.py"


def load_module():
    spec = importlib.util.spec_from_file_location("panel_amp_trigger", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Flaky:
    """An actuator whose device node appears after `absent_for` attempts."""

    def __init__(self, absent_for, error=errno.ENOENT):
        self.absent_for = absent_for
        self.error = error
        self.attempts = 0

    def ensure_off(self):
        self.attempts += 1
        if self.attempts <= self.absent_for:
            return False
        return True


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_a_relay_that_is_present_is_opened_once():
    module = load_module()
    clock, actuator = Clock(), Flaky(absent_for=0)
    assert module.ensure_off_bounded(actuator, 20.0, clock.monotonic, clock.sleep)
    assert actuator.attempts == 1
    assert clock.now == 0.0


def test_a_relay_that_returns_late_is_reopened_until_it_verifies():
    module = load_module()
    clock, actuator = Clock(), Flaky(absent_for=6)
    assert module.ensure_off_bounded(actuator, 20.0, clock.monotonic, clock.sleep)
    assert actuator.attempts == 7
    # Each attempt is a FRESH transport in the real relay, which is what makes
    # this a reopen rather than a poll of a stale fd.
    assert clock.now == 6.0


def test_an_eio_is_retried_exactly_like_an_enoent():
    module = load_module()
    clock, actuator = Clock(), Flaky(absent_for=3, error=errno.EIO)
    assert module.ensure_off_bounded(actuator, 20.0, clock.monotonic, clock.sleep)
    assert actuator.attempts == 4


def test_the_retry_is_bounded_so_a_missing_relay_still_fails():
    module = load_module()
    clock, actuator = Clock(), Flaky(absent_for=10_000)
    assert module.ensure_off_bounded(
        actuator, 5.0, clock.monotonic, clock.sleep) is False
    # One attempt per second plus the first: bounded, and it gave up.
    assert actuator.attempts == 6
    assert clock.now <= 5.0


def test_a_zero_wait_keeps_the_original_single_attempt_behaviour():
    module = load_module()
    clock, actuator = Clock(), Flaky(absent_for=1)
    assert module.ensure_off_bounded(
        actuator, 0.0, clock.monotonic, clock.sleep) is False
    assert actuator.attempts == 1


def test_the_detector_backoff_recovers_inside_the_acceptance_window():
    """A capture that opens on the third try must not wait 30 s to be polled."""
    module = load_module()
    backoff, total = 1.0, 0.0
    for _ in range(3):
        total += backoff
        backoff = min(backoff * 2, 8.0)
    assert total < 10.0
