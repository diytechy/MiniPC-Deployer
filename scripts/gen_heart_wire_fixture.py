#!/usr/bin/env python3
"""Print the weight feeder's heart gauge bodies, as JSON, from the REAL code.

The wall panel (OfficeWallNaglight) tests its Heart gauge against these bodies.
Run from the repo root:

    python scripts/gen_heart_wire_fixture.py > tests/fixtures/heart-wire/heart-gauge-bodies.json

and copy the output byte for byte into OfficeWallNaglight
`tests/fixtures/heart-gauge-bodies.json`. `tests/test_heart_wire_fixture.py`
fails if `heart_gauge_body` stops producing what the committed copy says.

Two bodies, both with the shipped knob defaults (40 / 100 / 60):
  live         a resting rate of 58 for 2026-10-11, stamped noon CDT (17:00Z)
  unavailable  the stale-on-arrival body: value 0 and NO observed_at
"""

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DAY_STAMP = 1791738000  # 2026-10-11T17:00:00Z (noon CDT), written as a literal


def load_feeder():
    spec = importlib.util.spec_from_file_location(
        "weight_feeder_heart_fixture", REPO / "stack" / "weight" / "weight_feeder.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bodies():
    feeder = load_feeder()
    config = feeder.resolve_heart_config({})
    return {
        "live": feeder.heart_gauge_body(58, DAY_STAMP, config),
        "unavailable": feeder.heart_gauge_body(0, None, config),
    }


def render():
    return json.dumps(bodies(), indent=2) + "\n"


if __name__ == "__main__":
    # Bytes, not print(): Windows text-mode stdout would write CRLF into a fixture that must be LF.
    sys.stdout.buffer.write(render().encode("utf-8"))
