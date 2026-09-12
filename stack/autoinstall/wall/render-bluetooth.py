#!/usr/bin/env python3
"""Render validated Bluetooth adapter policy; Implements: SR-017, SR-023.

Reads explicitly exported WALL_BLUETOOTH_* config and writes only the supplied
output path. Does not touch the adapter, register an agent, or start a service.

WHY THE AT-REST STATE IS CLOSED, AND WHY THAT IS NOT TIMIDITY
-------------------------------------------------------------
A wall panel is a fixed, unattended radio in a room. `Pairable` is not a
convenience toggle on such a box: BlueZ will complete a Just Works pairing with
NO user interaction whenever an adapter is pairable and no agent is registered
to demand otherwise. Left on, the panel silently accepts whoever asks, from
whatever side of the wall, and the SR-023 broker's careful, journaled,
authorization-gated `pair` method is bypassed entirely -- it governs the panel
reaching OUT, not the front door standing open.

So the default is `window`: closed at rest, opened deliberately for a bounded
number of seconds, and closed again by BlueZ's own timeout even if nothing
tidies up after it. `always` exists because a bench or a kiosk in a locked room
is a real case, but it is a decision someone has to type, not one they inherit.

A PAIRING WINDOW WITHOUT AN AGENT IS STILL AN OPEN DOOR. `agent_capability` is
rendered here so the policy is complete and reviewable, but nothing in this
image registers an agent yet; `pairing=always` without one is refused outright
rather than quietly shipped, because that combination is the unattended-silent-
pairing case and the only one where the window cannot limit the damage.
"""

import json
import os
from pathlib import Path
import re
import sys


# BlueZ agent capabilities. DisplayYesNo is the honest one for a panel that has
# a screen and a touch digitizer: it can show a passkey and take a confirmation.
# NoInputNoOutput is what produces silent Just Works pairing and is deliberately
# not offered -- a panel that can ask has no business not asking.
AGENT_CAPABILITIES = ("DisplayYesNo", "DisplayOnly", "KeyboardDisplay")
PAIRING_MODES = ("off", "window", "always")


def _flag(env, key, default):
    value = env.get(key, default)
    if value not in ("true", "false"):
        raise ValueError("invalid-bluetooth-flag")
    return value == "true"


def render(env):
    """Return the adapter policy for one panel, or raise ValueError."""
    enabled = _flag(env, "WALL_BLUETOOTH_ENABLED", "true")

    alias = env.get("WALL_BLUETOOTH_ALIAS", "wall-panel")
    # The alias is broadcast to every device in range, so it is held to a plain
    # charset: no control characters, no wildcards, nothing that would need
    # quoting by whatever consumes it later.
    if not re.fullmatch(r"[A-Za-z0-9 _.-]{1,32}", alias):
        raise ValueError("invalid-bluetooth-alias")

    pairing = env.get("WALL_BLUETOOTH_PAIRING", "window")
    if pairing not in PAIRING_MODES:
        raise ValueError("invalid-bluetooth-pairing-mode")

    window = int(env.get("WALL_BLUETOOTH_PAIRING_WINDOW_SECONDS", 120))
    # Long enough to walk to the panel and finish a pairing, short enough that
    # forgetting about it is not a standing invitation. BlueZ takes the same
    # value as its own DiscoverableTimeout, so the door shuts even if the helper
    # that opened it dies first.
    if not 30 <= window <= 600:
        raise ValueError("invalid-bluetooth-pairing-window")

    capability = env.get("WALL_BLUETOOTH_AGENT_CAPABILITY", "DisplayYesNo")
    if capability not in AGENT_CAPABILITIES:
        raise ValueError("invalid-bluetooth-agent-capability")

    at_rest = _flag(env, "WALL_BLUETOOTH_DISCOVERABLE_AT_REST", "false")
    if at_rest and pairing == "off":
        # Advertising a panel that will refuse every pairing is a trap for the
        # person holding the phone, not a security posture.
        raise ValueError("discoverable-at-rest-requires-pairing")

    if pairing == "always":
        # See the module docstring. This is the one combination whose blast
        # radius the window cannot bound, so it is refused until an agent
        # actually exists to demand a confirmation.
        raise ValueError("pairing-always-requires-a-registered-agent")

    return {
        "enabled": enabled,
        "alias": alias,
        "pairing": pairing,
        "pairingWindowSeconds": window,
        "agentCapability": capability,
        # The at-rest adapter state. `window` is closed here BY DEFINITION --
        # the window is opened later, by wall-bluetooth-pairing, never at boot.
        "discoverableAtRest": at_rest,
        "pairableAtRest": pairing == "always",
    }


if __name__ == "__main__":
    try:
        policy = render(os.environ)
        Path(sys.argv[1]).write_text(json.dumps(policy, indent=2) + "\n")
    except (ValueError, KeyError, IndexError):
        raise SystemExit("Invalid Bluetooth configuration; no adapter policy changed")
