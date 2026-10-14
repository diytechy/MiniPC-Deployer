#!/usr/bin/env python3
"""Render validated touch-filter JSON and udev rules; Implements: SR-017.

Reads explicitly exported TOUCH_* config, writes only supplied output paths.
Does not grab devices or enable services. The application owns policy validation.
"""

import json
import os
from dataclasses import fields
from pathlib import Path
import re
import sys
from touchfilter.core import Policy

# The image's pre-protocol-2 settings, exactly as the daemon's migration names
# them (touchfilter/daemon.py, load_config). Each means "the default" now.
LEGACY_DEFAULTS = {"min_tap_ms": 60, "startup_ms": 1500, "max_gesture_ms": 10000, "max_points": 2048}


def adaptive(env, policy):
    """The extra config the adaptive trial needs, and nothing the others read.

    Rendered rather than hand-written because a hand-written one drifted: the
    panel carried a session_cgroup naming a Chromium scope by PID, the browser
    respawned under a new PID, and the bridge rejected the real kiosk forever.
    The daemon then failed open with the touchscreen ungrabbed, filtering
    nothing while its status still said "protecting-fail-open".

    So the scope is emitted as a PATTERN, with `*` standing for that PID, and
    the slice is derived from the kiosk uid instead of being repeated as a
    literal. Nothing here goes stale when Chromium restarts or the uid changes.
    """
    uid = int(env.get("TOUCH_KIOSK_UID", 1000))
    gid = int(env.get("TOUCH_KIOSK_GID", uid))
    if not 0 < uid < 65536 or not 0 < gid < 65536:
        raise ValueError("invalid-kiosk-peer")
    scope = env.get("TOUCH_KIOSK_SCOPE", "app-org.chromium.Chromium-*.scope")
    if not re.fullmatch(r"[A-Za-z0-9@:_.*\-]{1,120}\.scope", scope) or scope.count("*") > 1:
        raise ValueError("invalid-kiosk-scope")
    quiet = float(env.get("TOUCH_PROTECTION_QUIET_MS", 1800000))
    if not 60000 <= quiet <= 86400000:
        raise ValueError("invalid-protection-quiet")
    # protection_quiet_ms rides in `policy` but is NOT a core Policy field --
    # daemon.load_config pops it before constructing one. Added after the
    # Policy(**policy) validation above for exactly that reason.
    policy["protection_quiet_ms"] = quiet
    return {
        "bridge": {
            "socket": "/run/wall-touch-filter/input.sock",
            "kiosk_uid": uid,
            "kiosk_gid": gid,
            "session_cgroup": f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{scope}",
        },
        "bench_force_protection": False,
    }


def render(env):
    """Return config/rules for one pinned touchscreen, never a guessed device."""
    mode = env.get("TOUCH_FILTER_MODE", "off")
    isolate = env.get("TOUCH_FILTER_ISOLATE", "false")
    if mode not in ("off", "shadow", "filter", "adaptive") or isolate not in ("true", "false"):
        raise ValueError("invalid-touch-filter-mode")
    if isolate == "true" and mode != "filter":
        raise ValueError("isolation-requires-filter")
    name = env.get("TOUCH_FILTER_NAME", "ELAN Touchscreen")
    if not re.fullmatch(r"[A-Za-z0-9 _.-]{1,80}", name):
        raise ValueError("invalid-touchscreen-name")
    ids = [
        env.get(key, default)
        for key, default in (
            ("TOUCH_FILTER_VENDOR", "04f3"),
            ("TOUCH_FILTER_PRODUCT", "222a"),
        )
    ]
    if any(not re.fullmatch(r"[a-fA-F0-9]{4}", value) for value in ids):
        raise ValueError("invalid-touchscreen-id")
    # Every default comes from touchfilter.core.Policy, the single source:
    # nothing is repeated here to drift from it.
    options = {
        "min_tap_ms": "TOUCH_MIN_TAP_MS",
        "band_hold_ms": "TOUCH_BAND_HOLD_MS",
        "stable_radius": "TOUCH_STABLE_RADIUS",
        "band_window_ms": "TOUCH_BAND_WINDOW_MS",
        "band_min_contacts": "TOUCH_BAND_MIN_CONTACTS",
        "band_min_rate": "TOUCH_BAND_MIN_RATE",
        "band_max_height": "TOUCH_BAND_MAX_HEIGHT",
        "band_min_width": "TOUCH_BAND_MIN_WIDTH",
        "band_fraction": "TOUCH_BAND_FRACTION",
        "band_padding": "TOUCH_BAND_PADDING",
        "band_ttl_ms": "TOUCH_BAND_TTL_MS",
        "startup_ms": "TOUCH_STARTUP_MS",
        "max_gesture_ms": "TOUCH_MAX_GESTURE_MS",
        "max_points": "TOUCH_MAX_POINTS",
    }
    defaults = {field.name: field.default for field in fields(Policy)}
    policy = {}
    for name, key in options.items():
        if key not in env:
            continue
        value = (int if name in ("band_min_contacts", "max_points") else float)(env[key])
        # Mirrors touchfilter.daemon.load_config, which migrates each EXACT
        # legacy value to the current default on every load. An older
        # wall.env still carries these four, so treat them as unset: a panel
        # that predates this renders the same as a fresh one.
        if name in LEGACY_DEFAULTS and value == LEGACY_DEFAULTS[name]:
            continue
        # Only a real difference from Policy is emitted; the daemon fills the rest.
        if value != defaults[name]:
            policy[name] = value
    Policy(**policy)
    replay = int(env.get("TOUCH_REPLAY_MAX_MS", 800))
    if not 100 <= replay <= 2000:
        raise ValueError("invalid-replay-limit")
    config = {
        "mode": mode,
        "isolate": isolate == "true",
        "vendor": int(ids[0], 16),
        "product": int(ids[1], 16),
        "name": name,
        "policy": policy,
        "replay_max_ms": replay,
    }
    if mode == "adaptive":
        config.update(adaptive(env, policy))
    # USB identity properties and the input name may live on different sysfs
    # ancestors. ENV+ATTRS avoids invalid multiple-parent ATTRS matching.
    match = f'SUBSYSTEM=="input", KERNEL=="event*", ENV{{ID_VENDOR_ID}}=="{ids[0].lower()}", ENV{{ID_MODEL_ID}}=="{ids[1].lower()}", ATTRS{{name}}=="{name}"'
    physical = match + ', SYMLINK+="input/wall-touchscreen"'
    if config["isolate"]:
        physical += ', ENV{LIBINPUT_IGNORE_DEVICE}="1"'
    rules = (
        physical
        + '\nSUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="OfficeWall Filtered Touchscreen", ENV{ID_INPUT_TOUCHSCREEN}="1", ENV{LIBINPUT_IGNORE_DEVICE}="0"\n'
    )
    return config, rules


if __name__ == "__main__":
    try:
        config, rules = render(os.environ)
        Path(sys.argv[1]).write_text(json.dumps(config, indent=2) + "\n")
        Path(sys.argv[2]).write_text(rules)
    except (ValueError, KeyError, IndexError):
        raise SystemExit("Invalid touch-filter configuration; no input policy changed")
