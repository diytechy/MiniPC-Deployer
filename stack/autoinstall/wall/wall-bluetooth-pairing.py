#!/usr/bin/env python3
"""Open a bounded Bluetooth pairing window, then close it. SR-023.

Usage: wall-bluetooth-pairing [open|close|status|list|forget <MAC>]

WHAT THE WINDOW BOUNDS, AND WHAT IT DOES NOT
--------------------------------------------
The window bounds who can BOND. It does not bound what an already-bonded device
may do afterwards, and saying otherwise -- as earlier comments here did -- is
simply wrong: a phone paired inside a window can reconnect and play at any time
later, with the adapter non-discoverable and non-pairable and no agent running.
That is the feature. It is what makes the panel a speaker rather than a thing
you re-pair every morning.

It is also the residual risk, stated plainly: a device that got through one
window keeps its access until someone takes it away. `list` shows what is
bonded and `forget` revokes one, which is the only way back out.

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
import re
import subprocess
import sys
import time

POLICY = Path("/etc/wall-panel/bluetooth.json")
APPLY = "/usr/local/sbin/wall-bluetooth-apply"
AGENT = "/usr/local/sbin/wall-bluetooth-agent"


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


def start_agent(policy):
    """Register a BlueZ pairing agent for the life of the window.

    wall-bluetooth-agent calls RegisterAgent AND RequestDefaultAgent, and exits
    non-zero if either is refused. That second call is the one this code used to
    skip entirely by shelling out to `bluetoothctl --agent`, which registers an
    agent BlueZ will not route an unsolicited pairing to -- so the window opened,
    announced itself, and then rejected the pairing it was opened for.

    The agent carries its own timeout too, so a window helper that is killed
    without running its cleanup still cannot leave an agent behind. Belt and
    braces, for the same reason the door has two timers.

    Returns the process, or None if no agent could be registered.
    """
    window = policy["pairingWindowSeconds"]
    try:
        agent = subprocess.Popen(
            (AGENT, "--capability", policy["agentCapability"], "--timeout", str(window)),
            stdin=subprocess.DEVNULL, stdout=None, stderr=None,
        )
    except OSError:
        return None
    # NOT A LIVENESS CHECK ON A PROCESS -- that was the bug. `bluetoothctl` is
    # perfectly alive while BlueZ has no agent registered, so watching the
    # process proved nothing and the window would open in front of nothing.
    # wall-bluetooth-agent exits non-zero if RegisterAgent or
    # RequestDefaultAgent fails, so an early exit here is a real signal; a
    # process still running after that has positively registered and said so.
    try:
        agent.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return agent
    return None


def stop_agent(agent):
    """Terminate the agent. Safe to call twice, and on any exit path."""
    if agent is None or agent.poll() is not None:
        return
    agent.terminate()
    try:
        agent.wait(timeout=5)
    except subprocess.TimeoutExpired:
        agent.kill()


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
    # BOTH TIMEOUTS MUST LAND BEFORE THE DOOR OPENS, and this is a hard gate
    # rather than an accumulated flag. It was written as
    # `ok = bluetoothctl("pairable", "yes") and ok`, which evaluates the call
    # FIRST: a failed timeout write still went on to make the adapter pairable,
    # producing exactly the unbounded, agent-less, permanently pairable panel
    # the whole window mechanism exists to prevent.
    if not bluetoothctl("discoverable-timeout", str(window)):
        sys.exit("wall-bluetooth-pairing: discoverable timeout refused; nothing was opened")
    if not bluetoothctl("pairable-timeout", str(window)):
        sys.exit("wall-bluetooth-pairing: pairable timeout refused; nothing was opened")

    # THE AGENT STARTS BEFORE THE DOOR OPENS, not after. Without a registered
    # agent BlueZ has nobody to ask about an incoming pairing, so a phone that
    # connected in the gap would simply fail -- and the person at the panel
    # would be left retrying a window that looks open and is not answering.
    agent = start_agent(policy)
    if agent is None:
        sys.exit("wall-bluetooth-pairing: no pairing agent; nothing was opened")

    if not bluetoothctl("pairable", "yes") or not bluetoothctl("discoverable", "yes"):
        stop_agent(agent)
        if close_window() != 0:
            # Half-open with no way to confirm it closed. Cut the power rather
            # than report a door state nothing has verified.
            bluetoothctl("power", "off")
            sys.exit("wall-bluetooth-pairing: could not open OR close the window; adapter powered off")
        sys.exit("wall-bluetooth-pairing: could not open the window; door re-closed")

    print(f"wall-bluetooth-pairing: open for {window}s as {policy['alias']} "
          f"(agent {policy['agentCapability']})")
    try:
        time.sleep(window)
    except KeyboardInterrupt:
        pass
    finally:
        # THE AGENT DIES WITH THE WINDOW, on every exit path including Ctrl-C.
        # An agent outliving its window is the one leak that would turn
        # "NoInputNoOutput is safe because it is temporary" back into a lie.
        stop_agent(agent)
    # BlueZ's own timeouts have expired by now, so this is the belt to their
    # braces -- but if it fails we cannot claim the door is shut, and an
    # unverified open door is the worst of the states to report as closed.
    if close_window() != 0:
        bluetoothctl("power", "off")
        sys.exit("wall-bluetooth-pairing: could not re-close the window; adapter powered off")
    return 0


def list_bonded():
    """Every device that may reconnect and play without any window being open."""
    try:
        done = subprocess.run(("bluetoothctl", "devices", "Paired"),
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        sys.exit("wall-bluetooth-pairing: adapter unavailable")
    lines = [line for line in done.stdout.splitlines() if line.startswith("Device ")]
    if not lines:
        print("no bonded devices; nothing can connect without a pairing window")
        return 0
    print("bonded -- each of these can reconnect and play at any time:")
    for line in lines:
        print("  " + line[len("Device "):])
    print("revoke with: wall-bluetooth-pairing forget <MAC>")
    return 0


def forget(mac):
    """Revoke a bond. The only way to withdraw what a window granted."""
    if not re.fullmatch(r"(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}", mac or ""):
        sys.exit("wall-bluetooth-pairing: expected a MAC like AA:BB:CC:DD:EE:FF")
    # Disconnect first: removing a device that is actively streaming leaves the
    # sink holding a PCM for a device that no longer exists.
    bluetoothctl("disconnect", mac)
    if not bluetoothctl("remove", mac):
        sys.exit("wall-bluetooth-pairing: bluez refused to remove %s" % mac)
    print("forgotten: %s" % mac)
    return 0


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
    if action not in ("open", "close", "status", "list", "forget"):
        sys.exit("usage: wall-bluetooth-pairing [open|close|status|list|forget <MAC>]")
    if action == "forget":
        return forget(argv[2] if len(argv) > 2 else "")
    if action == "list":
        return list_bonded()
    policy = load()
    if action == "open":
        return open_window(policy)
    if action == "close":
        return close_window()
    return status(policy)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
