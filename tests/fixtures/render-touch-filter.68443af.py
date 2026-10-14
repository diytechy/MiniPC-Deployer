#!/usr/bin/env python3
"""Render validated touch-filter JSON and udev rules; Implements: SR-017.

Reads explicitly exported TOUCH_* config, writes only supplied output paths.
Does not grab devices or enable services. The application owns policy validation.
"""

import json
import os
from pathlib import Path
import re
import sys
from touchfilter.core import Policy


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
    options = {
        "min_tap_ms": ("TOUCH_MIN_TAP_MS", 60),
        "band_hold_ms": ("TOUCH_BAND_HOLD_MS", 250),
        "stable_radius": ("TOUCH_STABLE_RADIUS", 0.015),
        "band_window_ms": ("TOUCH_BAND_WINDOW_MS", 2000),
        "band_min_contacts": ("TOUCH_BAND_MIN_CONTACTS", 12),
        "band_min_rate": ("TOUCH_BAND_MIN_RATE", 6),
        "band_max_height": ("TOUCH_BAND_MAX_HEIGHT", 0.025),
        "band_min_width": ("TOUCH_BAND_MIN_WIDTH", 0.20),
        "band_fraction": ("TOUCH_BAND_FRACTION", 0.8),
        "band_padding": ("TOUCH_BAND_PADDING", 0.006),
        "band_ttl_ms": ("TOUCH_BAND_TTL_MS", 60000),
        "startup_ms": ("TOUCH_STARTUP_MS", 1500),
        "max_gesture_ms": ("TOUCH_MAX_GESTURE_MS", 10000),
        "max_points": ("TOUCH_MAX_POINTS", 2048),
    }
    policy = {
        name: (int if name in ("band_min_contacts", "max_points") else float)(
            env.get(key, default)
        )
        for name, (key, default) in options.items()
    }
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
