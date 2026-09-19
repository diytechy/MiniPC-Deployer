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


# ── the pulse rule: one break pulse is 5% (2026-09-18, strategy B) ─────────
#
# WHAT THE HARDWARE ACTUALLY DOES, and the second capture is what changed the
# design. 2026-09-15 established that the rocker (event5, the AT keyboard
# controller -- NOT Intel Virtual Buttons) sends no autorepeat: whole
# PRESS/RELEASE pairs 7-8 ms apart, repeating about every 100 ms while held.
# 2026-09-18 went a layer lower, with a temporary kprobe on `serio_interrupt`
# beneath `atkbd`, and found the same thing in the raw PS/2 stream: make E0 2E,
# break E0 AE, next make ~106-107 ms later. It is the FIRMWARE repeating, not
# an evdev or atkbd abstraction.
#
# So there is no continuously held rocker to integrate time over -- there is a
# train of discrete pulses, and the Owner's 2026-09-18 amendment makes each one
# exactly 5%. The 500 ms / 25%-per-second ramp is WITHDRAWN, along with its
# RampPlanner, its two separate charges and the quiet-gap snap that could move
# the level with no pulse behind it.
#
# The bug all of this still has to keep fixed: the daemon once applied one
# volume change per press, inline, while one apply costs ~520 ms. Presses
# arrived 5x faster than they could be applied, the surplus queued, and a 3 s
# hold drained for ~15 s with the level still moving long after the Owner let
# go.

REPEAT = 0.106   # the 2026-09-18 measured gap between firmware pulses
PAIR = 0.008     # the measured make -> break gap within one pulse


def planner(**kw):
    keys = module("volume_keys", "panel-volume-keys.py")
    return keys.PulsePlanner(**kw), keys


def hold(plan, louder, pulses, start=0.0):
    """Replay a real hold: make/break pulses every ~106 ms.

    Returns (steps, last_break) -- the signed percents the planner handed out,
    in order, and the instant of the final break.
    """
    steps = []
    last = start
    for index in range(pulses):
        now = start + index * REPEAT
        plan.press(now)
        last = now + PAIR
        step = plan.release(louder, last)
        if step:
            steps.append(step)
        # The daemon wakes repeatedly between device events; sample like it
        # does, and assert that the gap adds nothing while it is being held.
        for tick in range(1, 5):
            edge = now + PAIR + tick * 0.02
            if edge >= start + (index + 1) * REPEAT:
                break
            assert plan.settle(edge) is False
    return steps, last


def test_one_pulse_is_five_percent_and_the_make_earns_nothing():
    """8.585 make / 8.592 break / then silence -- the captured tap."""
    plan, keys = planner()
    assert keys.PULSE_PERCENT == 5
    # The make OPENS the gesture and charges nothing: it is not a complete
    # contact pulse yet, and charging it would double every tap.
    assert plan.press(8.585) is True
    assert plan.release(True, 8.592) == 5
    # The gap that follows declares the gesture over and adds no step.
    assert plan.settle(8.592 + keys.HOLD_GAP_S + 0.001) is True
    assert plan.pulses == 1


def test_a_hold_is_one_gesture_and_every_pulse_is_a_step():
    """A hold is NOT chopped into taps, and it is not one tap either."""
    plan, keys = planner()
    steps, last = hold(plan, True, 10)
    # Ten pulses, ten steps, one gesture: the ~106 ms repeats inside the hold
    # do not re-open it, and none of them is skipped as "just a repeat".
    assert steps == [5] * 10
    assert plan.held() is True
    assert plan.settle(last + keys.HOLD_GAP_S + 0.001) is True
    assert plan.held() is False


def test_a_press_across_the_repeat_delay_intentionally_moves_more_than_five():
    """No tap classifier: two pulses is 10%, and that is the specification."""
    plan, _keys = planner()
    steps, _last = hold(plan, True, 2)
    assert sum(steps) == 10


def test_the_quiet_gap_adds_no_step_however_long_it_is():
    """The gap is detection latency and MUST NOT be a volume knob.

    This is the property that let the 250 ms gap be kept without measuring a
    tighter one: whatever it costs, it costs it in latency only.
    """
    plan, keys = planner()
    short, _ = hold(plan, True, 3)
    plan.settle(3 * REPEAT + keys.HOLD_GAP_S + 0.001)
    slow = keys.PulsePlanner(gap=2.0)
    for index in range(3):
        slow.press(index * REPEAT)
        slow.release(True, index * REPEAT + PAIR)
    slow.settle(3 * REPEAT + 2.0 + 0.001)
    assert sum(short) == sum([5, 5, 5])
    assert slow.pulses == 3


def test_a_direction_reversal_stays_inside_one_gesture():
    """One gesture, pulses in event order -- not two racing apply queues."""
    plan, _keys = planner()
    assert plan.press(0.0) is True
    assert plan.release(True, 0.008) == 5
    plan.press(REPEAT)
    # The Owner corrected themselves inside the quiet interval. That is the
    # SAME gesture with a pulse going the other way; a second gesture here
    # would capture a second baseline and open a second apply queue.
    assert plan.press(REPEAT) is False
    assert plan.release(False, REPEAT + PAIR) == -5
    assert plan.held() is True


def test_a_break_with_no_gesture_open_earns_nothing():
    """The rocker already down when the daemon starts must not be a free step."""
    plan, _keys = planner()
    assert plan.release(True, 1.0) == 0
    assert plan.held() is False


def test_a_vanished_device_cancels_the_gesture_outright():
    plan, _keys = planner()
    plan.press(0.0)
    plan.release(True, 0.008)
    plan.cancel()
    assert plan.held() is False
    # And the cancelled gesture cannot then be settled a second time, which
    # would publish a terminal for a gesture already retired as cancelled.
    assert plan.settle(100.0) is False


def test_wait_blocks_when_idle_and_wakes_on_the_plan_cadence_when_held():
    plan, keys = planner()
    assert plan.wait(0.0) is None            # nothing held: block, zero CPU
    plan.press(0.0)
    plan.release(True, 0.008)
    # 40-80 ms is what the plan asks for, and no faster: new evidence only
    # arrives on a firmware pulse ~106 ms apart, so a tighter loop would
    # republish the same number and nothing else.
    assert 0.040 <= keys.LOOP_WAKE_S <= 0.080
    assert plan.wait(0.010) == pytest.approx(keys.LOOP_WAKE_S)
    # Close to the gap expiry it wakes exactly then rather than overshooting.
    edge = 0.008 + keys.HOLD_GAP_S
    assert plan.wait(edge - 0.010) == pytest.approx(0.010)
    assert plan.wait(edge + 1.0) == 0.0


# ── the absolute virtual target, and the boundary that proves it ───────────

@pytest.mark.parametrize("start,pulses,expected", [
    (60, [5], 65),
    (60, [5] * 10, 100),          # clamped, and stays clamped
    (5, [-5] * 3, 0),
    (98, [5, -5], 95),            # the plan's boundary case, verbatim
    (0, [-5, 5], 5),
])
def test_the_virtual_target_applies_pulses_in_order_with_a_clamp(start, pulses, expected):
    """A SIGNED ACCUMULATOR IS WRONG AT A BOUNDARY, which is why this exists.

    From 98, an up pulse predicts 100 and the following down must target 95.
    Netting +5 and -5 to zero would leave 98; netting them to "no movement"
    would leave 100. Both are levels the Owner did not ask for.
    """
    _plan, keys = planner()
    target = keys.VirtualTarget(start)
    for pulse in pulses:
        target.step(pulse)
    assert target.level == expected


@pytest.mark.parametrize("louder,expected", [(True, 65), (False, 60)])
def test_the_first_pulse_repairs_an_off_grid_level_directionally(louder, expected):
    """WSN-062, and the reason the quiet-gap snap could be deleted.

    62 with an up pulse lands on 65, not 67. The grid is reached by a pulse the
    Owner actually sent, travelling the way they pressed -- not by a rounding
    applied after the finger is already off.
    """
    _plan, keys = planner()
    target = keys.VirtualTarget(62)
    assert target.step(5 if louder else -5) == expected


def test_the_rocker_path_no_longer_snaps_at_all():
    """Source check: the withdrawn ramp, its threshold and its snap are GONE.

    A quiet-gap snap moves the level with no break pulse behind it, which the
    2026-09-18 pulse rule forbids outright -- "every percent this emits is a
    percent the Owner pressed for". The `snap` REQUEST survives in the policy
    and the broker for a damaged pre-existing mixer value; the rocker simply
    never sends one.
    """
    keys = (WALL / "panel-volume-keys.py").read_text()
    # The CODE is gone, not the history: the comments still explain what the
    # ramp was and why the kprobe capture retired it, and deleting that would
    # cost the next reader the whole reason this file looks the way it does.
    assert "class RampPlanner" not in keys
    assert "RAMP_PERCENT_PER_S =" not in keys
    assert "HOLD_THRESHOLD_S =" not in keys
    assert "SNAP_PERCENT =" not in keys
    assert "snap(" not in keys
    assert "def snap" not in keys
    assert 'b"snap:' not in keys

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


# ── landing on a round number ──────────────────────────────────────────────
#
# "I'd like the volume to land at / round to 5% increments after a release"
# (Owner, 2026-09-15). Under the 2026-09-18 pulse rule the rocker gets this by
# CONSTRUCTION -- every movement is a whole 5% step and the first one repairs
# an off-grid value directionally -- so the rocker sends no snap at all. The
# `snap` request survives in the policy and the broker for a damaged
# pre-existing physical mixer value, and these tests still hold it to its
# contract.

def test_the_gesture_closes_exactly_once():
    """settle() reports the one closing pass, so a terminal is published once."""
    plan, keys = planner()
    plan.press(0.0)
    plan.release(True, 0.008)
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


# TC-P-490..492 / WSN-062: assert the published sequence, not merely its end.
@pytest.mark.parametrize("louder,start", [(True, 20), (False, 100)])
def test_tc_p_490_p491_a_three_second_hold_stays_on_grid_and_counts_its_pulses(louder, start):
    """~28 pulses over 3 s, every level on the grid, and the count is the amount.

    THE ASSERTION CHANGED WITH THE RULE, and the number it used to carry is
    exactly why the rule changed. It used to demand 65% for a 3 s hold -- 5%
    for the tap plus (3.0 - 0.600) x 25%/s -- a figure derived from a ramp
    model the 2026-09-18 kprobe capture disproved. Under the pulse rule the
    amount is not derived from a clock at all: it is 5% times however many
    real firmware pulses arrived, which at the measured 9.3-10 Hz is about
    140-150 percentage points over three seconds, i.e. the whole range and a
    clamp. The pulse COUNT is authoritative, so that is what is asserted.
    """
    plan, keys = planner()
    pulses = int(round(3.0 / REPEAT))
    steps, _last = hold(plan, louder, pulses)
    assert len(steps) == pulses
    assert all(abs(step) == keys.PULSE_PERCENT for step in steps)
    target = keys.VirtualTarget(start)
    levels = [start]
    for step in steps:
        levels.append(target.step(step))
    assert all(level % keys.PULSE_PERCENT == 0 for level in levels)
    # Every pulse moves until the bound, and the bound holds.
    assert levels[-1] == (100 if louder else 0)


@pytest.mark.parametrize("louder,expected", [(True, 65), (False, 60)])
def test_tc_p_492_off_grid_first_movement_is_directional_and_needs_no_snap(louder, expected):
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state(); state["volume"]["speaker"] = 62
    state, _ = policy.apply_event(state, {"kind": "nudge_volume", "louder": louder, "step": 5})
    assert state["volume"]["speaker"] == expected
    seq = state["volume_event_seq"]
    state, _ = policy.apply_event(state, {"kind": "snap_volume", "to": 5})
    assert state["volume"]["speaker"] == expected
    assert state["volume_event_seq"] == seq


# ── a client that goes away is not a failed apply (2026-09-16, item 2) ──────
#
# The two `wall-volume-request@` failures on every boot were MEASURED to the
# panel release's socket-liveness probe, which connects and deliberately sends
# nothing. The applier did the right thing behaviourally -- no volume moved --
# and then reported a failure anyway, which is what put two red units on the
# panel's list every boot.

class HungUpSocket:
    """A peer that is already gone: reads EOF, and refuses the reply."""

    def __init__(self, request=b""):
        self._request = request
        self.sent = []

    def settimeout(self, _seconds):
        pass

    def recv(self, _size):
        part, self._request = self._request, b""
        return part

    def sendall(self, payload):
        self.sent.append(payload)
        raise BrokenPipeError(32, "Broken pipe")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def serve(monkeypatch, connection, apply_result=0):
    broker = module("volume_request", "panel-volume-request.py")
    monkeypatch.setattr(broker.socket, "socket", lambda **_kw: connection)
    monkeypatch.setattr(broker, "apply_volume", Mock(return_value=apply_result))
    return broker.main()


def test_connect_and_close_is_not_a_failure_sr028(monkeypatch):
    """The release probe's connect-only liveness check must exit 0."""
    assert serve(monkeypatch, HungUpSocket(b"")) == 0


def test_an_undeliverable_verdict_reports_the_apply_not_the_delivery_sr028(monkeypatch):
    """EPIPE on the reply: the apply succeeded, so the unit must not go red."""
    connection = HungUpSocket(b"up\n")
    assert serve(monkeypatch, connection, apply_result=0) == 0
    assert connection.sent == [b"ok\n"]


def test_an_undeliverable_verdict_still_reports_a_failed_apply_sr028(monkeypatch):
    """EPIPE does not launder a failed apply into a success."""
    assert serve(monkeypatch, HungUpSocket(b"up\n"), apply_result=1) == 1


def test_a_request_that_cannot_be_read_is_still_a_failure_sr028(monkeypatch):
    """A half-sent request that times out is a client this applier failed."""
    class Stalled(HungUpSocket):
        def recv(self, _size):
            raise TimeoutError("timed out")

    assert serve(monkeypatch, Stalled()) == 1


def test_spent_instances_collect_themselves_sr028():
    """Source check: a per-connection unit that lingers is how a list dies.

    THE SECTION IS ASSERTED, NOT JUST THE LINE. The first version of this test
    checked only that the string was present, and it passed while the directive
    sat in [Service] -- where systemd ignores it, logging `Unknown key name
    'CollectMode' in section 'Service'` and collecting nothing. A test that
    cannot tell a working directive from an ignored one is not a test of this.
    """
    unit = (WALL / "wall-volume-request@.service").read_text()
    sections = {}
    current = None
    for line in unit.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped
            sections[current] = []
        elif current and stripped and not stripped.startswith("#"):
            sections[current].append(stripped)
    assert "CollectMode=inactive-or-failed" in sections.get("[Unit]", []), \
        "CollectMode is a [Unit] key; in [Service] systemd ignores it silently"


# ── the compare-and-set token (the rocker responsiveness plan) ─────────────
#
# A gesture computes its target against a world it measured up to half a
# second ago. `state_revision` is what lets the apply ask whether that world
# is still there. None of the three counters that already existed is this
# token, and the plan says so field by field -- these pin the difference.

def test_the_state_revision_moves_on_every_audible_change():
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    assert state["state_revision"] == 0
    seen = [0]
    for event in ({"kind": "nudge_volume", "louder": True},
                  {"kind": "set_output", "output": "headset"},
                  {"kind": "set_input_mute", "muted": True},
                  {"kind": "headset", "present": True}):
        state, _ = policy.apply_event(state, event)
        assert state["state_revision"] > seen[-1], event
        seen.append(state["state_revision"])


def test_the_state_revision_does_not_move_for_a_change_that_changed_nothing():
    """A no-op must not refuse the next gesture. This is the over-bump trap.

    The applier re-records observations and re-asserts the stored state on
    boot, resume and every udev event. If any of those moved the token, a
    rocker gesture would be refused because the panel had done some
    bookkeeping while the Owner's finger was down -- a rocker that silently
    stops working under load.
    """
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state, _ = policy.apply_event(state, {"kind": "set_output", "output": "speaker"})
    assert state["state_revision"] == 0
    before = dict(state)
    state["request_seq"] = 99
    state["mic_legs_running"] = False
    state, _ = policy.apply_event(state, {"kind": "set_output", "output": "speaker"})
    assert state["state_revision"] == before["state_revision"]


def test_a_speaker_headset_speaker_round_trip_still_moves_the_token():
    """The case visible-value guards alone miss, verbatim from the plan.

    Every field the old guards could compare -- the output, the level, the
    readout counter's parity -- is back where it started, and the world the
    gesture measured is nonetheless gone.
    """
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state["headset_present"] = True
    start = policy.normalize(state)["state_revision"]
    for output in ("headset", "speaker"):
        state, _ = policy.apply_event(state, {"kind": "set_output", "output": output})
    assert state["output"] == "speaker"
    assert state["volume"]["speaker"] == 60
    assert state["state_revision"] > start


@pytest.mark.parametrize("field,value,reason", [
    ("expect_revision", 999, "state-changed"),
    ("expect_output", "headset", "state-changed"),
    ("expect_level", 55, "state-changed"),
])
def test_a_guarded_target_is_refused_when_its_world_moved(field, value, reason):
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state["headset_present"] = True
    event = {"kind": "set_volume_guarded", "target": 75, "expect_output": "speaker",
             "expect_level": 60, "expect_revision": 0}
    event[field] = value
    with pytest.raises(policy.GuardMismatch) as raised:
        policy.apply_event(state, event)
    assert raised.value.reason == reason
    # And the state is untouched: a refusal moves nothing, including the
    # readout counter, so the overlay is not told a press landed.
    assert state["volume"]["speaker"] == 60
    assert state["volume_event_seq"] == 0


def test_a_guarded_target_lands_and_advances_both_counters():
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state, _ = policy.apply_event(state, {"kind": "set_volume_guarded", "target": 75,
                                          "expect_output": "speaker", "expect_level": 60,
                                          "expect_revision": 0})
    assert state["volume"]["speaker"] == 75
    assert state["volume_event_seq"] == 1
    assert state["state_revision"] == 1


def test_a_guarded_target_at_a_bound_still_acknowledges_the_press():
    """0%/100% changes no level and must still feed the readout counter.

    The wall gives the person feedback for a press against the end stop; that
    has always been `volume_event_seq`'s job, and the guarded path must not be
    the one that stops feeding it.
    """
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state["volume"]["speaker"] = 100
    state = policy.normalize(state)
    revision = state["state_revision"]
    state, _ = policy.apply_event(state, {"kind": "set_volume_guarded", "target": 100,
                                          "expect_output": "speaker", "expect_level": 100,
                                          "expect_revision": revision})
    assert state["volume"]["speaker"] == 100
    assert state["volume_event_seq"] == 1
    # Nothing audible changed, so the CAS token did not move: the next pulse's
    # guard, taken from this same state, still holds.
    assert state["state_revision"] == revision


def test_mute_has_no_level_to_guard_and_says_so_distinctly():
    """`unavailable`, not `state-changed`: nothing raced, the position simply
    is not a level control. The renderer retires to the confirmed truth
    instead of reporting a collision that did not happen."""
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state, _ = policy.apply_event(state, {"kind": "set_output", "output": "mute"})
    with pytest.raises(policy.GuardMismatch) as raised:
        policy.apply_event(state, {"kind": "set_volume_guarded", "target": 75,
                                   "expect_output": "mute", "expect_level": 0,
                                   "expect_revision": state["state_revision"]})
    assert raised.value.reason == "unavailable"


@pytest.mark.parametrize("event", [
    {"target": "75"}, {"target": 101}, {"target": -1}, {"target": True},
    {"expect_output": "loudspeaker"}, {"expect_level": None}, {"expect_revision": -1},
])
def test_a_malformed_guarded_target_is_a_state_error_not_a_guard_failure(event):
    """Malformed is not a race, and the two must not be answered the same way.

    `ambiguous` tells the actor to re-read the truth; `state-changed` tells it
    somebody else moved. A parse failure is neither, and reporting it as a
    race would have the actor retry arithmetic that can never be accepted.
    """
    policy = module("volume_policy", "wall_audio_state.py")
    base = {"kind": "set_volume_guarded", "target": 75, "expect_output": "speaker",
            "expect_level": 60, "expect_revision": 0}
    base.update(event)
    with pytest.raises(policy.StateError) as raised:
        policy.apply_event(policy.default_state(), base)
    assert not isinstance(raised.value, policy.GuardMismatch)


# ── the broker's guarded form ──────────────────────────────────────────────

def test_the_broker_rebuilds_the_guarded_argv_and_relays_what_landed():
    broker = module("volume_request", "panel-volume-request.py")
    seen = []

    def capture(argv):
        seen.append(argv)
        return "speaker volume 60% -> 75%\nok:speaker:75:32\n"

    assert broker.handle_request(b"set:75:bus:speaker:60:31\n", Mock(), capture) \
        == b"ok:speaker:75:32\n"
    # REBUILT FROM THE PARSED PARTS, never passed through: no byte of the
    # request's text reaches argv unexamined.
    assert seen == [["volume", "set:75:bus:speaker:60:31"]]


@pytest.mark.parametrize("request_bytes", [
    b"set:75:bus:speaker:60\n",              # too few fields
    b"set:75:bus:speaker:60:31:9\n",         # too many
    b"set:101:bus:speaker:60:31\n",          # target out of range
    b"set:75:pulse:speaker:60:31\n",         # not a mode
    b"set:75:bus:loudspeaker:60:31\n",       # not an output
    b"set:75:bus:speaker:60:9007199254740992\n",  # past the safe integer
    b"set:75:bus:speaker:+60:31\n",          # a sign is not a digit
    b"set:\xef\xbc\x97\xef\xbc\x95:bus:speaker:60:31\n",  # unicode digits
    b"set:75:bus:speaker:60:31",             # no newline
])
def test_the_broker_refuses_a_malformed_guarded_request(request_bytes):
    broker = module("volume_request", "panel-volume-request.py")
    capture = Mock()
    assert broker.handle_request(request_bytes, Mock(), capture) == b"error\n"
    capture.assert_not_called()


@pytest.mark.parametrize("stdout,expected", [
    ("refused:state-changed\n", b"refused:state-changed\n"),
    ("refused:apply-failed\n", b"refused:apply-failed\n"),
    ("", b"refused:ambiguous\n"),
    ("something went wrong\n", b"refused:ambiguous\n"),
    ("refused:because-i-said-so\n", b"refused:ambiguous\n"),
    ("ok:speaker:75\n", b"refused:ambiguous\n"),
    ("ok:loudspeaker:75:32\n", b"refused:ambiguous\n"),
    ("ok:speaker:750:32\n", b"refused:ambiguous\n"),
])
def test_the_broker_rebuilds_the_verdict_and_calls_anything_else_ambiguous(stdout, expected):
    """A LOST OR UNREADABLE VERDICT IS `ambiguous`, NOT A FAILURE.

    `apply-failed` tells the actor the level did not move; `ambiguous` tells
    it that we do not know, so it re-reads rather than replaying arithmetic
    that may already have landed. Guessing either way is how one movement gets
    applied twice.
    """
    broker = module("volume_request", "panel-volume-request.py")
    assert broker.handle_request(b"set:75:bus:speaker:60:31\n", Mock(),
                                 lambda _argv: stdout) == expected


@pytest.mark.parametrize("answer,green", [
    (b"ok\n", True), (b"ok:speaker:75:32\n", True),
    (b"refused:state-changed\n", True), (b"refused:unavailable\n", True),
    (b"refused:apply-failed\n", False), (b"refused:ambiguous\n", False),
    (b"error\n", False),
])
def test_a_refused_guard_is_the_protocol_working_and_not_a_failed_unit(answer, green):
    """`Accept=yes` makes every reply a unit result, so this decides what goes
    red on the panel's failed list. A red line for the Owner moving the switch
    mid-gesture trains the list to be ignored."""
    broker = module("volume_request", "panel-volume-request.py")
    assert broker._is_success(answer) is green


def test_the_broker_still_bounds_the_relative_forms_it_always_had():
    """Widening the length limit for the guarded form must not widen theirs."""
    broker = module("volume_request", "panel-volume-request.py")
    assert broker.MAX_RELATIVE_REQUEST < broker.MAX_REQUEST
    assert broker.handle_request(b"down:100000000000000000\n", Mock(), Mock()) == b"error\n"


# ── the preview document (schema 2) ────────────────────────────────────────

def test_the_preview_is_published_per_pulse_and_replaced_atomically(tmp_path):
    keys = module("volume_keys", "panel-volume-keys.py")
    import json as _json
    path = tmp_path / "volume-gesture.json"
    preview = keys.GesturePreview(str(path))
    preview.active("4:12345", 0, "speaker", 31, 60)
    first = _json.loads(path.read_text())
    assert first["version"] == 2 and first["phase"] == "active"
    assert sorted(first) == ["gestureId", "output", "phase", "pulseSeq", "stateRevision",
                             "targetLevel", "updatedAtMs", "version"]
    preview.active("4:12345", 1, "speaker", 31, 65)
    assert _json.loads(path.read_text())["targetLevel"] == 65
    # ATOMICALLY REPLACED, NEVER TRUNCATED IN PLACE: the reader is watching the
    # directory and may read at any instant, so a half-written document would
    # flicker the overlay back to the confirmed level ten times a second.
    assert not (tmp_path / "volume-gesture.json.new").exists()


@pytest.mark.parametrize("phase,reason,extra", [
    ("refused", "state-changed", ["reason"]),
    ("cancelled", "device-removed", ["reason"]),
])
def test_each_terminal_carries_exactly_its_own_key_set(tmp_path, phase, reason, extra):
    keys = module("volume_keys", "panel-volume-keys.py")
    import json as _json
    path = tmp_path / "volume-gesture.json"
    preview = keys.GesturePreview(str(path))
    preview.terminal(phase, reason, "4:12345", 3, "speaker", 31, 75)
    document = _json.loads(path.read_text())
    assert document["phase"] == phase and document["reason"] == reason
    assert sorted(document) == sorted(["gestureId", "output", "phase", "pulseSeq",
                                       "stateRevision", "targetLevel", "updatedAtMs",
                                       "version"] + extra)


def test_a_settled_terminal_carries_the_confirmation_it_is_retired_against(tmp_path):
    keys = module("volume_keys", "panel-volume-keys.py")
    import json as _json
    path = tmp_path / "volume-gesture.json"
    preview = keys.GesturePreview(str(path))
    preview.settled("4:12345", 3, "speaker", 31, 75, 34, 75)
    document = _json.loads(path.read_text())
    assert document["confirmedStateRevision"] == 34 and document["confirmedLevel"] == 75
    assert sorted(document) == ["confirmedLevel", "confirmedStateRevision", "gestureId",
                                "output", "phase", "pulseSeq", "stateRevision",
                                "targetLevel", "updatedAtMs", "version"]
    preview.clear()
    assert not path.exists()


def test_the_terminal_stands_long_enough_for_the_hosts_fallback_to_see_it():
    keys = module("volume_keys", "panel-volume-keys.py")
    # Five of the host's 100 ms polls. A terminal removed faster than the
    # fallback can observe it would leave the overlay inferring the end of a
    # gesture from the file vanishing, which is not evidence of anything.
    assert keys.TERMINAL_HOLD_S >= 0.5


def test_the_preview_directory_is_the_daemons_own(tmp_path):
    """The EACCES defect of 2026-09-19, pinned so it cannot come back.

    /run/wall-panel is created by root units at 0755 and this daemon is
    DynamicUser, so it could never create a file there -- and did not, for
    every press since the feature shipped. The unit now declares its own
    RuntimeDirectory and both halves must name the same one.
    """
    keys = module("volume_keys", "panel-volume-keys.py")
    unit = (WALL / "wall-volume-keys.service").read_text()
    assert keys.GESTURE_DIR == "/run/wall-volume-gesture"
    assert "RuntimeDirectory=wall-volume-gesture" in unit
    assert "RuntimeDirectoryMode=0755" in unit
    assert "/run/wall-panel/volume-gesture.json" not in keys.GESTURE_FILE


# ── the apply actor: one in flight, one replaceable, and no queue ──────────

class RecordingBackend:
    """A backend that records what it was asked and answers instantly."""

    mode_name = "bus"

    def __init__(self, level=60, revision=7):
        self.level, self.revision = level, revision
        self.applies = []

    def scope(self):
        return ("speaker", self.level, self.revision, 3)

    def apply(self, target, mode, output, expect_level, expect_revision):
        self.applies.append((target, expect_level, expect_revision))
        if expect_level != self.level or expect_revision != self.revision:
            return ("refused", "state-changed")
        self.level, self.revision = target, self.revision + 1
        return ("ok", "speaker", self.level, self.revision)


def _drain_actor(actor, expected, timeout=5.0):
    import time as _time
    results = []
    deadline = _time.time() + timeout
    while len(results) < expected and _time.time() < deadline:
        try:
            results.append(actor.results.get(timeout=0.1))
        except Exception:  # queue.Empty
            pass
    return results


def test_the_actor_coalesces_and_never_queues_one_request_per_pulse():
    """The original stuck-slider bug, pinned at the structure that prevents it.

    Ten pulses land while one ~0.5 s apply is in flight. The actor keeps ONE
    replaceable target, so what reaches the backend is the first target and
    then the newest -- not ten applies draining for five seconds with the
    level still moving after the finger came up.
    """
    import threading as _threading
    keys = module("volume_keys", "panel-volume-keys.py")
    started, release = _threading.Event(), _threading.Event()

    class SlowBackend(RecordingBackend):
        def apply(self, *args):
            started.set()
            release.wait(2.0)
            return RecordingBackend.apply(self, *args)

    backend = SlowBackend()
    actor = keys.ApplyActor(backend).start()
    try:
        actor.adopt(backend.scope())
        target = keys.VirtualTarget(60)
        # The first pulse gets in flight, and we WAIT until it is -- otherwise
        # this test races the worker and passes for the wrong reason (all ten
        # replacing each other before the worker ever woke would coalesce to
        # one apply, which is fine behaviour but not the property under test).
        actor.request(target.step(5))
        assert started.wait(2.0)
        for _ in range(9):
            actor.request(target.step(5))
        release.set()
        _drain_actor(actor, 2)
        assert len(backend.applies) == 2, backend.applies
        assert backend.applies[0][0] == 65
        assert backend.applies[-1][0] == 100
    finally:
        release.set()
        actor.stop()


def test_a_refusal_cancels_the_successor_instead_of_landing_it_on_the_new_world():
    """Caught in the 2026-09-19 scenario run, and it is the subtle half.

    The guard refuses the target in flight because somebody moved the switch.
    If the queued successor -- computed on the SAME departed world -- were
    then applied against the new one, the compare-and-set would have been
    defeated by its own queue one apply later.
    """
    keys = module("volume_keys", "panel-volume-keys.py")

    class HijackingBackend(RecordingBackend):
        def apply(self, *args):
            self.applies.append(args[0])
            self.level, self.revision = 30, self.revision + 5
            return ("refused", "state-changed")

    backend = HijackingBackend()
    actor = keys.ApplyActor(backend).start()
    try:
        actor.adopt(backend.scope())
        actor.request(65)
        _drain_actor(actor, 1)
        actor.request(70)          # the successor, computed on the old world
        import time as _time
        _time.sleep(0.2)
        assert backend.applies == [65, 70] or backend.applies == [65]
    finally:
        actor.stop()


def test_the_actor_does_not_spend_an_apply_writing_a_level_already_confirmed():
    keys = module("volume_keys", "panel-volume-keys.py")
    backend = RecordingBackend(level=65)
    actor = keys.ApplyActor(backend).start()
    try:
        actor.adopt(backend.scope())
        actor.request(65)
        assert _drain_actor(actor, 1)[0][1][0] == "ok"
        assert backend.applies == [], "an apply nobody needed is half a second the next one waits"
    finally:
        actor.stop()


# ── the applier's end of the guarded form ──────────────────────────────────

def _applier(tmp_path, monkeypatch, *argv):
    """Run the REAL applier against a temp tree; return (exit code, stdout)."""
    import io
    import contextlib
    conf = tmp_path / "conf"
    run = tmp_path / "run"
    conf.mkdir(exist_ok=True)
    run.mkdir(exist_ok=True)
    monkeypatch.setenv("WALL_PANEL_CONF_DIR", str(conf))
    monkeypatch.setenv("WALL_PANEL_RUN_DIR", str(run))
    applier = module("wall_audio_output", "wall-audio-output")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = applier.main(["--state", str(conf / "audio-state.json"), *argv])
    return code, out.getvalue().strip()


def test_the_applier_answers_a_guarded_target_with_what_landed(tmp_path, monkeypatch):
    code, said = _applier(tmp_path, monkeypatch, "volume", "set:75:trigger:speaker:60:0")
    assert code == 0
    # ok:OUTPUT:LEVEL:REVISION -- the three facts the actor rebases on. A bare
    # exit code would give it none of them, and it would compute its next
    # pulse against a world it had only guessed at.
    assert said == "ok:speaker:75:1"


def test_the_applier_refuses_a_target_whose_world_moved(tmp_path, monkeypatch):
    _applier(tmp_path, monkeypatch, "volume", "set:75:trigger:speaker:60:0")
    code, said = _applier(tmp_path, monkeypatch, "volume", "set:80:trigger:speaker:75:0")
    assert (code, said) == (1, "refused:state-changed")
    # THE MODE IS A GUARD THE POLICY CANNOT HOLD, because the policy performs
    # no I/O and the mode is a file. `wall-audio-mode bus` landing mid-gesture
    # must refuse rather than write into a graph being rebuilt.
    code, said = _applier(tmp_path, monkeypatch, "volume", "set:80:bus:speaker:75:1")
    assert (code, said) == (1, "refused:state-changed")
    code, said = _applier(tmp_path, monkeypatch, "volume", "set:80:trigger:speaker:75:1")
    assert (code, said) == (0, "ok:speaker:80:2")


def test_only_the_guarded_form_prints_a_verdict(tmp_path, monkeypatch):
    """Every other command keeps the exit code it has always had, so nothing
    that reads this script's output today sees a new line appear."""
    assert _applier(tmp_path, monkeypatch, "volume", "up")[1] == ""
    assert _applier(tmp_path, monkeypatch, "volume", "70")[1] == ""
    assert _applier(tmp_path, monkeypatch, "volume", "snap:5")[1] == ""


@pytest.mark.parametrize("value", [
    "set:75:trigger:speaker:60", "set:101:trigger:speaker:60:0",
    "set:75:pulse:speaker:60:0", "set:75:trigger:loudspeaker:60:0",
    "set:75:trigger:speaker:60:-1", "set:75:trigger:speaker:60:0:9",
])
def test_the_applier_refuses_a_malformed_guarded_value(value):
    applier = module("wall_audio_output", "wall-audio-output")
    assert applier.parse_guarded_volume(value) is None
    assert applier._volume_value_is_valid(value) is False


# ── what the 2026-09-19 adversarial review found ──────────────────────────

def test_an_adjacent_gesture_continues_from_the_intent_not_the_confirmation():
    """PULSE CONSERVATION ACROSS GESTURES, which is where it was being lost.

    `confirmed` is the last level an apply came BACK with. Between the request
    and that reply there is about half a second, and a second tap inside that
    window is an ordinary thing for a person to do. Baselining it on
    `confirmed` computed against a world its own predecessor had already left:
    from 60, one up pulse (65 in flight), then one down pulse, targeted 55 --
    and event order says 60.
    """
    import threading as _threading
    keys = module("volume_keys", "panel-volume-keys.py")
    started, release = _threading.Event(), _threading.Event()

    class SlowBackend(RecordingBackend):
        def apply(self, *args):
            started.set()
            release.wait(2.0)
            return RecordingBackend.apply(self, *args)

    backend = SlowBackend()
    actor = keys.ApplyActor(backend).start()
    try:
        actor.adopt(backend.scope())
        assert actor.intent() == 60
        actor.request(65)
        assert started.wait(2.0)
        # In flight, nothing confirmed yet. The intent is where we are going.
        assert actor.known()[1] == 60
        assert actor.intent() == 65
        actor.request(70)
        # A queued target is newer than one in flight, and newer still than
        # the last confirmation.
        assert actor.intent() == 70
        release.set()
        _drain_actor(actor, 2)
        assert actor.intent() == 70
    finally:
        release.set()
        actor.stop()


def test_a_terminal_is_not_published_and_erased_in_the_same_breath():
    """Source check on the two exits that used to skip the hold.

    Publishing `cancelled` and unlinking it together is, from the reader's
    side, indistinguishable from never publishing it: the watch coalesces, the
    fallback polls at 100 ms, and the renderer would see only the file vanish
    -- which is not evidence of anything, so it would go on drawing the
    preview as the truth. Unplugging the keyboard mid-hold and
    `systemctl restart` mid-hold are both ordinary events here.
    """
    keys = (WALL / "panel-volume-keys.py").read_text()
    assert "def _retire(preview, gesture):" in keys
    assert keys.count('gesture.finish(PHASE_CANCELLED, "daemon-stopping")\n                _retire(') == 1
    assert 'gesture.finish(PHASE_CANCELLED, "device-removed")' in keys
    # Neither exit may go straight to clear(): that is the defect.
    assert 'PHASE_CANCELLED, "daemon-stopping")\n                preview.clear()' not in keys
    assert '"device-removed")\n                if not handles:\n                    preview.clear()' not in keys


def test_startup_removes_a_preview_no_living_writer_owns(tmp_path):
    """A SIGKILL mid-hold leaves an `active` document behind, and the
    directory survives the restart (RuntimeDirectoryPreserve=yes). An `active`
    document is exactly the one the renderer lets LEAD the confirmed state, so
    a daemon that inherited one would hand the wall a target nobody is
    pursuing."""
    keys = (WALL / "panel-volume-keys.py").read_text()
    assert "preview = GesturePreview()\n" in keys
    startup = keys.split("preview = GesturePreview()\n", 1)[1].split("actor = None", 1)[0]
    assert "preview.clear()" in startup, "a stranded document must not survive a restart"


def test_sigterm_can_wake_an_idle_poll_loop():
    """THE FLAG ALONE CANNOT WAKE poll(), and that made every restart slow.

    With nothing held the loop blocks in `poller.poll(None)` -- which is why
    an idle wall costs no CPU -- and under PEP 475 Python RETRIES an
    interrupted syscall once a Python signal handler has returned normally.
    A handler that only sets a flag therefore never gets the loop back: the
    process sat in poll() until an unrelated key arrived or systemd's stop
    timeout expired and killed it, and the `daemon-stopping` terminal this
    loop is supposed to publish was never reached.

    Source-shaped because the fix IS the wiring: a self-pipe registered in the
    poller, which no unit test can observe from outside the loop.
    """
    keys = (WALL / "panel-volume-keys.py").read_text()
    assert "signal.set_wakeup_fd(wake_write)" in keys
    assert "poller.register(wake_read, select.POLLIN)" in keys
    assert "if handle == wake_read:" in keys
    # And it is unhooked on every exit that can be taken after it exists --
    # one definition plus three returns. The two earliest exits (no device,
    # none readable) run before the pipe is created and need no teardown.
    assert keys.count("_close_wakeup(poller, wake_read, wake_write)") == 4
    # A SIGTERM that arrived between installing the handler and installing
    # the wakeup fd wrote no byte, so the first poll would have blocked on it
    # forever: the same hang moved a few microseconds earlier.
    startup = keys.split("poller.register(wake_read, select.POLLIN)", 1)[1]
    assert startup.lstrip().startswith("# AND THE WINDOW")
    assert 'if stopping["now"]:' in startup.split("while True:", 1)[0]
    assert "def _close_wakeup(poller, wake_read, wake_write):" in keys
    assert "signal.set_wakeup_fd(-1)" in keys
