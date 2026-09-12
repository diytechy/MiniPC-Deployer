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
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

AGENT_PATH = "/org/wallpanel/bluetooth/agent"
CAPABILITIES = ("NoInputNoOutput", "DisplayYesNo", "DisplayOnly", "KeyboardDisplay")

# A2DP Sink and Source, plus AVRCP. These are what a phone or laptop needs to
# play audio to the panel. Everything else an already-bonded device might ask
# for is refused -- pairing to play music is not consent to become a keyboard.
AUDIO_UUIDS = {
    "0000110b-0000-1000-8000-00805f9b34fb",  # AudioSink
    "0000110a-0000-1000-8000-00805f9b34fb",  # AudioSource
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
        raise Rejected("this panel cannot accept a typed passkey")

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        # No pairing UI in the shell yet, so this reaches the journal only. It
        # is still worth emitting: it is the one record of what was paired.
        log("passkey for %s: %06u" % (device, passkey))

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        log("pin for %s: %s" % (device, pincode))

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        # The window IS the confirmation. Said plainly rather than hidden: with
        # no UI to show the passkey on, a person cannot compare it, so this
        # accepts and logs the value for the record.
        log("confirmed %s (passkey %06u)" % (device, passkey))

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log("authorized %s" % device)

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        log("pairing cancelled by the remote end")


def log(message):
    print("wall-bluetooth-agent: %s" % message, flush=True)


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
