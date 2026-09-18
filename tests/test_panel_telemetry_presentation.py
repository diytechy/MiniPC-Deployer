"""`read_presentation`: the collector's end of the renderer's snapshot file.

WHY THIS FILE EXISTS. `read_presentation` had NO tests, and on 2026-09-18 the
panel was found recording `"presentation": {"status": "invalid"}` on every
sample, with:

    ["unexpected field(s): ['current', 'transitions']",
     "missing field: displayMode", "missing field: fullscreenOwner", ...]

The renderer writes `{"current": {...}, "transitions": [...]}` -- the record
plus a short history -- and the collector handed the whole file to a validator
that describes the flat record. So the display mode, the fullscreen owner and
the media preference were absent from the telemetry log entirely, silently, for
as long as the wrapper has existed.

It was invisible from outside because `payload` is populated only on "ok": a
reader sees `null` and cannot tell "no snapshot yet" from "a snapshot this
collector could not read". That is the property these tests pin.
"""

import importlib.util
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WALL = os.path.join(_HERE, "stack", "autoinstall", "wall")

_core_spec = importlib.util.spec_from_file_location(
    "panel_telemetry_core", os.path.join(_WALL, "panel_telemetry_core.py"))
core = importlib.util.module_from_spec(_core_spec)
sys.modules["panel_telemetry_core"] = core
_core_spec.loader.exec_module(core)

_spec = importlib.util.spec_from_file_location(
    "panel_telemetry", os.path.join(_WALL, "panel-telemetry.py"))
telemetry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(telemetry)


def _record(**over):
    record = {
        "displayMode": "FULL",
        "selectedTab": "CHECKLIST",
        "fullscreenOwner": "PROJECTM",
        "mediaPreference": "projectm",
        "visualizerStatus": None,
        "displayPower": "on",
        "musicActive": True,
        "revision": 12,
        "monotonicMs": 1000,
    }
    record.update(over)
    return record


def _write(tmp_path, payload):
    path = tmp_path / "presentation-snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_the_wrapper_the_renderer_actually_writes_is_read(tmp_path):
    """The exact shape on the panel: `current` plus `transitions`."""
    path = _write(tmp_path, {"current": _record(), "transitions": [_record(revision=11)]})
    got = telemetry.read_presentation(path, 1000)
    assert got["status"] == "ok", got["errors"]
    assert got["payload"]["fullscreenOwner"] == "PROJECTM"
    assert got["payload"]["displayMode"] == "FULL"


def test_the_active_preset_survives_the_unwrap(tmp_path):
    """Owner, 2026-09-18: the preset must land beside the thermal numbers."""
    path = _write(tmp_path, {"current": _record(projectmPreset="Dancer/Aurora.milk"),
                             "transitions": []})
    got = telemetry.read_presentation(path, 1000)
    assert got["status"] == "ok", got["errors"]
    assert got["payload"]["projectmPreset"] == "Dancer/Aurora.milk"


def test_a_flat_record_still_reads(tmp_path):
    """Backward compatible: unwrapping is conditional, not assumed."""
    got = telemetry.read_presentation(_write(tmp_path, _record()), 1000)
    assert got["status"] == "ok", got["errors"]
    assert got["payload"]["displayMode"] == "FULL"


def test_a_genuinely_invalid_record_is_still_invalid(tmp_path):
    """The unwrap must not become a way to smuggle a bad record through."""
    bad = _record()
    del bad["displayMode"]
    got = telemetry.read_presentation(_write(tmp_path, {"current": bad, "transitions": []}), 1000)
    assert got["status"] == "invalid"
    assert got["payload"] is None
    assert any("displayMode" in e for e in got["errors"])


def test_a_stale_wrapped_snapshot_is_stale_not_ok(tmp_path):
    """The S rule -- never log a stale mode as current -- survives unwrapping."""
    path = _write(tmp_path, {"current": _record(monotonicMs=0), "transitions": []})
    got = telemetry.read_presentation(path, 10_000_000)
    assert got["status"] == "stale"
    assert got["payload"] is None
    assert got["last_seen"]["displayMode"] == "FULL"


def test_a_missing_file_is_unknown_not_invalid(tmp_path):
    got = telemetry.read_presentation(str(tmp_path / "absent.json"), 1000)
    assert got["status"] == "unknown"
    assert got["payload"] is None


def test_unparsable_json_is_invalid(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text("{not json", encoding="utf-8")
    got = telemetry.read_presentation(str(path), 1000)
    assert got["status"] == "invalid"
    assert got["payload"] is None


@pytest.mark.parametrize("wrapper", [
    {"current": None, "transitions": []},
    {"current": "FULL"},
    {"transitions": []},
])
def test_a_wrapper_without_a_usable_current_is_invalid_not_a_crash(tmp_path, wrapper):
    got = telemetry.read_presentation(_write(tmp_path, wrapper), 1000)
    assert got["status"] == "invalid"
    assert got["payload"] is None
