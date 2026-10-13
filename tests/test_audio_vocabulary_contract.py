"""LLR-1129: the audio vocabulary both repos spell out, held to one fixture.

TC-1226 asserts this repo's REAL constants and documents against
tests/fixtures/audio-vocabulary-v1.json. TC-1227 asserts the fixture is
byte-identical to the copy in OfficeWallNaglight, whose own test
(tests/audio-vocabulary-contract.test.mjs) asserts that repo's constants against
the same file. Nothing here reads a constant back from the file it was defined
in.
"""

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack/autoinstall/wall"
sys.path.insert(0, str(ROOT / "stack/panel-audio"))
sys.path.insert(0, str(WALL))

import bluetooth_state
import routing
import switch_backend
import wall_audio_state

FIXTURE_PATH = ROOT / "tests/fixtures/audio-vocabulary-v1.json"
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


def test_output_positions_and_levelled_subset_agree_tc1226():
    outputs = tuple(FIXTURE["outputs"])
    levelled = tuple(FIXTURE["levelled"])
    assert wall_audio_state.OUTPUTS == outputs
    assert wall_audio_state.LEVELLED_OUTPUTS == levelled
    assert routing.SWITCH_OUTPUTS == outputs
    assert routing.LEVELLED_OUTPUTS == levelled
    assert switch_backend.OUTPUTS == outputs
    assert switch_backend.LEVELLED_OUTPUTS == levelled


def test_bluetooth_capabilities_agree_tc1226():
    # The app sorts the set; the broker lists it in another order. The
    # vocabulary is the SET, so compare sorted.
    expected = sorted(FIXTURE["capabilities"])
    assert sorted(routing.CAPABILITIES) == expected
    assert sorted(bluetooth_state.CAPABILITIES) == expected


def test_volume_gesture_vocabulary_and_documents_agree_tc1226(tmp_path):
    keys = load_script("volume_keys_vocab", "panel-volume-keys.py")
    gesture = FIXTURE["gesture"]
    assert keys.PREVIEW_VERSION == gesture["version"]
    assert list(keys.REFUSE_REASONS) == gesture["refuse"]
    assert list(keys.CANCEL_REASONS) == gesture["cancel"]
    # The key set is read off the document the daemon really writes.
    target = tmp_path / "volume-gesture.json"
    preview = keys.GesturePreview(str(target))
    preview.active("g", 1, "speaker", 3, 40)
    document = json.loads(target.read_text(encoding="utf-8"))
    assert sorted(document) == gesture["activeKeys"]
    assert document["version"] == gesture["version"]


def test_fixture_is_byte_identical_to_the_panel_repo_tc1227():
    sibling = OWN_REPO / "tests/fixtures/audio-vocabulary-v1.json"
    if not sibling.is_file():
        pytest.skip("panel repo not present: %s" % sibling)
    assert hashlib.sha256(sibling.read_bytes()).hexdigest() == hashlib.sha256(
        FIXTURE_PATH.read_bytes()).hexdigest()
