"""SR-028/SR-041, LLR-015/LLR-952: the merged bus as the visualizer's source.

Contract of record: OfficeWallNaglight
`docs/design/audio-intent-contract-2026-09-14.md`, section 4.

WHAT CHANGED ON 2026-09-17 AND WHY. Item L wired `telemetry` to a document
`wall-amp-trigger` published as a by-product of the capture it already held --
`speaker_tap`, the POST-switch tap it needs for the amplifier relay. That made
the visualizer a function of the Speaker leg: Headset showed nothing at all, and
no source that did not reach the speaker could be drawn. The Owner ruled that
visualization moves to `bus_monitor`, the merged PRE-switch bus, published by a
service of its own (`wall-bus-visualizer`, SR-041).

Three consequences this file holds down, each of which is a behaviour change on
the wall rather than a refactor:

  * HEADSET VISUALIZES. It could not before.
  * MUTE DOES NOT. `bus_monitor` is upstream of the switch and carries samples
    during Mute, so bus activity alone is not "playing"; the confirmed output
    selection is ANDed in here, and Mute reports an inactive bus WITHOUT
    pretending the capture is unavailable.
  * THE MICROPHONE MAY NOT STAND IN FOR THE BUS. The old backend answered
    `available: true` with a fabricated silent bus whenever the microphone had
    telemetry and the bus had none, promoting a microphone reading into the
    bus's place. That substitution is removed. The BLOCK stays: it is the level
    ring on the microphone button in the audio chrome, a different consumer with
    a different question, and it is never a source of bands or of `active`.

What this file still holds down, unchanged in intent:
  * no producer at all is `available: false` -- honestly absent, and
    distinguishable from a silent room;
  * a producer that says it is not measuring reports SILENCE, because the
    visualizer should show a quiet room rather than disappear;
  * a producer that says `unavailable` -- the panel is not in bus mode -- is
    absent, NOT silent, so the shell falls through to Frame Media;
  * a STALE document reports silence too, never the last frame it saw;
  * `source` is an enum, so a post-switch level can never be presented as the
    merged bus;
  * a malformed document is unavailable, never half-believed.
"""

import json
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stack/panel-audio"))
import switch_backend
from switch_backend import SwitchApplierBackend
from audio_router import AudioBroker as RealAudioBroker
from routing import switch_only_authorization


def AudioBroker(*args, **kwargs):
    kwargs.setdefault("isolate_backend", False)
    kwargs.setdefault("authorize", switch_only_authorization)
    return RealAudioBroker(*args, **kwargs)


def wire(method, params=None, generation=0, request_id="r1"):
    body = {"id": request_id, "method": method, "params": params or {},
            "generation": generation}
    return (json.dumps(body) + "\n").encode()


def reply(broker, raw):
    return json.loads(broker.handle(raw))


@pytest.fixture
def panel(tmp_path, monkeypatch):
    """An installed panel whose two producers write into tmp_path."""
    applier = tmp_path / "wall-audio-output"
    applier.write_text("#!/bin/sh\n", encoding="utf-8")
    state = tmp_path / "audio-state.json"
    bus = tmp_path / "bus-telemetry.json"
    aec = tmp_path / "aec-status.json"
    monkeypatch.setattr(switch_backend, "BUS_TELEMETRY_PATH", bus)
    monkeypatch.setattr(switch_backend, "AEC_STATUS_PATH", aec)
    handle = {"applier": applier, "state": state, "bus": bus, "aec": aec,
              "request": tmp_path / "run" / "request.json"}
    write_state(handle)
    return handle


def write_state(panel, output="speaker"):
    panel["state"].write_text(json.dumps({
        "version": 1, "output": output, "input_muted": False,
        "volume": {"headset": 60, "speaker": 72}, "headset_present": False,
        "headset_autoswitch_armed": True, "request_seq": 41, "generation": 7,
    }), encoding="utf-8")


def backend(panel):
    return SwitchApplierBackend(request_path=panel["request"],
                                state_path=panel["state"],
                                applier_path=panel["applier"])


def telemetry(panel):
    return reply(AudioBroker(backend(panel)), wire("telemetry"))


def write_bus(panel, **fields):
    document = {
        "schema": 2, "source": "bus_monitor", "state": "live", "valid": True,
        "active": True, "generation": 3,
        "rms": 0.4, "peak": 0.8, "bands": [0.1] * 8,
        "observed_monotonic_ms": int(time.monotonic() * 1000),
        "reference_dbfs": -12.0,
    }
    document.update(fields)
    panel["bus"].write_text(json.dumps(document), encoding="utf-8")


def write_aec(panel, **fields):
    microphone = {
        "level": 0.42, "source": "aec_post_filter", "state": "live",
        "valid": True, "reference_dbfs": -18.0,
        "observed_monotonic_ms": int(time.monotonic() * 1000),
    }
    microphone.update(fields.pop("microphone", {}))
    document = {"schema": 1, "state": "cancelling", "microphone": microphone}
    document.update(fields)
    panel["aec"].write_text(json.dumps(document), encoding="utf-8")


# --- nothing published -------------------------------------------------------

def test_no_producer_at_all_is_unavailable_not_silence_sr041(panel):
    """Absent is not a quiet room, and the shell must render the difference."""
    answer = telemetry(panel)
    assert answer["ok"] is True
    assert answer["result"] == {"available": False}


def test_a_malformed_document_is_unavailable_never_half_believed_sr041(panel):
    for bad in ["{", "null", "[]", '{"schema": 3}', '{"schema": 2}',
                '{"schema": 1, "source": "speaker_tap", "valid": true}',
                '{"schema": 2, "source": "hw:PCH,0", "valid": true}',
                '{"schema": 2, "source": "bus_monitor", "state": "guessing"}']:
        panel["bus"].write_text(bad, encoding="utf-8")
        assert telemetry(panel)["result"] == {"available": False}, bad


def test_the_old_speaker_tap_document_is_refused_outright_sr041(panel):
    """A version-1 document answers a DIFFERENT question and must not be believed.

    This is the one regression that would be invisible on the glass: a
    post-switch level drawn under a label claiming the merged bus looks exactly
    like a working visualizer, and would quietly restore the Headset blind spot
    the Owner asked to have removed.
    """
    write_bus(panel, schema=1, source="speaker_tap")
    assert telemetry(panel)["result"] == {"available": False}


def test_an_oversized_document_is_refused_sr041(panel):
    write_bus(panel)
    panel["bus"].write_text("{\"pad\": \"" + "x" * 20000 + "\"}", encoding="utf-8")
    assert telemetry(panel)["result"] == {"available": False}


# --- the bus -----------------------------------------------------------------

def test_the_bus_levels_reach_the_client_sr041(panel):
    write_bus(panel)
    result = telemetry(panel)["result"]
    assert result["available"] is True
    assert result["active"] is True
    assert result["rms"] == 0.4 and result["peak"] == 0.8
    assert result["bands"] == [0.1] * 8
    assert result["observedMonotonicMs"] >= 0
    assert result["bus"]["source"] == "bus_monitor"


def test_a_microphone_can_never_stand_in_for_an_absent_bus_sr041(panel):
    """THE SUBSTITUTION IS THE THING THAT WAS WRONG, and this is it.

    The old backend answered `available: true` with a fabricated silent bus
    whenever the microphone had telemetry and the bus had none -- promoting a
    microphone reading into the bus's place in the reply, which is exactly the
    confusion the 2026-09-17 ruling exists to prevent. An absent bus is absent,
    whatever the microphone is doing.
    """
    write_aec(panel)                       # a canceller, and no bus at all
    assert telemetry(panel)["result"] == {"available": False}
    # And the same for every other reason the bus might be missing: an absent
    # producer, a malformed document, and a panel that is not in bus mode.
    panel["bus"].write_text("{not json", encoding="utf-8")
    assert telemetry(panel)["result"] == {"available": False}
    write_bus(panel, state="unavailable", valid=False, active=False)
    assert telemetry(panel)["result"] == {"available": False}


def test_the_microphone_block_is_still_published_for_its_own_consumer_sr041(panel):
    """It is the level ring on the microphone button, not a visualizer feed.

    The Owner's ruling is about what the VISUALIZER may draw. Reading it as
    "the block leaves the document" would delete a working indicator nobody
    asked to lose, so the block stays and only the substitution went.
    """
    write_bus(panel)
    write_aec(panel)
    microphone = telemetry(panel)["result"]["microphone"]
    assert microphone["level"] == 0.42
    assert microphone["source"] == "aec_post_filter"
    assert microphone["state"] == "live"
    assert microphone["valid"] is True
    assert microphone["referenceDbfs"] == -18.0
    assert microphone["ageMs"] >= 0


def test_the_microphone_never_reaches_the_bands_or_the_activity_claim_sr041(panel):
    """The half of the ruling that is unconditional: not a visualization source.

    A loud microphone over a silent bus must draw a quiet room, not a busy one.
    """
    write_bus(panel, state="silent", valid=False, active=False,
              rms=0.0, peak=0.0, bands=[0.0] * 8)
    write_aec(panel, microphone={"level": 1.0})
    result = telemetry(panel)["result"]
    assert result["bands"] == [0.0] * 8
    assert result["rms"] == 0.0 and result["peak"] == 0.0
    assert result["active"] is False
    assert result["microphone"]["level"] == 1.0    # reported, never mixed in
    assert result["bus"]["source"] == "bus_monitor"


@pytest.mark.parametrize("state", ["muted", "stale", "unavailable"])
def test_a_level_that_is_not_live_is_zeroed_and_named_sr028(panel, state):
    """Never a frozen ring, and never one without a reason beside it."""
    write_bus(panel)
    write_aec(panel, microphone={"state": state, "valid": False})
    microphone = telemetry(panel)["result"]["microphone"]
    assert microphone["state"] == state
    assert microphone["valid"] is False
    assert microphone["level"] == 0.0


def test_a_level_older_than_the_window_becomes_stale_sr028(panel):
    """The canceller publishes at 0.1 Hz; past two and a half periods it is stale.

    The two producers get DIFFERENT windows and always have: one window for both
    would either call the canceller stale constantly or let a frozen bus readout
    sit on the wall for ten seconds.
    """
    write_bus(panel)
    write_aec(panel, microphone={"observed_monotonic_ms": 0})
    # `time.monotonic()` on a machine that has been up for a while makes the age
    # enormous; on one that has just booted it may not. Only assert the rule
    # when the fixture can actually exercise it.
    if int(time.monotonic() * 1000) > switch_backend.AEC_STALE_MS:
        microphone = telemetry(panel)["result"]["microphone"]
        assert microphone["state"] == "stale"
        assert microphone["valid"] is False
        assert microphone["level"] == 0.0


def test_a_level_from_the_future_is_stale_rather_than_negative_sr028(panel):
    """A restarted canceller's monotonic clock no longer shares our origin.

    An age that cannot be defended is not a fresh level; it is an unknown one.
    """
    write_bus(panel)
    write_aec(panel, microphone={
        "observed_monotonic_ms": int(time.monotonic() * 1000) + 600000})
    microphone = telemetry(panel)["result"]["microphone"]
    assert microphone["ageMs"] == 0
    assert microphone["state"] == "stale"
    assert microphone["valid"] is False


def test_a_raw_source_is_carried_but_never_relabelled_sr028(panel):
    """`source` is the one field standing between a ring and an overclaim."""
    write_bus(panel)
    write_aec(panel, microphone={"source": "raw_capture"})
    assert telemetry(panel)["result"]["microphone"]["source"] == "raw_capture"
    for bad in ("post_filter", "", None, 1, "aec"):
        write_aec(panel, microphone={"source": bad})
        result = telemetry(panel)["result"]
        assert "microphone" not in result, bad


def test_a_malformed_microphone_block_drops_only_the_microphone_sr028(panel):
    """The bus is a different producer and must not be taken down with it."""
    write_bus(panel)
    for bad in ({"level": 1.5}, {"level": "0.4"}, {"state": "busy"},
                {"observed_monotonic_ms": -1}, {"reference_dbfs": "loud"}):
        write_aec(panel, microphone=bad)
        result = telemetry(panel)["result"]
        assert result["available"] is True, bad
        assert "microphone" not in result, bad
        assert result["rms"] == 0.4, bad


def test_a_backend_that_omits_the_microphone_is_still_valid_sr028(panel):
    """Optional, for the reason the whole block is: a panel with no canceller."""
    write_bus(panel)
    result = telemetry(panel)["result"]
    assert result["available"] is True
    assert "microphone" not in result


def test_the_broker_refuses_a_contradictory_microphone_block_sr028():
    """valid + a non-live state is a contradiction, and it dies at the broker.

    The backend cannot produce one, but the backend is not the only thing that
    could ever fill this seam, and the renderer must never be handed a
    contradiction to resolve on the glass.
    """
    class Lying:
        def inventory(self, cancel):
            return []

        def call(self, method, params, cancel):
            return {"available": True, "active": True, "rms": 0.1, "peak": 0.2,
                    "bands": [0.1], "observedMonotonicMs": 5,
                    "microphone": {"level": 0.5, "source": "aec_post_filter",
                                   "state": "muted", "ageMs": 10, "valid": True,
                                   "referenceDbfs": -18.0}}

    answer = reply(AudioBroker(Lying()), wire("telemetry"))
    assert answer["ok"] is False
    assert answer["error"]["code"] == "unsafe_backend_result"


def test_a_producer_that_is_not_measuring_reports_SILENCE_sr041(panel):
    """A quiet bus is a real answer, and the visualizer should show a quiet room."""
    write_bus(panel, state="silent", valid=False, active=False)
    result = telemetry(panel)["result"]
    assert result["available"] is True
    assert result["active"] is False
    assert result["rms"] == 0.0 and result["peak"] == 0.0
    assert result["bands"] == [0.0] * 8
    assert result["bus"]["state"] == "silent"


def test_a_panel_that_is_not_in_bus_mode_is_absent_not_silent_sr041(panel):
    """`bus_monitor` exists in ONE of the four ALSA configurations.

    In the other three the producer says `unavailable`, and that has to reach
    the shell as absence: the visualizer falls through to Frame Media rather
    than drawing a flat one (Owner, 2026-09-17).
    """
    write_bus(panel, state="unavailable", valid=False, active=False,
              rms=0.0, peak=0.0, bands=[0.0] * 8)
    assert telemetry(panel)["result"] == {"available": False}


def test_a_stale_document_reports_silence_and_never_the_last_frame_sr041(panel):
    """A producer that stopped must not leave a still picture on the wall."""
    write_bus(panel, observed_monotonic_ms=0)
    result = telemetry(panel)["result"]
    assert result["rms"] == 0.0 and result["bands"] == [0.0] * 8
    assert result["active"] is False


def test_an_out_of_range_level_fails_the_whole_block_sr041(panel):
    for bad in ({"rms": 1.5}, {"rms": -0.1}, {"rms": "0.4"}, {"rms": True},
                {"peak": 2}, {"bands": [0.1] * 7}, {"bands": [0.1] * 9},
                {"bands": [0.1] * 7 + [1.5]}, {"bands": "loud"},
                {"observed_monotonic_ms": -1}, {"observed_monotonic_ms": 1.5}):
        write_bus(panel, **bad)
        assert telemetry(panel)["result"] == {"available": False}, bad


# --- the price of being pre-switch: Mute, Headset and Speaker ----------------

def test_headset_visualizes_sr041(panel):
    """The visible change on the wall. `speaker_tap` showed nothing here."""
    write_state(panel, output="headset")
    write_bus(panel)
    result = telemetry(panel)["result"]
    assert result["active"] is True
    assert result["bands"] == [0.1] * 8


def test_mute_reports_inactive_without_claiming_the_capture_is_gone_sr041(panel):
    """Mute yields Frame Media, and must not be a frozen or flat visualizer.

    Two separate claims, and the difference is the whole ruling: `active` is
    false because nothing audible is happening, while `bus.state` stays `live`
    because the capture is fine and saying otherwise would be a lie about the
    panel's health.
    """
    write_state(panel, output="mute")
    write_bus(panel)
    result = telemetry(panel)["result"]
    assert result["active"] is False
    assert result["bus"]["state"] == "live" and result["bus"]["valid"] is True
    # The LEVELS are still reported: they are what the bus is carrying, which is
    # true regardless of where the switch sends it. Only the activity claim --
    # the one the shell gates presentation on -- is withdrawn.
    assert result["rms"] == 0.4


def test_an_unreadable_switch_position_is_not_assumed_audible_sr041(panel):
    """"We cannot confirm it is audible" is not a claim that it is.

    Assuming Speaker would draw a moving visualizer over a possibly-muted room
    on precisely the panel that can no longer tell.
    """
    panel["state"].write_text("{not json", encoding="utf-8")
    write_bus(panel)
    assert telemetry(panel)["result"]["active"] is False


def test_a_sparse_state_file_is_repaired_exactly_as_the_applier_would_sr041(panel):
    """A readable file with no `output` is a Speaker, because that is what the
    applier will act on -- and this must answer about the position the applier
    would act on, not about the bytes in the file."""
    panel["state"].write_text(json.dumps({"version": 1}), encoding="utf-8")
    write_bus(panel)
    assert telemetry(panel)["result"]["active"] is True


# --- terra 2026-09-14: freshness must be the producer's, and absence must show

def test_the_published_timestamp_is_the_producers_not_the_read_time_sr041(panel):
    """Stamping `now` here made a 1.4-second-old capture look newly observed.

    Both clocks are CLOCK_MONOTONIC on the same machine, so the producer's
    instant needs no conversion -- and it is the only one that carries the
    freshness claim this field exists for.
    """
    observed = int(time.monotonic() * 1000) - 800
    write_bus(panel, observed_monotonic_ms=observed)
    result = telemetry(panel)["result"]
    assert result["observedMonotonicMs"] == observed
    assert result["bus"]["ageMs"] >= 700
    assert result["bus"]["state"] == "live" and result["bus"]["valid"] is True


def test_the_three_reasons_a_bus_is_not_live_are_distinguishable_sr041(panel):
    """`silent`, `stale` and absent are different facts about the panel."""
    write_bus(panel, state="silent", valid=False)
    assert telemetry(panel)["result"]["bus"]["state"] == "silent"
    write_bus(panel, observed_monotonic_ms=0)
    if int(time.monotonic() * 1000) > switch_backend.BUS_STALE_MS:
        assert telemetry(panel)["result"]["bus"]["state"] == "stale"
    panel["bus"].unlink()
    assert telemetry(panel)["result"] == {"available": False}


def test_the_broker_refuses_a_bus_block_that_contradicts_itself_sr028():
    class Lying:
        def inventory(self, cancel):
            return []

        def call(self, method, params, cancel):
            return {"available": True, "active": False, "rms": 0.0, "peak": 0.0,
                    "bands": [0.0], "observedMonotonicMs": 5,
                    "bus": {"state": "stale", "ageMs": 9000, "valid": True,
                            "source": "bus_monitor"}}

    answer = reply(AudioBroker(Lying()), wire("telemetry"))
    assert answer["ok"] is False
    assert answer["error"]["code"] == "unsafe_backend_result"
