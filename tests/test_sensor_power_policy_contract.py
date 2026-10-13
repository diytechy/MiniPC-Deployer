"""LLR-1127: the camera hierarchy's truth table, held to one fixture.

TC-1222 asserts this repo's REAL power policy (wall-sensor-power-policy.py)
against the `keepAwake` column of tests/fixtures/sensing-hierarchy-v1.json. The
column is the INTENDED rule: the CPU stays awake only for a camera wake source
that is actually in effect (motion or face-shape presence under a consented
camera). TC-1223 asserts the fixture is byte-identical to the copy in
OfficeWallNaglight, whose own tests (tests/settings-sensing-hierarchy.test.mjs
and tests/sensors/test_sensing_hierarchy_fixture.py) assert that repo's
renderer and sensor service against the same file.

KNOWN DIFFERENCE (open question Q5, unanswered): keep_awake reads the STORED
ticks and ignores the hierarchy, so a panel with only face unlock ticked
(camera on and consented, face-shape presence and motion off) is kept awake for
a face unlock that cannot run, because face unlock sits under attention under
face-shape presence. Those rows are xfail(strict) with reason 'Q5'. When the
policy is fixed they XPASS, strict turns that into a failure, and the marker is
removed on purpose rather than the difference disappearing unseen.
"""

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack/autoinstall/wall"
FIXTURE_PATH = ROOT / "tests/fixtures/sensing-hierarchy-v1.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
# Sibling resolution honours OWN_REPO so a worktree can point at its own panel
# checkout (precedent: TOUCH_APP_REPO in test_touch_filter_release.py).
OWN_REPO = Path(os.environ.get("OWN_REPO") or ROOT.parent / "OfficeWallNaglight")


def load_script(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(WALL / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


POLICY = load_script("wall_sensor_power_policy_contract", "wall-sensor-power-policy.py")


def only_face_unlock_ticked(inputs):
    """The Q5 rows: a consented camera whose only wake tick is face unlock."""
    return (inputs["cameraEnabled"] and inputs["cameraConsentVersion"] == 1
            and inputs["faceLoginEnabled"]
            and not inputs["presenceFace"] and not inputs["presenceMotion"])


def row_id(row):
    i = row["inputs"]
    return "cam{}-consent{}-motion{}-face{}-attn{}-unlock{}".format(
        *(int(i[k]) for k in ("cameraEnabled", "cameraConsentVersion", "presenceMotion",
                              "presenceFace", "attentionEnabled", "faceLoginEnabled")))


def row_param(row):
    marks = []
    if only_face_unlock_ticked(row["inputs"]):
        marks.append(pytest.mark.xfail(strict=True, reason="Q5"))
    return pytest.param(row, marks=marks, id=row_id(row))


def stored_config(inputs):
    # `bluetoothEnabled` is held false by the fixture: it is not part of the hierarchy.
    return {"schemaVersion": 2, **inputs, **FIXTURE["held"]}


@pytest.mark.parametrize("row", [row_param(row) for row in FIXTURE["rows"]])
def test_keep_awake_matches_fixture_tc1222(row):
    assert POLICY.keep_awake(stored_config(row["inputs"])) is row["keepAwake"]


def test_fixture_is_the_full_table_tc1222():
    assert len(FIXTURE["rows"]) == 64
    assert FIXTURE["held"] == {"bluetoothEnabled": False}
    assert all(type(row["keepAwake"]) is bool for row in FIXTURE["rows"])
    # The known difference is real and bounded: 2 rows (attention is free, and
    # the camera and consent are both on, and motion and face-shape are off).
    assert sum(only_face_unlock_ticked(row["inputs"]) for row in FIXTURE["rows"]) == 2


def test_fixture_byte_identical_tc1223():
    sibling = OWN_REPO / "tests/fixtures/sensing-hierarchy-v1.json"
    if not sibling.is_file():
        pytest.skip("OfficeWallNaglight checkout not found; set OWN_REPO")
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == hashlib.sha256(sibling.read_bytes()).hexdigest()
    assert FIXTURE_PATH.read_bytes() == sibling.read_bytes()
