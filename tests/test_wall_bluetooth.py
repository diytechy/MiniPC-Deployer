"""SR-023: the Bluetooth adapter policy, the pairing window, and the A2DP sink.

A wall panel is an unattended radio in a room, and BlueZ completes a Just Works
pairing with no user interaction whenever an adapter is pairable and an agent is
registered. The design answer here is that BOTH of those are true only inside a
bounded pairing window someone deliberately opened at the panel: outside one
there is no agent and the adapter is not pairable, which are two independent
reasons nothing NEW can bond. So the interesting assertions below are the
refusals and the lifecycle -- what keeps the door shut, and what guarantees the
agent cannot outlive the window that justified it.

Said precisely, because an earlier version of this file said otherwise: the
window bounds BONDING, not use. A device paired inside one reconnects and plays
whenever it likes afterwards, which is what makes the panel a speaker rather
than something you re-pair every morning, and `forget` is the only way back.

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


def test_the_agent_capability_defaults_to_the_authenticated_one():
    """DisplayYesNo since 2026-09-19, and the reversal has a measurement behind it.

    The default was NoInputNoOutput, on the argument that the pairing WINDOW is
    the consent -- the agent exists only while one is open, outside it the
    adapter is neither pairable nor discoverable. That still holds and the
    window has not changed. What it could not give is authentication of the
    PEER: NoInputNoOutput is Just Works, and the bond measured from the dev PC
    came back `ConfirmOnly` with `protection: None`. The comment that shipped
    it named its own exit condition -- a pairing UI to display a number in --
    and WSN-024 built one. Owner ruling 2026-09-19.

    NoInputNoOutput stays SELECTABLE. A panel with no glass, or a device that
    cannot do numeric comparison, still needs it, and refusing it outright is
    the mistake an even earlier revision made and reversed.
    """
    assert render({})["agentCapability"] == "DisplayYesNo"
    assert render({"WALL_BLUETOOTH_AGENT_CAPABILITY": "NoInputNoOutput"})["agentCapability"] == "NoInputNoOutput"
    with pytest.raises(ValueError):
        render({"WALL_BLUETOOTH_AGENT_CAPABILITY": "Whatever"})


def test_the_shipped_wall_env_and_the_code_default_agree_on_the_capability():
    """Two files, one fact -- and they disagreed for the whole of WSN-024.

    `wall.env.example` shipped NoInputNoOutput while the pane it was paired
    with had been built to display a passkey, so the panel paired by Just
    Works and nothing failed to say so. The renderer's default only ever
    applies to a wall.env that omits the key, which makes a drift between the
    two invisible on every panel that has one -- which is every panel.
    """
    example = (WALL / "wall.env.example").read_text(encoding="utf-8")
    shipped = "WALL_BLUETOOTH_AGENT_CAPABILITY=%s" % render({})["agentCapability"]
    assert any(line.strip() == shipped for line in example.splitlines()), (
        "wall.env.example ships a capability the renderer would not default to")


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
    """Both the bluetoothctl verbs and the D-Bus timeout writes land in one
    ordered call log, so ordering assertions still see the whole sequence."""
    ctl = FakeCtl(fail)
    monkeypatch.setattr(wall_bluetooth_apply, "bluetoothctl", ctl)
    monkeypatch.setattr(wall_bluetooth_apply, "set_timeout",
                        lambda prop, secs: ctl(TIMEOUT_VERB[prop], str(secs)))
    failed = wall_bluetooth_apply.apply(policy)
    return ctl, failed


# The properties are named on D-Bus; the tests speak the old verb names because
# that is what the ordering assertions read.
TIMEOUT_VERB = {"DiscoverableTimeout": "discoverable-timeout",
                "PairableTimeout": "pairable-timeout"}


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


def test_the_agent_is_the_real_one_and_gets_the_capability_and_window(monkeypatch):
    spawned = {}

    def fake_popen(argv, **kwargs):
        spawned["argv"] = argv
        return FakeAgent()

    monkeypatch.setattr(wall_bluetooth_pairing.subprocess, "Popen", fake_popen)
    policy = render({"WALL_BLUETOOTH_PAIRING_WINDOW_SECONDS": "90"})
    assert wall_bluetooth_pairing.start_agent(policy) is not None
    argv = spawned["argv"]
    # NOT bluetoothctl. `bluetoothctl --agent` registers an agent without ever
    # calling RequestDefaultAgent, so BlueZ has one it will not route an
    # unsolicited pairing to -- the window opens and then refuses the pairing it
    # was opened for, with the subprocess perfectly alive throughout.
    assert argv[0].endswith("wall-bluetooth-agent"), argv
    assert "bluetoothctl" not in argv[0]
    assert argv[argv.index("--capability") + 1] == "DisplayYesNo"
    assert argv[argv.index("--timeout") + 1] == "90"


def test_the_agent_calls_request_default_agent_not_merely_register():
    """The distinction the bluetoothctl version silently got wrong. Asserted on
    the agent's source because the call itself needs a live BlueZ."""
    agent_src = (WALL / "wall-bluetooth-agent.py").read_text(encoding="utf-8")
    assert "RegisterAgent" in agent_src
    assert "RequestDefaultAgent" in agent_src
    # And a failure of either must abort rather than leave a half-registered
    # agent behind a door that is about to open.
    assert agent_src.count("sys.exit") >= 3
    assert "UnregisterAgent" in agent_src


def test_the_agent_refuses_non_audio_services():
    """AuthorizeService is the one callback an ALREADY-BONDED device can reach
    outside a window, so it is the only place the panel can still say no to a
    device it once said yes to. Pairing to play music is not consent to become
    a keyboard."""
    agent_src = (WALL / "wall-bluetooth-agent.py").read_text(encoding="utf-8")
    assert "def AuthorizeService" in agent_src
    assert "AUDIO_UUIDS" in agent_src
    # A2DP Sink is the one that must be allowed, or nothing plays at all.
    assert "0000110b-0000-1000-8000-00805f9b34fb" in agent_src


def test_an_agent_that_exits_at_once_is_reported_as_no_agent(monkeypatch):
    """wall-bluetooth-agent exits non-zero when RegisterAgent or
    RequestDefaultAgent is refused, so an early exit is a REAL signal here --
    unlike the process-liveness check this replaced, which proved nothing."""
    monkeypatch.setattr(wall_bluetooth_pairing.subprocess, "Popen",
                        lambda argv, **kw: FakeAgent(dies_immediately=True))
    assert wall_bluetooth_pairing.start_agent(render({})) is None


def test_an_unstartable_agent_is_reported_rather_than_raising(monkeypatch):
    def boom(argv, **kwargs):
        raise OSError("no agent installed")

    monkeypatch.setattr(wall_bluetooth_pairing.subprocess, "Popen", boom)
    assert wall_bluetooth_pairing.start_agent(render({})) is None


def test_stopping_the_agent_is_idempotent_and_escalates():
    agent = FakeAgent()
    wall_bluetooth_pairing.stop_agent(agent)
    assert agent.terminated
    wall_bluetooth_pairing.stop_agent(agent)  # already dead; must not raise
    wall_bluetooth_pairing.stop_agent(None)


def test_the_agent_output_is_not_sent_to_dev_null():
    """It logs which device paired and with what passkey, and that is the only
    record of what a window let in. Discarding it was how the bluetoothctl
    version also made every non-Just-Works capability unusable."""
    src = (WALL / "wall-bluetooth-pairing.py").read_text(encoding="utf-8")
    popen = src[src.index("agent = subprocess.Popen("):]
    popen = popen[:popen.index("\n        )")]
    assert "stdout=None" in popen and "stderr=None" in popen, popen


# ── a bond outlives the window, and that has to be revocable ────────────────

def test_bonded_devices_can_be_listed_and_forgotten():
    """THE CORRECTED CLAIM. A window bounds who can BOND, not what a bonded
    device may do afterwards -- a phone paired in a window reconnects and plays
    whenever it likes, which is the whole point of a speaker. Earlier comments
    here said "nothing can attach", which was simply wrong. `forget` is the only
    way to withdraw what a window granted, so it has to exist."""
    src = (WALL / "wall-bluetooth-pairing.py").read_text(encoding="utf-8")
    assert "def list_bonded" in src and "def forget" in src
    assert '"remove", mac' in src, "forget must actually remove the bond"
    assert '"disconnect", mac' in src, "an active stream must be dropped first"


@pytest.mark.parametrize("bad", ["", "not-a-mac", "AA:BB:CC:DD:EE", "../../etc", "AA:BB:CC:DD:EE:FF extra"])
def test_forget_refuses_anything_that_is_not_a_mac(bad, monkeypatch):
    calls = []
    monkeypatch.setattr(wall_bluetooth_pairing, "bluetoothctl",
                        lambda *a: calls.append(a) or True)
    with pytest.raises(SystemExit):
        wall_bluetooth_pairing.forget(bad)
    assert calls == [], "nothing may reach bluetoothctl unvalidated"


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


# ── the audio claim's two load-bearing facts ────────────────────────────────

@pytest.mark.parametrize("mode_conf", ["asound-trigger-mode.conf", "asound-panel-mode.conf"])
def test_default_is_a_plug_so_a_44_1khz_phone_can_open_it(mode_conf):
    """WITHOUT THIS THE WHOLE FEATURE IS DEAD, and a review flagged it as
    already dead.

    dmix has ONE fixed configuration -- S16_LE/48000/2 in both mode files -- and
    bluealsa-aplay passes the Bluetooth stream's own parameters through rather
    than converting. A phone negotiating SBC at 44,100 Hz would therefore be
    refused by dmix, and there would be neither audio nor a detector input.

    What saves it is that `default` is not dmix: it is a `plug` WRAPPING dmix,
    in both modes, so rate and format conversion happens before the fixed layer.
    That is load-bearing and easy to "simplify" away, so it is pinned here.
    """
    conf = (WALL / mode_conf).read_text(encoding="utf-8")
    block = conf[conf.index("pcm.!default"):]
    block = block[:block.index("}") + 1]
    assert "type plug" in block, block
    # And the fixed layer really is fixed, which is why the plug is required.
    assert "rate 48000" in conf


def test_a_mode_switch_takes_the_bluetooth_sink_with_it():
    """bluealsa-aplay resolves `default` when it opens a stream, exactly like
    the kiosk, so a connected phone would keep playing into the OLD chain after
    `wall-audio-mode panel` -- and worse, clear_ipc skips segments with
    attachments, so an aplay holding the old dmix leaves a stale segment for the
    next mode to reuse. That is the failure wall-audio-mode's own comments call
    "garbage that looks convincingly like a signal"."""
    script = (WALL / "wall-audio-mode").read_text(encoding="utf-8")
    stop_at = script.index("unit stop wall-amp-trigger.service")
    clear_at = script.index("clear_ipc\n", stop_at)
    bt_stop = script.index("unit stop bluealsa-aplay.service")
    assert stop_at < bt_stop < clear_at, "the sink must be stopped before the IPC sweep"
    assert "start_if_enabled bluealsa-aplay.service" in script, "and started again after"


def test_the_pairable_timeout_is_set_over_dbus_not_via_a_verb_bluez_lacks():
    """FOUND ON THE PANEL AT DEPLOY TIME. bluez 5.72's bluetoothctl has
    `discoverable-timeout` and NO pairable equivalent, so the symmetrical verb
    is a trap: it fails with "Invalid command in menu main" and the at-rest
    assertion never completes. Both properties exist on org.bluez.Adapter1, so
    both go through D-Bus."""
    for src in ("wall-bluetooth-apply.py", "wall-bluetooth-pairing.py"):
        text = (WALL / src).read_text(encoding="utf-8")
        assert "PairableTimeout" in text and "DiscoverableTimeout" in text, src
        assert "busctl" in text, src
        # Code lines only: the comments explaining this deliberately NAME the
        # verb that does not exist, which is the whole point of them.
        code = [ln for ln in text.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#") and '"""' not in ln]
        body = chr(10).join(code)
        assert 'bluetoothctl("pairable-timeout"' not in body, src
        assert 'bluetoothctl("discoverable-timeout"' not in body, src
