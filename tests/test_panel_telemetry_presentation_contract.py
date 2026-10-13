"""LLR-1126 / TC-1220, TC-1221: the presentation snapshot schema both repos validate.

tests/fixtures/presentation-v1.json is BYTE-IDENTICAL to the copy in
OfficeWallNaglight. The snapshot shape is checked in two repos (the panel host's
validatePresentation, and this collector's `validate_presentation`), and on
2026-09-18 a field added in two places and missed in the third deleted every
record. This file runs the fixture through THIS repo's real validator; the panel
repo's tests/presentation-contract.test.mjs runs the same file through its host
validator and its real deriveSnapshot.

A case where the two validators disagree is not in `cases`; it is a pinned entry
in `knownDifferences`, so a later fix flips a test visibly instead of drifting.
"""

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OWN_REPO = Path(os.environ.get("OWN_REPO") or ROOT.parent / "OfficeWallNaglight")
FIXTURE = ROOT / "tests/fixtures/presentation-v1.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))

_spec = importlib.util.spec_from_file_location(
    "panel_telemetry_core", ROOT / "stack/autoinstall/wall/panel_telemetry_core.py")
core = importlib.util.module_from_spec(_spec)
sys.modules["panel_telemetry_core"] = core
_spec.loader.exec_module(core)


def decode(value):
    """{"$number": "NaN"} and {"$repeat": [s, n]} are the fixture's two encodings."""
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        if "$number" in value:
            return float(value["$number"])
        if "$repeat" in value:
            return value["$repeat"][0] * value["$repeat"][1]
        return {k: decode(v) for k, v in value.items()}
    return value


def test_tc1220_fixture_names_the_collectors_five_owners():
    assert sorted(DATA["owners"]) == sorted(core.FULLSCREEN_OWNERS)
    exercised = {c["payload"]["fullscreenOwner"] for c in DATA["cases"]
                 if c["expectedOk"] and c["payload"].get("fullscreenOwner")}
    assert sorted(exercised) == sorted(core.FULLSCREEN_OWNERS)


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda c: c["name"])
def test_presentation_fixture_matches_core_validate_tc1220(case):
    ok, errors = core.validate_presentation(decode(case["payload"]))
    assert ok == case["expectedOk"], f"expected ok={case['expectedOk']}, got {ok}: {errors}"
    if not case["expectedOk"]:
        assert any(case["expectedErrorSubstring"] in e for e in errors), \
            f'no error contains {case["expectedErrorSubstring"]!r}; got {errors}'


@pytest.mark.parametrize("diff", DATA["knownDifferences"], ids=lambda d: d["name"])
def test_tc1220_known_difference_is_pinned_to_what_the_collector_does_today(diff):
    assert diff["ownOk"] != diff["mpdOk"], "a known difference must differ"
    ok, _ = core.validate_presentation(decode(diff["payload"]))
    assert ok == diff["mpdOk"], "collector validator changed; update the pin and the panel side together"


def test_presentation_fixture_is_byte_identical_to_sibling_tc1221():
    sibling = OWN_REPO / "tests/fixtures/presentation-v1.json"
    if not sibling.is_file():
        pytest.skip("OfficeWallNaglight checkout (or its copy of the fixture) is absent; set OWN_REPO")
    assert hashlib.sha256(sibling.read_bytes()).hexdigest() == hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), \
        "presentation-v1.json differs between the repos"
