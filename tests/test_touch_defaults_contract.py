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


# --- whole-output comparison against the previous renderer, run for real -----
#
# tests/fixtures/render-touch-filter.68443af.py is that renderer verbatim
# (git show 68443af:stack/autoinstall/wall/render-touch-filter.py). Comparing
# only the policy cannot see a clobbered device name or a udev rule that never
# matches, which is exactly the defect a loop variable once caused here.

LEGACY_POLICY = {"min_tap_ms", "startup_ms", "max_gesture_ms", "max_points"}


@pytest.fixture(scope="module")
def old_renderer(app):
    spec = importlib.util.spec_from_file_location(
        "renderer_68443af", ROOT / "tests" / "fixtures" / "render-touch-filter.68443af.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CASES = {
    "example": {},
    "example-legacy-lines": dict(LEGACY_LINES),
    "custom-name": {"TOUCH_FILTER_NAME": "Other Panel 2", "TOUCH_FILTER_VENDOR": "1a2b", "TOUCH_FILTER_PRODUCT": "3C4D"},
}


@pytest.mark.parametrize("mode", ["off", "shadow", "filter", "adaptive"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_tc1230_everything_but_the_policy_matches_the_previous_renderer(
    renderer, old_renderer, mode, case
):
    env = {**example_env(), **CASES[case], "TOUCH_FILTER_MODE": mode}
    if mode == "filter":
        env["TOUCH_FILTER_ISOLATE"] = "true"
    new, new_rules = renderer.render(dict(env))
    old, old_rules = old_renderer.render(dict(env))
    expected_name = env.get("TOUCH_FILTER_NAME", "ELAN Touchscreen")
    assert new["name"] == old["name"] == expected_name
    assert f'ATTRS{{name}}=="{expected_name}"' in new_rules
    assert new_rules == old_rules
    assert (new["vendor"], new["product"]) == (old["vendor"], old["product"])
    assert {k: v for k, v in new.items() if k != "policy"} == {
        k: v for k, v in old.items() if k != "policy"
    }
    # The policy differs only by keys that equal the application default.
    assert set(old["policy"]) - set(new["policy"]) >= LEGACY_POLICY
    for key, value in new["policy"].items():
        assert old["policy"][key] == value


def test_tc1230_name_in_rules_survives_a_policy_override(renderer):
    config, rules = renderer.render({"TOUCH_MIN_TAP_MS": "55"})
    assert config["name"] == "ELAN Touchscreen"
    assert 'ATTRS{name}=="ELAN Touchscreen"' in rules


# --- single path (TOUCH_FILTER_SINGLE_PATH), 2026-10-16 ------------------------
#
# The daemon refuses a config key it does not know, so the knob must render
# NOTHING while false: an image with the knob off has to keep working with an
# app that predates it.


def test_single_path_off_renders_exactly_as_before(renderer):
    for mode in ("off", "shadow", "filter", "adaptive"):
        env = {**example_env(), "TOUCH_FILTER_MODE": mode}
        assert env["TOUCH_FILTER_SINGLE_PATH"] == "false"
        config, _ = renderer.render(env)
        assert "single_path" not in config


def test_single_path_on_reaches_the_daemon(app, renderer, tmp_path):
    _, load_config = app
    if "single_path" not in json.dumps(load_config.__code__.co_consts, default=str):
        pytest.skip("the application checkout predates single_path; set TOUCH_APP_REPO")
    config, _ = renderer.render({**example_env(), "TOUCH_FILTER_MODE": "adaptive",
                                 "TOUCH_FILTER_SINGLE_PATH": "true"})
    assert config["single_path"] is True
    path = tmp_path / "touch-filter.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert load_config(path)["single_path"] is True


@pytest.mark.parametrize("env", [
    {"TOUCH_FILTER_MODE": "adaptive", "TOUCH_FILTER_SINGLE_PATH": "yes"},
    {"TOUCH_FILTER_MODE": "adaptive", "TOUCH_FILTER_SINGLE_PATH": "1"},
    {"TOUCH_FILTER_MODE": "filter", "TOUCH_FILTER_SINGLE_PATH": "true"},
    {"TOUCH_FILTER_MODE": "off", "TOUCH_FILTER_SINGLE_PATH": "true"},
])
def test_single_path_is_refused_when_invalid_or_outside_adaptive(renderer, env):
    with pytest.raises(ValueError):
        renderer.render(env)
