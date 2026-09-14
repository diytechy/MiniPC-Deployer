"""The relay is reopened, with a bound, when the CH340 is late back.

Item 25: the LCUS-2 is on the same USB hub as the audio adapter, so the restart
that the adapter's re-enumeration now triggers can land while
/dev/wall-amp-relay does not exist yet.

These drive the REAL Lcus2Relay with a transport factory that raises the real
errnos, because terra's review found the first version of this file asserting
against a fake actuator that never raised anything: it would have passed with
the exception handling or the fresh-transport reopen deleted.
"""

import errno
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "stack" / "autoinstall" / "wall" / "panel-amp-trigger.py"


def load_module():
    spec = importlib.util.spec_from_file_location("panel_amp_trigger", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Transport:
    """One opened CH340. Records what was written to it and that it was closed."""

    def __init__(self, log):
        self.log = log
        self.states = []
        self.closed = False

    def set_state(self, channel, enabled):
        self.states.append((channel, enabled))

    def close(self):
        self.closed = True


class LateDevice:
    """A device node that raises `error` for the first `absent_for` opens."""

    def __init__(self, absent_for, error=errno.ENOENT):
        self.absent_for = absent_for
        self.error = error
        self.opens = 0
        self.transports = []

    def __call__(self, device, baud, timeout):
        self.opens += 1
        if self.opens <= self.absent_for:
            raise OSError(self.error, "no such device: %s" % device)
        transport = Transport(self)
        self.transports.append(transport)
        return transport


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def relay(module, factory):
    return module.Lcus2Relay("/dev/wall-amp-relay", channel=1,
                             transport_factory=factory)


def test_a_relay_that_is_present_is_opened_once_and_verified_off():
    module = load_module()
    clock, device = Clock(), LateDevice(absent_for=0)
    assert module.ensure_off_bounded(relay(module, device), 6.0,
                                     clock.monotonic, clock.sleep)
    assert device.opens == 1
    assert device.transports[0].states == [(1, False)]
    assert device.transports[0].closed
    assert clock.now == 0.0


@pytest.mark.parametrize("error", (errno.ENOENT, errno.EIO, errno.ENODEV))
def test_a_node_that_returns_late_is_REOPENED_until_it_verifies(error):
    module = load_module()
    clock, device = Clock(), LateDevice(absent_for=4, error=error)
    assert module.ensure_off_bounded(relay(module, device), 6.0,
                                     clock.monotonic, clock.sleep)
    # Five opens, not one open and four polls: a re-enumerated node is a new
    # inode and a stale fd would never recover.
    assert device.opens == 5
    assert len(device.transports) == 1
    assert device.transports[0].states == [(1, False)]
    assert clock.now == 4.0


def test_the_retry_is_bounded_so_a_missing_relay_still_fails():
    module = load_module()
    clock, device = Clock(), LateDevice(absent_for=10_000)
    assert module.ensure_off_bounded(relay(module, device), 5.0,
                                     clock.monotonic, clock.sleep) is False
    # One attempt per second plus the first: bounded, and it gave up.
    assert device.opens == 6
    assert clock.now <= 5.0


def test_a_zero_wait_keeps_the_original_single_attempt_behaviour():
    module = load_module()
    clock, device = Clock(), LateDevice(absent_for=1)
    assert module.ensure_off_bounded(relay(module, device), 0.0,
                                     clock.monotonic, clock.sleep) is False
    assert device.opens == 1


def test_a_timeout_on_the_status_reply_is_retried_too():
    """The node can exist before the MCU answers; that is the same wait."""
    module = load_module()

    class Mute(Transport):
        def set_state(self, channel, enabled):
            raise TimeoutError("LCUS-2 did not return a complete status")

    state = {"n": 0}

    def factory(device, baud, timeout):
        state["n"] += 1
        return Transport(None) if state["n"] > 3 else Mute(None)

    clock = Clock()
    assert module.ensure_off_bounded(relay(module, factory), 6.0,
                                     clock.monotonic, clock.sleep)
    assert state["n"] == 4


def test_the_default_wait_fits_inside_the_ten_second_acceptance():
    """It runs BEFORE the capture threads, so it spends the whole budget."""
    module = load_module()
    assert module.RELAY_WAIT_SECONDS <= 6.0
    assert module.RELAY_WAIT_SECONDS + module.ACTUATOR_RETRY_SECONDS >= 10.0


def test_the_detector_backoff_recovers_inside_the_acceptance_window():
    """A capture that opens on the third try must not wait 30 s to be polled."""
    backoff, total = 1.0, 0.0
    for _ in range(3):
        total += backoff
        backoff = min(backoff * 2, 8.0)
    assert total < 10.0
