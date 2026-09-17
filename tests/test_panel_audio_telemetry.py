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
  * THE MICROPHONE IS GONE FROM THIS DOCUMENT. It reaches neither speaker nor
    headset, so it is not a visualization source, and the old backend could
    substitute a microphone block for an absent bus. The substitution and the
    block are both removed at the producer rather than filtered downstream.
    Microphone CAPTURE is untouched; `wall-audio-aec` still runs.

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
    """An installed panel whose bus producer writes into tmp_path."""
    applier = tmp_path / "wall-audio-output"
    applier.write_text("#!/bin/sh\n", encoding="utf-8")
    state = tmp_path / "audio-state.json"
    bus = tmp_path / "bus-telemetry.json"
    monkeypatch.setattr(switch_backend, "BUS_TELEMETRY_PATH", bus)
    handle = {"applier": applier, "state": state, "bus": bus,
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


def test_the_document_never_carries_a_microphone_block_sr041(panel):
    """Ruled 2026-09-17: the mic is not a visualization source, so it is gone.

    Removed at the producer rather than ignored downstream, which is why this
    asserts on the backend's own result and not on what a client chooses to
    read.
    """
    write_bus(panel)
    result = telemetry(panel)["result"]
    assert "microphone" not in result
    assert not hasattr(SwitchApplierBackend, "_microphone_block")


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
