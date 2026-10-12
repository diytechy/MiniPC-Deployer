"""The Heart gauge wire fixture the wall panel tests against is the feeder's real output.

OfficeWallNaglight holds a byte-for-byte copy of
`tests/fixtures/heart-wire/heart-gauge-bodies.json` (its
`tests/fixtures/heart-gauge-bodies.json`, with a SOURCE note naming the commit
here it was taken at) and builds its Heart-gauge unit tests from it. This test
is the other half of that arrangement: it regenerates the bodies from
`heart_gauge_body` and fails if they stop equalling the fixture, so a changed
wire shape cannot leave the panel tested against the old one.

If this fails on purpose (the wire changed), regenerate and copy:

    python scripts/gen_heart_wire_fixture.py > tests/fixtures/heart-wire/heart-gauge-bodies.json

then copy the file into OfficeWallNaglight and update its SOURCE note.

Verifies: LLR-1091 (the heart gauge body)
"""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/heart-wire/heart-gauge-bodies.json"

spec = importlib.util.spec_from_file_location("gen_heart_wire_fixture", ROOT / "scripts/gen_heart_wire_fixture.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def test_the_fixture_is_exactly_what_heart_gauge_body_produces():
    assert FIXTURE.read_bytes() == gen.render().encode("utf-8")


def test_the_two_bodies_have_the_shape_the_panel_relies_on():
    held = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert set(held) == {"live", "unavailable"}
    for body in held.values():
        assert (body["id"], body["icon"], body["unit"], body["favourable"]) == ("heart", "heart", "bpm", "low")
        assert body["window"] == {"kind": "daily"}
        assert "start" not in body["window"] and "end" not in body["window"]
    assert "observed_at" in held["live"] and held["live"]["value"] > 0
    # The sentinel 0 exists together with a missing stamp and never without one.
    assert "observed_at" not in held["unavailable"] and held["unavailable"]["value"] == 0
