"""The amplifier-side USB hub is re-enumerated once when it comes back empty.

Item 25 addendum (2026-09-13 21:52): after a hot move the Atmel 03eb:0902 hub
answered on the bus and none of its downstream ports did --
`hub 1-2:1.0: hub_ext_port_status failed (err = -71)` -- so the audio adapter
and the CH340 relay were both absent. Item 25's BindsTo= recovery is correct and
did nothing, because it depends on the hub cooperating. `authorized` 0 then 1 on
the hub brought both back.

What these tests pin is the BOUND as much as the recovery: a toggle of
`authorized` is a hardware reset of everything downstream, the LCUS-2 relay
included, so it must happen at most twice per hub and never on a timer.
"""

import importlib.util
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack" / "autoinstall" / "wall"
SCRIPT = WALL / "wall-usb-hub-reset.py"
RULES = WALL / "91-wall-usb-hub-reset.rules"
UNIT = WALL / "wall-usb-hub-reset@.service"


def load():
    spec = importlib.util.spec_from_file_location("wall_usb_hub_reset", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Bus:
    """A fake /sys/bus/usb/devices, and a record of what was written to it."""

    def __init__(self, tmp_path, vendor="03eb", product="0902", children=()):
        self.root = tmp_path / "usb"
        self.hub = self.root / "1-2"
        self.hub.mkdir(parents=True)
        (self.hub / "idVendor").write_text(vendor + "\n", encoding="ascii")
        (self.hub / "idProduct").write_text(product + "\n", encoding="ascii")
        (self.hub / "authorized").write_text("1\n", encoding="ascii")
        # The hub's own interface is NOT a child; the fault is that the
        # interface was there and the ports were not. It is injected into the
        # listing rather than created: a colon is not a legal filename on the
        # machine these tests run on, and the colon is the entire point.
        self.injected = ["1-2:1.0"]
        for name in children:
            (self.root / name).mkdir()
        self.state = tmp_path / "run"

    def install(self, module, monkeypatch):
        monkeypatch.setattr(module, "SYSFS_USB", str(self.root))
        real_listdir = os.listdir

        def listdir(path):
            entries = real_listdir(path)
            if os.path.abspath(path) == os.path.abspath(str(self.root)):
                return entries + self.injected
            return entries

        monkeypatch.setattr(module.os, "listdir", listdir)

    @property
    def authorized(self):
        return (self.hub / "authorized").read_text(encoding="ascii")


def runner(*returncodes):
    """A fake systemctl. Each call consumes one exit code; 0 means active."""
    codes = list(returncodes)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        code = codes.pop(0) if codes else 1

        class Result:
            returncode = code
        return Result()

    run.calls = calls
    return run


def drive(module, bus, monkeypatch, run, wait=15.0, argv=None):
    bus.install(module, monkeypatch)
    written = []
    real_open = open

    def recording_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if "w" in mode and str(path).endswith("authorized"):
            written.append(str(path))
        return handle

    monkeypatch.setattr("builtins.open", recording_open)
    code = module.main(argv or ["1-2", "--wait", str(wait)],
                       sleep=lambda _seconds: None,
                       monotonic=_clock(), run=run,
                       state_dir=str(bus.state))
    return code, written


def _clock():
    """Monotonic that advances a second per call, so no test really waits."""
    state = {"now": 0.0}

    def monotonic():
        state["now"] += 1.0
        return state["now"]
    return monotonic


# --- the recovery itself ---------------------------------------------------

def test_an_empty_hub_is_toggled_once_and_the_adapter_comes_back(tmp_path, monkeypatch):
    module = load()
    bus = Bus(tmp_path)
    # Inactive through the first wait, then active: the toggle worked.
    run = runner(*([1] * 20 + [0]))
    code, written = drive(module, bus, monkeypatch, run)

    assert code == 0
    assert len(written) == 2, written          # 0 then 1, one toggle
    assert bus.authorized.strip() == "1"       # never left deauthorized
    assert os.path.exists(os.path.join(bus.state, "1-2"))


def test_a_hub_with_children_is_never_reset(tmp_path, monkeypatch):
    """The adapter being unplugged on purpose is not a hub fault.

    Resetting here would be a hardware reset of the amplifier's LATCHING relay
    as a side effect of somebody tidying a desk.
    """
    module = load()
    bus = Bus(tmp_path, children=("1-2.1",))
    code, written = drive(module, bus, monkeypatch, runner(*([1] * 40)))

    assert code == 0
    assert written == []
    assert not os.path.exists(os.path.join(bus.state, "1-2"))


def test_a_healthy_adapter_is_not_waited_out_or_touched(tmp_path, monkeypatch):
    module = load()
    bus = Bus(tmp_path)
    run = runner(0)
    code, written = drive(module, bus, monkeypatch, run)

    assert code == 0
    assert written == []
    assert len(run.calls) == 1   # asked once, answered active, exited


def test_another_vendors_hub_on_the_same_rule_is_left_alone(tmp_path, monkeypatch):
    module = load()
    bus = Bus(tmp_path, vendor="1a86", product="7523")
    code, written = drive(module, bus, monkeypatch, runner(*([1] * 40)))

    assert code == 0
    assert written == []


# --- the bound -------------------------------------------------------------

def test_at_most_two_toggles_for_one_add_event(tmp_path, monkeypatch):
    module = load()
    bus = Bus(tmp_path)
    # Never comes back, however many times it is reset.
    code, written = drive(module, bus, monkeypatch, runner(*([1] * 400)))

    assert code == 0                     # a visible give-up, not a failed unit
    assert len(written) == 2 * module.MAX_ATTEMPTS
    assert len(module.attempts_so_far("1-2", state_dir=str(bus.state))) == 2


def test_a_later_add_event_inside_the_cooling_window_does_not_toggle_again(tmp_path, monkeypatch):
    """Re-authorizing a hub can itself produce udev events.

    The in-process counter cannot see those, so the bound has to survive
    process exit. This is the difference between a recovery and a loop.
    """
    module = load()
    bus = Bus(tmp_path)
    drive(module, bus, monkeypatch, runner(*([1] * 400)))
    code, written = drive(module, bus, monkeypatch, runner(*([1] * 400)))

    assert code == 0
    assert written == []


def test_the_window_expires_so_a_fault_next_week_is_still_recovered(tmp_path, monkeypatch):
    module = load()
    bus = Bus(tmp_path)
    drive(module, bus, monkeypatch, runner(*([1] * 400)))
    stamps = module.attempts_so_far("1-2", state_dir=str(bus.state))
    assert stamps
    # Same file, read far enough in the future.
    later = stamps[-1] + module.COOL_OFF_SECONDS + 1.0
    assert module.attempts_so_far("1-2", now=later, state_dir=str(bus.state)) == []


# --- the sysfs path --------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "", "usb1", "../../../../devices", "1-2/../1-1", "1-2;rm", "1-2\n", "usb1/1-2",
])
def test_only_a_real_bus_id_is_ever_joined_to_a_sysfs_path(bad):
    """`authorized` exists on every USB device, the touchscreen included."""
    module = load()
    assert module.hub_path(bad) is None


def test_a_bus_id_with_nested_ports_is_accepted():
    module = load()
    for good in ("1-2", "1-2.4", "3-1.1.2"):
        assert module.hub_path(good).endswith(good)


def test_the_hubs_own_interface_is_not_counted_as_a_child(tmp_path, monkeypatch):
    module = load()
    bus = Bus(tmp_path)
    bus.install(module, monkeypatch)
    # 1-2:1.0 exists in the fixture and is the hub's interface. Counting it
    # would make the measured fault -- interface present, ports absent -- look
    # like a healthy hub, and nothing would ever be recovered.
    assert module.children("1-2") == []
    (bus.root / "1-2.4").mkdir()
    bus.injected.append("1-2:1.1")
    assert module.children("1-2") == ["1-2.4"]


def test_a_hub_that_left_again_is_not_an_error(tmp_path, monkeypatch):
    module = load()
    bus = Bus(tmp_path)
    bus.install(module, monkeypatch)
    assert module.main(["1-9"], sleep=lambda _s: None, run=runner(1),
                       state_dir=str(bus.state)) == 0


# --- the rule and the unit -------------------------------------------------

def test_the_rule_matches_the_hub_device_and_not_its_interface():
    body = "\n".join(line for line in RULES.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))

    assert 'DEVTYPE=="usb_device"' in body
    assert 'ATTR{idVendor}=="03eb"' in body and 'ATTR{idProduct}=="0902"' in body
    # ATTRS would walk up to the parent hubs as well.
    assert "ATTRS{idVendor}" not in body
    assert 'ENV{SYSTEMD_WANTS}+="wall-usb-hub-reset@$kernel.service"' in body
    # The recovery starts on an add and on nothing else: a change event that
    # re-ran it would be the loop this must not become.
    # udev line continuations first: the ACTION= and the ENV{SYSTEMD_WANTS}=
    # that must agree live on two physical lines of one logical rule.
    body = body.replace("\\\n", " ")
    wants = [line for line in body.splitlines() if "SYSTEMD_WANTS" in line]
    assert wants and all('ACTION=="add"' in line for line in wants)
    # ...but the systemd tag is set on every non-remove event, or it goes stale
    # exactly as card1's did on the adapter rule.
    tag = [line for line in body.splitlines() if 'TAG+="systemd"' in line]
    assert tag and all('ACTION=="add"' not in line for line in tag)


def test_the_unit_is_a_bounded_oneshot_that_never_restarts():
    text = UNIT.read_text(encoding="utf-8")

    assert "Type=oneshot" in text
    assert "Restart=no" in text
    assert "Restart=always" not in text and "Restart=on-failure" not in text
    assert "ExecStart=/usr/local/lib/wall-panel/wall-usb-hub-reset.py %i" in text
    assert "TimeoutStartSec=" in text
    # A template, started by udev per hub. No [Install]: enabling it would mean
    # a reset watch running against a hub nobody touched.
    assert "[Install]" not in text
    assert UNIT.name.endswith("@.service")


def test_firstboot_installs_the_rule_the_unit_and_the_script():
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")

    assert "91-wall-usb-hub-reset.rules" in firstboot
    assert "wall-usb-hub-reset@.service" in firstboot
    assert "wall-usb-hub-reset.py" in firstboot
    # An installed rule with no unit behind it is a silent no-op.
    assert "wall-usb-hub-reset@.service is not" in firstboot
