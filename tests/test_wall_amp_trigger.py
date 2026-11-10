"""The amplifier detector can command the jack or the measured LCUS-2 relay."""

import importlib.util
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack" / "autoinstall" / "wall"
MODULE_PATH = WALL / "panel-amp-trigger.py"


def test_firstboot_installs_the_amp_before_applying_audio_mode_llr1440():
    """The mode script must restart onto the newly installed detector.

    Implements: SR-143, LLR-1440.
    """
    lines = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8").splitlines()
    loop = next(i for i, line in enumerate(lines)
                if line.startswith("for _f in ") and "panel-amp-trigger.py" in line.split())
    install = next(i for i in range(loop, len(lines))
                   if lines[i].strip() ==
                   'install -m 0755 "$PAYLOAD/$_f" "/usr/local/lib/wall-panel/$_f"')
    first_apply = next(i for i, line in enumerate(lines)
                       if line.lstrip().startswith("apply_audio_mode "))
    assert install < first_apply, "install the new detector before the mode script restarts it"


@pytest.mark.parametrize("mode", ["bus", "trigger"])
@pytest.mark.parametrize("same_mode", [True, False], ids=["unchanged", "changed"])
@pytest.mark.parametrize("running", [True, False], ids=["active", "inactive"])
def test_mode_application_stops_then_starts_the_amp_even_if_unchanged_llr1440(
        tmp_path, mode, same_mode, running):
    """Execute the release's mode paths; hardware/system calls stay in the sandbox.

    Firstboot installs the new detector before applying the mode. Its existing
    stop/start must load that code even when the requested mode is unchanged.
    Implements: SR-143, LLR-1440.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no POSIX bash")
    source = (WALL / "wall-audio-mode").read_text(encoding="utf-8")

    def extract(name):
        start = source.index("\n%s() {\n" % name) + 1
        return source[start:source.index("\n}\n", start) + 3]

    # Execute the real entry-point case and the real enable/start helpers.
    # Only unrelated effects and the systemd boundary are replaced. In
    # particular, start_if_enabled is NOT a fake that could conceal a missing
    # call in either mode's start branch.
    functions = "\n".join(extract(name) for name in (
        "remember_active", "was_active", "start_if_enabled", "apply_trigger", "apply_bus"))
    entry = source[source.index('case "${1:-}" in'):]
    mode_file = tmp_path / "audio-mode"
    mode_file.write_text(mode if same_mode else "panel", encoding="utf-8")
    harness = r'''
set -eu
FAILED=0
ACTIVE_BEFORE=""
ADAPTER=TestAdapter
BUILTIN=TestBuiltin
AMP_RUNNING=%s
CONF_DIR="%s/conf"
MODE_FILE="%s"
id() { echo 0; }
adapter_present() { return 0; }
stop_mic_legs_or_refuse() { return 0; }
restart_kiosk() { :; }
clear_ipc() { :; }
sleep() { :; }
alsactl() { :; }
mix() { :; }
require_mode_selection() { printf '%%s\n' "$1" > "$MODE_FILE"; }
systemctl() {
    case "$1" in
        is-active)
            case "$*" in
                *wall-amp-trigger.service) [ "$AMP_RUNNING" = 1 ] ;;
                *) return 3 ;;
            esac ;;
        is-enabled) echo enabled ;;
        *) echo "unexpected systemctl: $*" >&2; exit 99 ;;
    esac
}
unit() {
    printf '%%s\n' "$*" >> calls
    case " $* " in
        *" wall-amp-trigger.service "*)
            case "$1" in stop) AMP_RUNNING=0 ;; start) AMP_RUNNING=1 ;; esac ;;
    esac
}
%s
%s
''' % (int(running), tmp_path.as_posix(), mode_file.as_posix(), functions, entry)
    script = tmp_path / "mode-harness.sh"
    script.write_bytes(harness.encode("utf-8"))
    result = subprocess.run([bash, script.as_posix(), mode], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [line.split() for line in (tmp_path / "calls").read_text().splitlines()]
    amp_actions = [call[0] for call in calls if "wall-amp-trigger.service" in call[1:]]
    assert amp_actions == ["stop", "enable", "start"], calls


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


def test_relay_off_since_keeps_the_earliest_verified_off_and_dies_with_the_proof(tmp_path):
    """MIN_OFF across restarts (2026-11-09): the ledger beside the S3 proof.

    A verified OFF records its instant only when none is held, so an idle
    daemon's later OFFs do not move it forward; revoking the proof before the
    relay may be energized removes it, so a dead process cannot vouch for an
    OFF that a later ON overtook.
    """
    module = load_module()
    module.SAFE_STATE_FILE = str(tmp_path / "off-verified")
    ledger = tmp_path / module.RELAY_OFF_SINCE_NAME

    assert module.relay_off_since(100.0) is None
    module.record_safe_state(True, now=10.0)
    assert module.relay_off_since(100.0) == 10.0
    module.record_safe_state(True, now=50.0)   # still off: the earlier instant stands
    assert module.relay_off_since(100.0) == 10.0
    module.record_safe_state(False)            # about to energize
    assert not ledger.exists() and not (tmp_path / "off-verified").exists()
    assert module.relay_off_since(100.0) is None
    module.record_safe_state(True, now=60.0)   # the next real OFF
    assert module.relay_off_since(100.0) == 60.0

    # Nothing that cannot be an instant of this boot that has already happened.
    for text in ("", "soon", "nan", "inf", "-1", "200.0"):
        ledger.write_text(text, encoding="ascii")
        assert module.relay_off_since(100.0) is None, text


class _RestartLevel:
    """A capture source whose loudness the test sets for every instance."""

    loud = False
    pcm = "speaker_tap"

    def __init__(self, pcm, _offset):
        self.pcm = pcm

    def start(self):
        pass

    def stop(self):
        pass

    def above(self, _threshold):
        return _RestartLevel.loud

    def report(self):
        return self.pcm


class _RestartRelay:
    """One physical relay shared by successive daemon processes."""

    device = "/dev/wall-amp-relay"

    def __init__(self):
        self.on = False
        self.events = []   # (monotonic, "ON" | "OFF"), physical transitions only

    def _set(self, on):
        import time
        if on != self.on:
            self.events.append((time.monotonic(), "ON" if on else "OFF"))
        self.on = on
        return True

    def ensure_off(self):
        return self._set(False)

    def start(self):
        return self._set(True)

    def stop(self):
        return self._set(False)

    def maintain(self, _now=None):
        return self.on


def _restart_daemon(module, monkeypatch, relay):
    """Start the real main() loop once in trigger mode; return its stopper."""
    import threading
    import time
    handlers = []
    monkeypatch.setattr(module, "build_actuator", lambda *_a, **_kw: relay)
    monkeypatch.setattr(module, "required_tools", lambda _method: ())
    monkeypatch.setattr(module, "current_mode", lambda: "trigger")
    monkeypatch.setattr(module, "Level", _RestartLevel)
    monkeypatch.setattr(module, "publish_bus_telemetry", lambda *_a, **_kw: None)
    monkeypatch.setattr(module.signal, "signal",
                        lambda _sig, handler: handlers.append(handler))
    monkeypatch.setenv("WALL_AMP_ENABLED", "true")
    started = time.monotonic()
    thread = threading.Thread(target=module.main, daemon=True)
    thread.start()

    def stop():
        deadline = time.monotonic() + 5.0
        while not handlers and time.monotonic() < deadline:
            time.sleep(0.01)
        handlers[0](15, None)
        thread.join(5.0)
        assert not thread.is_alive(), "the daemon did not stop"

    return started, stop


def _wait_for(predicate, seconds=5.0):
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def restart_module(monkeypatch, tmp_path):
    module = load_module()
    for name, value in (("ATTACK_SECONDS", 0.0), ("MIN_OFF_SECONDS", 0.6),
                        ("MIN_ON_SECONDS", 0.0), ("HOLD_OFF_SECONDS", 3600.0),
                        ("BLOCK_SECONDS", 0.02), ("RELAY_WAIT_SECONDS", 0.0)):
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(module, "SAFE_STATE_FILE", str(tmp_path / "off-verified"))
    return module


def test_a_restart_cannot_close_the_relay_inside_min_off_of_its_predecessors_off(
        restart_module, monkeypatch):
    """The 2026-11-09 follow-up: `changed_at = 0.0` let any restart skip MIN_OFF.

    The first process closes the relay on loud audio and is stopped, which
    opens it. The second starts at once with the audio still loud: it may not
    close the relay until MIN_OFF after that OFF. Before the ledger it closed it
    in one 20 ms block.
    """
    module, relay = restart_module, _RestartRelay()
    _RestartLevel.loud = True

    first_start, stop = _restart_daemon(module, monkeypatch, relay)
    assert _wait_for(lambda: relay.on), "the first process never closed the relay"
    # A fresh boot (no ledger) counts its own start-up OFF.
    assert relay.events[0][0] - first_start >= module.MIN_OFF_SECONDS - 0.02
    stop()
    off_at = relay.events[-1]
    assert off_at[1] == "OFF"

    _second_start, stop = _restart_daemon(module, monkeypatch, relay)
    assert _wait_for(lambda: relay.on), "the second process never closed the relay"
    stop()
    on_at = [moment for moment, kind in relay.events if kind == "ON"][1]
    assert on_at - off_at[0] >= module.MIN_OFF_SECONDS - 0.02, (on_at - off_at[0])


def test_a_restart_after_a_long_quiet_spell_closes_the_relay_without_waiting(
        restart_module, monkeypatch):
    """And the fix costs nothing when the relay has long been open.

    The first process runs quiet past MIN_OFF and is stopped. Its stop is a
    verified OFF too, but it must not move the ledger forward: the relay has
    been open since the first start-up, so the second process closes it on
    the first loud block rather than waiting a fresh MIN_OFF.
    """
    import time
    module, relay = restart_module, _RestartRelay()
    _RestartLevel.loud = False

    _first_start, stop = _restart_daemon(module, monkeypatch, relay)
    time.sleep(module.MIN_OFF_SECONDS + 0.2)
    stop()
    assert relay.events == []

    _RestartLevel.loud = True
    second_start, stop = _restart_daemon(module, monkeypatch, relay)
    assert _wait_for(lambda: relay.on)
    stop()
    assert relay.events[0][1] == "ON"
    assert relay.events[0][0] - second_start < module.MIN_OFF_SECONDS / 2, (
        relay.events[0][0] - second_start)


def _sigterm_run(module, monkeypatch, actuator):
    """Drive main() through one SIGTERM inside the running loop.

    The actuator's `device` is what separates "the relay is unplugged" from
    "the relay is there and refused", so each caller supplies its own.
    """
    handlers = {}

    class Level:
        pcm = "speaker_tap"

        def __init__(self, *_args):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def above(self, _threshold):
            return False

    class StopOnTerm:
        def __init__(self):
            self.stopped = False

        def set(self):
            self.stopped = True

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            handlers[module.signal.SIGTERM](module.signal.SIGTERM, None)
            return True

    monkeypatch.setattr(module, "build_actuator", lambda *_a, **_kw: actuator)
    monkeypatch.setattr(module, "required_tools", lambda _method: ())
    monkeypatch.setattr(module, "ensure_off_bounded", lambda _actuator: False)
    monkeypatch.setattr(module, "record_safe_state", lambda _verified: None)
    monkeypatch.setattr(module, "current_mode", lambda: "bus")
    monkeypatch.setattr(module, "sources_for", lambda _mode: (("speaker_tap", 0.0),))
    monkeypatch.setattr(module, "Level", Level)
    monkeypatch.setattr(module, "publish_bus_telemetry", lambda *_a, **_kw: None)
    monkeypatch.setattr(module.threading, "Event", StopOnTerm)
    monkeypatch.setattr(module.signal, "signal",
                        lambda signum, handler: handlers.setdefault(signum, handler))
    return module.main()


def test_a_present_relay_that_refuses_its_off_stays_fatal_on_sigterm_sr039(monkeypatch, tmp_path):
    """TERRA 2026-09-17, major. Only ABSENCE excuses an unverified OFF.

    The first version of this fix returned zero for every unverified stop, so a
    relay that was plugged in and refused the write reported success while the
    amplifier may still have been energized. The device node is the evidence.
    """
    module = load_module()
    node = tmp_path / "wall_amp_relay"
    node.write_bytes(b"")

    class PresentButRefusing:
        device = str(node)

        def stop(self):
            return False

    assert _sigterm_run(module, monkeypatch, PresentButRefusing()) == 1


def test_sigterm_during_relay_absence_exits_cleanly_sr039(monkeypatch):
    """A dependency stop is clean even when the unplugged relay cannot be turned off."""
    module = load_module()

    class Absent:
        device = "/dev/wall-amp-relay-that-is-not-plugged-in"

        def stop(self):
            return False

    assert _sigterm_run(module, monkeypatch, Absent()) == 0


def test_relay_absence_without_a_termination_request_remains_fatal_sr039(monkeypatch):
    """Only systemd-requested shutdown is clean; an idle unsafe start still fails."""
    module = load_module()

    monkeypatch.setattr(module, "build_actuator", lambda *_a, **_kw: object())
    monkeypatch.setattr(module, "required_tools", lambda _method: ())
    monkeypatch.setattr(module, "ensure_off_bounded", lambda _actuator: False)
    monkeypatch.setattr(module, "record_safe_state", lambda _verified: None)
    monkeypatch.setattr(module, "current_mode", lambda: "panel")

    assert module.main() == 1


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


# --- item L: the bus telemetry the detector publishes as a by-product --------
#
# NO SECOND CAPTURE, and that is the whole architectural point. The detector
# already reads `speaker_tap` every 100 ms for the relay; these levels come out
# of the block it has already unpacked. The broker -- which cannot open a sound
# device at all -- reads the file this writes.

def test_band_levels_follow_the_tone_that_is_actually_there_llr015():
    """A tone in one band must move that band and not its neighbours much.

    Goertzel over a decimated mono sum gives the energy AT the band centre
    rather than an integral over the band, so what is asserted is the ordering
    and the bound -- which is what a decoration needs -- and not a filter shape
    the implementation does not claim to have.
    """
    import math
    import struct
    module = load_module()
    rate = module.RATE
    frames = module.BLOCK_FRAMES

    def tone(hz, amplitude=0.5):
        samples = []
        for i in range(frames):
            value = amplitude * math.sin(2 * math.pi * hz * i / rate)
            pcm = max(-32768, min(32767, int(value * 32767)))
            samples.extend([pcm, pcm])
        return samples

    for index, centre in enumerate(module.BUS_BANDS_HZ):
        if centre >= rate / module.BUS_DECIMATE / 2.0:
            continue
        bands = module.band_levels(tone(centre), module.CHANNELS, frames)
        assert len(bands) == len(module.BUS_BANDS_HZ)
        assert all(0.0 <= b <= 1.0 for b in bands), bands
        assert bands[index] == max(bands), (
            "a tone at %g Hz did not put band %d on top: %s" % (centre, index, bands))
        assert bands[index] > 0.3, (centre, bands[index])


def test_silence_publishes_zeros_and_never_a_floor_llr015():
    module = load_module()
    bands = module.band_levels([0] * (module.BLOCK_FRAMES * module.CHANNELS),
                               module.CHANNELS, module.BLOCK_FRAMES)
    assert bands == [0.0] * len(module.BUS_BANDS_HZ)
    # And the scalar mapping floors rather than going negative or to -inf.
    assert module.scalar_from_amplitude(0.0) == 0.0
    assert module.scalar_from_amplitude(-1.0) == 0.0
    assert module.scalar_from_amplitude(1.0) == 1.0


def test_a_short_block_is_answered_with_zeros_rather_than_noise_llr015():
    """A truncated read must not become a spectrum nobody can explain."""
    module = load_module()
    assert module.band_levels([1000] * 16, module.CHANNELS, 8) == [0.0] * len(module.BUS_BANDS_HZ)


def test_the_published_document_is_the_contract_shape_llr015(tmp_path):
    import json
    module = load_module()

    class FakeLevel:
        pcm = "speaker_tap"
        alive = True
        stale = False
        dbfs = -20.0
        peak = 0.5
        bands = [0.25] * len(module.BUS_BANDS_HZ)
        updated = 1234.5

    path = tmp_path / "bus-telemetry.json"
    module.publish_bus_telemetry(FakeLevel(), True, path=str(path))
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema"] == 1
    # NAMED, so a level from somewhere else can never be presented as the bus.
    assert document["source"] == "speaker_tap"
    assert document["valid"] is True and document["active"] is True
    assert 0.0 <= document["rms"] <= 1.0
    assert document["peak"] == 0.5
    assert document["bands"] == [0.25] * len(module.BUS_BANDS_HZ)
    assert document["observed_monotonic_ms"] == 1234500


def test_a_dead_or_stale_capture_publishes_silence_llr015(tmp_path):
    """A frozen visualizer is indistinguishable from a quiet room, so it is not
    allowed to be frozen: a capture that is not live publishes zeros and says
    `valid: false` beside them."""
    import json
    module = load_module()

    class DeadLevel:
        pcm = "speaker_tap"
        alive = False
        stale = True
        dbfs = -20.0
        peak = 0.9
        bands = [0.9] * len(module.BUS_BANDS_HZ)
        updated = 1234.5

    path = tmp_path / "bus-telemetry.json"
    module.publish_bus_telemetry(DeadLevel(), True, path=str(path))
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["valid"] is False
    assert document["rms"] == 0.0 and document["peak"] == 0.0
    assert document["bands"] == [0.0] * len(module.BUS_BANDS_HZ)


def test_no_bus_tap_in_this_mode_publishes_an_invalid_document_llr015(tmp_path):
    """`trigger` mode has no speaker_tap at all. Saying so beats saying nothing."""
    import json
    module = load_module()
    path = tmp_path / "bus-telemetry.json"
    module.publish_bus_telemetry(None, False, path=str(path))
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["valid"] is False and document["bands"] == [0.0] * len(module.BUS_BANDS_HZ)


def test_telemetry_can_never_take_the_detector_down_llr015():
    """A full /run or a missing directory is not an amplifier fault."""
    module = load_module()

    class FakeLevel:
        pcm = "speaker_tap"
        alive = True
        stale = False
        dbfs = -20.0
        peak = 0.5
        bands = [0.25] * len(module.BUS_BANDS_HZ)
        updated = 1234.5

    # No exception, whatever the path does.
    module.publish_bus_telemetry(FakeLevel(), True, path="/nonexistent/dir/x.json")


def test_an_out_of_band_tone_does_not_light_a_displayed_band_llr015():
    """THE ALIAS THE DECIMATION MUST NOT CREATE (terra, second pass).

    At 4x decimation the Nyquist is 6 kHz and a 4-tap boxcar keeps about 57 % of
    a 6.8 kHz tone, which then folds down and LIGHTS the band labelled 5.2 kHz.
    That is not an imprecise readout, it is one that is wrong about where the
    energy is. At 2x the Nyquist is 12 kHz, the boxcar's first null sits exactly
    there, and nothing audible folds into a displayed band at all.
    """
    import math
    module = load_module()
    rate, frames = module.RATE, module.BLOCK_FRAMES

    def tone(hz, amplitude=0.5):
        samples = []
        for i in range(frames):
            value = amplitude * math.sin(2 * math.pi * hz * i / rate)
            pcm = max(-32768, min(32767, int(value * 32767)))
            samples.extend([pcm, pcm])
        return samples

    reference = module.band_levels(tone(5200.0), module.CHANNELS, frames)
    top = reference.index(max(reference))
    for hz in (6800.0, 9000.0, 14800.0, 19000.0):
        bands = module.band_levels(tone(hz), module.CHANNELS, frames)
        assert all(0.0 <= b <= 1.0 for b in bands), (hz, bands)
        assert bands[top] < reference[top] / 2.0, (
            "a %g Hz tone lit the 5.2 kHz band at %.3f against the real tone's "
            "%.3f" % (hz, bands[top], reference[top]))
