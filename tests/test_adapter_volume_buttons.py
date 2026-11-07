"""SR-150: the USB audio adapter's Mute / Volume- / Volume+ buttons.

Captured from the hardware 2026-11-07 (HomeHub session, panel event7, "USB
Sound Device", C-Media 0d8c:0102):

    0.000 VOLUP   PRESS     scancode 0xc00e9      <- a tap is ~300 ms down
    0.320 VOLUP   release
    8.160 MUTE    PRESS     scancode 0xc00e2      <- mute is one instant pulse
    8.160 MUTE    release
   10.752 VOLUP   PRESS                           <- a 1.8 s hold is ONE press
   12.576 VOLUP   release                            and ONE release, no repeats

So the + button really says VOLUMEUP (the rocker's orientation swap must not
reach it), a hold has to be timed by the daemon, and Mute is one clean edge.
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack/autoinstall/wall"
APPLIER = WALL / "wall-audio-output"


def module(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(WALL / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


@pytest.fixture(scope="module")
def policy():
    return module("adapter_buttons_policy", "wall_audio_state.py")


@pytest.fixture(scope="module")
def keys():
    return module("adapter_buttons_keys", "panel-volume-keys.py")


def on(policy, output, mic_muted):
    """A state on `output` with the microphone as given."""
    state = policy.default_state()
    state["input_muted"] = mic_muted
    state["output"] = output
    return policy.normalize(json.loads(json.dumps(state)))


def toggle(policy, state):
    return policy.apply_event(state, {"kind": "toggle_mute"})


# ── the policy: the second press returns the position AND the microphone ─────

@pytest.mark.parametrize("output", ["speaker", "headset", "builtin"])
@pytest.mark.parametrize("mic_muted", [False, True])
def test_toggle_returns_to_the_position_with_the_mic_as_it_was_tc1706(
        policy, output, mic_muted):
    """Owner 2026-11-07, "toggle, mic follows"."""
    state = on(policy, output, mic_muted)
    muted, lines = toggle(policy, state)
    assert muted["output"] == "mute"
    assert muted["input_muted"] is True, "item J still latches the mic on the way in"
    assert muted["mute_return"] == {"output": output, "input_muted": mic_muted}
    assert lines[0].startswith("mute button: output %s -> mute" % output)
    # The record survives the state file, which is where it has to live: the
    # second press is a separate applier process.
    reloaded = policy.normalize(json.loads(json.dumps(muted)))
    assert reloaded == muted

    back, lines = toggle(policy, reloaded)
    assert back["output"] == output
    assert back["input_muted"] is mic_muted
    assert back["mute_return"] is None
    assert back["state_revision"] != reloaded["state_revision"]
    if not mic_muted:
        assert any("input mute off" in line for line in lines)
        assert not any("(item J)" in line for line in lines), \
            "the journal must not say the mic stayed muted when it did not"


def test_a_glass_mute_undone_by_the_button_keeps_the_mic_muted_tc1707(policy):
    """Review 2026-11-07: the Owner's words are "before the FIRST PRESS", and a
    Mute entered from the glass had no press. The position comes back; the
    microphone does not."""
    state = on(policy, "speaker", False)
    state, _ = policy.apply_event(state, {"kind": "set_output", "output": "mute"})
    assert state["mute_return"] == {"output": "speaker", "input_muted": True}
    back, _ = toggle(policy, state)
    assert (back["output"], back["input_muted"]) == ("speaker", True)


def test_leaving_mute_from_the_glass_keeps_item_j_and_clears_the_record_tc1707(policy):
    """Only the BUTTON's exit restores the microphone."""
    state, _ = toggle(policy, on(policy, "speaker", False))
    assert state["mute_return"] == {"output": "speaker", "input_muted": False}
    state, lines = policy.apply_event(state, {"kind": "set_output", "output": "speaker"})
    assert state["input_muted"] is True
    assert state["mute_return"] is None
    assert any("(item J)" in line for line in lines)
    # And the next press mutes again rather than reading a stale record.
    state, _ = toggle(policy, state)
    assert state["output"] == "mute"
    assert state["mute_return"] == {"output": "speaker", "input_muted": True}


def test_mute_to_mute_keeps_the_record_tc1707(policy):
    state, _ = toggle(policy, on(policy, "builtin", False))
    again, lines = policy.apply_event(state, {"kind": "set_output", "output": "mute"})
    assert again["mute_return"] == {"output": "builtin", "input_muted": False}
    assert lines == ["output already mute"]


@pytest.mark.parametrize("record", [
    None,
    "speaker",
    {"output": "mute", "input_muted": False},
    {"output": "panel", "input_muted": False},
    {"output": "speaker", "input_muted": 0},
    {"output": "speaker", "input_muted": "false"},
    {"output": "speaker", "input_muted": False, "extra": 1},
    {"output": "speaker"},
])
def test_no_trustworthy_record_returns_to_speaker_with_the_mic_muted_tc1708(
        policy, record):
    raw = policy.default_state()
    raw["output"] = "mute"
    raw["input_muted"] = True
    raw["mute_return"] = record
    state = policy.normalize(json.loads(json.dumps(raw)))
    assert state["mute_return"] is None
    back, lines = toggle(policy, state)
    assert back["output"] == "speaker"
    assert back["input_muted"] is True
    assert any("no recorded position" in line for line in lines)


def test_a_record_is_never_a_repair_and_never_outlives_mute_tc1708(policy):
    """Not a repair: a damaged record must not mute a live mic, and the switch
    backend's mirror of normalize does not know the field."""
    raw = policy.default_state()
    raw["input_muted"] = False
    raw["mute_return"] = {"output": "speaker", "input_muted": False}
    state = policy.normalize(json.loads(json.dumps(raw)))
    assert state["mute_return"] is None, "outside Mute there is nothing to return from"
    assert state["input_muted"] is False
    raw["mute_return"] = ["garbage"]
    assert policy.normalize(json.loads(json.dumps(raw)))["input_muted"] is False
    # A file written before the field: absent is simply None.
    del raw["mute_return"]
    raw["output"] = "mute"
    assert policy.normalize(raw)["mute_return"] is None


def test_a_repaired_file_drops_a_live_mic_record_tc1708(policy):
    """Review 2026-11-07: a document that needed ANY repair never opens a mic."""
    state, _ = toggle(policy, on(policy, "speaker", False))
    raw = json.loads(json.dumps(state))
    raw["volume"]["speaker"] = "x"
    repaired = policy.normalize(raw)
    assert repaired["mute_return"] is None
    back, _ = toggle(policy, repaired)
    assert (back["output"], back["input_muted"]) == ("speaker", True)


def test_toggle_takes_no_arguments_and_the_glass_cannot_send_it_tc1708(policy):
    with pytest.raises(policy.StateError):
        policy.apply_event(policy.default_state(),
                           {"kind": "toggle_mute", "input_muted": False})
    applier_text = APPLIER.read_text(encoding="utf-8")
    allowed = applier_text.split("ALLOWED_REQUEST_KINDS = frozenset(", 1)[1].split(")", 1)[0]
    assert "toggle_mute" not in allowed, \
        "the button's verb stays off the renderer's request file"


# ── the applier CLI and the root socket ─────────────────────────────────────

def run_applier(tmp_path, *arguments):
    state = tmp_path / "audio-state.json"
    command = [sys.executable, str(APPLIER), "--dry-run", "--state", str(state),
               *arguments]
    return subprocess.run(command, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin",
                               "WALL_AUDIO_MODE_OVERRIDE": "bus",
                               "SystemRoot": "C:\\Windows"})


def test_the_applier_toggles_through_its_state_file_tc1709(tmp_path):
    state_file = tmp_path / "audio-state.json"
    assert run_applier(tmp_path, "set", "builtin").returncode == 0
    assert run_applier(tmp_path, "input-mute", "off").returncode == 0
    done = run_applier(tmp_path, "mute-toggle")
    assert done.returncode == 0, done.stderr
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert (state["output"], state["input_muted"]) == ("mute", True)
    done = run_applier(tmp_path, "mute-toggle")
    assert done.returncode == 0, done.stderr
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert (state["output"], state["input_muted"]) == ("builtin", False)
    assert run_applier(tmp_path, "mute-toggle", "on").returncode == 2


def test_the_socket_has_exactly_one_new_spelling_tc1709():
    broker = module("adapter_buttons_request", "panel-volume-request.py")
    apply = Mock(return_value=0)
    assert broker.handle_request(b"mute-toggle\n", apply) == b"ok\n"
    apply.assert_called_once_with(["mute-toggle"])
    assert broker.handle_request(b"mute-toggle\n", Mock(return_value=1)) == b"error\n"
    for near_miss in (b"mute-toggle", b"mute-toggle:1\n", b"MUTE-TOGGLE\n",
                      b"mute\n", b"mute-toggle\nup\n", b" mute-toggle\n"):
        refused = Mock(return_value=0)
        assert broker.handle_request(near_miss, refused) == b"error\n", near_miss
        refused.assert_not_called()


# ── the daemon: a held key is timed, the rocker is unchanged ────────────────

def test_a_tap_on_a_held_key_is_one_step_tc1710(keys):
    plan = keys.PulsePlanner()
    opened, steps = plan.hold(True, 0.0)
    assert opened is True and steps == [keys.PULSE_PERCENT]
    # Measured taps are 288-320 ms: nothing more is due before the release.
    assert plan.due(0.32) == []
    assert plan.let_go(True, 0.32) == [], "the release adds no step of its own"
    assert plan.settle(0.32 + keys.HOLD_GAP_S - 0.001) is False
    assert plan.settle(0.32 + keys.HOLD_GAP_S + 0.001) is True
    assert plan.due(5.0) == []


def test_a_held_key_repeats_at_the_rockers_rate_after_half_a_second_tc1710(keys):
    plan = keys.PulsePlanner()
    _opened, steps = plan.hold(False, 10.0)
    now = 10.0
    while now < 11.8:          # the captured 1.8 s hold, sampled like the loop
        now += keys.LOOP_WAKE_S
        steps += plan.due(now)
        assert plan.settle(now) is False, "a key that is down is never a quiet gap"
    steps += plan.let_go(False, 11.8)
    assert all(step == -keys.PULSE_PERCENT for step in steps)
    expected = 1 + 1 + int((1.8 - keys.HELD_REPEAT_DELAY_S) / keys.HELD_REPEAT_PERIOD_S)
    assert len(steps) == expected
    assert keys.HELD_REPEAT_PERIOD_S == pytest.approx(0.106)
    assert keys.HELD_REPEAT_DELAY_S > 0.32, "an ordinary tap must not earn a repeat"


def test_a_late_wake_pays_exactly_what_is_owed_tc1710(keys):
    plan = keys.PulsePlanner()
    plan.hold(True, 0.0)
    owed = plan.due(keys.HELD_REPEAT_DELAY_S + 3 * keys.HELD_REPEAT_PERIOD_S + 0.001)
    assert owed == [keys.PULSE_PERCENT] * 4
    assert plan.due(keys.HELD_REPEAT_DELAY_S + 3 * keys.HELD_REPEAT_PERIOD_S + 0.002) == []


def test_a_release_drained_late_still_pays_what_the_hold_earned_tc1710(keys):
    """Codex Sol 2026-11-07: hold 650 ms, the loop last looked at 490 ms and
    next drains the release. The repeats due at 500 and 606 ms are owed."""
    plan = keys.PulsePlanner()
    _opened, steps = plan.hold(True, 0.0)
    assert plan.due(0.49) == []
    steps += plan.let_go(True, 0.65)
    assert steps == [keys.PULSE_PERCENT] * 3
    assert plan.due(5.0) == []


def test_a_second_button_over_the_first_pays_the_first_tc1710(keys):
    plan = keys.PulsePlanner()
    plan.hold(True, 0.0)
    opened, steps = plan.hold(False, 0.65)
    assert opened is False
    assert steps == [keys.PULSE_PERCENT, keys.PULSE_PERCENT, -keys.PULSE_PERCENT]
    # +'s release does not stop -, which is still down.
    assert plan.let_go(True, 0.7) == []
    assert plan.due(0.65 + keys.HELD_REPEAT_DELAY_S) == [-keys.PULSE_PERCENT]
    assert plan.settle(2.0) is False
    plan.let_go(False, 2.0)
    assert plan.settle(2.0 + keys.HOLD_GAP_S + 0.001) is True


def test_a_vanished_device_stops_the_hold_tc1710(keys):
    plan = keys.PulsePlanner()
    plan.hold(True, 0.0)
    plan.cancel()
    assert plan.due(10.0) == [] and plan.held() is False and plan.wait(10.0) is None


def test_the_rocker_still_earns_on_the_break_tc1710(keys):
    plan = keys.PulsePlanner()
    assert plan.press(0.0) is True
    assert plan.release(True, 0.008) == keys.PULSE_PERCENT
    assert plan.due(1.0) == []
    assert plan.settle(0.008 + keys.HOLD_GAP_S + 0.001) is True


@pytest.mark.parametrize("phys,kind", [
    ("isa0060/serio0/input0", "rocker"),
    ("usb-0000:00:14.0-1/input3", "held"),
    ("", "held"),
    (None, "held"),
])
def test_only_the_keyboard_controller_port_is_the_rocker_tc1711(keys, tmp_path, phys, kind):
    device = tmp_path / "event5" / "device"
    device.mkdir(parents=True)
    if phys is not None:
        (device / "phys").write_text(phys + "\n", encoding="ascii")
    assert keys.device_kind("/dev/input/event5", str(tmp_path)) == kind


def test_the_orientation_swap_is_reached_only_by_the_rocker_tc1711():
    text = (WALL / "panel-volume-keys.py").read_text(encoding="utf-8")
    loop = text.split("def main():", 1)[1]
    held = loop.index("and kind == KIND_HELD:")
    swap = loop.index("if SWAP_FOR_PANEL_ORIENTATION:")
    assert held < swap, "the held branch must be taken before the swap is reached"
    assert "planner.hold(code == KEY_VOLUMEUP, when)" in loop


def test_the_mute_button_is_sent_off_the_loop_and_bounded_tc1712(keys):
    toggler = keys.MuteToggler(socket_path="/nonexistent")
    started, release = threading.Event(), threading.Event()
    sent = []

    def round_trip(payload):
        sent.append(payload)
        started.set()
        release.wait(5)
        return b"ok\n"

    toggler.backend._round_trip = round_trip
    assert toggler.request() is True
    assert started.wait(5)
    # One in flight, two waiting, the fourth dropped and said so.
    assert toggler.request() is True
    assert toggler.request() is True
    assert toggler.request() is False
    release.set()
    for _ in range(100):
        if len(sent) == 3:
            break
        threading.Event().wait(0.02)
    assert sent == [b"mute-toggle"] * 3


def test_a_replugged_adapter_restarts_the_daemon_tc1713():
    rule = (WALL / "92-wall-volume-keys.rules").read_text(encoding="utf-8")
    body = "\n".join(line for line in rule.splitlines() if not line.lstrip().startswith("#"))
    for part in ('ACTION=="add"', 'SUBSYSTEM=="input"', 'KERNEL=="event*"',
                 'ATTRS{idVendor}=="0d8c"', 'ATTRS{idProduct}=="0102"',
                 'RUN+="/usr/bin/systemctl --no-block try-restart wall-volume-keys.service"'):
        assert part in body, part
    assert b"\r" not in (WALL / "92-wall-volume-keys.rules").read_bytes()
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    assert ('install -m 0644 "$PAYLOAD/92-wall-volume-keys.rules" '
            '/etc/udev/rules.d/92-wall-volume-keys.rules') in firstboot
    # The adapter's own rule keeps its never-add invariant.
    adapter = (WALL / "90-wall-audio-adapter.rules").read_text(encoding="utf-8")
    assert "wall-volume-keys" not in adapter
