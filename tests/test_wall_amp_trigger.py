"""The amplifier detector can command either the jack or an MSP430 actuator."""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack" / "autoinstall" / "wall"
MODULE_PATH = WALL / "panel-amp-trigger.py"


def load_module():
    spec = importlib.util.spec_from_file_location("panel_amp_trigger", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTransport:
    def __init__(self, device, baud, ack_timeout):
        self.args = (device, baud, ack_timeout)
        self.states = []
        self.closed = False

    def set_state(self, enabled):
        self.states.append(enabled)

    def close(self):
        self.closed = True


def test_msp430_start_renews_the_lease_and_stop_fails_off():
    module = load_module()
    transports = []

    def factory(*args):
        transport = FakeTransport(*args)
        transports.append(transport)
        return transport

    relay = module.Msp430Relay(
        "/dev/serial/by-id/test-msp430", heartbeat_seconds=1.0,
        transport_factory=factory, monotonic=lambda: 10.0,
    )

    assert relay.start() is True
    assert transports[0].states == [False, True]
    assert relay.maintain(10.5) is True
    assert transports[0].states == [False, True]
    assert relay.maintain(11.0) is True
    assert transports[0].states == [False, True, True]

    relay.stop()
    assert transports[0].states == [False, True, True, False]
    assert transports[0].closed is True
    assert relay.running is False


def test_msp430_transport_failure_is_not_reported_as_on():
    module = load_module()

    class BrokenTransport(FakeTransport):
        def set_state(self, enabled):
            super().set_state(enabled)
            if enabled:
                raise OSError("disconnected")

    transport = BrokenTransport("device", 9600, 0.5)
    relay = module.Msp430Relay(
        "/dev/serial/by-id/test-msp430",
        transport_factory=lambda *_args: transport,
    )

    assert relay.start() is False
    assert transport.states == [False, True]
    assert transport.closed is True
    assert relay.running is False


def test_actuator_choice_is_exact_and_msp430_requires_an_absolute_device():
    module = load_module()

    assert isinstance(module.build_actuator("audio-jack"), module.Tone)
    assert isinstance(
        module.build_actuator("msp430", msp430_device="/dev/wall-amp-msp430"),
        module.Msp430Relay,
    )
    for bad in ("", "ttyUSB0", "/tmp/not-a-device", "/dev/a\nother"):
        with pytest.raises(module.ConfigurationError):
            module.build_actuator("msp430", msp430_device=bad)
    with pytest.raises(module.ConfigurationError):
        module.build_actuator("automatic")


def test_msp430_backend_does_not_require_the_headphone_tools():
    module = load_module()

    assert module.required_tools("msp430") == ("/usr/bin/arecord",)
    assert module.required_tools("audio-jack") == (
        "/usr/bin/arecord", "/usr/bin/aplay", "/usr/bin/amixer",
    )


def test_image_config_carries_the_choice_without_guessing_a_device():
    env = (WALL / "wall.env.example").read_text(encoding="utf-8")
    trigger_env = (WALL / "amp-trigger.env").read_text(encoding="utf-8")
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")

    assert "WALL_AMP_ACTIVATOR=audio-jack" in env
    assert "WALL_AMP_MSP430_DEVICE=" in env
    assert "WALL_AMP_ACTIVATOR=audio-jack" in trigger_env
    assert "WALL_AMP_MSP430_DEVICE=" in trigger_env
    assert "WALL_AMP_ACTIVATOR" in firstboot
    assert "WALL_AMP_MSP430_DEVICE" in firstboot


def test_firmware_uses_p14_and_expires_an_unrenewed_on_command():
    source = (ROOT / "stack" / "firmware" / "msp430-amp-trigger" / "main.c").read_text(
        encoding="utf-8"
    )

    assert "#define AMP_OUTPUT BIT4" in source
    assert "P1OUT &= ~AMP_OUTPUT" in source
    assert '"SET 1"' in source and '"SET 0"' in source
    assert "lease_ticks" in source
    assert "WDT_VECTOR" in source
