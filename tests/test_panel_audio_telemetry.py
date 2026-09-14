"""SR-028/LLR-015: item L -- the bus levels and the post-filter mic scalar.

Contract of record: OfficeWallNaglight
`docs/design/audio-intent-contract-2026-09-14.md`, section 4.

WHAT CHANGED AND WHY IT HAD TO. `telemetry` answered `{"available": false}`
unconditionally, which was a structural answer rather than a stub: the switch
backend replaced the routed one, and it performs no device I/O at all -- the
broker runs as `panel` under ProtectSystem=strict with PrivateDevices=true and
AF_UNIX as its only address family. It still performs none. What is new is that
two services which ALREADY hold the captures publish what they have measured:

  wall-amp-trigger  reads `speaker_tap` every 100 ms for the amplifier relay
  wall-audio-aec    reads the panel microphone as the canceller's near end

so the backend reads two bounded documents from /run and never opens a device.

What this file holds down:
  * no producer at all is `available: false` -- honestly absent, and
    distinguishable from a silent room;
  * a producer that says its capture is NOT live reports SILENCE, because the
    visualizer should show a quiet room rather than disappear;
  * a STALE document reports silence too, never the last frame it saw;
  * every microphone state is explicit, and `valid` may never contradict it;
  * `source` is an enum, so a raw level can never be presented as post-filter;
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
    """An installed panel whose two telemetry producers write into tmp_path."""
    applier = tmp_path / "wall-audio-output"
    applier.write_text("#!/bin/sh\n", encoding="utf-8")
    state = tmp_path / "audio-state.json"
    state.write_text(json.dumps({
        "version": 1, "output": "speaker", "input_muted": False,
        "volume": {"headset": 60, "speaker": 72}, "headset_present": False,
        "headset_autoswitch_armed": True, "request_seq": 41, "generation": 7,
    }), encoding="utf-8")
    bus = tmp_path / "bus-telemetry.json"
    aec = tmp_path / "aec-status.json"
    monkeypatch.setattr(switch_backend, "BUS_TELEMETRY_PATH", bus)
    monkeypatch.setattr(switch_backend, "AEC_STATUS_PATH", aec)
    return {"applier": applier, "state": state, "bus": bus, "aec": aec,
            "request": tmp_path / "run" / "request.json"}


def backend(panel):
    return SwitchApplierBackend(request_path=panel["request"],
                                state_path=panel["state"],
                                applier_path=panel["applier"])


def telemetry(panel):
    return reply(AudioBroker(backend(panel)), wire("telemetry"))


def write_bus(panel, **fields):
    document = {
        "schema": 1, "source": "speaker_tap", "valid": True, "active": True,
        "rms": 0.4, "peak": 0.8, "bands": [0.1] * 8,
        "observed_monotonic_ms": int(time.monotonic() * 1000),
        "reference_dbfs": -12.0, "updated_utc": "2026-09-14T02:10:00Z",
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

def test_no_producer_at_all_is_unavailable_not_silence_sr028(panel):
    """Absent is not a quiet room, and the shell must render the difference."""
    answer = telemetry(panel)
    assert answer["ok"] is True
    assert answer["result"] == {"available": False}


def test_a_malformed_document_is_unavailable_never_half_believed_sr028(panel):
    for bad in ["{", "null", "[]", '{"schema": 2}', '{"schema": 1}',
                '{"schema": 1, "source": "hw:PCH,0", "valid": true}']:
        panel["bus"].write_text(bad, encoding="utf-8")
        assert telemetry(panel)["result"] == {"available": False}, bad


def test_an_oversized_document_is_refused_sr028(panel):
    write_bus(panel)
    panel["bus"].write_text("{\"pad\": \"" + "x" * 20000 + "\"}", encoding="utf-8")
    assert telemetry(panel)["result"] == {"available": False}


# --- the bus -----------------------------------------------------------------

def test_the_bus_levels_reach_the_client_sr028(panel):
    write_bus(panel)
    result = telemetry(panel)["result"]
    assert result["available"] is True
    assert result["active"] is True
    assert result["rms"] == 0.4 and result["peak"] == 0.8
    assert result["bands"] == [0.1] * 8
    assert result["observedMonotonicMs"] >= 0


def test_a_producer_whose_capture_is_not_live_reports_SILENCE_sr028(panel):
    """The switch has left Speaker, or the tap has not opened.

    That is a silent bus, not an absent one, and the difference matters: the
    visualizer should show a quiet room rather than vanish.
    """
    write_bus(panel, valid=False)
    result = telemetry(panel)["result"]
    assert result["available"] is True
    assert result["active"] is False
    assert result["rms"] == 0.0 and result["peak"] == 0.0
    assert result["bands"] == [0.0] * 8


def test_a_stale_document_reports_silence_and_never_the_last_frame_sr028(panel):
    """A producer that stopped must not leave a still picture on the wall."""
    write_bus(panel, observed_monotonic_ms=0)
    result = telemetry(panel)["result"]
    assert result["rms"] == 0.0 and result["bands"] == [0.0] * 8
    assert result["active"] is False


def test_an_out_of_range_level_fails_the_whole_block_sr028(panel):
    for bad in ({"rms": 1.5}, {"rms": -0.1}, {"rms": "0.4"}, {"rms": True},
                {"peak": 2}, {"bands": [0.1] * 7}, {"bands": [0.1] * 9},
                {"bands": [0.1] * 7 + [1.5]}, {"bands": "loud"},
                {"observed_monotonic_ms": -1}, {"observed_monotonic_ms": 1.5}):
        write_bus(panel, **bad)
        assert telemetry(panel)["result"] == {"available": False}, bad


# --- the microphone ----------------------------------------------------------

def test_the_post_filter_level_reaches_the_client_sr028(panel):
    write_bus(panel)
    write_aec(panel)
    microphone = telemetry(panel)["result"]["microphone"]
    assert microphone["level"] == 0.42
    assert microphone["source"] == "aec_post_filter"
    assert microphone["state"] == "live"
    assert microphone["valid"] is True
    assert microphone["referenceDbfs"] == -18.0
    assert microphone["ageMs"] >= 0


def test_a_microphone_without_a_bus_still_publishes_sr028(panel):
    """The canceller may be running with the switch away from Speaker."""
    write_aec(panel)
    result = telemetry(panel)["result"]
    assert result["available"] is True
    assert result["rms"] == 0.0 and result["bands"] == [0.0] * 8
    assert result["microphone"]["level"] == 0.42


@pytest.mark.parametrize("state", ["muted", "stale", "unavailable"])
def test_a_level_that_is_not_live_is_zeroed_and_named_sr028(panel, state):
    """Never a frozen ring, and never one without a reason beside it."""
    write_aec(panel, microphone={"state": state, "valid": False})
    microphone = telemetry(panel)["result"]["microphone"]
    assert microphone["state"] == state
    assert microphone["valid"] is False
    assert microphone["level"] == 0.0


def test_a_level_older_than_the_window_becomes_stale_sr028(panel):
    """The canceller publishes at 0.1 Hz; past two and a half periods it is stale."""
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
    write_aec(panel, microphone={"observed_monotonic_ms": int(time.monotonic() * 1000) + 600000})
    microphone = telemetry(panel)["result"]["microphone"]
    assert microphone["ageMs"] == 0
    assert microphone["state"] == "stale"
    assert microphone["valid"] is False


def test_a_raw_source_is_carried_but_never_relabelled_sr028(panel):
    """`source` is the one field standing between a ring and an overclaim."""
    write_aec(panel, microphone={"source": "raw_capture"})
    assert telemetry(panel)["result"]["microphone"]["source"] == "raw_capture"
    for bad in ("post_filter", "", None, 1, "aec"):
        write_aec(panel, microphone={"source": bad})
        result = telemetry(panel)["result"]
        assert "microphone" not in result or result == {"available": False}, bad


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


def test_the_broker_refuses_a_contradictory_block_from_any_backend_sr028(panel):
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


def test_a_backend_that_omits_the_microphone_is_still_valid_sr028(panel):
    """Optional, for the reason the whole block is: a panel with no canceller."""
    write_bus(panel)
    result = telemetry(panel)["result"]
    assert result["available"] is True
    assert "microphone" not in result


# --- terra 2026-09-14: freshness must be the producer's, and absence must show

def test_the_published_timestamp_is_the_producers_not_the_read_time_sr028(panel):
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


def test_an_absent_bus_is_named_unavailable_not_passed_off_as_quiet_sr028(panel):
    """A bus nobody is publishing must not look like a measured quiet room."""
    write_aec(panel)
    result = telemetry(panel)["result"]
    assert result["rms"] == 0.0
    assert result["bus"]["state"] == "unavailable"
    assert result["bus"]["valid"] is False


def test_the_three_reasons_a_bus_is_silent_are_distinguishable_sr028(panel):
    """`silent`, `stale` and `unavailable` are different facts about the panel."""
    write_bus(panel, valid=False)
    assert telemetry(panel)["result"]["bus"]["state"] == "silent"
    write_bus(panel, observed_monotonic_ms=0)
    if int(time.monotonic() * 1000) > switch_backend.BUS_STALE_MS:
        assert telemetry(panel)["result"]["bus"]["state"] == "stale"
    panel["bus"].unlink()
    write_aec(panel)
    assert telemetry(panel)["result"]["bus"]["state"] == "unavailable"


def test_the_broker_refuses_a_bus_block_that_contradicts_itself_sr028():
    class Lying:
        def inventory(self, cancel):
            return []

        def call(self, method, params, cancel):
            return {"available": True, "active": False, "rms": 0.0, "peak": 0.0,
                    "bands": [0.0], "observedMonotonicMs": 5,
                    "bus": {"state": "stale", "ageMs": 9000, "valid": True,
                            "source": "speaker_tap"}}

    answer = reply(AudioBroker(Lying()), wire("telemetry"))
    assert answer["ok"] is False
    assert answer["error"]["code"] == "unsafe_backend_result"
