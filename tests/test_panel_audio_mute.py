"""SR-023/LLR-007: set_mute is journaled but never sticky.

The class decision behind this file is PANEL_MUTE_BUTTON_PLAN.md section 5.
Every other mutation leaves the world genuinely unknown if it dies mid-flight,
which is why a pending journal entry is sticky and needs an operator. Mute is
idempotent and its truth can be read straight back off the mixer control, so a
pending mute is settled by OBSERVING the device instead. The stickiness that
Bluetooth pairing depends on had to survive that change unwidened, and the last
two tests here are what hold that line.
"""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "stack/panel-audio"
sys.path.insert(0, str(AUDIO))
from routing import Device, PolicyError, validate_action
from audio_router import AudioBroker as RealAudioBroker, BrokerError


def AudioBroker(*args, **kwargs):
    """White-box fakes stay in-process, matching test_panel_audio.py."""
    kwargs.setdefault("isolate_backend", False)
    return RealAudioBroker(*args, **kwargs)


def request(method="status", params=None, generation=0, request_id="r1"):
    return (json.dumps({"id": request_id, "method": method, "params": params or {},
                        "generation": generation}) + "\n").encode()


def response(broker, raw):
    return json.loads(broker.handle(raw))


class FakeBackend:
    def __init__(self):
        self.calls = []

    def call(self, method, params, cancel):
        self.calls.append((method, params))
        return {"accepted": True}

    def inventory(self, cancel):
        return [Device("speaker", "output", True), Device("desktop-in", "input", True)]


class MuteAwareBackend(FakeBackend):
    """Reports a readable mute switch, which is what makes reconciliation possible."""

    def __init__(self, muted=False):
        super().__init__()
        self.muted = muted

    def call(self, method, params, cancel):
        self.calls.append((method, params))
        if method == "status":
            return {"protocolVersion": 1, "available": True, "reason": "ready",
                    "devices": [], "route": None, "visualizer": {"available": False},
                    "mute": {"supported": True, "muted": self.muted}}
        return {"accepted": True}


def lost_after_intent(lost_method):
    """A backend that dies AFTER the journal records intent, for `lost_method`."""

    class LostAfterIntent(MuteAwareBackend):
        def call(self, method, params, cancel):
            if method == lost_method:
                raise BrokerError("backend_unavailable", "lost after a possible device change")
            return super().call(method, params, cancel)

    return LostAfterIntent


# ── policy ───────────────────────────────────────────────────────────────────

def test_set_mute_requires_an_explicit_boolean_sr023():
    validate_action("set_mute", {"muted": True})
    validate_action("set_mute", {"muted": False})
    # No default. A mute request that does not say which way is a caller bug,
    # and guessing it would silently toggle the room.
    for params in ({}, {"muted": "true"}, {"muted": 1}, {"muted": None}):
        with pytest.raises(PolicyError):
            validate_action("set_mute", params)
    with pytest.raises(PolicyError):
        validate_action("set_mute", {"muted": True, "alias": "speaker"})


def test_set_mute_needs_no_alias_and_advances_the_generation_sr023():
    backend = FakeBackend()
    broker = AudioBroker(backend, authorize=lambda _m, _p: True)
    reply = response(broker, request("set_mute", {"muted": True}, 0, "mute"))
    assert reply["ok"] and reply["result"] == {"accepted": True}
    assert ("set_mute", {"muted": True}) in backend.calls
    assert reply["generation"] == 1


# ── the journal class ────────────────────────────────────────────────────────

def test_an_interrupted_mute_reconciles_by_reading_the_switch_not_by_hand(tmp_path):
    """THE POINT OF THE WHOLE CLASS: one failed mute tap must not wedge every
    later routing change on the panel until someone SSHes in."""
    state = tmp_path / "audio-state.json"
    first = AudioBroker(lost_after_intent("set_mute")(), state_path=str(state),
                        authorize=lambda _m, _p: True)
    failed = response(first, request("set_mute", {"muted": True}, 0, "lost"))
    assert failed["error"]["code"] == "backend_unavailable"
    assert json.loads(state.read_text())["pending"]["method"] == "set_mute"

    # A restarted broker observes the control and carries on. An UNRELATED
    # mutation is what proves nothing stayed wedged.
    backend = MuteAwareBackend(muted=True)
    restarted = AudioBroker(backend, state_path=str(state), authorize=lambda _m, _p: True)
    reply = response(restarted, request("connect", {"alias": "speaker"}, 0, "after"))
    assert reply["ok"], reply
    assert ("status", {}) in backend.calls, "the switch was never actually read"
    assert json.loads(state.read_text())["pending"] is None


def test_reconciliation_does_not_claim_the_lost_mutation_landed(tmp_path):
    """`resolve` clears pending without touching generation. The mute was never
    confirmed, so it must not be reported as having taken effect; the truth is
    whatever the next status reads off the device."""
    state = tmp_path / "audio-state.json"
    first = AudioBroker(lost_after_intent("set_mute")(), generation=7, state_path=str(state),
                        authorize=lambda _m, _p: True)
    response(first, request("set_mute", {"muted": True}, 7, "lost"))

    restarted = AudioBroker(MuteAwareBackend(), state_path=str(state),
                            authorize=lambda _m, _p: True)
    assert restarted.generation == 7
    # The client's generation is therefore still current and its next request lands.
    assert response(restarted, request("status", {}, 7, "s"))["ok"]


def test_a_failed_observation_leaves_the_entry_pending(tmp_path):
    """Failing to look is not the same as having looked."""
    state = tmp_path / "audio-state.json"
    first = AudioBroker(lost_after_intent("set_mute")(), state_path=str(state),
                        authorize=lambda _m, _p: True)
    response(first, request("set_mute", {"muted": True}, 0, "lost"))

    class BlindBackend(FakeBackend):
        def call(self, method, params, cancel):
            raise BrokerError("backend_unavailable", "cannot read the control either")

    restarted = AudioBroker(BlindBackend(), state_path=str(state), authorize=lambda _m, _p: True)
    assert response(restarted, request("connect", {"alias": "speaker"}, 0, "after"))["ok"] is False
    assert json.loads(state.read_text())["pending"] is not None


def test_an_interrupted_pairing_is_still_sticky_and_still_needs_an_operator(tmp_path):
    """The stickiness is load-bearing for Bluetooth; it must not have widened."""
    state = tmp_path / "audio-state.json"
    first = AudioBroker(lost_after_intent("pair")(), state_path=str(state),
                        authorize=lambda _m, _p: True)
    response(first, request("pair", {"alias": "speaker"}, 0, "lost"))
    restarted = AudioBroker(MuteAwareBackend(), state_path=str(state),
                            authorize=lambda _m, _p: True)
    refused = response(restarted, request("set_mute", {"muted": True}, 0, "after"))
    assert refused["error"]["code"] == "mutation_uncertain"


def test_a_journal_without_a_recorded_method_reconciles_the_old_way(tmp_path):
    """A journal written before the method was recorded stays conservative."""
    state = tmp_path / "audio-state.json"
    state.write_text(json.dumps({"version": 1, "generation": 0,
                                 "pending": {"signature": "abc"}, "completed": None}))
    broker = AudioBroker(MuteAwareBackend(), state_path=str(state),
                         authorize=lambda _m, _p: True)
    refused = response(broker, request("set_mute", {"muted": True}, 0, "after"))
    assert refused["error"]["code"] == "mutation_uncertain"


# ── the status field ─────────────────────────────────────────────────────────

def test_status_carries_mute_when_the_panel_has_a_switch_and_omits_it_otherwise():
    with_mute = AudioBroker(MuteAwareBackend(muted=True), authorize=lambda _m, _p: True)
    result = response(with_mute, request("status", {}, 0, "s"))["result"]
    assert result["mute"] == {"supported": True, "muted": True}

    # A backend that predates mute, or a panel whose output has no switch to
    # throw, omits the field and stays valid. Absent means "cannot tell you",
    # which is NOT "not muted", and the shell must render the two differently.
    class NoMuteBackend(FakeBackend):
        def call(self, method, params, cancel):
            return {"protocolVersion": 1, "available": True, "reason": "ready",
                    "devices": [], "route": None, "visualizer": {"available": False}}

    plain = response(AudioBroker(NoMuteBackend(), authorize=lambda _m, _p: True),
                     request("status", {}, 0, "s"))
    assert plain["ok"] and "mute" not in plain["result"]


@pytest.mark.parametrize("bad", [
    {"supported": True},
    {"supported": True, "muted": "yes"},
    {"supported": True, "muted": True, "extra": 1},
    {"muted": True},
    True,
])
def test_a_malformed_mute_block_is_refused_like_any_other_backend_lie(bad):
    class BadBackend(FakeBackend):
        def call(self, method, params, cancel):
            return {"protocolVersion": 1, "available": True, "reason": "ready",
                    "devices": [], "route": None, "visualizer": {"available": False},
                    "mute": bad}

    reply = response(AudioBroker(BadBackend(), authorize=lambda _m, _p: True),
                     request("status", {}, 0, "s"))
    assert reply["error"]["code"] == "unsafe_backend_result"
