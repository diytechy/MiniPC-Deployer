#!/usr/bin/env python3
"""Apply the rendered Bluetooth at-rest policy to the adapter. SR-023.

Runs at boot (wall-bluetooth.service) and again whenever a pairing window
closes. Every property here is an org.bluez.Adapter1 D-Bus property, driven
through bluetoothctl rather than by rewriting /etc/bluetooth/main.conf: the
shipped main.conf is stock, and a stock file is worth keeping stock. Nothing in
this script pairs, trusts, connects or removes a device.

IT IS SAFE TO RUN AT ANY TIME, AND THAT IS THE POINT. Closing a pairing window
is the same operation as booting: re-assert the at-rest state. There is no
separate teardown path that could be skipped, and no state kept anywhere but
the adapter itself.
"""

import json
from pathlib import Path
import subprocess
import sys

POLICY = Path("/etc/wall-panel/bluetooth.json")


def bluetoothctl(*args):
    """One bluetoothctl command. Returns True on a clean exit."""
    try:
        done = subprocess.run(("bluetoothctl", *args), capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def apply(policy):
    """Assert the at-rest adapter state. Returns a list of failed operations."""
    failed = []

    # ORDER MATTERS. Powering off last would strand a discoverable adapter, and
    # setting discoverable before the timeout would open an unbounded window --
    # briefly, but on a radio "briefly" is still an opportunity.
    if not bluetoothctl("power", "on" if policy["enabled"] else "off"):
        failed.append("power")
    if not policy["enabled"]:
        # A powered-down adapter has no other state worth asserting, and
        # bluetoothctl will refuse most of it anyway.
        return failed

    if not bluetoothctl("system-alias", policy["alias"]):
        failed.append("alias")

    window = str(policy["pairingWindowSeconds"])
    if not bluetoothctl("discoverable-timeout", window):
        failed.append("discoverable-timeout")
    if not bluetoothctl("pairable-timeout", window):
        failed.append("pairable-timeout")

    if not bluetoothctl("pairable", "yes" if policy["pairableAtRest"] else "no"):
        failed.append("pairable")
    if not bluetoothctl("discoverable", "yes" if policy["discoverableAtRest"] else "no"):
        failed.append("discoverable")
    return failed


def main():
    if not POLICY.exists():
        # Not configured is not a failure: the panel simply keeps whatever BlueZ
        # itself decided, which is what every pre-policy image did.
        print("wall-bluetooth: no policy installed; adapter left as BlueZ set it")
        return 0
    try:
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("wall-bluetooth: policy unreadable; adapter unchanged", file=sys.stderr)
        return 1
    failed = apply(policy)
    if failed:
        print("wall-bluetooth: could not assert " + ", ".join(failed), file=sys.stderr)
        return 1
    state = "open" if policy["pairableAtRest"] else "closed"
    print(f"wall-bluetooth: adapter {policy['alias']}, pairing {policy['pairing']}, door {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
