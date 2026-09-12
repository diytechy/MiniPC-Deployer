"""SR-023: the Bluetooth adapter policy, its defaults, and what it refuses.

The behaviour under test is mostly a set of refusals, and they are the point.
A wall panel is an unattended radio in a room; BlueZ completes a Just Works
pairing with no user interaction whenever an adapter is pairable and no agent
is registered, and this image registers no agent. So the interesting assertions
here are the ones that keep the door shut by default and refuse the two
configurations that would quietly prop it open.
"""

import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack/autoinstall/wall"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, WALL / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


render_bluetooth = _load("render_bluetooth", "render-bluetooth.py")
wall_bluetooth_apply = _load("wall_bluetooth_apply", "wall-bluetooth-apply.py")
render = render_bluetooth.render


# ── defaults ─────────────────────────────────────────────────────────────────

def test_the_default_panel_is_invisible_and_unpairable_at_rest():
    """The whole posture in one assertion: an untouched wall.env yields a panel
    that nothing in the building can see or attach to.

    `enabled` defaults FALSE and that is checked here rather than in passing:
    it read "true" while wall.env.example claimed off, so every panel already in
    the field -- whose /etc/wall-panel/wall.env predates the knob entirely --
    would have rendered enabled:true and had its adapter powered on by the boot
    unit. A code default that disagrees with the documented one is the shape of
    that bug, so the two are pinned together.
    """
    policy = render({})
    assert policy["enabled"] is False
    assert policy["pairing"] == "window"
    assert policy["discoverableAtRest"] is False
    assert policy["pairableAtRest"] is False
    assert policy["pairingWindowSeconds"] == 120
    assert policy["agentCapability"] == "DisplayYesNo"
    assert policy["alias"] == "wall-panel"


def test_window_mode_is_never_open_at_rest():
    """`window` describes what CAN be opened, never what is. The window is
    opened by wall-bluetooth-pairing, never at boot."""
    policy = render({"WALL_BLUETOOTH_PAIRING": "window",
                     "WALL_BLUETOOTH_PAIRING_WINDOW_SECONDS": "600"})
    assert policy["pairableAtRest"] is False and policy["discoverableAtRest"] is False


def test_discoverable_at_rest_is_available_without_becoming_pairable():
    """Advertising and accepting are separate decisions, and the panel can do
    the first without the second."""
    policy = render({"WALL_BLUETOOTH_ENABLED": "true", "WALL_BLUETOOTH_DISCOVERABLE_AT_REST": "true"})
    assert policy["discoverableAtRest"] is True
    assert policy["pairableAtRest"] is False


# ── refusals ─────────────────────────────────────────────────────────────────

def test_always_pairable_is_refused_while_no_agent_exists():
    """THE LOAD-BEARING REFUSAL. `always` without a registered agent is the
    unattended silent-pairing case, and the one a window cannot bound."""
    with pytest.raises(ValueError, match="agent"):
        render({"WALL_BLUETOOTH_PAIRING": "always"})


def test_advertising_a_panel_that_refuses_every_pairing_is_refused():
    """A trap for the person holding the phone, not a security posture."""
    with pytest.raises(ValueError, match="discoverable-at-rest"):
        render({"WALL_BLUETOOTH_PAIRING": "off",
                "WALL_BLUETOOTH_DISCOVERABLE_AT_REST": "true"})


def test_no_input_no_output_capability_is_not_selectable():
    """It is the capability that produces silent Just Works pairing. A panel
    with a screen and a digitizer has no business claiming it cannot ask."""
    with pytest.raises(ValueError):
        render({"WALL_BLUETOOTH_AGENT_CAPABILITY": "NoInputNoOutput"})


@pytest.mark.parametrize("window", ["29", "601", "0", "-120", "abc", ""])
def test_the_window_is_bounded_at_both_ends(window):
    with pytest.raises(ValueError):
        render({"WALL_BLUETOOTH_PAIRING_WINDOW_SECONDS": window})


@pytest.mark.parametrize("alias", ["", "a" * 33, "wall panel\n", "wall;panel", "wall$(id)"])
def test_the_broadcast_alias_is_held_to_a_plain_charset(alias):
    # It is broadcast to every device in range and consumed by a shell helper.
    with pytest.raises(ValueError):
        render({"WALL_BLUETOOTH_ALIAS": alias})


@pytest.mark.parametrize("key,value", [
    ("WALL_BLUETOOTH_ENABLED", "yes"),
    ("WALL_BLUETOOTH_DISCOVERABLE_AT_REST", "1"),
    ("WALL_BLUETOOTH_PAIRING", "sometimes"),
])
def test_near_miss_values_are_refused_rather_than_coerced(key, value):
    # "yes" reading as false, or "1" as true, is how a door silently changes state.
    with pytest.raises(ValueError):
        render({key: value})


# ── the applier ──────────────────────────────────────────────────────────────

class FakeCtl:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, *args):
        self.calls.append(args)
        return args[0] not in self.fail


def apply_with(policy, monkeypatch, fail=()):
    ctl = FakeCtl(fail)
    monkeypatch.setattr(wall_bluetooth_apply, "bluetoothctl", ctl)
    failed = wall_bluetooth_apply.apply(policy)
    return ctl, failed


def test_the_timeouts_are_set_before_the_door_can_open(monkeypatch):
    """On a radio, a briefly-unbounded window is still an opportunity."""
    policy = render({"WALL_BLUETOOTH_ENABLED": "true", "WALL_BLUETOOTH_DISCOVERABLE_AT_REST": "true"})
    ctl, failed = apply_with(policy, monkeypatch)
    assert failed == []
    order = [call[0] for call in ctl.calls]
    assert order.index("discoverable-timeout") < order.index("discoverable")
    assert order.index("pairable-timeout") < order.index("pairable")


def test_a_disabled_adapter_is_powered_off_and_nothing_else_is_asserted(monkeypatch):
    ctl, failed = apply_with(render({}), monkeypatch)
    assert failed == []
    assert ctl.calls == [("power", "off")]


def test_the_at_rest_state_is_asserted_explicitly_not_assumed(monkeypatch):
    """Both properties are written every time, including when the answer is
    "no". Assuming BlueZ powered up closed is how a panel stays open."""
    ctl, failed = apply_with(render({"WALL_BLUETOOTH_ENABLED": "true"}), monkeypatch)
    assert failed == []
    assert ("pairable", "no") in ctl.calls
    assert ("discoverable", "no") in ctl.calls


def test_a_failed_property_write_is_reported_not_swallowed(monkeypatch):
    ctl, failed = apply_with(render({"WALL_BLUETOOTH_ENABLED": "true"}), monkeypatch, fail=("discoverable",))
    assert failed == ["discoverable"]


def test_the_rendered_policy_is_json_and_round_trips(tmp_path):
    # configure-bluetooth.sh writes exactly this and the applier reads it back.
    policy = render({})
    assert json.loads(json.dumps(policy)) == policy


def test_the_code_default_and_the_documented_default_agree():
    """The mismatch that shipped: render() said true, wall.env.example said
    false, and a panel whose wall.env predates the knob got the code default."""
    example = (ROOT / "stack/autoinstall/wall/wall.env.example").read_text(encoding="utf-8")
    assert "WALL_BLUETOOTH_ENABLED=false" in example
    assert render({})["enabled"] is False
    # And the shell agrees, so the tab is hidden on the same panel the adapter
    # is powered down on.
    assert render({"WALL_BLUETOOTH_ENABLED": "true"})["enabled"] is True
