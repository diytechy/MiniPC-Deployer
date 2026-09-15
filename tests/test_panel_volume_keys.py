"""SR-028 rocker privilege boundary and readout event regression tests."""
import importlib.machinery
import importlib.util
from pathlib import Path
from unittest.mock import Mock
import pytest

WALL = Path(__file__).resolve().parents[1] / "stack/autoinstall/wall"

def module(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(WALL / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result

@pytest.mark.parametrize("command", [b"up\n", b"down\n"])
def test_broker_only_maps_exact_directions_sr028(command):
    broker = module("volume_request", "panel-volume-request.py")
    apply = Mock(return_value=0)
    assert broker.handle_request(command, apply) == b"ok\n"
    apply.assert_called_once_with(["volume", command[:-1].decode()])

@pytest.mark.parametrize("command", [b"up", b"up\nset speaker\n", b"status\n", b"", b"UP\n", b"down\nX"])
def test_broker_refuses_other_requests_sr028(command):
    broker = module("volume_request", "panel-volume-request.py")
    apply = Mock()
    assert broker.handle_request(command, apply) == b"error\n"
    apply.assert_not_called()

def test_broker_reports_failed_apply_sr028():
    broker = module("volume_request", "panel-volume-request.py")
    assert broker.handle_request(b"up\n", Mock(return_value=1)) == b"error\n"


@pytest.mark.parametrize("sequence", [-1, True, 9007199254740992])
def test_invalid_rocker_readout_sequence_repairs_to_muted_sr028(sequence):
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state["input_muted"] = False
    state["volume_event_seq"] = sequence

    normalized = policy.normalize(state)
    assert normalized["volume_event_seq"] == 0
    assert normalized["input_muted"] is True


@pytest.mark.parametrize("level,louder", [(0, False), (100, True), (40, True)])
def test_every_rocker_event_has_readout_sequence_sr028(level, louder):
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state["volume"]["speaker"] = level
    for seq in (1, 2):
        state, _ = policy.apply_event(state, {"kind": "nudge_volume", "louder": louder})
        assert state["volume_event_seq"] == seq
    assert policy.normalize(state)["volume_event_seq"] == 2

def test_rocker_keeps_dynamic_identity_and_only_unix_socket_sr028():
    unit = (WALL / "wall-volume-keys.service").read_text()
    assert "DynamicUser=true" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit
    assert "ProtectSystem=strict" in unit
    sock = (WALL / "wall-volume-request.socket").read_text()
    assert "SocketGroup=input" in sock
    assert "SocketMode=0660" in sock


# ── the ramp: released means stopped (2026-09-15) ──────────────────────────
#
# WHAT THE HARDWARE ACTUALLY DOES, captured from the panel 2026-09-15: the
# rocker (event5, the AT keyboard controller -- NOT Intel Virtual Buttons) sends
# no autorepeat at all. It sends whole PRESS/RELEASE pairs 7-8 ms apart,
# repeating every ~100 ms for as long as it is held. So a lone RELEASE proves
# nothing, and a hold must be detected as a burst of presses.
#
# The bug these pin: the daemon applied one volume change per press, inline,
# while one apply costs ~520 ms. Presses arrived 5x faster than they could be
# applied, the surplus queued, and release was not handled -- so a 3 s hold
# drained for ~15 s and the level kept moving long after the Owner let go.

REPEAT = 0.100   # the measured gap between repeats of a held rocker
PAIR = 0.008     # the measured press -> release gap within one pair


def planner(**kw):
    keys = module("volume_keys", "panel-volume-keys.py")
    return keys.RampPlanner(**kw), keys


def hold(plan, louder, seconds, start=0.0):
    """Replay a real hold: pairs every ~100 ms. Returns (tap, steps, released)."""
    tap = 0
    steps = []
    # Counted, not accumulated: adding 0.1 repeatedly drifts and silently drops
    # the last pair, which makes the ramp look 3% short.
    pairs = int(round(seconds / REPEAT)) + 1
    for index in range(pairs):
        now = start + index * REPEAT
        tap += plan.press(louder, now)
        plan.release(now + PAIR)
        # The daemon wakes repeatedly between device events; sample like it does.
        for tick in range(1, 5):
            edge = now + PAIR + tick * 0.02
            if edge >= start + (index + 1) * REPEAT:
                break
            plan.settle(edge)
            step = plan.due(edge)
            if step:
                steps.append(step)
    # One more sample after the last release. The real daemon takes it: wait()
    # returns ~40 ms while a burst is open, so it wakes and pays out the final
    # sliver of ramp before the gap closes the burst.
    released = start + (pairs - 1) * REPEAT + PAIR
    plan.settle(released)
    step = plan.due(released)
    if step:
        steps.append(step)
    return tap, steps, released


def test_a_tap_is_one_pair_and_bumps_five_percent():
    """8.585 PRESS / 8.592 RELEASE / then silence -- the captured tap."""
    plan, keys = planner()
    assert keys.TAP_PERCENT == 5
    assert plan.press(True, 8.585) == 5
    plan.release(8.592)
    # Still "held" during the gap: a repeat could yet arrive.
    plan.settle(8.700)
    assert plan.held()
    # Once the gap passes, the hold is over and nothing more is ever owed.
    plan.settle(8.592 + keys.HOLD_GAP_S + 0.001)
    assert not plan.held()
    assert plan.due(60.0) is None
    assert plan.wait(60.0) is None


def test_a_repeat_inside_a_hold_is_not_another_tap():
    """The 10 Hz repeats must not each charge 5% -- that is 50%/s and a queue."""
    plan, _ = planner()
    assert plan.press(True, 0.0) == 5
    assert plan.press(True, REPEAT) == 0
    assert plan.press(True, REPEAT * 2) == 0


def test_a_held_rocker_is_not_chopped_into_taps():
    """The whole point of the gap: 28 pairs are ONE hold, so one 5% tap."""
    plan, _ = planner()
    tap, _steps, _ = hold(plan, True, 2.8)
    assert tap == 5


def test_ramp_does_not_start_before_the_hold_threshold():
    plan, keys = planner()
    assert keys.HOLD_THRESHOLD_S == 0.600
    tap, steps, _ = hold(plan, True, 0.55)
    assert tap == 5
    assert steps == []


def test_ramp_holds_twenty_five_percent_per_second():
    """A 2.6 s hold owes 5% for the tap plus 25%/s for the 2 s past 600 ms."""
    plan, keys = planner()
    assert keys.RAMP_PERCENT_PER_S == 25.0
    tap, steps, _ = hold(plan, True, 2.6)
    assert tap == 5
    assert all(louder is True for louder, _ in steps)
    assert sum(step for _, step in steps) == pytest.approx(50, abs=1)


def test_release_stops_the_ramp_then_and_not_a_gap_later():
    """The guarantee: no percent is owed for time after the finger came up.

    The burst is only DECLARED over HOLD_GAP_S after the last event, but the
    ramp must not keep accruing across that gap -- it accrues to the last
    contact, so the extra 250 ms of detection latency costs no volume.
    """
    plan, _ = planner()
    tap, steps, released = hold(plan, True, 2.0)
    spent = sum(step for _, step in steps)
    # Let a long time pass with no further events, sampling as the daemon does.
    for edge in (released + 0.1, released + 0.3, released + 5.0, released + 60.0):
        plan.settle(edge)
        assert plan.due(edge) is None
    assert sum(step for _, step in steps) == spent
    assert tap == 5


def test_a_slow_applier_makes_steps_bigger_not_more_numerous():
    """The queue is gone: elapsed time becomes ONE larger step, never a pile."""
    plan, _ = planner()
    plan.press(True, 0.0)
    # The finger stays down; only the SAMPLING is slow, as a ~520 ms apply forces.
    plan.release(2.000)
    first = plan.due(1.100)
    second = plan.due(1.620)
    assert first == (True, 12)
    assert second == (True, 13)
    assert first[1] + second[1] == 25


def test_a_reversal_starts_a_fresh_hold():
    """Pressing the other way ends the burst and re-arms the threshold."""
    plan, _ = planner()
    plan.press(True, 0.0)
    plan.release(2.000)
    assert plan.due(1.000) == (True, 10)
    assert plan.press(False, 1.000) == -5
    assert plan.due(1.500) is None          # inside the new 600 ms threshold
    plan.release(2.000)
    # Ramping the other way now. Sampled a little past the boundary rather than
    # exactly on it: `due` truncates to whole percent and float subtraction can
    # land a hair under. Nothing is lost -- the remainder stays in the anchor.
    assert plan.due(1.801) == (False, 5)


def test_wait_blocks_when_idle_and_paces_the_ramp_when_held():
    plan, keys = planner()
    assert plan.wait(0.0) is None           # nothing held: block, zero CPU
    plan.press(True, 0.0)
    # Before the ramp starts it need only wake for whichever comes first: the
    # threshold, or the gap that would end the burst.
    assert plan.wait(0.0) == pytest.approx(min(0.600, keys.HOLD_GAP_S))
    plan.release(1.000)
    assert plan.wait(1.000) == pytest.approx(0.04)


def test_step_is_clamped_into_the_protocol_range():
    """A very long hold cannot emit a step the broker would refuse."""
    plan, _ = planner()
    plan.press(True, 0.0)
    plan.release(3600.0)
    louder, step = plan.due(3600.0)
    assert (louder, step) == (True, 100)


def test_the_arrears_survive_the_gap_that_closes_the_hold():
    """Ramp earned but not yet paid must not be dropped when the burst closes.

    Each apply blocks ~520 ms, so when the gap expires there is normally still a
    step outstanding. Measured on the panel before this was fixed: a 1.0 s hold
    moved 10% where 15% was owed.
    """
    plan, keys = planner()
    plan.press(True, 0.0)
    plan.release(1.0)
    # One step paid early in the hold, leaving arrears behind.
    assert plan.due(0.7) == (True, 2)
    # The gap expires while those arrears are still unpaid: stay open.
    plan.settle(1.0 + keys.HOLD_GAP_S + 0.01)
    assert plan.held()
    assert plan.due(1.0 + keys.HOLD_GAP_S + 0.01) == (True, 8)
    # Paid in full (5% tap + 10% ramp for 0.4 s past the threshold), so now close.
    plan.settle(1.0 + keys.HOLD_GAP_S + 0.02)
    assert not plan.held()
    assert plan.due(60.0) is None


def test_a_vanished_device_cancels_the_hold_outright():
    plan, _ = planner()
    plan.press(True, 0.0)
    plan.cancel()
    assert not plan.held()
    assert plan.due(10.0) is None


# ── the broker's magnitude form ────────────────────────────────────────────

@pytest.mark.parametrize("command,expected", [
    (b"up:1\n", "up:1"), (b"down:25\n", "down:25"), (b"up:100\n", "up:100"),
])
def test_broker_maps_bounded_magnitudes_sr028(command, expected):
    broker = module("volume_request", "panel-volume-request.py")
    apply = Mock(return_value=0)
    assert broker.handle_request(command, apply) == b"ok\n"
    apply.assert_called_once_with(["volume", expected])


@pytest.mark.parametrize("command", [
    b"up:0\n",        # below range: a step of nothing is not a request
    b"up:101\n",      # above range
    b"up:-5\n",       # sign is not a digit
    b"up: 5\n",       # whitespace is not a digit
    b"up:5x\n",       # trailing rubbish
    b"up:\n",         # no magnitude at all
    b"up:005\n;rm\n", # over-long, and must not truncate to a valid prefix
    b"sideways:5\n",  # not a direction
    b"UP:5\n",        # case is exact
    b"up:\xef\xbc\x95\n",  # a unicode digit str.isdigit() would have accepted
])
def test_broker_refuses_malformed_magnitudes_sr028(command):
    broker = module("volume_request", "panel-volume-request.py")
    apply = Mock()
    assert broker.handle_request(command, apply) == b"error\n"
    apply.assert_not_called()


# ── the state layer's optional step ────────────────────────────────────────

def test_nudge_step_defaults_to_one_press():
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state["volume"]["speaker"] = 50
    state, _ = policy.apply_event(state, {"kind": "nudge_volume", "louder": True})
    assert state["volume"]["speaker"] == 50 + policy.VOLUME_STEP


@pytest.mark.parametrize("louder,step,expected", [
    (True, 25, 75), (False, 25, 25), (True, 100, 100), (False, 100, 0),
])
def test_nudge_applies_a_stated_step_and_clamps(louder, step, expected):
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state["volume"]["speaker"] = 50
    state, _ = policy.apply_event(
        state, {"kind": "nudge_volume", "louder": louder, "step": step})
    assert state["volume"]["speaker"] == expected


@pytest.mark.parametrize("step", [0, 101, -4, 1.5, True, "25", None])
def test_nudge_refuses_a_step_outside_the_contract(step):
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    with pytest.raises(policy.StateError):
        policy.apply_event(
            state, {"kind": "nudge_volume", "louder": True, "step": step})


def test_a_stepped_nudge_still_moves_the_readout_sequence():
    """The on-glass overlay is driven by this; a ramp must keep it alive."""
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    for seq in (1, 2):
        state, _ = policy.apply_event(
            state, {"kind": "nudge_volume", "louder": False, "step": 13})
        assert state["volume_event_seq"] == seq


# ── landing on a round number (Owner, 2026-09-15) ──────────────────────────
#
# "I'd like the volume to land at / round to 5% increments after a release."
# ONE snap per gesture, at the end -- the ramp itself is never quantised.

def test_release_closes_the_burst_exactly_once():
    """settle() reports the closing pass, so the level is snapped once only."""
    plan, keys = planner()
    plan.press(True, 0.0)
    plan.release(0.008)
    assert plan.settle(0.1) is False                     # still inside the gap
    assert plan.settle(0.008 + keys.HOLD_GAP_S + 0.01) is True
    assert plan.settle(10.0) is False                    # and never again
    assert plan.settle(60.0) is False


@pytest.mark.parametrize("level,expected", [
    (52, 50), (53, 55), (47, 45), (48, 50), (0, 0), (100, 100), (97, 95), (98, 100),
])
def test_snap_rounds_to_the_nearest_increment(level, expected):
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state["volume"]["speaker"] = level
    state, _ = policy.apply_event(state, {"kind": "snap_volume"})
    assert state["volume"]["speaker"] == expected


def test_snap_defaults_to_five_and_honours_an_explicit_increment():
    policy = module("volume_policy", "wall_audio_state.py")
    assert policy.VOLUME_SNAP == 5
    state = policy.default_state()
    state["volume"]["speaker"] = 52
    snapped, _ = policy.apply_event(dict(state, volume=dict(state["volume"])),
                                    {"kind": "snap_volume", "to": 10})
    assert snapped["volume"]["speaker"] == 50


def test_snap_moves_the_readout_sequence_so_the_overlay_shows_the_rest_level():
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state["volume"]["speaker"] = 52
    state, _ = policy.apply_event(state, {"kind": "snap_volume"})
    assert state["volume_event_seq"] == 1


@pytest.mark.parametrize("to", [0, 101, -5, 2.5, True, "5"])
def test_snap_refuses_an_increment_outside_the_contract(to):
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    with pytest.raises(policy.StateError):
        policy.apply_event(state, {"kind": "snap_volume", "to": to})


def test_broker_maps_snap_sr028():
    broker = module("volume_request", "panel-volume-request.py")
    apply = Mock(return_value=0)
    assert broker.handle_request(b"snap:5\n", apply) == b"ok\n"
    apply.assert_called_once_with(["volume", "snap:5"])


@pytest.mark.parametrize("command", [
    b"snap:0\n", b"snap:101\n", b"snap\n", b"snap:\n", b"snap:5x\n", b"SNAP:5\n",
])
def test_broker_refuses_malformed_snaps_sr028(command):
    broker = module("volume_request", "panel-volume-request.py")
    apply = Mock()
    assert broker.handle_request(command, apply) == b"error\n"
    apply.assert_not_called()


def test_the_daemon_snaps_on_release_and_only_in_one_place():
    """Source check: the snap is wired to the burst close, not to the ramp."""
    keys = (WALL / "panel-volume-keys.py").read_text()
    assert "SNAP_PERCENT = 5" in keys
    assert "if planner.settle(time.monotonic()):" in keys
    assert keys.count("snap(SNAP_PERCENT)") == 1
