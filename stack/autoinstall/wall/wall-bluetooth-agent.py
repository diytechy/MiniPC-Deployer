#!/usr/bin/env python3
"""A real BlueZ pairing agent, for the life of one pairing window. SR-023.

Usage: wall-bluetooth-agent.py --capability CAP --timeout SECONDS

WHY THIS EXISTS RATHER THAN `bluetoothctl --agent`
--------------------------------------------------
The first version of the pairing window shelled out to
`bluetoothctl --agent CAP --timeout N` and assumed that registered a usable
default agent. Adversarial review found two independent reasons that is not
safe to assume, and both are the kind that fail silently:

  * `--agent` maps to RegisterAgent. Becoming the DEFAULT agent -- the one BlueZ
    routes an incoming pairing to when nothing else claims it -- is a separate
    RequestDefaultAgent call, which `bluetoothctl` exposes as `default-agent`;
  * bluetoothctl's non-interactive mode (which `--timeout` selects) does not
    necessarily register the agent at all.

Either way the window would open, report itself open, and reject the pairing it
was opened for. A liveness check on the subprocess could not tell the
difference, because the process is perfectly alive while BlueZ has no agent.

So this registers explicitly, calls RequestDefaultAgent explicitly, and exits
non-zero if either fails. A window that cannot get an agent does not open.

WHAT IT CONSENTS TO, AND WHAT IT DOES NOT
-----------------------------------------
Inside the window this agent accepts pairing. That IS the security model: the
consent is a person standing at the panel who deliberately opened a bounded
window, exactly like the pairing button on any speaker. The agent exists only
for that window, and outside it the adapter is neither pairable nor
discoverable.

It deliberately does NOT authorize arbitrary services on an already-bonded
device: AuthorizeService accepts only the A2DP sink UUIDs, so a device that
paired to play music cannot later quietly claim, say, HID.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

AGENT_PATH = "/org/wallpanel/bluetooth/agent"
LINE_END = chr(10)

# -- THE PAIRING IS NOW VISIBLE ON THE GLASS (Owner, 2026-09-19) -------------
# "Need bluetooth to have a method to trust a device, might need a method to
# either enter a PIN, set a number, or turn on discoverability."
#
# Everything below used to reach the JOURNAL and stop there, and the comment on
# DisplayPasskey said so: "No pairing UI in the shell yet". There is one now, so
# the two numbers BlueZ can produce -- the passkey to COMPARE and the PIN to
# READ OUT -- are published where `wall-bluetooth-device publish` can put them
# in front of the person holding the phone.
#
# WHAT IS PUBLISHED IS BOUNDED AND SHORT-LIVED. One address, one number, one
# expiry; 0644 because the broker runs as `panel` and has to read it; removed
# when the agent exits. A passkey that outlived its window would be compared
# against the NEXT pairing, which is worse than showing nothing at all.
RUN_DIR = Path(os.environ.get("WALL_BLUETOOTH_RUN_DIR") or "/run/wall-bluetooth")
PAIRING_PATH = RUN_DIR / "pairing.json"
# Written by wall-bluetooth-device before a `pair` it was asked to perform with
# a passkey the person typed. Single-use: the applier removes it afterwards.
PASSKEY_PATH = RUN_DIR / "passkey.json"
CAPABILITIES = ("NoInputNoOutput", "DisplayYesNo", "DisplayOnly", "KeyboardDisplay")

# A2DP Sink and Source, plus AVRCP. These are what a phone or laptop needs to
# play audio to the panel. Everything else an already-bonded device might ask
# for is refused -- pairing to play music is not consent to become a keyboard.
AUDIO_UUIDS = {
    "0000110b-0000-1000-8000-00805f9b34fb",  # AudioSink
    "0000110a-0000-1000-8000-00805f9b34fb",  # AudioSource
    # Advanced Audio Distribution: the profile UUID a phone presents when it
    # opens the A2DP stream itself. Measured 2026-09-13 with a Pixel: bluez
    # asked for 110d, the agent refused it, and the phone reported "incorrect
    # passkey" for what was actually a service refusal after a good bond.
    "0000110d-0000-1000-8000-00805f9b34fb",  # AdvancedAudioDistribution
    "0000110c-0000-1000-8000-00805f9b34fb",  # A/V Remote Control Target
    "0000110e-0000-1000-8000-00805f9b34fb",  # A/V Remote Control
    "0000111e-0000-1000-8000-00805f9b34fb",  # Handsfree
}


class Rejected(dbus.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class PairingAgent(dbus.service.Object):
    """Accepts pairing; the window is what bounds it."""

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        log("released by bluez")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        # NOT a formality. This is the one callback an ALREADY-BONDED device can
        # reach outside a pairing window, so it is the only place the panel can
        # still say no to a device it once said yes to.
        if str(uuid).lower() not in AUDIO_UUIDS:
            log("refused non-audio service %s" % uuid)
            raise Rejected("only audio services are authorized")
        log("authorized audio service %s" % uuid)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        # Legacy pairing. A fixed PIN would be a shared secret printed in a repo;
        # refusing is honest, and any device from this century negotiates SSP.
        raise Rejected("legacy PIN pairing is not supported")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        """A passkey the PERSON typed at the panel, or a refusal.

        This used to be an unconditional refusal, and it was right to be: there
        was no way to ask anyone. With the pairing UI there is, so
        `wall-bluetooth-device` leaves the number the person entered where this
        can find it before it starts the pair, and this hands it to BlueZ.

        STILL A REFUSAL WHEN NOTHING WAS OFFERED. A default would be a shared
        secret, which is exactly the objection RequestPinCode makes below, and
        guessing zero is how a panel bonds with something nobody typed a number
        for.
        """
        offered = _offered_passkey()
        if offered is None:
            raise Rejected("this panel has no passkey to offer for this pairing")
        log("offering the passkey entered at the panel")
        return dbus.UInt32(offered)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        # SIX DIGITS WITH THE LEADING ZERO KEPT. `%06u` is not decoration here:
        # the phone shows 012345, and an integer would offer 12345 to compare
        # against it -- a comparison that fails for no reason at all.
        log("passkey for %s: %06u" % (device, passkey))
        _publish({"address": _address_of(device), "passkey": "%06u" % passkey})

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        log("pin for %s: %s" % (device, pincode))
        _publish({"address": _address_of(device), "passkey": str(pincode)})

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        # THE WINDOW IS STILL THE CONFIRMATION, and that has not changed: this
        # accepts. What has changed is that the number is now published, so a
        # person who wants to compare it against the phone CAN -- which is the
        # whole of the Owner's "set a number".
        #
        # ACCEPTING FIRST AND SHOWING THE NUMBER IS DELIBERATE. A callback that
        # blocked waiting for a tap would sit inside BlueZ's own pairing
        # timeout, and a timeout that expires mid-bond leaves the device half
        # paired -- the state wall-bluetooth-pairing's docstring calls the one
        # that needs an operator. Comparison after the fact costs a `forget`;
        # comparison during it costs a bond nobody can finish or undo.
        log("confirmed %s (passkey %06u)" % (device, passkey))
        _publish({"address": _address_of(device), "passkey": "%06u" % passkey})

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log("authorized %s" % device)

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        log("pairing cancelled by the remote end")


def log(message):
    print("wall-bluetooth-agent: %s" % message, flush=True)


def _address_of(device):
    """The MAC inside a BlueZ object path, or "".

    `/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF` -> `AA:BB:CC:DD:EE:FF`. This stays
    on the privileged side: `wall-bluetooth-device` turns it into an alias
    before anything the broker can read ever sees it.
    """
    tail = str(device).rsplit("/dev_", 1)[-1]
    parts = tail.split("_")
    if len(parts) != 6 or not all(len(part) == 2 for part in parts):
        return ""
    return ":".join(part.upper() for part in parts)


def _publish(update):
    """Merge one fact into the pairing document. Never raises.

    A failure to publish must not take the pairing down with it. The bond is
    what the person is standing there for; the number on the glass is how they
    check it. Losing the number is a worse experience, losing the bond is a
    failure, and only one of those is worth an exception.
    """
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        try:
            current = json.loads(PAIRING_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current.update(update)
        temporary = PAIRING_PATH.with_name(PAIRING_PATH.name + ".new")
        temporary.write_text(json.dumps(current, sort_keys=True) + LINE_END, encoding="utf-8")
        os.chmod(temporary, 0o644)
        os.replace(temporary, PAIRING_PATH)
    except OSError as error:
        log("could not publish the pairing state: %s" % error)


def _offered_passkey():
    """The passkey the panel was given for this pairing, as an int, or None."""
    try:
        stored = json.loads(PASSKEY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = stored.get("passkey") if isinstance(stored, dict) else None
    # BlueZ passkeys are 0..999999. A wider number is not a passkey, and
    # truncating one would offer a different number than the person typed.
    if not isinstance(value, str) or not value.isdigit() or not 0 <= int(value) <= 999999:
        return None
    return int(value)


def _retire():
    try:
        PAIRING_PATH.unlink()
    except OSError:
        pass



def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--capability", default="NoInputNoOutput", choices=CAPABILITIES)
    parser.add_argument("--timeout", type=int, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 3600:
        sys.exit("wall-bluetooth-agent: timeout out of range")

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    agent = PairingAgent(bus, AGENT_PATH)
    manager = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"),
                             "org.bluez.AgentManager1")
    try:
        manager.RegisterAgent(AGENT_PATH, args.capability)
    except dbus.DBusException as exc:
        sys.exit("wall-bluetooth-agent: RegisterAgent failed: %s" % exc)
    try:
        # THE CALL THE bluetoothctl VERSION NEVER MADE. Without it BlueZ has an
        # agent object it will not route an unsolicited pairing to.
        manager.RequestDefaultAgent(AGENT_PATH)
    except dbus.DBusException as exc:
        try:
            manager.UnregisterAgent(AGENT_PATH)
        except dbus.DBusException:
            pass
        sys.exit("wall-bluetooth-agent: RequestDefaultAgent failed: %s" % exc)

    log("registered (%s) for %ds" % (args.capability, args.timeout))
    # THE EXPIRY IS PUBLISHED AT THE START, not when a passkey appears. The
    # panel draws "pairing is open, N seconds left" from the moment the window
    # opens; most pairings never produce a number at all (a NoInputNoOutput
    # speaker has nothing to show), and a UI that only appeared for the ones
    # that did would leave the common case looking like nothing had happened.
    _publish({"expiresAt": int(time.time()) + args.timeout})
    loop = GLib.MainLoop()
    # A SECOND BOUND on top of the caller's own lifetime management: if the
    # window helper is killed without cleaning up, this still exits on its own.
    GLib.timeout_add_seconds(args.timeout, lambda: (loop.quit(), False)[1])
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            manager.UnregisterAgent(AGENT_PATH)
            log("unregistered")
        except dbus.DBusException:
            pass
        # The window is over, so the number is not something anybody can still
        # compare. Removing it is what makes `active` false on the next publish.
        _retire()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
