"""The amplifier detector can command the jack or the measured LCUS-2 relay."""

import importlib.util
import sys
import types
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
    def __init__(self, device, baud, timeout):
        self.args = (device, baud, timeout)
        self.states = []
        self.closed = False

    def set_state(self, channel, enabled):
        self.states.append((channel, enabled))

    def close(self):
        self.closed = True


def test_measured_lcus2_commands_and_status_parser_are_exact():
    module = load_module()

    assert module.lcus2_command(1, True) == bytes((0xA0, 0x01, 0x01, 0xA2))
    assert module.lcus2_command(1, False) == bytes((0xA0, 0x01, 0x00, 0xA1))
    assert module.lcus2_command(2, True) == bytes((0xA0, 0x02, 0x01, 0xA3))
    assert module.lcus2_command(2, False) == bytes((0xA0, 0x02, 0x00, 0xA2))
    measured = b"CH1: ON \r\nCH2: OFF\r\n"
    assert module.parse_lcus2_status(measured) == {1: True, 2: False}
    for bad in (b"", b"CH1: ON\n", b"CH1: MAYBE\nCH2: OFF\n",
                b"CH1: ON\nCH1: OFF\nCH2: OFF\n"):
        with pytest.raises(ValueError):
            module.parse_lcus2_status(bad)


def test_lcus2_start_checks_off_then_on_renews_and_stops_owned_channel():
    module = load_module()
    transports = []

    def factory(*args):
        transport = FakeTransport(*args)
        transports.append(transport)
        return transport

    relay = module.Lcus2Relay(
        "/dev/serial/by-path/test-lcus2", channel=2, heartbeat_seconds=1.0,
        transport_factory=factory, monotonic=lambda: 10.0,
    )

    assert relay.start() is True
    assert transports[0].states == [(2, False), (2, True)]
    assert relay.maintain(10.5) is True
    assert transports[0].states == [(2, False), (2, True)]
    assert relay.maintain(11.0) is True
    assert transports[0].states == [(2, False), (2, True), (2, True)]
    relay.stop()
    assert transports[0].states == [(2, False), (2, True), (2, True), (2, False)]
    assert transports[0].closed is True


def test_lcus2_stop_establishes_off_even_without_a_live_transport():
    module = load_module()
    transports = []

    def factory(*args):
        transport = FakeTransport(*args)
        transports.append(transport)
        return transport

    relay = module.Lcus2Relay(
        "/dev/serial/by-path/test-lcus2", channel=1, transport_factory=factory
    )

    assert relay.stop() is True
    assert transports[0].states == [(1, False)]
    assert transports[0].closed is True


def test_suspend_proof_exists_only_after_verified_off(tmp_path):
    module = load_module()
    marker = tmp_path / "off-verified"
    module.SAFE_STATE_FILE = str(marker)

    module.record_safe_state(True)
    assert marker.read_text(encoding="ascii") == "OFF\n"
    module.record_safe_state(False)
    assert not marker.exists()

    class Actuator:
        def __init__(self, result):
            self.result = result

        def stop(self):
            return self.result

    assert module.stop_and_record(Actuator(True)) is True
    assert marker.read_text(encoding="ascii") == "OFF\n"
    assert module.stop_and_record(Actuator(False)) is False
    assert not marker.exists()


def test_lcus2_failure_is_not_reported_as_on():
    module = load_module()

    class BrokenTransport(FakeTransport):
        def set_state(self, channel, enabled):
            super().set_state(channel, enabled)
            if enabled:
                raise OSError("disconnected")

    transport = BrokenTransport("device", 9600, 0.75)
    relay = module.Lcus2Relay(
        "/dev/serial/by-path/test-lcus2",
        transport_factory=lambda *_args: transport,
    )

    assert relay.start() is False
    assert transport.states == [(1, False), (1, True)]
    assert transport.closed is True
    assert relay.running is False


def test_actuator_choice_is_exact_and_lcus2_requires_safe_configuration():
    module = load_module()

    assert isinstance(module.build_actuator(
        "lcus-2", lcus2_device="/dev/wall-amp-relay", lcus2_channel="1"
    ), module.Lcus2Relay)
    for bad in ("", "ttyUSB0", "/tmp/not-a-device", "/dev/a\nother"):
        with pytest.raises(module.ConfigurationError):
            module.build_actuator("lcus-2", lcus2_device=bad, lcus2_channel="1")
    for channel in ("", "0", "3", "one"):
        with pytest.raises(module.ConfigurationError):
            module.build_actuator(
                "lcus-2", lcus2_device="/dev/wall-amp-relay", lcus2_channel=channel
            )
    with pytest.raises(module.ConfigurationError):
        module.build_actuator("automatic")


def test_the_retired_tone_actuator_is_refused_and_its_code_is_gone():
    """Ratified 2026-09-13: LCUS-2 is the only actuator, the jack is free.

    A panel still configured for the tone must FAIL, not fall back: falling
    back to the relay would look like it worked while nobody had re-proved the
    relay, and reviving the tone would put a full-scale signal on the jack the
    headset leg of item 23 now owns.
    """
    module = load_module()

    assert not hasattr(module, "Tone")
    for gone in ("TRIGGER_PCM", "TRIGGER_FREQ", "TRIGGER_AMPLITUDE",
                 "JACK_CONTROL", "jack_present", "assert_trigger_output"):
        assert not hasattr(module, gone), gone

    with pytest.raises(module.ConfigurationError) as refused:
        module.build_actuator("audio-jack")
    assert "lcus-2" in str(refused.value)
    with pytest.raises(module.ConfigurationError):
        module.required_tools("audio-jack")

    source = (WALL / "panel-amp-trigger.py").read_text(encoding="utf-8")
    assert "WALL_AMP_TONE_HZ" not in source
    # Nothing in the daemon may open the built-in codec any more.
    assert "trigger_out" not in source


def test_no_shipped_configuration_still_names_the_tone():
    trigger_mode = (WALL / "asound-trigger-mode.conf").read_text(encoding="utf-8")
    amp_env = (WALL / "amp-trigger.env").read_text(encoding="utf-8")
    env = (WALL / "wall.env.example").read_text(encoding="utf-8")
    mode_script = (WALL / "wall-audio-mode").read_text(encoding="utf-8")
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")

    # The PCM that carried the tone is gone from the graph, so the built-in
    # codec has no playback path defined in trigger mode at all.
    assert "pcm.trigger_out" not in trigger_mode
    assert "card_builtin" not in trigger_mode
    assert "WALL_AMP_TONE_HZ" not in amp_env
    assert "WALL_AMP_TONE_HZ" not in env
    assert "WALL_AMP_ACTIVATOR=lcus-2" in amp_env
    assert "trigger_out" not in mode_script
    # firstboot accepts exactly one activator and names the retired one only to
    # refuse it -- and refusing it FAILS the provisioning run rather than
    # warning past it, so a panel configured for the retired tone cannot come
    # up wearing a green marker with an amplifier that will never switch on.
    assert "audio-jack|lcus-2)" not in firstboot
    block = firstboot[firstboot.index("_amp_activator=${WALL_AMP_ACTIVATOR"):]
    block = block[:block.index("esac")]
    assert block.count("fail_step") == 2, block
    assert "warn " not in block
    assert block.count("_amp_activator=invalid") == 2


def test_lcus2_backend_does_not_require_headphone_tools():
    module = load_module()

    assert module.required_tools("lcus-2") == ("/usr/bin/arecord",)


def test_image_config_selects_lcus2_through_a_stable_measured_udev_link():
    env = (WALL / "wall.env.example").read_text(encoding="utf-8")
    trigger_env = (WALL / "amp-trigger.env").read_text(encoding="utf-8")
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")

    rule = (WALL / "99-wall-amp-lcus2.rules").read_text(encoding="utf-8")

    assert "WALL_AMP_ACTIVATOR=lcus-2" in env
    assert "WALL_AMP_LCUS2_DEVICE=/dev/wall-amp-relay" in env
    assert "WALL_AMP_LCUS2_CHANNEL=1" in env
    assert "WALL_AMP_ACTIVATOR=lcus-2" in trigger_env
    assert "WALL_AMP_LCUS2_DEVICE=/dev/wall-amp-relay" in trigger_env
    assert "WALL_AMP_LCUS2_CHANNEL=1" in trigger_env
    assert "WALL_AMP_ACTIVATOR" in firstboot
    assert "WALL_AMP_LCUS2_DEVICE" in firstboot
    assert "WALL_AMP_LCUS2_CHANNEL" in firstboot
    assert 'ATTRS{idVendor}=="1a86"' in rule
    assert 'ATTRS{idProduct}=="7523"' in rule
    assert 'SYMLINK+="wall-amp-relay"' in rule


def test_a_non_serial_device_is_refused_gracefully_and_leaks_no_descriptor(monkeypatch):
    """termios.error is built on Exception, NOT on OSError.

    Unconverted it walks straight through Lcus2Relay's `except OSError` guards,
    so a device that opens but is not a TTY killed the daemon on a traceback
    instead of taking the designed "OFF was not verified" path -- and, because
    the raise happened after os.open, leaked one descriptor per attempt, which
    a heartbeat reconnect loop turns into descriptor exhaustion. The device is
    a config value (WALL_AMP_LCUS2_DEVICE), so this is reachable in the field.
    """
    module = load_module()

    class FakeTermiosError(Exception):
        pass

    fake_termios = types.ModuleType("termios")
    fake_termios.error = FakeTermiosError
    for name in ("CS8", "CREAD", "CLOCAL", "B9600", "TCSANOW",
                 "TCIOFLUSH", "TCIFLUSH"):
        setattr(fake_termios, name, 0)

    def refuse(_fd):
        raise FakeTermiosError(25, "Inappropriate ioctl for device")

    fake_termios.tcgetattr = refuse

    closed = []
    monkeypatch.setitem(sys.modules, "termios", fake_termios)
    # The Windows build host has neither flag; the daemon only ever runs on
    # the panel, but this defect is worth holding from either host.
    for flag in ("O_NOCTTY", "O_NONBLOCK"):
        monkeypatch.setattr(module.os, flag, 0, raising=False)
    monkeypatch.setattr(module.os, "open", lambda *_a, **_k: 4242)
    monkeypatch.setattr(module.os, "close", closed.append)

    relay = module.Lcus2Relay("/dev/wall-amp-relay")

    assert relay.ensure_off() is False
    assert relay.start() is False
    assert relay.running is False
    # Each refused open closed exactly the descriptor it had opened.
    assert closed == [4242, 4242]
