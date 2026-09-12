"""SR-023: the Bluetooth adapter policy, the pairing window, and the A2DP sink.

A wall panel is an unattended radio in a room, and BlueZ completes a Just Works
pairing with no user interaction whenever an adapter is pairable and an agent is
registered. The design answer here is that BOTH of those are true only inside a
bounded pairing window someone deliberately opened at the panel: outside one
there is no agent and the adapter is not pairable, which are two independent
reasons nothing can attach. So the interesting assertions below are the refusals
and the lifecycle -- what keeps the door shut, and what guarantees the agent
cannot outlive the window that justified it.

The last group covers the audio path, where the whole amplifier integration
rests on the sink playing to the ALSA `default` PCM and pinning nothing.
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

    `enabled` defaults TRUE, because the panel is an A2DP sink a phone plays
    through -- but enabled means POWERED, never "anything may pair". The at-rest
    assertions are what carry the posture and they are untouched by it.

    The default is asserted here rather than left implicit because the code and
    the documentation disagreed once, and a panel whose wall.env predates the
    knob silently gets the code default. Pinning the two together is cheap; the
    bug is the same shape in either direction.
    """
    policy = render({})
    assert policy["enabled"] is True
    assert policy["pairing"] == "window"
    assert policy["discoverableAtRest"] is False
    assert policy["pairableAtRest"] is False
    assert policy["pairingWindowSeconds"] == 120
    assert policy["agentCapability"] == "NoInputNoOutput"
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

def test_always_pairable_is_refused():
    """THE LOAD-BEARING REFUSAL, for a sharper reason since the agent landed.

    The agent is window-scoped by design, so `always` would mean a permanently
    pairable adapter whose agent is usually absent: attempts fail confusingly
    most of the time, and whenever a window WAS open the door would already have
    been standing open for hours. No state makes it the safer or more useful
    setting."""
    with pytest.raises(ValueError, match="always"):
        render({"WALL_BLUETOOTH_PAIRING": "always"})


def test_advertising_a_panel_that_refuses_every_pairing_is_refused():
    """A trap for the person holding the phone, not a security posture."""
    with pytest.raises(ValueError, match="discoverable-at-rest"):
        render({"WALL_BLUETOOTH_PAIRING": "off",
                "WALL_BLUETOOTH_DISCOVERABLE_AT_REST": "true"})


def test_the_agent_capability_defaults_to_the_window_scoped_one():
    """NoInputNoOutput is the default and IS accepted, which reverses an earlier
    refusal. It produces silent Just Works pairing, and that is safe here only
    because the agent exists solely while a window is open -- outside one there
    is no agent AND the adapter is not pairable. The consent is a person opening
    the window, since the shell has no pairing UI to show a passkey in.
    DisplayYesNo stays selectable for when it does."""
    assert render({})["agentCapability"] == "NoInputNoOutput"
    assert render({"WALL_BLUETOOTH_AGENT_CAPABILITY": "DisplayYesNo"})["agentCapability"] == "DisplayYesNo"
    with pytest.raises(ValueError):
        render({"WALL_BLUETOOTH_AGENT_CAPABILITY": "Whatever"})


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
    ctl, failed = apply_with(render({"WALL_BLUETOOTH_ENABLED": "false"}), monkeypatch)
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
    """The mismatch that shipped once: render() and wall.env.example disagreed,
    and a panel whose wall.env predates the knob gets the code default."""
    example = (ROOT / "stack/autoinstall/wall/wall.env.example").read_text(encoding="utf-8")
    assert "WALL_BLUETOOTH_ENABLED=true" in example
    assert render({})["enabled"] is True
    assert render({"WALL_BLUETOOTH_ENABLED": "false"})["enabled"] is False


# ── the pairing agent lives and dies with the window ─────────────────────────

wall_bluetooth_pairing = _load("wall_bluetooth_pairing", "wall-bluetooth-pairing.py")


class FakeAgent:
    """Stands in for the bluetoothctl subprocess."""

    def __init__(self, dies_immediately=False):
        self.dies_immediately = dies_immediately
        self.terminated = False
        self.killed = False
        self._returncode = 0 if dies_immediately else None

    def wait(self, timeout=None):
        if self.dies_immediately:
            return 0
        if not self.terminated:
            raise __import__("subprocess").TimeoutExpired("bluetoothctl", timeout)
        return 0

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = 0

    def kill(self):
        self.killed = True
        self._returncode = -9


def test_the_agent_is_registered_with_the_configured_capability(monkeypatch):
    spawned = {}

    def fake_popen(argv, **kwargs):
        spawned["argv"] = argv
        return FakeAgent()

    monkeypatch.setattr(wall_bluetooth_pairing.subprocess, "Popen", fake_popen)
    policy = render({"WALL_BLUETOOTH_PAIRING_WINDOW_SECONDS": "90"})
    agent = wall_bluetooth_pairing.start_agent(policy)
    assert agent is not None
    assert spawned["argv"] == ("bluetoothctl", "--agent", "NoInputNoOutput", "--timeout", "90")


def test_the_agents_own_timeout_matches_the_window(monkeypatch):
    """A SECOND BOUND, deliberately. If this script is killed without running
    its cleanup, bluetoothctl still exits on its own and the agent goes with
    it -- the same belt-and-braces the door's two timers use."""
    spawned = {}
    monkeypatch.setattr(wall_bluetooth_pairing.subprocess, "Popen",
                        lambda argv, **kw: spawned.setdefault("argv", argv) and None or FakeAgent())
    policy = render({"WALL_BLUETOOTH_PAIRING_WINDOW_SECONDS": "300"})
    wall_bluetooth_pairing.start_agent(policy)
    argv = spawned["argv"]
    assert argv[argv.index("--timeout") + 1] == str(policy["pairingWindowSeconds"])


def test_an_agent_that_dies_at_once_is_reported_as_no_agent(monkeypatch):
    """A capability BlueZ refuses makes bluetoothctl exit immediately, and a
    dead agent is indistinguishable from one that never registered. Opening the
    door in front of nothing is the failure this catches."""
    monkeypatch.setattr(wall_bluetooth_pairing.subprocess, "Popen",
                        lambda argv, **kw: FakeAgent(dies_immediately=True))
    assert wall_bluetooth_pairing.start_agent(render({})) is None


def test_an_unstartable_agent_is_reported_rather_than_raising(monkeypatch):
    def boom(argv, **kwargs):
        raise OSError("no bluetoothctl")

    monkeypatch.setattr(wall_bluetooth_pairing.subprocess, "Popen", boom)
    assert wall_bluetooth_pairing.start_agent(render({})) is None


def test_stopping_the_agent_is_idempotent_and_escalates():
    agent = FakeAgent()
    wall_bluetooth_pairing.stop_agent(agent)
    assert agent.terminated
    wall_bluetooth_pairing.stop_agent(agent)  # already dead; must not raise
    wall_bluetooth_pairing.stop_agent(None)


# ── the A2DP sink reaches the amplifier by reaching `default` ────────────────

def _override(name):
    return (WALL / name).read_text(encoding="utf-8")


def test_the_sink_plays_to_default_and_never_pins_a_card():
    """THE ENTIRE AMPLIFIER INTEGRATION IS THIS ONE WORD.

    `default` resolves through the wall-audio-mode symlink: in trigger mode to
    kiosk_mix -> snd-aloop -> kiosk_monitor, which panel-amp-trigger.py already
    measures, so Bluetooth powers the amplifier with no change to the daemon; in
    panel mode to the internal speaker, where the amplifier is not commanded at
    all. Pinning a card or naming kiosk_mix would break one mode or the other
    and would decouple Bluetooth from wall-audio-mode.
    """
    conf = _override("wall-bluealsa-aplay-override.conf")
    assert "--pcm=default" in conf
    # Directives only. The comments deliberately NAME kiosk_mix and
    # speaker_shared while explaining why neither is pinned, so matching raw
    # text here would fail on its own documentation.
    directives = [line for line in conf.splitlines()
                  if line.strip() and not line.lstrip().startswith("#") and "=" in line]
    for pinned in ("kiosk_mix", "speaker_shared", "hw:", "card_usb", "plughw"):
        assert not [d for d in directives if pinned in d], pinned


def test_the_sink_does_not_also_offer_to_take_audio_off_the_panel():
    """The stock unit runs a2dp-source as well. The panel is the thing you play
    INTO; a source profile would let a phone pull audio off it."""
    conf = _override("wall-bluealsa-override.conf")
    assert "-p a2dp-sink" in conf
    directives = [line for line in conf.splitlines()
                  if line.strip() and not line.lstrip().startswith("#") and "=" in line]
    assert not [d for d in directives if "a2dp-source" in d]


def test_the_dmix_hostile_sandboxing_is_relaxed_with_its_reason():
    """dmix is SysV shared memory created by the kiosk with ipc_gid 29. Inside
    a user namespace this service's credentials are not the ones those
    permissions were written for, and RemoveIPC would tear down segments the
    kiosk still needs. Pairing would succeed and nothing would ever be audible,
    which is the worst way for this to fail."""
    conf = _override("wall-bluealsa-aplay-override.conf")
    assert "PrivateUsers=false" in conf
    assert "RemoveIPC=false" in conf
