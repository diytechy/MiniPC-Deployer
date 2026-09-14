"""SR-028/LLR-015: the audio intent contract's epoch and echoed applied state.

Contract of record: OfficeWallNaglight
`docs/design/audio-intent-contract-2026-09-14.md`.

What this file holds down, on the BROKER side (the applier's half is the
hermetic shell suite, `stack/autoinstall/wall/tests/audio-switch.test.sh`,
block B18):

  * the request envelope carries the epoch read off the state file the applier
    owns, and OMITS it rather than guessing when the file cannot be read;
  * the `switch` status block publishes the epoch and the high-water mark, as
    nulls when unreadable and never as zeros;
  * the mutation reply snapshots the state as it stood BEFORE the request,
    named for when it was taken rather than for what it predicts;
  * all three new fields ride behind the same `echoSeq` opt-in, because the
    shipped renderer validates the action result as an EXACT key set and a
    wider result reaches an un-upgraded panel as an error;
  * the broker's own result validators refuse a malformed epoch or echo rather
    than passing it to a client that would reconcile against it.
"""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stack/panel-audio"))
sys.path.insert(0, str(ROOT / "stack/autoinstall/wall"))
import switch_request
import wall_audio_state
from switch_backend import SwitchApplierBackend
from audio_router import AudioBroker as RealAudioBroker, BrokerError
from routing import switch_only_authorization


def AudioBroker(*args, **kwargs):
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
    applier = tmp_path / "wall-audio-output"
    applier.write_text("#!/bin/sh\n", encoding="utf-8")
    state = tmp_path / "audio-state.json"
    state.write_text(json.dumps({
        "version": 1, "output": "speaker", "input_muted": False,
        "volume": {"headset": 60, "speaker": 72}, "headset_present": False,
        "headset_autoswitch_armed": True, "request_seq": 41, "generation": 7,
    }), encoding="utf-8")
    return {"applier": applier, "state": state,
            "request": tmp_path / "run" / "request.json"}


def backend(panel):
    return SwitchApplierBackend(request_path=panel["request"],
                                state_path=panel["state"],
                                applier_path=panel["applier"])


def written(panel):
    return json.loads(panel["request"].read_text(encoding="utf-8"))


def set_state(panel, **fields):
    state = json.loads(panel["state"].read_text(encoding="utf-8"))
    state.update(fields)
    panel["state"].write_text(json.dumps(state), encoding="utf-8")


# --- the envelope -----------------------------------------------------------

def test_the_request_envelope_carries_the_appliers_epoch_sr028(panel):
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_output", {"output": "headset"}, echo_seq=True))
    assert answer["ok"] is True
    assert written(panel)["generation"] == 7
    assert answer["result"]["generation"] == 7


def test_an_envelope_epoch_is_optional_and_refuses_a_bad_one_llr015():
    plain = switch_request.envelope(5, {"kind": "set_output", "output": "mute"})
    assert "generation" not in plain, "omitted, never defaulted: see the module note"
    scoped = switch_request.envelope(5, {"kind": "set_output", "output": "mute"}, 9)
    assert scoped["generation"] == 9
    for bad in (-1, 1.5, "9", True):
        with pytest.raises(switch_request.RequestError):
            switch_request.envelope(5, {"kind": "set_output", "output": "mute"}, bad)


def test_an_older_state_file_with_no_epoch_sends_an_unscoped_request_sr028(panel):
    """A panel whose applier predates the contract must keep working.

    The broker reads no epoch, so it names none, and the applier accepts the
    request as unscoped. Guessing zero here would be a claim the applier would
    refuse, taking the switch down on a panel whose only fault is being old.
    """
    state = json.loads(panel["state"].read_text(encoding="utf-8"))
    del state["generation"]
    panel["state"].write_text(json.dumps(state), encoding="utf-8")
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_output", {"output": "headset"}, echo_seq=True))
    assert answer["ok"] is True
    # Normalization reads an ABSENT epoch as 0 -- it is an older-but-honest
    # document, not damage -- so the request is scoped to 0, which the applier
    # (also at 0, for the same reason) accepts.
    assert written(panel)["generation"] == 0
    assert answer["result"]["generation"] == 0


def test_the_backend_normalizes_the_epoch_exactly_as_the_applier_does_llr013(panel):
    """The two normalizers are separate copies; they must not drift."""
    cases = [{}, {"generation": 0}, {"generation": 12}, {"generation": -1},
             {"generation": 1.5}, {"generation": "4"}, {"generation": True},
             {"generation": None}]
    for extra in cases:
        raw = {"version": 1, "output": "speaker", "input_muted": False,
               "volume": {"headset": 60, "speaker": 72}, "headset_present": False,
               "headset_autoswitch_armed": True, "request_seq": 41, **extra}
        panel["state"].write_text(json.dumps(raw), encoding="utf-8")
        mine = backend(panel)._normalized()
        theirs = wall_audio_state.normalize(raw)
        assert mine["generation"] == theirs["generation"], extra
        # And the epoch's damage must decide the microphone the same way too.
        assert mine["input_muted"] == theirs["input_muted"], extra


# --- the status block -------------------------------------------------------

def test_the_switch_block_publishes_the_epoch_and_the_mark_sr028(panel):
    broker = AudioBroker(backend(panel))
    switch = reply(broker, wire("status"))["result"]["switch"]
    assert switch["generation"] == 7
    assert switch["requestSeq"] == 41


def test_an_unreadable_state_reports_nulls_and_never_zeros_sr028(panel):
    panel["state"].write_text("{", encoding="utf-8")
    broker = AudioBroker(backend(panel))
    switch = reply(broker, wire("status"))["result"]["switch"]
    assert switch["supported"] is False
    # Zero is a number a client would COMPARE against. Null is the only honest
    # answer for an epoch this broker could not read.
    assert switch["generation"] is None
    assert switch["requestSeq"] is None


def test_a_fresh_panel_reports_the_schema_default_mark_sr028(panel):
    set_state(panel, request_seq=-1, generation=0)
    broker = AudioBroker(backend(panel))
    switch = reply(broker, wire("status"))["result"]["switch"]
    # -1 is the applier's own "no broker request has ever been applied", and it
    # must survive the broker's validator, which otherwise takes non-negative.
    assert switch["requestSeq"] == -1 and switch["generation"] == 0


# --- the echoed applied state ------------------------------------------------

def test_the_reply_snapshots_the_state_as_it_stood_BEFORE_the_request_sr028(panel):
    set_state(panel, output="headset", input_muted=True, headset_present=True)
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_output", {"output": "speaker"}, echo_seq=True))
    # NAMED FOR WHEN IT WAS TAKEN. Two independent reviews read the previous
    # name (`effective`) as a claim about the outcome, and a field two readers
    # get wrong is named wrong however carefully the contract explains it. The
    # applier is asynchronous, so no reply can say whether this request has been
    # applied; the STATUS verb is where the applied state lives.
    assert answer["result"]["observedBefore"] == {
        "output": "headset", "inputMuted": True, "volume": 60,
        "requestSeq": 41, "generation": 7}


def test_the_snapshot_of_a_mute_position_reports_no_level_sr028(panel):
    set_state(panel, output="mute")
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_output", {"output": "speaker"}, echo_seq=True))
    assert answer["result"]["observedBefore"]["volume"] == 0


# --- compatibility -----------------------------------------------------------

def test_a_client_that_did_not_opt_in_gets_the_historic_shape_if015(panel):
    """The whole correlation group is stripped, not just `seq`.

    The shipped renderer validates the action result as an EXACT key set, so
    leaving `generation` or `effective` behind would reach an un-upgraded panel
    as an error -- in whichever order the two repos are deployed.
    """
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_output", {"output": "headset"}))
    assert answer["result"] == {"accepted": True}


def test_an_opted_in_client_gets_exactly_the_contract_keys_if015(panel):
    broker = AudioBroker(backend(panel))
    answer = reply(broker, wire("set_output", {"output": "headset"}, echo_seq=True))
    assert set(answer["result"]) == {"accepted", "seq", "generation", "observedBefore"}


# --- the broker's own validators --------------------------------------------

class Lying:
    """A backend that answers a shape no client should be handed."""

    def __init__(self, result):
        self.result = result

    def inventory(self, cancel):
        return []

    def call(self, method, params, cancel):
        return self.result


BAD_ACTIONS = [
    {"accepted": True, "seq": -1},
    {"accepted": True, "generation": -1},
    {"accepted": True, "generation": 1.5},
    {"accepted": True, "observedBefore": {"output": "bluetooth", "inputMuted": False,
                                     "volume": 60, "requestSeq": 1, "generation": 1}},
    {"accepted": True, "observedBefore": {"output": "speaker", "inputMuted": "yes",
                                     "volume": 60, "requestSeq": 1, "generation": 1}},
    {"accepted": True, "observedBefore": {"output": "speaker", "inputMuted": False,
                                     "volume": 101, "requestSeq": 1, "generation": 1}},
    {"accepted": True, "observedBefore": {"output": "speaker", "inputMuted": False,
                                     "volume": 60, "requestSeq": -2, "generation": 1}},
    {"accepted": True, "observedBefore": {"output": "speaker", "inputMuted": False,
                                     "volume": 60, "requestSeq": 1, "generation": -1}},
    {"accepted": True, "observedBefore": {"output": "speaker"}},
    {"accepted": True, "observedBefore": []},
]


@pytest.mark.parametrize("result", BAD_ACTIONS)
def test_a_malformed_epoch_or_echo_never_reaches_a_client_sr028(result):
    broker = AudioBroker(Lying(result))
    answer = reply(broker, wire("set_output", {"output": "mute"}, echo_seq=True))
    assert answer["ok"] is False
    assert answer["error"]["code"] == "unsafe_backend_result"


BAD_SWITCH = [
    {"generation": -1}, {"generation": 1.5}, {"generation": "7"},
    {"requestSeq": -2}, {"requestSeq": 1.5}, {"unexpected": 1},
]


@pytest.mark.parametrize("extra", BAD_SWITCH)
def test_a_malformed_switch_block_never_reaches_a_client_sr028(extra):
    status = {"protocolVersion": 1, "available": False, "reason": "routing-disabled",
              "devices": [], "route": None, "visualizer": {"available": False},
              "switch": {"supported": True, "output": "speaker", "inputMuted": False,
                         "available": True, "reason": None, "volume": 60, **extra}}
    broker = AudioBroker(Lying(status))
    answer = reply(broker, wire("status"))
    assert answer["ok"] is False
    assert answer["error"]["code"] == "unsafe_backend_result"


def test_a_switch_block_that_predates_the_epoch_is_still_valid_sr028():
    """Both new keys are OPTIONAL, for the reason the whole block is."""
    status = {"protocolVersion": 1, "available": False, "reason": "routing-disabled",
              "devices": [], "route": None, "visualizer": {"available": False},
              "switch": {"supported": True, "output": "speaker", "inputMuted": False,
                         "available": True, "reason": None, "volume": 60}}
    broker = AudioBroker(Lying(status))
    answer = reply(broker, wire("status"))
    assert answer["ok"] is True
    assert "generation" not in answer["result"]["switch"]


# --- item J: the coupled mute, as the broker reports it ----------------------

def test_output_mute_reports_an_effective_mute_and_a_held_unmute_item_j(panel):
    """The chrome reads the FACT, not the rule.

    `inputMuted` is the effective mute -- what the microphone actually is --
    and `inputMuteHeld` is what lets the UI explain a refused unmute instead of
    merely showing one.
    """
    set_state(panel, output="mute", input_muted=False, mic_legs_running=False)
    broker = AudioBroker(backend(panel))
    switch = reply(broker, wire("status"))["result"]["switch"]
    assert switch["inputMuted"] is True
    assert switch["inputMuteHeld"] is True
    assert switch["inputMutedConfirmed"] is True


def test_a_mute_is_NEVER_confirmed_while_a_mic_leg_still_transmits_item_j(panel):
    """The plan's hard rule, and the only one here with a privacy failure behind it.

    `mic_legs_running` is the applier's OBSERVATION after its pass: true when a
    leg that was meant to stop would not. The broker must then report the mute
    as unconfirmed, however emphatically the stored intention says muted.
    """
    set_state(panel, output="mute", input_muted=True, mic_legs_running=True)
    broker = AudioBroker(backend(panel))
    switch = reply(broker, wire("status"))["result"]["switch"]
    assert switch["inputMuted"] is True, "the intention"
    assert switch["inputMutedConfirmed"] is False, "and the honest evidence"


def test_an_absent_observation_is_unconfirmed_not_silent_item_j(panel):
    """A file from an applier that predates item J has never looked.

    Defaulting to "confirmed" there would draw a settled, silent microphone on
    the strength of a field nobody has ever written.
    """
    state = json.loads(panel["state"].read_text(encoding="utf-8"))
    state.update({"output": "mute", "input_muted": True})
    state.pop("mic_legs_running", None)
    panel["state"].write_text(json.dumps(state), encoding="utf-8")
    broker = AudioBroker(backend(panel))
    switch = reply(broker, wire("status"))["result"]["switch"]
    assert switch["inputMutedConfirmed"] is False


def test_the_backend_couples_the_mute_exactly_as_the_applier_does_item_j(panel):
    """Two separate copies of normalize; the coupling must not drift between them."""
    cases = [
        {"output": "mute", "input_muted": False},
        {"output": "mute", "input_muted": True},
        {"output": "speaker", "input_muted": False},
        {"output": "headset", "input_muted": False},
    ]
    for extra in cases:
        raw = {"version": 1, "volume": {"headset": 60, "speaker": 72},
               "headset_present": False, "headset_autoswitch_armed": True,
               "request_seq": 41, "generation": 7, "mic_legs_running": False, **extra}
        panel["state"].write_text(json.dumps(raw), encoding="utf-8")
        mine = backend(panel)._normalized()
        theirs = wall_audio_state.normalize(raw)
        assert mine["input_muted"] == theirs["input_muted"], extra
