#!/usr/bin/env python3
"""Open a bounded Bluetooth pairing window, then close it. SR-023.

Usage: wall-bluetooth-pairing [open|close|status]

The window is bounded THREE times over, deliberately, because the failure this
guards is a panel left permanently pairable:

  1. BlueZ's own DiscoverableTimeout/PairableTimeout, set from the policy, shut
     the door with no help from this script -- so killing it mid-window, or the
     box losing power, still closes the door;
  2. this script sleeps the window out and re-asserts the at-rest state;
  3. `close` re-asserts that state at any time, and is the same code path as
     boot, so there is no teardown that can be skipped.

`pairing=off` refuses to open at all. A window is a deliberate act, but it is
still bounded by the policy the panel was configured with.
"""

import json
from pathlib import Path
import subprocess
import sys
import time

POLICY = Path("/etc/wall-panel/bluetooth.json")
APPLY = "/usr/local/sbin/wall-bluetooth-apply"


def load():
    try:
        return json.loads(POLICY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sys.exit("wall-bluetooth-pairing: no usable policy at " + str(POLICY))


def bluetoothctl(*args):
    try:
        done = subprocess.run(("bluetoothctl", *args), capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def close_window():
    """Re-assert the at-rest state -- the same operation the boot service runs."""
    try:
        return subprocess.run((APPLY,), timeout=30).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def open_window(policy):
    if not policy["enabled"]:
        sys.exit("wall-bluetooth-pairing: adapter is disabled by policy")
    if policy["pairing"] == "off":
        sys.exit("wall-bluetooth-pairing: pairing is off by policy; nothing was opened")

    window = policy["pairingWindowSeconds"]
    # Timeouts BEFORE the door opens, so the window is bounded from the instant
    # it exists rather than a few milliseconds later.
    ok = bluetoothctl("discoverable-timeout", str(window))
    ok = bluetoothctl("pairable-timeout", str(window)) and ok
    ok = bluetoothctl("pairable", "yes") and ok
    ok = bluetoothctl("discoverable", "yes") and ok
    if not ok:
        close_window()
        sys.exit("wall-bluetooth-pairing: could not open the window; door re-closed")

    print(f"wall-bluetooth-pairing: open for {window}s as {policy['alias']}")
    # NOTE FOR WHOEVER WIRES THE UI TO THIS: no agent is registered on this image
    # yet, so a pairing completed inside this window is a Just Works pairing and
    # nobody is asked to confirm anything. The window bounds WHEN that can
    # happen, not WHETHER it is confirmed. See render-bluetooth.py.
    try:
        time.sleep(window)
    except KeyboardInterrupt:
        pass
    return close_window()


def status(policy):
    try:
        done = subprocess.run(("bluetoothctl", "show"), capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        sys.exit("wall-bluetooth-pairing: adapter unavailable")
    live = {key.strip(): value.strip()
            for key, _, value in (line.partition(":") for line in done.stdout.splitlines())
            if key.strip() in ("Alias", "Powered", "Discoverable", "Pairable")}
    print(f"policy: pairing={policy['pairing']} window={policy['pairingWindowSeconds']}s "
          f"alias={policy['alias']}")
    print("adapter: " + " ".join(f"{k}={v}" for k, v in sorted(live.items())))
    return 0


def main(argv):
    action = argv[1] if len(argv) > 1 else "status"
    if action not in ("open", "close", "status"):
        sys.exit("usage: wall-bluetooth-pairing [open|close|status]")
    policy = load()
    if action == "open":
        return open_window(policy)
    if action == "close":
        return close_window()
    return status(policy)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
