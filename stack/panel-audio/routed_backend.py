"""WSN-024: the routed-device backend. Bluetooth devices, through the applier.

WHAT THIS CLOSES. `switch_backend` says, at length, that it "routes nothing" and
that `inventory` is always empty so `pair`, `connect` and `select_output` are
refused before they reach any device -- because WSN-024 kept the routed-device
backend disabled until the BlueZ feasibility gate settled. The Owner settled it
on 2026-09-19 by asking for the whole thing: a way to trust a device, a way to
deal with a PIN, and a way to turn discoverability on.

THE SHAPE, AND WHY IT IS A WRAPPER RATHER THAN A REPLACEMENT. The panel's own
Mute/Headset/Speaker switch is a working, shipped control with its own applier,
its own state file and its own reconciliation rules. None of that changes. This
backend DELEGATES every switch verb to the one that already owns it, and adds
the device half: its `inventory` and the `devices`/`route`/`pairing` parts of
`status` come from an observer document, and its mutations become request files
for a second, separate applier.

IT PERFORMS NO DEVICE I/O, for exactly the reason `switch_backend` does not: the
broker runs as `panel` under ProtectSystem=strict with PrivateDevices=true and
AF_UNIX as its only address family (SR-023). It could not drive BlueZ if it
wanted to -- an unprivileged `Device1.Pair` or a write to `Trusted` is refused --
and giving it the privilege would move device control into the process the
renderer talks to, which is the boundary SR-023 exists to hold.

AND NO HARDWARE ADDRESS IS EVER IN THIS PROCESS. The observer document carries
aliases and facts, never a MAC; the request carries the alias; the applier
resolves it. See bluetooth_state for why that is a stronger statement than "the
broker does not send it".

Contract:
  Inputs:  validated (method, params) pairs from AudioBroker; see routing.
  Outputs: {"accepted": bool, "seq": int} for device verbs; a merged status.
  Config:  state_path:    /run/wall-bluetooth/state.json, read only
           request_path:  the file wall-bluetooth-device-apply.path watches
           applier_path:  the root applier, tested for existence only
  Raises:  BrokerError("backend_unavailable") when the applier is not
           installed; BrokerError("backend_failure") otherwise.
Implements: SR-023, LLR-007 (WSN-024)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Mapping

import bluetooth_request
import routing

DEFAULT_STATE_PATH = Path("/run/wall-bluetooth/state.json")
DEFAULT_APPLIER_PATH = Path("/usr/local/sbin/wall-bluetooth-device")

# How old the observer's document may be before its contents stop being facts.
# The observer republishes on a timer and after every apply; a document older
# than this means the observer is dead or wedged, and a stale device list is
# worse than none -- it offers a row that will be refused, with no way for the
# person to tell why.
MAX_DOCUMENT_AGE_SECONDS = 30

# What `status.reason` says when there is nothing to route. These are the tokens
# js/views/bluetooth.js already knows: keeping to them means the existing
# renderer messages stay correct rather than falling through to "unavailable".
REASON_READY = "ready"
REASON_NO_ADAPTER = "no-adapter"
REASON_POWERED_OFF = "powered-off"
REASON_UNAVAILABLE = "unavailable"

ADAPTER_REASONS = {REASON_READY, REASON_NO_ADAPTER, REASON_POWERED_OFF, REASON_UNAVAILABLE}


def _counter(value):
    """A non-negative integer from the document, or None. Never a bool.

    `isinstance(True, int)` is True in Python, so a document carrying
    `"requestSeq": true` would otherwise become the sequence 1 and silently
    discard every request below it.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _broker_error(code, message):
    # Imported inside, exactly as switch_backend does: audio_router imports this
    # module to expose the backend, so a module-level import would be circular.
    from audio_router import BrokerError
    return BrokerError(code, message)


class RoutedDeviceBackend:
    """Bluetooth device management, layered over the switch backend."""

    # ANSWERED FROM FILES, SO THEY NEED NO PROCESS ISOLATION. Both read a
    # regular file in /run and delegate to a backend that does the same. The
    # declaration is deliberately narrow: every MUTATION still gets a whole
    # interpreter of its own, because it writes a request the root applier will
    # act on and `backend_answers_locally` documents at length that a local call
    # runs on a thread the broker cannot kill.
    LOCAL_ONLY_METHODS = frozenset({"status", "telemetry"})

    def __init__(self, switch, *, state_path=None, request_path=None,
                 applier_path=None, clock=time.time_ns, now=time.time):
        self.switch = switch
        self.state_path = Path(state_path or DEFAULT_STATE_PATH)
        self.request_dir = Path(request_path or bluetooth_request.DEFAULT_DIR)
        self.applier_path = Path(applier_path or DEFAULT_APPLIER_PATH)
        self.clock = clock
        self.now = now
        self._seq_lock = threading.Lock()
        self._last_seq = -1

    # ---- the Backend protocol -------------------------------------------

    def inventory(self, cancel: threading.Event) -> list:
        """The devices the broker may validate an alias against.

        NEVER RAISES. `AudioBroker._mutation` calls this before every mutation
        and treats anything that is not a list as `unsafe_backend_result`, so an
        unreadable document must come back EMPTY -- which makes every aliased
        verb refuse with "device alias is not in the current inventory". That is
        the honest answer when the panel cannot see its own Bluetooth: it is not
        the same as pretending the device is there.
        """
        document = self._document()
        if document is None:
            return []
        devices = []
        for entry in document["devices"]:
            try:
                devices.append(routing.Device(alias=entry["alias"], kind=entry["kind"],
                                              trusted=entry["trusted"], connected=entry["connected"]))
            except routing.PolicyError:
                # ONE BAD ROW IS DROPPED, NOT THE WHOLE LIST. The observer is
                # ours and should never emit one, but if it did, refusing the
                # entire inventory would take away every OTHER device's row too
                # -- and the person standing at the panel would see a working
                # speaker vanish because an unrelated phone had an odd name.
                continue
            if len(devices) >= 64:
                # The broker refuses an inventory longer than this outright, so
                # truncating here is the difference between "one crowded room"
                # and "Bluetooth stopped working".
                break
        return devices

    def call(self, method: str, params: Mapping[str, object], cancel: threading.Event) -> object:
        if method == "status":
            return self._status()
        # DISPATCHED HERE AND NOT LEFT TO THE ATTRIBUTE. `AudioBroker` probes
        # `hasattr(backend, "request_landed")` to decide whether the backend can
        # answer at all, and then asks through `call` like everything else -- so
        # a `request_landed` method that is never routed to is a method the
        # broker believes in and never reaches. Without this line every device
        # sequence was checked against the SWITCH applier's high-water mark,
        # which is the exact confusion the `method` parameter was added to end.
        if method == "request_landed":
            return self.request_landed(params)
        if method in routing.DEVICE_METHODS:
            self._require_applier()
            return self._submit(self._event(method, params))
        # Everything else -- telemetry, the switch verbs, `request_landed` --
        # belongs to the backend that owns it. Delegated rather than
        # reimplemented, and delegated by ASKING it rather than by copying its
        # list of methods, so a verb added there needs no change here.
        return self.switch.call(method, params, cancel)

    def request_landed(self, params):
        """Whether a device request we acknowledged is still pending.

        The broker uses this to decide whether a completed journal entry that
        outlived its request file may be replayed (`_effect_survived`). Device
        verbs are NOT all idempotent -- a replayed `forget` revokes a device
        somebody re-paired in between -- so the answer matters more here than it
        does for the switch.

        A request we cannot see the outcome of reports LANDED, which suppresses
        the replay. Between doing a destructive thing twice and possibly not
        doing it once, not doing it is the recoverable failure: the person taps
        again.
        """
        # WHICH APPLIER MINTED IT. Both mint from the same microsecond clock, so
        # the sequence alone cannot say, and asking the wrong one is a coin toss
        # between replaying a `forget` and dropping a `pair`. The broker passes
        # the method it is asking about; a call that does not (an older broker)
        # is answered by the switch, which is where every seq came from before
        # this backend existed.
        method = params.get("method")
        if method not in routing.DEVICE_METHODS:
            return self.switch.call("request_landed", params, threading.Event())
        seq = params.get("seq")
        document = self._document()
        if document is None or isinstance(seq, bool) or not isinstance(seq, int):
            return {"accepted": True}
        mark = document["requestSeq"]
        return {"accepted": mark is not None and mark >= seq}

    # ---- the device verbs -----------------------------------------------

    @staticmethod
    def _event(method: str, params: Mapping[str, object]) -> dict:
        """One validated IF-015 verb as one applier event.

        The translation is deliberately explicit and not a dict splat: IF-015
        and the request file are two contracts that happen to overlap today, and
        a splat would silently carry a new IF-015 parameter across a boundary
        nobody had reviewed it for.
        """
        if method == "discover":
            return {"kind": "discover", "timeoutSeconds": int(params.get("timeoutSeconds", 30))}
        if method == "cancel":
            return {"kind": "cancel"}
        if method == "set_discoverable":
            return {"kind": "set_discoverable", "enabled": bool(params["enabled"])}
        if method == "trust":
            return {"kind": "trust", "alias": params["alias"], "trusted": bool(params["trusted"])}
        if method == "pair":
            event = {"kind": "pair", "alias": params["alias"]}
            if params.get("confirmation") is not None:
                event["confirmation"] = str(params["confirmation"])
            return event
        # connect / disconnect / forget / select_input / select_output: alias
        # only. `select_input`'s `explicit` is an IF-015 GATE and not a fact
        # about the device -- routing.validate_action has already refused the
        # request if it was absent -- so it stops here rather than travelling.
        return {"kind": method, "alias": params["alias"]}

    def _submit(self, event: dict) -> dict:
        document = self._document()
        seq = self._next_seq(document)
        try:
            bluetooth_request.write(seq, event, self.request_dir,
                                    None if document is None else document["generation"])
        except bluetooth_request.RequestError as error:
            # A request this module built and this module refused is a bug in
            # `_event`, not a transient. It is still not allowed to carry its
            # message out: `backend_failure`'s text is fixed at the seam.
            raise _broker_error("backend_failure", str(error))
        except OSError:
            raise _broker_error("backend_failure", "could not write the device request")
        return {"accepted": True, "seq": seq}

    def _next_seq(self, document=None) -> int:
        """A sequence above every one this process has minted AND above the
        applier's own high-water mark.

        THE SECOND HALF IS NOT OPTIONAL and this file shipped without it (terra,
        2026-09-19, finding 5). Two taps inside one microsecond are the obvious
        case and `_last_seq` covers them. The one that actually bites is a
        BROKER RESTART: `_last_seq` starts at -1 again, and if the wall clock has
        stepped backwards in between -- NTP correcting a dead RTC is the ordinary
        way that happens on this box -- the next sequence lands BELOW the mark
        the applier persisted in /run and every request is discarded as old, in
        silence, while `_submit` goes on returning accepted. `switch_backend`
        documents this exact guard; this one merely referred to it.
        """
        mark = None if document is None else document["requestSeq"]
        with self._seq_lock:
            seq = bluetooth_request.next_seq(self.clock)
            if seq <= self._last_seq:
                seq = self._last_seq + 1
            if mark is not None and seq <= mark:
                seq = mark + 1
            self._last_seq = seq
            return seq

    def _require_applier(self) -> None:
        if not self.applier_path.exists():
            raise _broker_error("backend_unavailable", "audio routing is not configured")

    # ---- observation ------------------------------------------------------

    def _status(self) -> dict:
        """The switch backend's status, with the device half filled in.

        THE SWITCH BLOCK IS TAKEN VERBATIM. It is the other backend's fact about
        the other backend's applier, and this one has no business normalizing
        it. Only the four fields WSN-024 owns are replaced.

        `available` becomes TRUE here, where `switch_backend` hard-codes it
        false with reason "routing-disabled". That top-level boolean means
        BLUETOOTH DEVICE ROUTING, and it is now a real question with a real
        answer: an adapter that is present and powered.
        """
        from audio_router import BrokerError
        try:
            base = self.switch.call("status", {}, threading.Event())
        except BrokerError:
            raise
        except Exception:
            raise _broker_error("backend_failure", "audio backend failed")
        if not isinstance(base, dict):
            raise _broker_error("backend_failure", "audio backend failed")
        merged = dict(base)
        document = self._document()
        if document is None:
            merged.update({"available": False, "reason": REASON_UNAVAILABLE,
                           "devices": [], "route": None})
            return merged
        merged.update({
            "available": document["reason"] == REASON_READY,
            "reason": document["reason"],
            "devices": document["devices"],
            "route": document["route"],
        })
        # The pairing block rides on status rather than on a channel of its own,
        # because it is a FACT ABOUT NOW that the renderer already polls for.
        # A passkey pushed down a second path would need its own freshness rule
        # and its own way of going stale; here it expires with the document.
        merged["pairing"] = document["pairing"]
        return merged

    def _document(self):
        """The observer document, normalized, or None if it cannot be trusted.

        NONE MEANS "DO NOT KNOW", and every caller treats it that way: the
        inventory comes back empty and the status reports unavailable. It never
        means "no devices", which is a claim the panel has not earned when it
        cannot read the file the claim would come from.
        """
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return None
        published = raw.get("publishedAt")
        if isinstance(published, bool) or not isinstance(published, (int, float)):
            return None
        # A document from the FUTURE is as untrustworthy as an old one and for a
        # sharper reason: it would never expire. The panel's clock and the
        # observer's are the same clock, so any gap at all is a fault; one
        # second of tolerance absorbs the write itself.
        age = self.now() - published
        if age > MAX_DOCUMENT_AGE_SECONDS or age < -1:
            return None
        reason = raw.get("reason")
        if reason not in ADAPTER_REASONS:
            return None
        devices = self._devices(raw.get("devices"))
        if devices is None:
            return None
        return {
            "reason": reason,
            "devices": devices,
            "route": self._route(raw.get("route"), devices),
            "pairing": self._pairing(raw.get("pairing")),
            "requestSeq": _counter(raw.get("requestSeq")),
            "generation": _counter(raw.get("generation")),
        }

    @staticmethod
    def _devices(value):
        if not isinstance(value, list) or len(value) > 64:
            return None
        devices = []
        for entry in value:
            if not isinstance(entry, dict):
                return None
            alias, name, kind = entry.get("alias"), entry.get("name"), entry.get("kind")
            trusted, connected = entry.get("trusted"), entry.get("connected")
            if not isinstance(alias, str) or not routing.ALIAS.fullmatch(alias):
                return None
            if routing.HARDWARE_ADDRESS.search(alias):
                return None
            if not isinstance(name, str) or len(name) > 96 or routing.HARDWARE_ADDRESS.search(name):
                return None
            if kind not in ("input", "output"):
                return None
            if not isinstance(trusted, bool) or not isinstance(connected, bool):
                return None
            device = {"alias": alias, "name": name, "kind": kind,
                      "trusted": trusted, "connected": connected}
            battery = entry.get("battery")
            if isinstance(battery, int) and not isinstance(battery, bool) and 0 <= battery <= 100:
                device["battery"] = battery
            devices.append(device)
        if len({device["alias"] for device in devices}) != len(devices):
            # Two rows under one alias would make `validate_inventory_action`
            # refuse every verb aimed at either of them, with a message about an
            # alias not being in the inventory when it is in it twice.
            return None
        return devices

    @staticmethod
    def _route(value, devices):
        """The current route, kept honest against the device list.

        A route naming a device that is not in the inventory is dropped rather
        than reported: the renderer draws it as the selected row, and drawing a
        selection the panel would refuse to re-make is how a route that silently
        fell apart goes on looking fine.
        """
        if not isinstance(value, dict):
            return None
        aliases = {device["alias"] for device in devices}
        route = {}
        for side in ("input", "output"):
            alias = value.get(side)
            route[side] = alias if isinstance(alias, str) and alias in aliases else None
        return route

    @staticmethod
    def _pairing(value):
        """The pairing window, and the passkey to compare against the phone.

        The whole point of the Owner's "enter a PIN, set a number" is that a
        person can COMPARE what BlueZ shows against what the phone shows. So the
        passkey is carried exactly as BlueZ gave it -- six digits, as a STRING,
        because `%06u` has a leading zero that an integer would eat and "12345"
        against the phone's "012345" is a comparison that fails for no reason.
        """
        if not isinstance(value, dict):
            return {"active": False, "discoverable": False, "passkey": None,
                    "device": None, "endsInSeconds": None}
        passkey = value.get("passkey")
        if not (isinstance(passkey, str) and passkey.isdigit() and 1 <= len(passkey) <= 16):
            passkey = None
        device = value.get("device")
        if not (isinstance(device, str) and routing.ALIAS.fullmatch(device)):
            device = None
        ends = value.get("endsInSeconds")
        if isinstance(ends, bool) or not isinstance(ends, int) or not 0 <= ends <= 600:
            ends = None
        return {
            "active": value.get("active") is True,
            "discoverable": value.get("discoverable") is True,
            "passkey": passkey,
            "device": device,
            "endsInSeconds": ends,
        }

    # `_generation` was folded into `_submit`, which already has the document in
    # hand. The rule it carried stands and is repeated there: the epoch is
    # OMITTED rather than guessed when the document cannot be read, because zero
    # is a CLAIM about which life of the applier a request belongs to and the
    # applier would refuse it.
    #
    # THE EPOCH IS CONSTANT WITHIN A BOOT, and that is correct rather than
    # unfinished. It exists to stop a request outliving the applier state that
    # orders it. Both live in /run -- the broker's spool under
    # RuntimeDirectoryPreserve=restart, the applier's mark in
    # /run/wall-bluetooth -- so they are wiped together at a reboot and survive
    # together across either process restarting. There is no event that
    # invalidates one and not the other, so there is nothing for the epoch to
    # count; the guard that does the real work is `_next_seq`'s mark comparison.


def backend_from_environment(switch):
    """Build the routed-device backend from WALL_BLUETOOTH_* paths.

    Config: WALL_BLUETOOTH_STATE_PATH, WALL_BLUETOOTH_REQUEST_PATH,
            WALL_BLUETOOTH_APPLIER_PATH -- all optional; the defaults are the
            installed locations. Provided so the unit file and the tests can
            point the backend at a tree without rewriting the entry point.
    """
    return RoutedDeviceBackend(
        switch,
        state_path=os.environ.get("WALL_BLUETOOTH_STATE_PATH") or None,
        request_path=os.environ.get("WALL_BLUETOOTH_REQUEST_PATH") or None,
        applier_path=os.environ.get("WALL_BLUETOOTH_APPLIER_PATH") or None,
    )
