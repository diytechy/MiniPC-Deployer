#!/usr/bin/env python3
"""The root half of WSN-024: BlueZ device management for the wall panel.

Usage: wall-bluetooth-device [publish|apply-request [PATH]|status]

WHY THIS EXISTS. The audio broker the renderer talks to runs as `panel` under
ProtectSystem=strict with PrivateDevices=true and AF_UNIX as its only address
family (SR-023). BlueZ refuses an unprivileged `Device1.Pair`, a write to
`Trusted`, or `Adapter1.StartDiscovery`, and nothing in this design asks for
that to change. So the broker writes ONE request file and this script -- started
by `wall-bluetooth-device-apply.path` -- is what answers it, exactly as
`wall-audio-output apply-request` answers the switch.

TWO DIRECTIONS, TWO DOCUMENTS.

  * `publish` writes /run/wall-bluetooth/state.json, which the broker READS. It
    carries aliases and facts and NEVER A HARDWARE ADDRESS -- the alias-to-MAC
    map stays on this side, re-derived from BlueZ by the same pure function the
    document was built with (bluetooth_state.assign_aliases). That is what makes
    "no address crosses IF-015" a property of the architecture rather than a
    promise somebody has to keep remembering.
  * `apply-request` DRAINS the broker's spool directory in sequence order,
    deduplicates by sequence, performs each request, and republishes. A spool
    rather than one mailbox file because device verbs are not idempotent: two
    taps before the path unit fires used to leave the first request overwritten
    and reported as landed.

WHAT IT DOES NOT DECIDE. The pairing WINDOW is still `wall-bluetooth-pairing`'s,
and `set_discoverable` calls it rather than re-implementing it: that script
already bounds the window three ways over (BlueZ's own timeouts, its own sleep,
and a `close` that is the same code path as boot), and a second opener would be
a second way for the panel to be left permanently pairable. The AGENT is still
`wall-bluetooth-agent`, which decides what a pairing consents to.

THE RESIDUAL RISK IS UNCHANGED AND IS STATED IN wall-bluetooth-pairing: a device
that got through one window keeps its access until someone takes it away. What
this script adds is the taking away -- `forget` and `trust false` are now
reachable from the glass instead of from an SSH session.

Contract:
  Inputs:  the broker's request file; BlueZ, through `bluetoothctl`
  Outputs: /run/wall-bluetooth/state.json (0644), /run/wall-bluetooth/route.json
  Config:  /etc/wall-panel/bluetooth.json (the policy), WALL_BLUETOOTH_*
  Raises:  nothing to the caller; every failure is a journal line and a
           republished document
Implements: SR-023, LLR-007 (WSN-024)
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

# THE PURE LAYER IS SHARED, NOT COPIED. `bluetooth_state` defines how a BlueZ
# device becomes an alias, and both sides of the privilege boundary must agree
# or the renderer names a device this script cannot find. /opt/wall-panel is
# root-owned with nothing group- or world-writable -- the autoinstall's step 3a
# refuses to finish an install where that is not true -- so importing from it as
# root is not a privilege hole.
#
# `switch_backend` and its applier chose to DUPLICATE their shared normalization
# and pin the copies with a test, because the applier's tree cannot import the
# broker's. This one can: the payload has carried stack/panel-audio since
# 2026-09-17, and one definition beats two definitions and a test that they
# agree.
PANEL_AUDIO = Path(os.environ.get("WALL_PANEL_AUDIO_DIR") or "/opt/wall-panel/stack/panel-audio")
sys.path.insert(0, str(PANEL_AUDIO))
try:
    import bluetooth_request
    import bluetooth_state
except ImportError:  # pragma: no cover - firstboot installs the pair together
    bluetooth_request = None
    bluetooth_state = None

STATE_DIR = Path(os.environ.get("WALL_BLUETOOTH_RUN_DIR") or "/run/wall-bluetooth")
STATE_PATH = STATE_DIR / "state.json"
ROUTE_PATH = STATE_DIR / "route.json"
# Written by the agent while a pairing is in flight; see wall-bluetooth-agent.
PAIRING_PATH = STATE_DIR / "pairing.json"
# The passkey the PANEL supplied, for a device that asks us to type one.
PASSKEY_PATH = STATE_DIR / "passkey.json"
MARK_PATH = STATE_DIR / "mark.json"
LOCK_PATH = STATE_DIR / "drain.lock"
DISCOVERY_PID_PATH = STATE_DIR / "discovery.pid"

DEFAULT_REQUEST_DIR = Path("/run/wall-audio-router/bluetooth-requests")
PAIRING_TOOL = os.environ.get("WALL_BLUETOOTH_PAIRING_TOOL") or "/usr/local/sbin/wall-bluetooth-pairing"

# Long enough for a real bond over a radio, short enough that the oneshot unit
# does not look hung. Measured against the panel's own pairing runs: a Pixel
# completes in about four seconds, a cheap speaker in about twelve.
PAIR_TIMEOUT_SECONDS = 40
# Everything else is a property write or a local D-Bus round trip.
CONTROL_TIMEOUT_SECONDS = 15
# Reading one device's properties is the cheapest thing here and is done once per
# device per publish, so it gets the tightest bound: at the control timeout, two
# stalled devices are already past the broker's staleness budget.
INFO_TIMEOUT_SECONDS = 4

ADDRESS = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")
# The broker's own HARDWARE_ADDRESS, repeated because this side does not import
# `routing`. Anything matching it is refused there, so anything matching it must
# not be written here -- one such value fails the WHOLE document and takes every
# other device's row with it.
ADDRESSISH = re.compile(
    r"(?i)(?<![0-9a-f])(?:(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|"
    r"(?:[0-9a-f]{2}_){5}[0-9a-f]{2}|(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}|"
    r"[0-9a-f]{12})(?![0-9a-f])"
)

# The whole-publish budget, well under the broker's thirty-second staleness
# bound, so a slow adapter produces an honest "do not know" rather than a
# document that arrives already expired.
PUBLISH_BUDGET_SECONDS = 12


def log(message):
    print("[wall-bluetooth-device] %s" % message, flush=True)


# ---------------------------------------------------------------- bluetoothctl


def _run(argv, timeout):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def bluetoothctl(*args, timeout=CONTROL_TIMEOUT_SECONDS):
    """Run one bluetoothctl command. Returns (ok, stdout).

    OK IS NOT ITS EXIT STATUS ALONE. bluetoothctl exits 0 for a great many
    refusals and prints the refusal instead -- `pair` on a device that says no
    is the common one -- so the output is searched for BlueZ's own failure
    words. Trusting the exit status was the first version and it reported every
    failed pairing as a success, which the renderer then drew as a paired
    device that could not connect.
    """
    done = _run(["bluetoothctl", *args], timeout)
    if done is None:
        return False, ""
    text = (done.stdout or "") + (done.stderr or "")
    if done.returncode != 0:
        return False, text
    lowered = text.lower()
    if "failed" in lowered or "not available" in lowered or "org.bluez.error" in lowered:
        return False, text
    return True, text


def adapter():
    """(reason, powered, discoverable, pairable) for the adapter.

    `reason` is one of the tokens js/views/bluetooth.js already renders a
    sentence for, so a new state here cannot fall through to a generic message.
    """
    if shutil.which("bluetoothctl") is None:
        return "unavailable", False, False, False
    done = _run(["bluetoothctl", "show"], CONTROL_TIMEOUT_SECONDS)
    if done is None:
        return "unavailable", False, False, False
    if done.returncode != 0 or "No default controller" in (done.stdout or "") + (done.stderr or ""):
        return "no-adapter", False, False, False
    fields = _fields(done.stdout or "")
    powered = fields.get("Powered") == "yes"
    return ("ready" if powered else "powered-off", powered,
            fields.get("Discoverable") == "yes", fields.get("Pairable") == "yes")


def _fields(text):
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip()] = value.strip()
    return values


def known_addresses(deadline=None):
    """Every address bluetoothctl will talk about, or None if it took too long.

    The listing is inside the publish budget too. It was outside it, which meant
    the worst case was the control timeout PLUS the whole budget PLUS one probe
    -- past the thirty seconds the broker treats as the document's shelf life,
    so a slow adapter produced a document that was stale the moment it landed.
    """
    budget = CONTROL_TIMEOUT_SECONDS
    if deadline is not None:
        budget = max(1, min(budget, int(deadline - time.monotonic())))
    ok, text = bluetoothctl("devices", timeout=budget)
    if not ok:
        return [] if deadline is None else None
    found = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Device" and ADDRESS.fullmatch(parts[1].upper()):
            found.append(parts[1].upper())
    return found


def safe_name(name, alias):
    """A display name with no hardware address in it.

    BLUEZ NAMES AN UNRESOLVED DEVICE AFTER ITS OWN ADDRESS, so `name` really can
    be `AA:BB:CC:DD:EE:FF` -- and `name` goes into the document the broker reads
    (terra, 2026-09-19, finding 2). Sanitising the ALIAS was not enough: the
    address crossed the boundary in the field beside it. The broker's own
    validator would then reject the whole document, which is the second half of
    the same bug -- one unresolved device made every OTHER device's row vanish.

    The alias is the substitute rather than a placeholder, because the alias is
    what the person is looking at anyway.
    """
    text = str(name or "")
    if not text or ADDRESSISH.search(text):
        return alias
    return text[:96]


def device_info(address):
    """The facts about one device, or None when BlueZ will not say."""
    ok, text = bluetoothctl("info", address, timeout=INFO_TIMEOUT_SECONDS)
    if not ok:
        return None
    fields = _fields(text)
    uuids = re.findall(r"UUID:\s*[^(]*\(([0-9a-fA-F-]+)\)", text)
    battery = None
    match = re.search(r"Battery Percentage:.*?\((\d{1,3})\)", text)
    if match and 0 <= int(match.group(1)) <= 100:
        battery = int(match.group(1))
    return {
        "address": address,
        # `Alias` is what the person renamed it to and `Name` is what it calls
        # itself; the rename is the more useful one and BlueZ always supplies a
        # fallback, so Alias wins and Name is the backstop.
        "name": fields.get("Alias") or fields.get("Name") or "",
        "paired": fields.get("Paired") == "yes",
        "trusted": fields.get("Trusted") == "yes",
        "connected": fields.get("Connected") == "yes",
        "uuids": uuids,
        "battery": battery,
    }


def _expired(deadline):
    return deadline is not None and time.monotonic() > deadline


def inventory(deadline=None):
    """[(address, info)] for every device BlueZ knows, or None on a deadline.

    NONE IS NOT AN EMPTY ROOM. The broker treats a document older than thirty
    seconds as "do not know", and one `bluetoothctl info` per device at the
    control timeout can blow that budget with two stalled devices and blow it by
    minutes with a crowded one (terra, 2026-09-19, finding 7). A publish that
    cannot finish inside its budget must say it does not know, because the
    alternative -- publishing the devices it managed to read as if that were the
    list -- hides a wedged adapter behind a short but plausible inventory, and a
    device whose row silently vanished is exactly what somebody would be
    standing at the panel trying to pair.
    """
    devices = []
    addresses = known_addresses(deadline)
    if addresses is None or _expired(deadline):
        log("listing the devices exceeded the deadline; publishing 'do not know'")
        return None
    for address in addresses:
        if _expired(deadline):
            log("inventory exceeded its deadline; publishing 'do not know'")
            return None
        info = device_info(address)
        if info is not None:
            devices.append(info)
    # CHECKED AGAIN AFTER THE LAST PROBE. Checking only BEFORE each one lets the
    # final probe run its whole timeout past the deadline and then return the
    # list as though it had been gathered in time -- with the timed-out device
    # silently missing from it, which is the one row somebody would be standing
    # at the panel looking for.
    if _expired(deadline):
        log("the last probe ran past the deadline; publishing 'do not know'")
        return None
    return devices


# ------------------------------------------------------------------- documents


def write_document(path, payload, mode=0o644):
    """One atomic write. A torn document is a device list nobody chose."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    text = json.dumps(payload, sort_keys=True) + "\n"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def read_document(path, default=None):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return value if isinstance(value, dict) else default


def mark():
    """The applier's epoch and the last sequence it consumed.

    The epoch is the BOOT: /run does not survive one, so a document that is not
    there means this is a new life of the applier and every sequence from the
    last one is meaningless. That is the same shape as the switch applier's
    epoch marker and for the same reason -- `seq` orders requests inside one
    life and cannot order across a restart.
    """
    stored = read_document(MARK_PATH, {}) or {}
    generation = stored.get("generation")
    request_seq = stored.get("requestSeq")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        generation = 0
    if isinstance(request_seq, bool) or not isinstance(request_seq, int) or request_seq < 0:
        request_seq = None
    return generation, request_seq


def remember(seq):
    generation, _ = mark()
    write_document(MARK_PATH, {"generation": generation, "requestSeq": int(seq)})


def route():
    stored = read_document(ROUTE_PATH, {}) or {}
    return {side: stored.get(side) if ADDRESS.fullmatch(str(stored.get(side) or "").upper()) else None
            for side in ("input", "output")}


def set_route(side, address):
    current = route()
    current[side] = address
    write_document(ROUTE_PATH, current)
    return current


def pairing_block(aliases):
    """What the agent is doing right now, in the panel's vocabulary.

    The agent writes an ADDRESS; this turns it into the alias the renderer
    holds, and drops the block entirely if that address is not in the current
    inventory -- a passkey attributed to a device the panel cannot name is a
    number with nothing to compare it to.
    """
    _, powered, discoverable, _ = adapter()
    stored = read_document(PAIRING_PATH, {}) or {}
    expires = stored.get("expiresAt")
    remaining = None
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        remaining = max(0, min(600, int(expires - time.time())))
    active = bool(remaining) and remaining > 0
    passkey = stored.get("passkey")
    if not (isinstance(passkey, str) and passkey.isdigit() and 1 <= len(passkey) <= 16):
        passkey = None
    device = aliases.get(str(stored.get("address") or "").upper())
    return {
        "active": active,
        "discoverable": powered and discoverable,
        # A passkey outlives nothing: once the window it belongs to has closed
        # the number is not a thing anybody can still compare, and leaving it on
        # the glass invites comparing it against the NEXT pairing.
        "passkey": passkey if active else None,
        "device": device if active else None,
        "endsInSeconds": remaining if active else None,
    }


def previous_aliases():
    """The alias each device was last published under.

    STABILITY IS WHY THIS EXISTS. Aliases used to be derived from the current
    device set alone, ordered by address, and that is stable for a FIXED set and
    not otherwise: a second phone also called "Pixel", with a lower address,
    took the bare `pixel` and pushed the first one to `pixel-2` -- so a tap on a
    row painted a moment earlier acted on the other phone (terra, 2026-09-19,
    finding 3). Carrying the previous assignment forward makes an alias a
    device's for as long as the panel keeps seeing it.

    /run and not /var/lib: the window that matters is between a paint and a
    finger, and a reboot repaints from nothing anyway.
    """
    stored = read_document(STATE_PATH, {}) or {}
    held = stored.get("aliasesByAddress")
    if not isinstance(held, dict):
        return {}
    return {str(key).upper(): value for key, value in held.items()
            if isinstance(value, str) and value}


def publish():
    """Write the document the broker reads. Always writes something."""
    deadline = time.monotonic() + PUBLISH_BUDGET_SECONDS
    reason, _powered, _discoverable, _pairable = adapter()
    devices = inventory(deadline) if reason == "ready" else []
    if devices is None:
        # The budget ran out. "Do not know" is the honest answer and is the same
        # one the broker derives from a stale document, so the panel already has
        # a sentence for it.
        reason, devices = "unavailable", []
    aliases = ({} if bluetooth_state is None
               else bluetooth_state.assign_aliases(
                   [(d["address"], d["name"]) for d in devices], previous_aliases()))
    current = route()
    generation, request_seq = mark()
    rows = []
    for info in devices:
        alias = aliases.get(info["address"])
        if not alias:
            continue
        row = {
            "alias": alias,
            # SCRUBBED, then truncated. The name is a person's own words about
            # their phone -- except when BlueZ has not resolved one and names the
            # device after its own address, which is a hardware address in the
            # field beside the alias we were so careful about.
            "name": safe_name(info["name"], alias),
            "kind": bluetooth_state.classify(info["uuids"]),
            # B2: the facts, beside the headline. See bluetooth_state for why
            # both exist rather than one replacing the other.
            "capabilities": bluetooth_state.capabilities(info["uuids"]),
            "trusted": bool(info["trusted"]),
            "connected": bool(info["connected"]),
        }
        if info["battery"] is not None:
            row["battery"] = info["battery"]
        rows.append(row)
    write_document(STATE_PATH, {
        "version": 1,
        "publishedAt": int(time.time()),
        "reason": reason,
        "devices": rows,
        "route": {side: aliases.get(str(current[side] or "").upper()) for side in ("input", "output")},
        "pairing": pairing_block(aliases),
        "generation": generation,
        "requestSeq": request_seq,
        # THE ONE FIELD IN THIS DOCUMENT THE BROKER MUST NOT READ. It is here
        # because this is the file that survives between runs of a oneshot
        # applier, and `previous_aliases` needs it to keep an alias attached to
        # its device. `routed_backend._document` validates the keys it uses and
        # ignores the rest; nothing in the broker, the IPC or the renderer ever
        # looks at this, and the file is 0644 on a single-user appliance whose
        # `panel` account already runs the kiosk.
        "aliasesByAddress": aliases,
    })
    return rows, aliases


def resolve(alias, aliases):
    """The address behind an alias, or None.

    The map is the one `publish` just built from the LIVE inventory, so an alias
    the renderer painted a moment ago and a device that has since gone away
    resolve to None -- and the verb refuses rather than acting on whichever
    device inherited the slug.
    """
    for address, candidate in aliases.items():
        if candidate == alias:
            return address
    return None


# --------------------------------------------------------------------- actions


def stop_discovery():
    """Kill any scan this applier started. Idempotent."""
    stored = read_document(DISCOVERY_PID_PATH, {}) or {}
    pid = stored.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1:
        try:
            os.kill(pid, 15)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        DISCOVERY_PID_PATH.unlink()
    except OSError:
        pass


def start_discovery(seconds):
    """Scan for `seconds`, without holding this oneshot unit open for them.

    DETACHED ON PURPOSE. `bluetoothctl --timeout N scan on` blocks for the whole
    window, and BlueZ stops the discovery when the CLIENT disconnects -- so the
    scan cannot simply be started and left. Running it as a background child and
    remembering its pid gives a scan that really lasts N seconds and a `cancel`
    that really stops it, while the apply unit returns immediately. A oneshot
    unit that sat on the radio for two minutes would hold every later request
    behind it, including the `cancel` meant to end this one.
    """
    stop_discovery()
    try:
        child = subprocess.Popen(
            ["bluetoothctl", "--timeout", str(int(seconds)), "scan", "on"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError as error:
        log("discovery could not start: %s" % error)
        return False
    write_document(DISCOVERY_PID_PATH, {"pid": child.pid, "endsAt": int(time.time()) + int(seconds)})
    log("scanning for %ds (pid %d)" % (int(seconds), child.pid))
    return True


def pairing_window(enabled):
    """Open or close the bounded pairing window, through the tool that owns it."""
    if not Path(PAIRING_TOOL).exists():
        log("no pairing tool at %s; discoverability is unavailable" % PAIRING_TOOL)
        return False
    done = _run([PAIRING_TOOL, "open" if enabled else "close"], CONTROL_TIMEOUT_SECONDS)
    if done is None or done.returncode != 0:
        log("pairing window %s refused" % ("open" if enabled else "close"))
        return False
    return True


def offer_passkey(value):
    """Leave a passkey where the agent's RequestPasskey will find it.

    Written BEFORE the pair is attempted and removed after, because it is a
    single-use answer to a question that has not been asked yet. A passkey left
    behind would be offered to the NEXT device that asks for one, which is a
    device nobody authorized typing a number into.
    """
    if value is None:
        try:
            PASSKEY_PATH.unlink()
        except OSError:
            pass
        return
    write_document(PASSKEY_PATH, {"passkey": str(value), "offeredAt": int(time.time())}, mode=0o600)


def perform(event, aliases):
    """One validated event. Returns True when it did what it said.

    EVERY BRANCH RETURNS A BOOLEAN AND NONE OF THEM RAISES. This runs from a
    oneshot unit whose failure is a red unit an operator has to notice; the
    thing that actually tells the person at the glass what happened is the
    republished document, so a refusal is a journal line and a `False`.
    """
    kind = event["kind"]
    if kind == "discover":
        return start_discovery(event["timeoutSeconds"])
    if kind == "cancel":
        stop_discovery()
        # Cancel means "stop what you opened", and that is both of them: a
        # person who taps Cancel during pairing means the window as well as the
        # scan. Closing an already-closed window is the tool's own boot path.
        pairing_window(False)
        return True
    if kind == "set_discoverable":
        return pairing_window(bool(event["enabled"]))

    address = resolve(event["alias"], aliases)
    if address is None:
        log("alias %r is not in the current inventory" % event["alias"])
        return False

    if kind == "pair":
        offer_passkey(event.get("confirmation"))
        try:
            ok, text = bluetoothctl("pair", address, timeout=PAIR_TIMEOUT_SECONDS)
        finally:
            offer_passkey(None)
        if not ok:
            log("pair refused: %s" % _quiet(text))
            return False
        # TRUST FOLLOWS A PAIRING THE PANEL ASKED FOR, and that is a decision
        # rather than a convenience: a bond without trust works until the device
        # leaves the room and then never reconnects, which reads as "Bluetooth
        # is broken" and, before the `trust` verb existed, had no control
        # anywhere to fix it. A pairing somebody deliberately started at the
        # glass is consent for the device to come back.
        trusted, _ = bluetoothctl("trust", address)
        if not trusted:
            log("paired, but the device could not be trusted; it will not reconnect on its own")
        return True
    if kind == "trust":
        ok, text = bluetoothctl("trust" if event["trusted"] else "untrust", address)
        if not ok:
            log("trust change refused: %s" % _quiet(text))
        return ok
    if kind in ("connect", "disconnect"):
        ok, text = bluetoothctl(kind, address, timeout=PAIR_TIMEOUT_SECONDS)
        if not ok:
            log("%s refused: %s" % (kind, _quiet(text)))
        return ok
    if kind == "forget":
        ok, text = bluetoothctl("remove", address)
        if not ok:
            log("forget refused: %s" % _quiet(text))
            return False
        # A forgotten device must not stay selected. Leaving it in the route
        # would make the next boot try to restore a device that no longer
        # exists, and `choose_restored_route` would have nothing to refuse it
        # with because the route is this side's document, not the inventory.
        for side in ("input", "output"):
            if route()[side] == address:
                set_route(side, None)
                apply_route(side, None)
        return True
    if kind in ("select_input", "select_output"):
        side = kind.split("_", 1)[1]
        set_route(side, address)
        return apply_route(side, address)
    log("unknown event kind %r" % kind)
    return False


def _quiet(text):
    """A bluetoothctl failure line, with anything address-shaped removed.

    This reaches the JOURNAL and not the renderer, so it is not an IF-015
    boundary -- but an address in a log that an operator pastes into a ticket is
    the same address either way, and the panel's own rule is that it does not
    write them down.
    """
    line = " ".join((text or "").split())[:200]
    return re.sub(r"(?i)(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", "<device>", line)


def apply_route(side, address):
    """Make the selected device the one the audio graph actually uses.

    TWO DIFFERENT MECHANISMS, because the two directions are two different
    things on this panel and pretending otherwise would be the interesting kind
    of wrong:

      * OUTPUT is an extra leg off the merged bus into BlueALSA. `bus_monitor`
        is a dsnoop and takes as many readers as ask, so this leg runs BESIDE
        the Headset or Speaker leg rather than instead of it. It deliberately
        does NOT become a fourth position of the Mute/Headset/Speaker switch:
        that enum is validated in six places and is the control the person at
        the glass is holding, and widening it to carry a Bluetooth device would
        put the room's music inside a radio's failure modes.
      * INPUT is a PREFERENCE handed to `wall-bt-call`, which already supervises
        the HFP SCO leg and already has to choose when two phones are connected.
        It is not a unit this script starts: an SCO PCM exists only while a call
        is up, which is why that supervisor exists at all.

    Both are best-effort and say so. A refusal leaves the route recorded --
    the Owner's choice stands -- and the next connection retries it.
    """
    if side == "input":
        # `wall-bt-call` re-reads the route document on its own poll; there is
        # nothing to restart and nothing that could fail here.
        log("input preference recorded")
        return True
    unit = "wall-bus-bluetooth.service"
    if address is None:
        _run(["systemctl", "stop", unit], CONTROL_TIMEOUT_SECONDS)
        return True
    _write_env(Path("/run/wall-panel/audio-bluetooth.env"), {"WALL_BT_OUTPUT_DEV": address})
    done = _run(["systemctl", "restart", unit], CONTROL_TIMEOUT_SECONDS)
    if done is None or done.returncode != 0:
        log("the Bluetooth output leg would not start; the selection is recorded but silent")
        return False
    return True


def _write_env(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join("%s=%s\n" % (key, value) for key, value in sorted(values.items()))
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(body, encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


# ---------------------------------------------------------------------- driver


def drain_lock():
    """An exclusive hold on the drain, or None if somebody else has it.

    TWO DRAINS MUST NOT RUN AT ONCE. The path unit triggers one and the observe
    timer triggers another -- different units, so systemd will happily run both
    -- and two processes reading the same spool would perform the same request
    twice. Non-blocking: a drain that is already running is going to do the work
    anyway, so the second caller has nothing to wait for.

    `fcntl` is POSIX-only and this file's unit tests run on the development
    Windows box, so it is imported here and its absence means "no lock". That is
    safe where it happens: the tests drive the drain directly and serially, and
    the panel always has fcntl.
    """
    try:
        import fcntl
    except ImportError:
        return None
    try:
        handle = open(LOCK_PATH, "w", encoding="utf-8")
    except OSError as error:
        # NOT "somebody else has it" (terra round 3). Mapping every OSError to
        # contention meant a /run that could not be written looked exactly like a
        # drain already in progress, and the caller exited 0 -- so the spool was
        # never processed and the unit reported success, forever. Failing is the
        # honest answer and it is also the RECOVERABLE one: the unit goes red
        # where an operator can see it, and the observe timer tries again in ten
        # seconds.
        log("cannot open %s: %s" % (LOCK_PATH, error))
        raise
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # This one really is contention: LOCK_NB fails with EWOULDBLOCK/EACCES
        # when another process holds it, and there is nothing else flock can
        # fail with on a descriptor we just opened.
        handle.close()
        return False
    return handle


def apply_request(directory):
    """Drain the spool in sequence order. Returns an exit code.

    ONE PUBLISH AT THE END, not one per request: the document is what the panel
    reads, and republishing between two requests of a burst only paints a state
    nobody asked to see.

    THE BOUND IS A SAFETY NET AND MUST NOT BE A TRAP (terra round 2, finding 1).
    Stopping at MAX_DRAIN leaves the directory non-empty, and whether
    `DirectoryNotEmpty=` fires again for a directory that was already non-empty
    when the unit started is a systemd detail this code should not be betting on.
    The same question applies to a file `_consume` could not unlink. So the
    observe timer drains too (`observe`), which makes ten seconds the worst case
    for a stranded request regardless of what the path unit does.
    """
    try:
        lock = drain_lock()
    except OSError:
        # Said once, here, rather than swallowed: the spool is untouched and the
        # exit code is what makes that visible.
        log("the drain could not be serialized; the spool is untouched")
        return 1
    if lock is False:
        log("another drain holds the lock; leaving the spool to it")
        return 0
    try:
        _rows, aliases = publish()
        spooled = bluetooth_request.pending(directory) if bluetooth_request else []
        if not spooled:
            log("nothing spooled in %s" % directory)
            return 0
        for path in spooled[:bluetooth_request.MAX_DRAIN]:
            aliases = _apply_one(path, aliases)
        remaining = bluetooth_request.pending(directory)
        if remaining:
            log("%d request(s) still spooled; the observe timer will take them"
                % len(remaining))
        publish()
        return 0
    finally:
        if lock:
            lock.close()


def _apply_one(path, aliases):
    """One spooled request: deduplicate, perform, consume. Returns the aliases
    to use for the next one -- a `forget` changes the inventory under it."""
    request = read_document(Path(path))
    if request is None:
        log("no readable request at %s" % path)
        _consume(path)
        return aliases
    if request.get("version") != 1:
        log("request version %r is not one this applier understands" % request.get("version"))
        _consume(path)
        return aliases
    seq = request.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        log("request carries no usable sequence")
        _consume(path)
        return aliases
    generation, last = mark()
    # SCOPED TO AN EPOCH WHEN THE BROKER NAMED ONE. An unscoped request is
    # accepted and journalled, which is what an older broker sends and what a
    # broker that could not read the document sends; a request naming a
    # DIFFERENT epoch is one from a previous life of this applier and its
    # sequence means nothing here.
    scoped = request.get("generation")
    if scoped is not None and scoped != generation:
        log("request is scoped to epoch %r; this applier is at %d" % (scoped, generation))
        _consume(path)
        return aliases
    # DEDUPLICATED, because systemd.path fires again for a rewrite of an
    # identical request and a replayed `forget` revokes a device somebody just
    # re-paired. The switch applier carries the same guard for the same reason.
    if last is not None and seq <= last:
        log("request %d is not newer than %d; ignored" % (seq, last))
        _consume(path)
        return aliases
    event = request.get("event")
    if not isinstance(event, dict):
        log("request carries no event")
        _consume(path)
        return aliases
    # THE MARK MOVES BEFORE THE ACTION, NOT AFTER. If this process dies halfway
    # through a pairing, the request must not be replayed when the unit is
    # started again by the same file: a half-finished bond re-attempted is the
    # sticky case wall-bluetooth-pairing's docstring describes, and the person
    # at the glass can simply tap again.
    # THE MARK AND THE FILE BOTH MOVE BEFORE THE ACTION. Consuming the file is
    # what stops a crash mid-pairing from re-running on the next fire, and the
    # mark is what stops a request the broker rewrites from being performed
    # twice. A half-finished bond re-attempted is the sticky case
    # wall-bluetooth-pairing's docstring describes; the person taps again.
    remember(seq)
    _consume(path)
    try:
        ok = perform(event, aliases)
    except Exception as error:  # noqa: BLE001 - a oneshot must not leave the door open
        log("request %d failed: %s" % (seq, error))
        ok = False
    log("request %d (%s): %s" % (seq, event.get("kind"), "done" if ok else "refused"))
    # A `forget` or a `pair` changes the inventory, so the next request in the
    # burst must be resolved against the new one rather than against the list
    # this one started with.
    _rows, aliases = publish()
    return aliases


def _consume(path):
    try:
        Path(path).unlink()
    except OSError as error:
        # A request that cannot be removed WOULD be performed again on the next
        # fire, so say so loudly: the sequence mark is the backstop and this is
        # the only warning that it is now load-bearing.
        log("could not consume %s: %s -- the sequence mark is the only guard now" % (path, error))


def status():
    rows, _ = publish()
    reason, powered, discoverable, pairable = adapter()
    print("adapter: reason=%s powered=%s discoverable=%s pairable=%s"
          % (reason, powered, discoverable, pairable))
    print("devices: %d" % len(rows))
    for row in rows:
        print("  %-24s %-6s trusted=%-5s connected=%s"
              % (row["alias"], row["kind"], row["trusted"], row["connected"]))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wall-bluetooth-device", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("publish", "observe", "apply-request", "status"),
                        nargs="?", default="status")
    parser.add_argument("path", nargs="?", default=str(DEFAULT_REQUEST_DIR))
    args = parser.parse_args(argv)
    if bluetooth_state is None or bluetooth_request is None:
        log("FATAL: the shared panel-audio modules are not importable from %s; the alias "
            "rule and the request format have no definition, and neither a document nor "
            "a drain may proceed from a guess" % PANEL_AUDIO)
        return 1
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o755)
    if args.action == "publish":
        publish()
        return 0
    # `observe` is the timer's verb: drain anything stranded, then republish. It
    # is `apply-request` plus the guarantee that it runs every ten seconds
    # whatever the path unit did or did not do.
    if args.action in ("observe", "apply-request"):
        return apply_request(args.path)
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
