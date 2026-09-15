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

THE AGENT LIVES AND DIES WITH THE WINDOW. wall-bluetooth-pairing runs
wall-bluetooth-agent for exactly the window's duration, so outside a window
there is no agent AND the adapter is neither pairable nor discoverable -- two
independent reasons nothing NEW can bond. Inside one, a person is standing at
the panel having deliberately opened it. That is the consent, in the absence of
any pairing UI in the shell to display a passkey.

SAID PRECISELY, BECAUSE AN EARLIER VERSION OF THIS COMMENT WAS WRONG: what the
window bounds is BONDING, not use. A device that paired inside a window can
reconnect and play afterwards at any time, with the adapter closed and no agent
running -- which is exactly what makes the panel a speaker instead of something
you re-pair every morning. The residual risk is that a device which got through
one window keeps its access until someone takes it away;
`wall-bluetooth-pairing list` and `... forget <MAC>` are the way back out.
"""

import json
import os
from pathlib import Path
import re
import sys


# BlueZ agent capabilities.
#
# NoInputNoOutput IS THE DEFAULT, AND THE CONSENT LIVES SOMEWHERE ELSE. An
# earlier revision refused this capability on the grounds that it produces
# silent Just Works pairing -- true, and it was the right call while no agent
# existed at all, because "pairable with no agent" means the adapter accepts
# whoever asks, indefinitely, with nothing anywhere recording that it happened.
#
# What makes it safe now is that the agent EXISTS ONLY INSIDE THE PAIRING
# WINDOW. wall-bluetooth-pairing starts it, the window closes, the agent exits,
# and outside that window the adapter is neither pairable nor discoverable. The
# consent is a person standing at the panel opening a bounded window on purpose
# -- the same model as the pairing button on any speaker -- rather than a
# passkey nobody can display, because this shell has no pairing UI to display
# one in. Adding that UI is what would make DisplayYesNo meaningful, and it is
# kept here for exactly that.
AGENT_CAPABILITIES = ("NoInputNoOutput", "DisplayYesNo", "DisplayOnly", "KeyboardDisplay")
PAIRING_MODES = ("off", "window", "always")


def _flag(env, key, default):
    value = env.get(key, default)
    if value not in ("true", "false"):
        raise ValueError("invalid-bluetooth-flag")
    return value == "true"


def render(env):
    """Return the adapter policy for one panel, or raise ValueError."""
    # TRUE, matching wall.env.example -- and the two are pinned together by a
    # test, because they disagreed once and a code default that contradicts the
    # documented one is how every panel in the field ends up in a state nobody
    # chose. Enabled means the adapter is powered and the A2DP sink runs; it
    # does NOT mean anything can pair, which is what `pairing` below governs.
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

    capability = env.get("WALL_BLUETOOTH_AGENT_CAPABILITY", "NoInputNoOutput")
    if capability not in AGENT_CAPABILITIES:
        raise ValueError("invalid-bluetooth-agent-capability")

    at_rest = _flag(env, "WALL_BLUETOOTH_DISCOVERABLE_AT_REST", "false")
    if at_rest and pairing == "off":
        # Advertising a panel that will refuse every pairing is a trap for the
        # person holding the phone, not a security posture.
        raise ValueError("discoverable-at-rest-requires-pairing")

    if pairing == "always":
        # STILL REFUSED, and now for a sharper reason than before. The agent is
        # window-scoped by design, so `always` would mean an adapter that is
        # permanently pairable with an agent that is usually absent: pairing
        # attempts fail confusingly most of the time, and whenever a window did
        # happen to be open the door would already have been open for hours.
        # There is no state in which `always` is the safer or the more useful
        # setting, so it stays a refusal rather than a footgun with a comment.
        raise ValueError("pairing-always-is-refused-the-window-is-the-consent")

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
