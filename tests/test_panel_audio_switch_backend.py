"""SR-023/SR-028/LLR-015: the broker backend that moves the panel's switch.

Item 23 step 5. The chrome on the glass submits `set_output`, `set_input_mute`
and `set_volume` through IF-015; until this backend existed the shipped one was
`UnavailableBackend` and every tap was honestly refused. What is asserted here
is the whole chain the tap has to survive: policy, authorization, the minted
sequence, the file the root applier picks up, and the reply the shell correlates
against.

Three things this file exists to hold down, each of which was a real hazard:
  * the reply shape stays backward compatible -- `seq` appears only for a client
    that asked, because the shipped renderer validates the result as an exact
    key set and would turn a wider one into an error;
  * the sequence is strictly increasing whatever the clock does, because the
    applier silently discards anything not newer than its high-water mark;
  * WSN-024 stays shut -- no routing verb becomes reachable because the switch
    became reachable.
"""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stack/panel-audio"))
sys.path.insert(0, str(ROOT / "stack/autoinstall/wall"))
import switch_request
import switch_backend
from switch_backend import SwitchApplierBackend
from audio_router import (AudioBroker as RealAudioBroker, BrokerError,
                          JS_SAFE_INTEGER)
from routing import PolicyError, switch_only_authorization, validate_action


def AudioBroker(*args, **kwargs):
    """White-box fakes stay in-process, matching test_panel_audio.py."""
    kwargs.setdefault("isolate_backend", False)
    kwargs.setdefault("authorize", switch_only_authorization)
    return RealAudioBroker(*args, **kwargs)


def wire(method, params=None, generation=0, request_id="r1", echo_seq=None):
    body = {"id": request_id, "method": method, "params": params or {},
            "generation": generation}
    if echo_seq is not None:
        body["echoSeq"] = echo_seq
    return (json.dumps(body) + "\n").encode()


def reply(broker, raw):
    return json.loads(broker.handle(raw))


@pytest.fixture
def panel(tmp_path):
    """A fake installed panel: an applier, a state file, a runtime directory."""
    applier = tmp_path / "wall-audio-output"
    applier.write_text("#!/bin/sh\n", encoding="utf-8")
    state = tmp_path / "audio-state.json"
    state.write_text(json.dumps({
        "version": 1, "output": "speaker", "input_muted": False,
        "volume": {"headset": 60, "speaker": 72}, "headset_present": False,
        "headset_autoswitch_armed": True, "request_seq": -1,
    }), encoding="utf-8")
    request = tmp_path / "run" / "request.json"
    return {"applier": applier, "state": state, "request": request, "root": tmp_path}


# The scripted clocks below are in NANOSECONDS, because that is what
# switch_request.next_seq divides down; the sequence it mints is microseconds.
US = 1_000


def backend(panel, clock=None):
    """The backend under test, optionally driven by a scripted clock (ns)."""
    if clock is None:
        return SwitchApplierBackend(request_path=panel["request"],
                                    state_path=panel["state"],
                                    applier_path=panel["applier"])
    ticks = iter(clock)
    return SwitchApplierBackend(request_path=panel["request"],
                                state_path=panel["state"],
                                applier_path=panel["applier"],
                                clock=lambda: next(ticks))


def written(panel):
    return json.loads(panel["request"].read_text(encoding="utf-8"))


def set_state(panel, **fields):
    state = json.loads(panel["state"].read_text(encoding="utf-8"))
    state.update(fields)
    panel["state"].write_text(json.dumps(state), encoding="utf-8")


# --- the three verbs reach the applier ---------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize("output", ["mute", "headset", "speaker"])
def test_set_output_writes_one_request_the_applier_accepts_sr028(panel, output):
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_output", {"output": output}, echo_seq=True))
    assert answer["ok"] is True and answer["result"]["accepted"] is True
    # `generation` joins the envelope by the intent contract (2026-09-14, 1.2):
    # the epoch the request is scoped to, read off the state file the applier
    # owns. The seeded state has none, so it normalizes to 0.
    assert written(panel) == {"version": 1, "seq": answer["result"]["seq"],
                              "generation": 0,
                              "event": {"kind": "set_output", "output": output}}


@pytest.mark.smoke
@pytest.mark.parametrize("muted", [True, False])
def test_set_input_mute_carries_the_microphone_button_sr028(panel, muted):
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_input_mute", {"muted": muted}, echo_seq=True))
    assert answer["result"]["accepted"] is True
    assert written(panel)["event"] == {"kind": "set_input_mute", "muted": muted}


@pytest.mark.smoke
@pytest.mark.parametrize("level", [0, 1, 50, 99, 100])
def test_set_volume_carries_a_percentage_for_the_selected_output_sr028(panel, level):
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_volume", {"level": level}, echo_seq=True))
    assert answer["result"]["accepted"] is True
    assert written(panel)["event"] == {"kind": "set_volume", "level": level}


# --- bounds: the level is closed at the broker, not clamped ------------------

@pytest.mark.parametrize("level", [-1, 101, 1000, 1.5, True, False, "50", None])
def test_set_volume_refuses_a_level_outside_the_percentage_sr023(panel, level):
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_volume", {"level": level}))
    assert answer["ok"] is False and answer["error"]["code"] == "policy_refused"
    assert not panel["request"].exists(), "a refused level must reach no file"


def test_set_volume_requires_a_level_sr023(panel):
    broker = AudioBroker(backend(panel))
    assert reply(broker, wire("set_volume", {}))["error"]["code"] == "policy_refused"


def test_set_volume_refuses_an_unknown_parameter_sr023(panel):
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_volume", {"level": 50, "alias": "speaker"}))
    assert answer["error"]["code"] == "policy_refused"


def test_validate_action_is_the_only_gate_and_it_is_exhaustive_sr023():
    """No shape of set_volume reaches a backend without passing policy."""
    validate_action("set_volume", {"level": 0})
    validate_action("set_volume", {"level": 100, "output": "headset"})
    for params in ({"level": -1}, {"level": 101}, {"level": True}, {},
                   {"level": 10, "output": "mute"}, {"level": 10, "output": "x"},
                   {"level": 10, "output": None}, {"level": 10, "extra": 1}):
        with pytest.raises(PolicyError):
            validate_action("set_volume", params)


# --- the wrong-output race ---------------------------------------------------

def test_volume_with_a_matching_output_guard_is_applied_sr028(panel):
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_volume", {"level": 40, "output": "speaker"}))
    assert answer["result"]["accepted"] is True
    assert written(panel)["event"] == {"kind": "set_volume", "level": 40}


def test_volume_for_an_output_the_switch_has_left_is_refused_sr028(panel):
    """The level would have landed in the other output's memory."""
    set_state(panel, output="headset")
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_volume", {"level": 40, "output": "speaker"}))
    assert answer["ok"] is False and answer["error"]["code"] == "switch_moved"
    assert not panel["request"].exists()


def test_volume_guarded_on_mute_is_refused_by_policy_sr023(panel):
    """`mute` has no level, so guarding on it could only ever refuse."""
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_volume", {"level": 40, "output": "mute"}))
    assert answer["error"]["code"] == "policy_refused"


# --- the applier is absent ---------------------------------------------------

def test_a_panel_without_the_applier_refuses_rather_than_dropping_a_file(panel):
    """A request nobody will read is worse than an honest refusal."""
    panel["applier"].unlink()
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_output", {"output": "mute"}))
    assert answer["ok"] is False and answer["error"]["code"] == "backend_unavailable"
    assert not panel["request"].exists()


def test_a_panel_without_the_state_file_refuses_sr028(panel):
    panel["state"].unlink()
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_input_mute", {"muted": True}))
    assert answer["error"]["code"] == "backend_unavailable"
    assert not panel["request"].exists()


def test_status_still_answers_on_a_panel_without_the_switch_sr023(panel):
    """Status must never refuse: the shell renders 'unknown', not an error."""
    panel["state"].unlink()
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("status"))
    assert answer["ok"] is True
    assert answer["result"]["switch"]["supported"] is False
    assert answer["result"]["mute"]["supported"] is False


# --- the minted sequence -----------------------------------------------------

@pytest.mark.smoke
def test_the_reply_carries_the_seq_that_was_written_sr028(panel):
    broker = AudioBroker(backend(panel, clock=[1700 * US, 1800 * US]))
    answer = reply(broker, wire("set_output", {"output": "headset"}, echo_seq=True))
    assert answer["result"]["seq"] == written(panel)["seq"] == 1700


@pytest.mark.smoke
def test_a_client_that_did_not_ask_gets_the_historic_reply_shape_if015(panel):
    """Backward compatibility: the shipped shell validates an exact key set."""
    broker = AudioBroker(backend(panel, clock=[1700 * US]))
    answer = reply(broker, wire("set_output", {"output": "headset"}))
    assert answer["result"] == {"accepted": True}
    assert written(panel)["seq"] == 1700, "the seq is still minted and written"


def test_echo_seq_must_be_a_boolean_sr023(panel):
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_output", {"output": "mute"}, echo_seq="yes"))
    assert answer["ok"] is False and answer["error"]["code"] == "bad_request"


def test_an_unknown_top_level_request_field_is_still_refused_sr023(panel):
    broker = AudioBroker(backend(panel))
    raw = (json.dumps({"id": "r1", "method": "status", "params": {},
                       "generation": 0, "surprise": 1}) + "\n").encode()
    assert json.loads(broker.handle(raw))["error"]["code"] == "bad_request"


def test_two_requests_inside_one_clock_tick_get_different_sequences_sr028(panel):
    """The applier discards anything not newer; a tie would be silent death."""
    switch = backend(panel, clock=[9_000 * US, 9_000 * US])
    broker = AudioBroker(switch)
    first = reply(broker, wire("set_output", {"output": "mute"}, request_id="a",
                               echo_seq=True))["result"]["seq"]
    second = reply(broker, wire("set_output", {"output": "speaker"}, request_id="b",
                                generation=1, echo_seq=True))["result"]["seq"]
    assert first == 9_000 and second == 9_001


def test_a_clock_stepped_backwards_still_mints_an_increasing_sequence_sr028(panel):
    switch = backend(panel, clock=[9_000 * US, 5 * US])
    broker = AudioBroker(switch)
    first = reply(broker, wire("set_output", {"output": "mute"}, request_id="a",
                               echo_seq=True))["result"]["seq"]
    second = reply(broker, wire("set_output", {"output": "speaker"}, request_id="b",
                                generation=1, echo_seq=True))["result"]["seq"]
    assert second > first


def test_the_sequence_starts_above_the_appliers_high_water_mark_sr028(panel):
    """A broker restart must not mint below what the applier already applied."""
    set_state(panel, request_seq=50_000)
    broker = AudioBroker(backend(panel, clock=[7 * US]))
    answer = reply(broker, wire("set_output", {"output": "mute"}, echo_seq=True))
    assert answer["result"]["seq"] == 50_001


def test_a_repeated_request_replays_the_same_sequence_sr023(panel, tmp_path):
    """The journal replays the completed reply; a retry must not mint twice."""
    broker = AudioBroker(backend(panel, clock=[4_000 * US, 4_001 * US]),
                         state_path=str(tmp_path / "journal.json"))
    raw = wire("set_output", {"output": "headset"}, echo_seq=True)
    first = reply(broker, raw)
    second = reply(broker, raw)
    assert second["result"]["seq"] == first["result"]["seq"] == 4_000
    assert second["generation"] == first["generation"]
    assert written(panel)["seq"] == 4_000, "the replay writes no second request"


def test_a_replay_redoes_a_request_the_runtime_directory_lost_sr023(panel, tmp_path):
    """terra 4.1: the journal outliving its effect must not be a lie.

    The completed record lives in StateDirectory and the request it acknowledged
    is one file in RuntimeDirectory, which systemd removes when the unit stops.
    A restart between the write and the applier consuming it would otherwise
    leave a journal saying the switch moved and nothing anywhere that will move
    it, and the client's retry would be told it had already succeeded.
    """
    broker = AudioBroker(backend(panel, clock=[4_000 * US, 4_007 * US]),
                         state_path=str(tmp_path / "journal.json"))
    raw = wire("set_output", {"output": "headset"}, echo_seq=True)
    first = reply(broker, raw)
    panel["request"].unlink()          # the runtime directory did not survive
    second = reply(broker, raw)
    assert second["ok"] is True
    assert written(panel)["event"] == {"kind": "set_output", "output": "headset"}
    assert second["result"]["seq"] == written(panel)["seq"] == 4_007
    # The SAME logical request: it is neither stale nor allowed to advance the
    # generation a second time.
    assert second["generation"] == first["generation"]


def test_a_replay_is_not_redone_once_the_applier_has_recorded_it_sr028(panel, tmp_path):
    """The request file is gone because it was CONSUMED, not lost."""
    broker = AudioBroker(backend(panel, clock=[4_000 * US, 4_007 * US]),
                         state_path=str(tmp_path / "journal.json"))
    raw = wire("set_output", {"output": "headset"}, echo_seq=True)
    first = reply(broker, raw)
    panel["request"].unlink()
    set_state(panel, output="headset", request_seq=4_000)
    second = reply(broker, raw)
    assert second["result"]["seq"] == first["result"]["seq"] == 4_000
    assert not panel["request"].exists(), "an applied request is not re-sent"


def test_a_replay_whose_state_cannot_be_read_refuses_rather_than_lying(panel, tmp_path):
    broker = AudioBroker(backend(panel, clock=[4_000 * US, 4_007 * US]),
                         state_path=str(tmp_path / "journal.json"))
    raw = wire("set_output", {"output": "headset"}, echo_seq=True)
    reply(broker, raw)
    panel["request"].unlink()
    panel["state"].write_text("not json at all", encoding="utf-8")
    second = reply(broker, raw)
    assert second["ok"] is False and second["error"]["code"] == "backend_unavailable"


def test_request_landed_is_not_a_method_any_client_can_ask_for_sr023(panel):
    """It is an internal observation, like the reconciliation status call."""
    with pytest.raises(PolicyError):
        validate_action("request_landed", {"seq": 1})
    answer = reply(AudioBroker(backend(panel)), wire("request_landed", {"seq": 1}))
    assert answer["ok"] is False and answer["error"]["code"] == "policy_refused"


# --- the legacy verb ---------------------------------------------------------

def test_set_mute_true_is_exactly_the_mute_position_sr023(panel):
    broker = AudioBroker(backend(panel))
    assert reply(broker, wire("set_mute", {"muted": True}))["result"]["accepted"] is True
    assert written(panel)["event"] == {"kind": "set_output", "output": "mute"}


def test_set_mute_false_from_mute_selects_the_audible_position_sr023(panel):
    set_state(panel, output="mute")
    broker = AudioBroker(backend(panel))
    assert reply(broker, wire("set_mute", {"muted": False}))["result"]["accepted"] is True
    assert written(panel)["event"] == {"kind": "set_output", "output": "speaker"}


def test_set_mute_false_when_nothing_is_muted_moves_no_switch_sr023(panel):
    """The old verb never named a position; it must not invent one."""
    set_state(panel, output="headset")
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_mute", {"muted": False}, echo_seq=True))
    # Accepted, and honestly seq-less and epoch-less: no request was minted, so
    # there is nothing for a client to correlate against. The state snapshot is
    # still carried, because the caller asked "is the output unmuted" and this is
    # the evidence that it is (contract 2026-09-14, section 1.4).
    assert answer["result"] == {"accepted": True, "seq": None, "generation": None,
                                "observedBefore": {"output": "headset", "inputMuted": False,
                                              "volume": 60, "requestSeq": -1,
                                              "generation": 0}}
    assert not panel["request"].exists()


# --- WSN-024 stays shut ------------------------------------------------------

@pytest.mark.smoke
@pytest.mark.parametrize("method,params", [
    ("connect", {"alias": "phone"}), ("pair", {"alias": "phone"}),
    ("select_output", {"alias": "phone"}), ("forget", {"alias": "phone"}),
    ("select_input", {"alias": "phone", "explicit": True}),
    ("set_visualizer", {"enabled": True}), ("discover", {}),
])
def test_no_routing_verb_became_reachable_wsn024(panel, method, params):
    """The switch shipped; the routed-device backend did not."""
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire(method, params))
    assert answer["ok"] is False
    assert answer["error"]["code"] == "authorization_required"
    assert not panel["request"].exists()


def test_the_backend_offers_no_inventory_so_no_alias_can_be_current_wsn024(panel):
    assert backend(panel).inventory(None) == []


def test_status_reports_routing_unavailable_and_the_switch_present_sr028(panel):
    set_state(panel, output="headset", headset_present=False, input_muted=True)
    broker = AudioBroker(backend(panel))
    result = reply(broker, wire("status"))["result"]
    assert result["available"] is False and result["devices"] == [] and result["route"] is None
    assert result["switch"] == {"supported": True, "output": "headset",
                                "inputMuted": True, "available": False,
                                "reason": "headset_absent", "volume": 60,
                                "generation": 0, "requestSeq": -1,
                                "inputMuteHeld": False,
                                "inputMutedConfirmed": False}


def test_status_volume_is_the_selected_outputs_memory_sr028(panel):
    broker = AudioBroker(backend(panel))
    assert reply(broker, wire("status"))["result"]["switch"]["volume"] == 72
    set_state(panel, output="mute")
    assert reply(broker, wire("status"))["result"]["switch"]["volume"] == 0


# --- reconciliation ----------------------------------------------------------

def test_a_lost_volume_request_is_settled_by_the_switch_block_llr015(panel, tmp_path):
    """set_volume is self-reconciling for the same reason set_output is."""
    journal = tmp_path / "journal.json"
    journal.write_text(json.dumps({
        "version": 1, "generation": 0,
        "pending": {"signature": "old", "method": "set_volume"}, "completed": None,
    }), encoding="utf-8")
    broker = AudioBroker(backend(panel), state_path=str(journal))
    answer = reply(broker, wire("set_volume", {"level": 30}))
    assert answer["ok"] is True and answer["result"]["accepted"] is True


def test_a_lost_volume_request_stays_uncertain_when_the_switch_cannot_be_read(panel, tmp_path):
    panel["state"].unlink()
    journal = tmp_path / "journal.json"
    journal.write_text(json.dumps({
        "version": 1, "generation": 0,
        "pending": {"signature": "old", "method": "set_volume"}, "completed": None,
    }), encoding="utf-8")
    broker = AudioBroker(backend(panel), state_path=str(journal))
    answer = reply(broker, wire("set_volume", {"level": 30}))
    assert answer["ok"] is False and answer["error"]["code"] == "mutation_uncertain"


# --- the two copies of the enum ---------------------------------------------

def test_the_backends_positions_match_the_appliers_llr013():
    """switch_backend cannot import wall_audio_state at runtime; hold them equal."""
    import wall_audio_state
    assert switch_backend.OUTPUTS == wall_audio_state.OUTPUTS
    assert switch_backend.LEVELLED_OUTPUTS == wall_audio_state.LEVELLED_OUTPUTS
    assert switch_backend.UNMUTE_OUTPUT in wall_audio_state.LEVELLED_OUTPUTS


def test_every_written_event_is_one_the_applier_accepts_llr014(panel):
    """The request kinds are a contract with a script in another tree."""
    source = (ROOT / "stack/autoinstall/wall/wall-audio-output").read_text(encoding="utf-8")
    assert "set_output" in source and "set_input_mute" in source and "set_volume" in source
    for kind in ("set_output", "set_input_mute", "set_volume"):
        assert kind in switch_request.REQUEST_KINDS


# --- the production seam: a spawned, killable child process -----------------

def isolated(panel):
    """The broker as `serve()` builds it: the backend runs in a child process.

    Worth its own tests rather than trusting the in-process seam. The backend
    has to be PICKLABLE to cross `multiprocessing` spawn at all, the request
    file is written by that child rather than by the broker, and an error code
    survives only as a token the parent re-attaches a message to.
    """
    return RealAudioBroker(SwitchApplierBackend(
        request_path=panel["request"], state_path=panel["state"],
        applier_path=panel["applier"]),
        authorize=switch_only_authorization, backend_timeout_seconds=30)


@pytest.mark.smoke
def test_the_switch_moves_through_the_isolated_backend_sr023(panel):
    answer = reply(isolated(panel), wire("set_output", {"output": "headset"}, echo_seq=True))
    assert answer["ok"] is True
    assert written(panel)["event"] == {"kind": "set_output", "output": "headset"}
    assert answer["result"]["seq"] == written(panel)["seq"]


def test_switch_moved_survives_the_process_boundary_sr028(panel):
    set_state(panel, output="headset")
    answer = reply(isolated(panel), wire("set_volume", {"level": 40, "output": "speaker"}))
    assert answer["error"]["code"] == "switch_moved"
    assert not panel["request"].exists()


def test_an_uninstalled_panel_refuses_through_the_isolated_backend_sr023(panel):
    panel["applier"].unlink()
    answer = reply(isolated(panel), wire("set_output", {"output": "mute"}))
    assert answer["error"]["code"] == "backend_unavailable"


def test_backend_from_environment_reads_the_installed_paths(panel, monkeypatch):
    monkeypatch.setenv("WALL_AUDIO_REQUEST_PATH", str(panel["request"]))
    monkeypatch.setenv("WALL_AUDIO_STATE_PATH", str(panel["state"]))
    monkeypatch.setenv("WALL_AUDIO_APPLIER_PATH", str(panel["applier"]))
    built = switch_backend.backend_from_environment()
    assert built.request_path == panel["request"] and built.state_path == panel["state"]
    monkeypatch.delenv("WALL_AUDIO_REQUEST_PATH")
    assert switch_backend.backend_from_environment().request_path == switch_request.DEFAULT_PATH


# --- terra 1.1: a state file that is present but unreadable ------------------

@pytest.mark.parametrize("damage", ["not json at all", "[]", '{"output": "speaker"'])
@pytest.mark.parametrize("method,params", [
    ("set_output", {"output": "mute"}), ("set_input_mute", {"muted": True}),
    ("set_volume", {"level": 30}), ("set_mute", {"muted": False}),
    ("set_mute", {"muted": True}),
])
def test_a_mutation_refuses_when_the_state_cannot_be_read_sr028(panel, damage, method, params):
    """Unreadable is not empty.

    Every mutation decision is taken against this file: the sequence floor, the
    volume guard and the legacy unmute. Reading it as `{}` would mint a sequence
    from the clock alone -- which a backward step can put below the applier's
    recorded mark, where the applier discards it in silence while this backend
    answers "accepted" -- and would answer a legacy unmute with a cheerful
    no-op.
    """
    panel["state"].write_text(damage, encoding="utf-8")
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire(method, params))
    assert answer["ok"] is False and answer["error"]["code"] == "backend_unavailable"
    assert not panel["request"].exists()


def test_a_backward_clock_below_the_appliers_mark_cannot_be_written_sr028(panel):
    """The exact silent-discard shape terra 1.1 named, end to end."""
    panel["state"].write_text("not json at all", encoding="utf-8")
    broker = AudioBroker(backend(panel, clock=[5 * US]))
    assert reply(broker, wire("set_output", {"output": "mute"}))["ok"] is False
    # ...and with the same clock and a READABLE mark, the floor still wins.
    set_state_fresh = {"version": 1, "output": "speaker", "input_muted": False,
                       "volume": {"headset": 60, "speaker": 72},
                       "headset_present": False, "headset_autoswitch_armed": True,
                       "request_seq": 900_000}
    panel["state"].write_text(json.dumps(set_state_fresh), encoding="utf-8")
    broker = AudioBroker(backend(panel, clock=[5 * US]))
    answer = reply(broker, wire("set_output", {"output": "mute"}, echo_seq=True))
    assert answer["result"]["seq"] == 900_001 == written(panel)["seq"]


def test_status_still_answers_on_an_unreadable_state_file_sr023(panel):
    panel["state"].write_text("not json at all", encoding="utf-8")
    result = reply(AudioBroker(backend(panel)), wire("status"))["result"]
    assert result["switch"]["supported"] is False and result["mute"]["supported"] is False


# --- terra: dispatch ordering, not just the pure validator -------------------

class SpyBackend:
    """Wraps the real backend and records every call that reached it."""

    def __init__(self, inner):
        self.inner, self.calls = inner, []

    def call(self, method, params, cancel):
        self.calls.append((method, dict(params)))
        return self.inner.call(method, params, cancel)

    def inventory(self, cancel):
        return self.inner.inventory(cancel)


@pytest.mark.parametrize("method,params", [
    ("set_volume", {"level": 101}), ("set_volume", {"level": -1}),
    ("set_volume", {"level": 50, "output": "mute"}),
    ("set_volume", {"level": 50, "alias": "speaker"}),
    ("set_output", {"output": "louder"}), ("set_input_mute", {"muted": "on"}),
    ("connect", {"alias": "phone"}), ("set_output", {"output": "mute", "level": 3}),
])
def test_a_refused_request_never_reaches_the_backend_at_all_sr023(panel, method, params):
    """Policy and authorization are BEFORE the seam, not inside it."""
    spy = SpyBackend(backend(panel))
    answer = reply(AudioBroker(spy), wire(method, params))
    assert answer["ok"] is False
    assert spy.calls == [], "a refused request must not be observed by a backend"
    assert not panel["request"].exists()


# --- terra 2.2: a code is a public diagnosis, not just a token --------------

class MisdiagnosingBackend:
    """A backend that raises `switch_moved` for a method it cannot explain."""

    def call(self, method, params, cancel):
        raise BrokerError("switch_moved", "not a thing this method can mean")

    def inventory(self, cancel):
        return []


@pytest.mark.parametrize("method,params", [
    ("set_output", {"output": "mute"}), ("set_input_mute", {"muted": True}),
    ("set_mute", {"muted": True}),
])
def test_switch_moved_cannot_explain_a_method_it_is_not_about_sr023(method, params):
    """"The selected output changed before the level could be applied" is only
    ever true of a guarded set_volume; an operator acts on the sentence."""
    answer = reply(AudioBroker(MisdiagnosingBackend()), wire(method, params))
    assert answer["error"]["code"] == "backend_failure"


def test_switch_moved_still_explains_the_method_it_is_about_sr028():
    answer = reply(AudioBroker(MisdiagnosingBackend()),
                   wire("set_volume", {"level": 20, "output": "speaker"}))
    assert answer["error"]["code"] == "switch_moved"


def test_a_status_call_cannot_be_answered_with_switch_moved_sr023():
    answer = reply(AudioBroker(MisdiagnosingBackend()), wire("status"))
    assert answer["error"]["code"] == "backend_failure"


# --- terra 2.1, rejected with its reason ------------------------------------

@pytest.mark.parametrize("partial", [
    {"output": "speaker"},                       # no request_seq at all
    {"output": "speaker", "request_seq": "abc"},  # a request_seq of the wrong type
    {"output": "speaker", "request_seq": True},   # bool is an int in Python
    {"request_seq": 40, "output": "nonsense"},    # a position the applier rejects
    {},                                           # a valid, empty object
])
def test_a_valid_but_partial_state_is_read_exactly_as_the_applier_reads_it_llr013(panel, partial):
    """WHY THIS IS NOT THE HOLE IT LOOKS LIKE (terra 2.1, rejected).

    `_state_strict` accepts any JSON object rather than validating the full
    schema, and a partial one leaves `request_seq` absent so the sequence is
    minted from the clock. That is not a bypass: the APPLIER reads the same file
    through `wall_audio_state.normalize`, which falls back field by field on
    purpose, so an absent or damaged `request_seq` is -1 on BOTH sides and a
    clock-minted sequence is above it by construction. Validating a stricter
    schema here than the applier validates would refuse switch requests on a
    panel whose switch works perfectly.

    (Terra also read the third damage string in the test above as a valid
    partial object; it is unterminated JSON and does refuse.)
    """
    import wall_audio_state
    panel["state"].write_text(json.dumps(partial), encoding="utf-8")
    switch = backend(panel, clock=[500 * US])
    assert switch._applied_seq() == wall_audio_state.normalize(partial)["request_seq"]
    broker = AudioBroker(switch)
    answer = reply(broker, wire("set_output", {"output": "mute"}, echo_seq=True))
    assert answer["result"]["seq"] == written(panel)["seq"]
    assert answer["result"]["seq"] > wall_audio_state.normalize(partial)["request_seq"]


def test_the_unreadable_cases_really_are_unreadable(panel):
    """The damage strings used above, named so nobody has to guess."""
    for damage in ("not json at all", "[]", '{"output": "speaker"'):
        panel["state"].write_text(damage, encoding="utf-8")
        with pytest.raises(BrokerError) as caught:
            backend(panel)._state_strict()
        assert caught.value.code == "backend_unavailable"


# --- terra 3.1: a sparse state file means what the applier makes it mean -----

@pytest.mark.parametrize("raw", [
    {}, {"output": "speaker"}, {"output": "headset"}, {"output": "nonsense"},
    {"volume": {"speaker": 5}}, {"volume": {"speaker": 500, "headset": -3}},
    {"volume": {"speaker": True}}, {"volume": "loud"},
    {"request_seq": -5}, {"request_seq": 12, "input_muted": "yes"},
    {"input_muted": True, "headset_present": True},
    # An invalid present readout sequence is a repair, so an otherwise explicit
    # unmute must still fail safe. Keep this table shared with the applier.
    {"input_muted": False, "volume_event_seq": -1},
    {"input_muted": False, "volume_event_seq": True},
    {"input_muted": False, "volume_event_seq": 9007199254740992},
])
def test_the_backend_normalizes_like_the_applier_llr013(panel, raw):
    """The two copies of the field-wise fallback cannot drift in silence.

    Reporting a sparse file as unsupported would have drawn an unknown switch on
    a panel whose switch works, and refused reconciliation of a lost mutation
    this same backend accepts (terra 3.1).
    """
    import wall_audio_state
    panel["state"].write_text(json.dumps(raw), encoding="utf-8")
    mine = backend(panel)._normalized()
    theirs = wall_audio_state.normalize(raw)
    # The applier also normalizes `version` and the auto-switch latch, which are
    # its own business and which this backend neither reads nor reports. Every
    # field the backend DOES carry must match.
    assert set(mine) < set(theirs)
    assert mine == {key: theirs[key] for key in mine}


def test_a_sparse_state_file_reports_a_supported_switch_sr028(panel):
    panel["state"].write_text("{}", encoding="utf-8")
    result = reply(AudioBroker(backend(panel)), wire("status"))["result"]
    # A sparse file is a SUPPORTED switch (terra 3.1) but, since step 4, a
    # microphone that reads MUTED: the applier mutes unless the document
    # explicitly says false, and the backend agrees with it field by field.
    assert result["switch"] == {"supported": True, "output": "speaker",
                                "inputMuted": True, "available": True,
                                "reason": None, "volume": 60,
                                "generation": 0, "requestSeq": -1,
                                "inputMuteHeld": False,
                                "inputMutedConfirmed": False}


def test_a_lost_mutation_reconciles_on_a_sparse_state_file_llr015(panel, tmp_path):
    panel["state"].write_text("{}", encoding="utf-8")
    journal = tmp_path / "journal.json"
    journal.write_text(json.dumps({
        "version": 1, "generation": 0,
        "pending": {"signature": "old", "method": "set_output"}, "completed": None,
    }), encoding="utf-8")
    broker = AudioBroker(backend(panel), state_path=str(journal))
    assert reply(broker, wire("set_output", {"output": "mute"}))["ok"] is True


# --- terra 5.1, rejected with its reason; terra 5.2, fixed ------------------

def test_a_superseded_request_is_settled_and_the_newer_one_still_lands_sr028(panel, tmp_path):
    """The request file is a ONE-SLOT MAILBOX holding the latest intent.

    A second tap overwrites an unconsumed first one on purpose: the newest
    position is the one being asked for, and applying the older one afterwards
    would move the switch away from it. So an older sequence counts as settled.
    The residue terra named is that replaying the older request's completed
    reply reports success for an intent that was overtaken; the alternatives are
    worse (an exact match makes the redo rewrite the older command over the
    newer one, and refusing while a request is unconsumed fails the second tap
    of a double-tap). Pinned here so a later change has to argue with it.
    """
    switch = backend(panel, clock=[100 * US, 101 * US])
    broker = AudioBroker(switch, state_path=str(tmp_path / "journal.json"))
    first = wire("set_output", {"output": "mute"}, request_id="a", echo_seq=True)
    reply(broker, first)
    second = reply(broker, wire("set_output", {"output": "headset"}, request_id="b",
                                generation=1, echo_seq=True))
    assert written(panel)["event"] == {"kind": "set_output", "output": "headset"}
    assert second["result"]["seq"] == 101
    # The older request replays as settled and does NOT overwrite the newer one.
    assert switch._landed(100) is True
    assert written(panel)["seq"] == 101


def test_a_redo_is_not_refused_by_the_generation_ceiling_sr023(panel, tmp_path):
    """terra 5.2: a redo does not advance the generation, so the ceiling that
    guards advancing it must not refuse the redo."""
    journal = tmp_path / "journal.json"
    broker = AudioBroker(backend(panel, clock=[4_000 * US, 4_009 * US]),
                         generation=JS_SAFE_INTEGER - 1, state_path=str(journal))
    raw = wire("set_output", {"output": "headset"},
               generation=JS_SAFE_INTEGER - 1, echo_seq=True)
    first = reply(broker, raw)
    assert first["ok"] is True and broker.generation == JS_SAFE_INTEGER
    panel["request"].unlink()
    second = reply(broker, raw)
    assert second["ok"] is True, "the request's own success must not strand it"
    assert written(panel)["seq"] == 4_009
    assert second["generation"] == first["generation"] == JS_SAFE_INTEGER
