"""The touch filter has one source of defaults: touchfilter.core.Policy.

Implements: SR-017; LLR-1131. TC-1230, TC-1231.

The image used to render 60 / 1500 / 10000 / 2048 while the daemon migrated
each of those exact values to 40 / 0 / 60000 / 8192 on every load, so the panel
ran the latter and its settings claimed the former. These tests load the
rendered result through the real daemon (`load_config`), because that is where
the policy actually takes effect.

The application repo is the sibling `OfficeWallNaglight` (`TOUCH_APP_REPO` or
`OWN_REPO` to override); the checks skip without it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = Path(
    os.environ.get("TOUCH_APP_REPO")
    or os.environ.get("OWN_REPO")
    or ROOT.parent / "OfficeWallNaglight"
)
WALL = ROOT / "stack" / "autoinstall" / "wall"

pytestmark = pytest.mark.skipif(
    not (APP / "touchfilter" / "core.py").is_file(),
    reason="application repo not found; set TOUCH_APP_REPO",
)

# What the live panel's wall.env says (2026-10-13), the four legacy lines.
LEGACY_LINES = {
    "TOUCH_MIN_TAP_MS": "60",
    "TOUCH_STARTUP_MS": "1500",
    "TOUCH_MAX_GESTURE_MS": "10000",
    "TOUCH_MAX_POINTS": "2048",
}
LEGACY_KEYS = ("min_tap_ms", "startup_ms", "max_gesture_ms", "max_points")
PANEL_PROVEN = {"min_tap_ms": 40, "startup_ms": 0, "max_gesture_ms": 60000, "max_points": 8192}


@pytest.fixture(scope="module")
def app():
    sys.path.insert(0, str(APP))
    from touchfilter.core import Policy
    from touchfilter.daemon import load_config

    yield Policy, load_config
    sys.path.remove(str(APP))


@pytest.fixture(scope="module")
def renderer(app):
    spec = importlib.util.spec_from_file_location("renderer_wi14", WALL / "render-touch-filter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def example_env():
    """Every uncommented TOUCH_* assignment in wall.env.example, as an env."""
    env = {}
    for line in (WALL / "wall.env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("TOUCH_") and "=" in line:
            key, _, value = line.partition("=")
            env[key] = value.strip().strip('"')
    return env


def load(load_config, config, tmp_path):
    path = tmp_path / "touch-filter.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return load_config(path)["policy"]


def test_tc1230_default_example_renders_nothing_the_application_does_not_default(
    app, renderer, tmp_path
):
    Policy, load_config = app
    env = example_env()
    assert env["TOUCH_FILTER_MODE"] == "off"
    # The example no longer states the four legacy values at all.
    assert not set(LEGACY_LINES) & set(env)
    config, _ = renderer.render(env)
    assert not set(LEGACY_KEYS) & set(config["policy"])
    policy = load(load_config, config, tmp_path)
    assert asdict(policy) == asdict(Policy())
    for name, value in PANEL_PROVEN.items():
        assert getattr(policy, name) == value


def test_tc1230_adaptive_example_loads_to_the_same_policy(app, renderer, tmp_path):
    Policy, load_config = app
    config, _ = renderer.render({**example_env(), "TOUCH_FILTER_MODE": "adaptive"})
    assert not set(LEGACY_KEYS) & set(config["policy"])
    assert asdict(load(load_config, config, tmp_path)) == asdict(Policy())


def test_tc1231_an_operator_override_survives_render_and_load(app, renderer, tmp_path):
    Policy, load_config = app
    config, _ = renderer.render({**example_env(), "TOUCH_MIN_TAP_MS": "55", "TOUCH_MAX_POINTS": "4096"})
    assert config["policy"]["min_tap_ms"] == 55
    assert config["policy"]["max_points"] == 4096
    policy = load(load_config, config, tmp_path)
    assert (policy.min_tap_ms, policy.max_points) == (55, 4096)
    # Everything the operator did not touch still follows Policy.
    assert (policy.startup_ms, policy.max_gesture_ms) == (0, 60000)


def test_tc1231_an_invalid_override_is_still_refused(renderer):
    with pytest.raises(ValueError):
        renderer.render({"TOUCH_MIN_TAP_MS": "nan"})


def test_tc1230_the_live_panels_legacy_lines_render_to_the_same_effective_policy(app, renderer, tmp_path):
    Policy, load_config = app
    config, _ = renderer.render({**example_env(), "TOUCH_FILTER_MODE": "adaptive", **LEGACY_LINES})
    assert not set(LEGACY_KEYS) & set(config["policy"])
    after = load(load_config, config, tmp_path)
    # What the previous renderer wrote for those lines (the panel's
    # touch-filter.json: all four legacy values), through the same loader.
    old = dict(config)
    old["policy"] = {**config["policy"], "min_tap_ms": 60.0, "startup_ms": 1500.0,
                     "max_gesture_ms": 10000.0, "max_points": 2048}
    before = load(load_config, old, tmp_path)
    assert asdict(after) == asdict(before) == asdict(Policy())
    for name, value in PANEL_PROVEN.items():
        assert getattr(after, name) == value
