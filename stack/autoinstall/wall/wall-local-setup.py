#!/usr/bin/env python3
"""Own the panel's narrow root-only local capability boundary.

The kiosk account may request local-access bootstrap and display wake, but may
not write root configuration or invent gateway authorization.  Gateway-owned
panels are reauthorized against their configured gateway before this helper
adds local access as an adjunct.  It never replaces remote authority, stores a
PIN/session, or opens a camera.

Contract:
  Input: one newline-delimited JSON request per Unix-socket connection.
  Output: one bounded JSON response and connection close.
  Config: private host.json and local-capabilities.json; canonical sensor socket.
  Raises: requests fail closed with a stable error string; secrets are omitted.
Implements: SR-017, LLR-017
"""
from __future__ import annotations

import json
import math
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

HOST_PATH = Path("/etc/wall-panel/host.json")
STATE_PATH = Path("/etc/wall-panel/local-capabilities.json")
HOST_BACKUP_PATH = Path("/etc/wall-panel/host.pre-local-capabilities.json")
LOCAL_AUTH_PATHS = (
    Path("/var/lib/wall-panel/access-state.json"),
    Path("/var/lib/wall-panel/access-state.key"),
)
SENSOR_SOCKET = "/run/wall-sensors/service.sock"
SETUP_SOCKET = "/run/wall-local-setup/service.sock"
POWER_COMMAND = ("/usr/local/sbin/wall-sleep.sh", "sensor-wake")
MAX_REQUEST = 16 * 1024
MAX_SECRET = 4096

# ── touch witness (LLR-910..913) ─────────────────────────────────────────────
# A tap on dark glass must light the panel, and the process that asks for power
# must never be the process that attests the evidence.  Root therefore observes
# the contact itself, in this process, from a kernel device node: there is no
# request, no peer and no parameter for the kiosk account to forge.
TOUCH_POWER_COMMAND = ("/usr/local/sbin/wall-sleep.sh", "touch-wake")
TOUCH_FILTER_CONFIG = Path("/etc/wall-panel/touch-filter.json")
BACKLIGHT_ROOT = Path("/sys/class/backlight")
# The uinput device wall-touch-filter creates for the filtered stream.  One
# fact, one home: this identity is declared by touchfilter/daemon.py
# (VIRTUAL_NAME, vendor=0, product=1) and must be kept equal to it.
TOUCH_VIRTUAL_NAME = "OfficeWall Filtered Touchscreen"
TOUCH_VIRTUAL_VENDOR = 0
TOUCH_VIRTUAL_PRODUCT = 1
# The ONLY mode that creates a uinput device.  `adaptive` also grabs the
# physical node, but its accepted taps leave over the AF_UNIX bridge to the
# kiosk rather than through a virtual device -- configure-touch-filter.sh
# modprobes uinput for `filter` alone, and says so.  Treating adaptive as a
# virtual-node mode made the witness hunt for a device that is never created
# and retry `touch-node-missing` for ever (review finding 2, 2026-09-15).
TOUCH_VIRTUAL_MODES = frozenset({"filter"})
# Longer backoff for a node that is PRESENT but grabbed by the filter: that is
# a steady state (adaptive with a healthy daemon), not a transient, and
# retrying it every second would say the same thing 86400 times a day.
TOUCH_BLOCKED_RETRY_S = 30.0
# One wake per deliberate tap.  2 s is sized to a person's tap, not measured;
# LLR-911 owns the number so a later measurement changes one constant.
TOUCH_WAKE_MIN_INTERVAL_S = 2.0
# Re-resolution backoff when the node disappears -- the filter unit is
# Restart=always and its uinput node is destroyed and recreated on every
# restart and across every suspend/resume.
TOUCH_RETRY_S = 1.0
EV_KEY = 1
BTN_TOUCH = 330


class Refused(Exception):
    """A request violated the helper contract without exposing input values."""


def private_object(path: Path, *, absent: dict | None = None,
                   group_read_gid: int | None = None) -> dict:
    """Read a private regular JSON object, optionally returning `absent`."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if absent is not None:
            return dict(absent)
        raise Refused("required-state-missing")
    mode = stat.S_IMODE(info.st_mode)
    allowed_modes = {0o600, 0o640} if group_read_gid is not None else {0o600}
    if not stat.S_ISREG(info.st_mode) or mode not in allowed_modes:
        raise Refused("private-state-invalid")
    if os.name != "nt":
        parent = path.parent.stat()
        if (info.st_uid != 0 or parent.st_uid != 0 or parent.st_mode & 0o022
                or (mode == 0o640 and info.st_gid != group_read_gid)):
            raise Refused("private-state-invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise Refused("private-state-invalid") from error
    if not isinstance(value, dict):
        raise Refused("private-state-invalid")
    return value


def remote_configured(host: dict) -> bool:
    """Return whether host holds a complete, possibly dormant registration."""
    return all(
        isinstance(host.get(key), str) and bool(host[key])
        for key in ("gatewayUrl", "deviceId", "deviceCredential")
    )


def gateway_protected(host: dict) -> bool:
    """Return whether enabled gateway authority owns protected configuration."""
    return host.get("enabled") is True and host.get("accessMode") != "local"


def _validate_host(host: dict) -> dict:
    if type(host.get("enabled")) is not bool:
        raise Refused("host-state-invalid")
    access_mode = host.get("accessMode")
    if access_mode is not None and not isinstance(access_mode, str):
        raise Refused("host-state-invalid")
    return host


def current_status(host: dict, state: dict, auth_paths=LOCAL_AUTH_PATHS) -> dict:
    """Return the credential-free status contract consumed by Electron."""
    revision = state.get("revision", 0)
    local = state.get("schemaVersion") == 1 and state.get("localAccessEnabled") is True
    remote = remote_configured(host)
    auth_exists = any(path.exists() for path in auth_paths)
    safe_host = not gateway_protected(host)
    return {
        "revision": revision if type(revision) is int and revision >= 0 else 0,
        "bootstrapAllowed": bool(safe_host and (local or not auth_exists)),
        "localConfigured": local,
        "remoteConfigured": remote,
    }


def _validate_state(state: dict) -> dict:
    if state == {}:
        return {"schemaVersion": 1, "revision": 0, "localAccessEnabled": False}
    if set(state) != {"schemaVersion", "revision", "localAccessEnabled"}:
        raise Refused("local-state-invalid")
    if state["schemaVersion"] != 1 or type(state["revision"]) is not int or state["revision"] < 0:
        raise Refused("local-state-invalid")
    if type(state["localAccessEnabled"]) is not bool:
        raise Refused("local-state-invalid")
    return state


def merged_host(host: dict) -> dict:
    """Add local capability fields without removing any gateway-owned field."""
    result = dict(host)
    result.update({
        "localAccessEnabled": True,
        "localCapabilitiesVersion": 1,
        "localFacePolicyVersion": 1,
        "sensorSocket": SENSOR_SOCKET,
        "sensorModelManifest": "/opt/wall-sensors/models/manifest.json",
        "localSetupSocket": SETUP_SOCKET,
    })
    if not gateway_protected(host):
        result["enabled"] = True
        result["accessMode"] = "local"
    return result


def atomic_json(path: Path, value: dict, *, mode: int, uid: int, gid: int) -> None:
    """Replace JSON durably in the target directory with exact mode/ownership."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        if hasattr(os, "fchown"):
            os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, separators=(",", ":"), sort_keys=True, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        # Windows cannot open a directory this way; installed Linux systems
        # fsync it so the rename itself is durable across sudden power loss.
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def verify_gateway(host: dict, session: object, pin: object) -> None:
    """Reauthorize a protected gateway setting using only root-read config."""
    if not gateway_protected(host):
        raise Refused("gateway-not-configured")
    if not remote_configured(host):
        raise Refused("gateway-config-invalid")
    if not all(isinstance(value, str) and 0 < len(value) <= MAX_SECRET for value in (session, pin)):
        raise Refused("gateway-authorization-required")
    try:
        parsed = urlsplit(host["gatewayUrl"])
        _ = parsed.port
    except ValueError as error:
        raise Refused("gateway-config-invalid") from error
    loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if (parsed.scheme != "https" and not loopback) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Refused("gateway-config-invalid")
    url = host["gatewayUrl"].rstrip("/") + "/v1/admin/authorize"
    request = urllib.request.Request(url, method="POST", data=json.dumps({"pin": pin}).encode(), headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + host["deviceCredential"],
        "X-Panel-Device": host["deviceId"],
        "X-Panel-Session": session,
    })
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, file_pointer, code, message, headers, new_url):
            return None
    opener = urllib.request.build_opener(urllib.request.HTTPHandler, urllib.request.HTTPSHandler, NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            body = response.read(MAX_REQUEST + 1)
            if len(body) > MAX_REQUEST:
                raise Refused("gateway-response-invalid")
            result = json.loads(body)
    except (OSError, UnicodeError, ValueError, urllib.error.URLError) as error:
        raise Refused("gateway-authorization-failed") from error
    if not isinstance(result, dict) or result.get("authorized") is not True:
        raise Refused("gateway-authorization-failed")


def sensor_status(socket_path: str = SENSOR_SOCKET) -> dict:
    """Read one bounded status response directly from the sensor owner."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(socket_path)
            client.sendall(b'{"method":"status","params":{}}\n')
            data = bytearray()
            while not data.endswith(b"\n") and len(data) <= MAX_REQUEST:
                part = client.recv(4096)
                if not part:
                    break
                data.extend(part)
    except OSError as error:
        raise Refused("sensor-unavailable") from error
    if not data.endswith(b"\n") or len(data) > MAX_REQUEST:
        raise Refused("sensor-status-invalid")
    try:
        response = json.loads(data)
        result = response["result"] if response.get("ok") is True else None
    except (UnicodeError, ValueError, KeyError, TypeError) as error:
        raise Refused("sensor-status-invalid") from error
    if not isinstance(result, dict) or result.get("protocolVersion") != 2:
        raise Refused("sensor-status-invalid")
    return result


def validate_wake(params: dict, status: dict, now_ms: int | None = None, *,
                  witness: bool = False) -> None:
    """Require the broker claim to match a fresh positive sensor observation.

    Contract:
      Inputs:  params: dict from the socket peer, or {"source": "touch"} from
               the in-process witness; status: a sensor status result;
               now_ms: int epoch milliseconds (test seam);
               witness: keyword-only, True ONLY for the in-process evdev
               witness of this same process.
      Outputs: None on acceptance.
      Raises:  Refused("wake-request-invalid"|"wake-not-observed"
               |"wake-observation-stale").
    The `witness` seam is keyword-only and is never passed by `Helper.request`,
    the sole caller reachable from the socket, so a socket peer sending
    `{"source": "touch", ...}` is refused by the unchanged line below.
    Implements: LLR-912
    """
    if witness:
        # The contact was observed by this process from a kernel device node.
        # There are no peer-supplied parameters to validate, and nothing here
        # may ever grow one.
        if params != {"source": "touch"}:
            raise Refused("wake-request-invalid")
        return
    if set(params) != {"source", "observedAt", "ttlMs"} or params.get("source") not in {"camera", "bluetooth"}:
        raise Refused("wake-request-invalid")
    observed = params.get("observedAt")
    ttl = params.get("ttlMs")
    if (type(observed) not in (int, float) or type(ttl) not in (int, float)
            or not math.isfinite(observed) or not math.isfinite(ttl) or not 0 < ttl <= 120000):
        raise Refused("wake-request-invalid")
    config = status.get("config")
    if not isinstance(config, dict) or config.get("schemaVersion") != 2:
        raise Refused("wake-not-observed")
    candidates = [("bluetooth", "bluetoothEnabled")] if params["source"] == "bluetooth" else [
        ("cameraFace", "presenceFace"), ("motion", "presenceMotion")]
    if params["source"] == "camera" and not (config.get("cameraEnabled") is True and config.get("cameraConsentVersion") == 1):
        raise Refused("wake-not-observed")
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if observed > now or now - observed >= ttl:
        raise Refused("wake-observation-stale")
    matches = [status.get(name) for name, setting in candidates if config.get(setting) is True]
    for reading in matches:
        if not isinstance(reading, dict) or reading.get("state") != "positive":
            continue
        actual_at, actual_ttl = reading.get("observedAt"), reading.get("ttlMs")
        if (type(actual_at) in (int, float) and type(actual_ttl) in (int, float)
                and math.isfinite(actual_at) and math.isfinite(actual_ttl)
                and 0 < actual_ttl <= 120000 and actual_at <= now
                and now - actual_at < actual_ttl and actual_at >= observed - 2):
            return
    raise Refused("wake-not-observed")


def journal(message: str) -> None:
    """Write one correlatable line to stderr, which systemd routes to the journal."""
    print(message, file=sys.stderr, flush=True)


def backlight_dark(root: Path = BACKLIGHT_ROOT) -> bool:
    """Return whether every readable backlight reads zero brightness.

    Contract:
      Inputs:  root: directory of backlight devices (default /sys/class/backlight).
      Outputs: True only when at least one backlight was readable and all of
               them read brightness 0.  No readable backlight is NOT darkness:
               with no evidence the witness declines rather than guessing.
      Raises:  nothing; an unreadable device contributes no signal.
    The predicate mirrors `electron/display-lit.cjs` displayLit() for the
    backlight half: integer files, max_brightness > 0, lit iff brightness > 0.
    The two sides are pinned by TC-911 here and TC-P-422 in the panel repo so
    they cannot drift.
    Implements: LLR-911
    """
    signals = []
    try:
        entries = sorted(Path(root).iterdir())
    except OSError:
        return False
    for base in entries:
        brightness, maximum = _integer_file(base / "brightness"), _integer_file(base / "max_brightness")
        if brightness is None or maximum is None or maximum <= 0 or brightness > maximum:
            continue
        signals.append(brightness > 0)
    return bool(signals) and not any(signals)


def _integer_file(path: Path) -> int | None:
    """Read one non-negative decimal integer, or None if it is not one."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return int(text) if text.isdigit() else None


def touch_targets(config: dict | None) -> list:
    """Return the input node identities to try, best first.

    Contract:
      Inputs:  config: the parsed /etc/wall-panel/touch-filter.json object, or
               None when the file is absent.
      Outputs: a non-empty list of
               {"virtual": bool, "name": str, "vendor": int, "product": int}.
               In `filter` mode the daemon's virtual node comes first and the
               physical node is the fallback for the window in which the daemon
               is down or restarting -- and in that window nobody holds the
               grab, so the physical node really is readable.  Every other mode
               offers the physical node alone: `adaptive` creates no uinput
               device, and `off`/`shadow` do not grab.
      Raises:  Refused("touch-config-invalid") for a config that does not carry
               a usable physical identity.
    Never an `eventN` path: node numbers are allocation-order dependent and
    change across a daemon restart or a USB re-enumeration.  Which target is
    actually USABLE is not decided here -- the persisted mode is a statement of
    intent, not an observation of the live grab, so TouchWitness.resolve()
    probes each candidate before trusting it (review finding 3, 2026-09-15).
    Implements: LLR-910
    """
    if not isinstance(config, dict):
        raise Refused("touch-config-invalid")
    name, vendor, product = config.get("name"), config.get("vendor"), config.get("product")
    if not isinstance(name, str) or not name or type(vendor) is not int or type(product) is not int:
        raise Refused("touch-config-invalid")
    physical = {"virtual": False, "name": name, "vendor": vendor, "product": product}
    virtual = {"virtual": True, "name": TOUCH_VIRTUAL_NAME,
               "vendor": TOUCH_VIRTUAL_VENDOR, "product": TOUCH_VIRTUAL_PRODUCT}
    if config.get("mode") in TOUCH_VIRTUAL_MODES:
        return [virtual, physical]
    return [physical]


def select_input_device(evdev, identity: dict):
    """Return the sole input device matching `identity`; close every other handle.

    Contract:
      Inputs:  evdev: the python-evdev module (injected for test);
               identity: as returned by touch_identity().
      Outputs: one opened evdev.InputDevice.
      Raises:  Refused("touch-node-missing") for zero matches and
               Refused("touch-node-ambiguous") for more than one, exactly as
               touchfilter's own select_device() refuses ambiguity.
    EVERY handle this opens is closed on EVERY path except the one it returns:
    a node can disappear mid-enumeration (the filter unit is Restart=always),
    and an exception there used to leak every handle opened so far, which on a
    re-resolving loop is an fd leak that ends in EMFILE (review finding 4,
    2026-09-15).
    Implements: LLR-910
    """
    opened, found, chosen = [], [], None
    try:
        for path in evdev.list_devices():
            device = evdev.InputDevice(path)
            opened.append(device)
            if (device.name == identity["name"] and device.info.vendor == identity["vendor"]
                    and device.info.product == identity["product"]):
                found.append(device)
        if not found:
            raise Refused("touch-node-missing")
        if len(found) > 1:
            raise Refused("touch-node-ambiguous")
        chosen = found[0]
        return chosen
    finally:
        for device in opened:
            if device is not chosen:
                close_quietly(device)


def close_quietly(device) -> None:
    """Close one device handle; a node that already vanished is not an error."""
    try:
        device.close()
    except OSError:
        pass


def is_contact_start(event) -> bool:
    """Return whether the event is the START of one physical contact.

    One wake per contact, not per frame: BTN_TOUCH down is the single event
    the daemon's virtual device emits at the head of a replayed tap.
    Implements: LLR-911
    """
    return (getattr(event, "type", None) == EV_KEY and getattr(event, "code", None) == BTN_TOUCH
            and getattr(event, "value", None) == 1)


class TouchWitness:
    """Observe one physical contact on the dark glass and restore the backlight.

    Contract:
      Inputs:  helper: the Helper that owns the power command;
               config_path/backlight_root: filesystem seams;
               clock: a CLOCK_MONOTONIC reader (the clock the filter daemon
               pins with EVIOCSCLOCKID), evdev_module: injected for test.
      Outputs: runs as a daemon thread for the life of the helper; never
               returns while the service is up.
      Config:  /etc/wall-panel/touch-filter.json (mode/name/vendor/product).
      Raises:  nothing outward -- a missing node, a wedged daemon or a refused
               wake is journaled and retried; the witness never fails the
               helper (LLR-913, the resume case).
    Implements: LLR-910, LLR-911
    """

    def __init__(self, helper, config_path=TOUCH_FILTER_CONFIG, backlight_root=BACKLIGHT_ROOT,
                 clock=time.monotonic, evdev_module=None, log=journal,
                 retry_s: float = TOUCH_RETRY_S, blocked_retry_s: float = TOUCH_BLOCKED_RETRY_S,
                 sleep=time.sleep):
        self.helper = helper
        self.config_path = Path(config_path)
        self.backlight_root = Path(backlight_root)
        self.clock = clock
        self.evdev_module = evdev_module
        self.log = log
        self.retry_s = retry_s
        self.blocked_retry_s = blocked_retry_s
        self.sleep = sleep
        self.last_wake = None
        self.last_note = None

    def _config(self) -> dict | None:
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise Refused("touch-config-invalid") from error
        return value

    def note(self, message: str) -> None:
        """Journal a recurring condition once, not once per retry."""
        if message != self.last_note:
            self.log(message)
            self.last_note = message

    def grabbed(self, device) -> bool:
        """Return whether somebody else holds EVIOCGRAB on this node.

        The persisted `mode` says what the daemon INTENDS; only the kernel
        knows what it currently holds, and the two disagree exactly when it
        matters -- while the filter is down, restarting or failed open.  So the
        grab is probed rather than inferred: take it and immediately give it
        back.  EVIOCGRAB is exclusive, so a busy node raises and an idle one
        does not.  The window in which this process holds the grab is a single
        pair of ioctls, it happens only at resolution time and never per
        contact, and it is only ever reached for a PHYSICAL node -- the virtual
        node is grabbed by nobody by construction.
        Implements: LLR-910
        """
        try:
            device.grab()
        except OSError:
            return True
        try:
            device.ungrab()
        except OSError:
            pass
        return False

    def resolve(self):
        """Open the best readable node for the live state of the touch filter.

        Tries each identity `touch_targets` offers, in order, and accepts a
        physical node only after PROVING no grab is held on it.  So a panel
        whose filter daemon is restarting falls back to the raw node and keeps
        waking, and returns to the virtual node as soon as the daemon is back.
        Raises the last refusal when no candidate is usable.
        """
        refusal = None
        for identity in touch_targets(self._config()):
            try:
                device = select_input_device(self._evdev(), identity)
            except Refused as error:
                refusal = error
                continue
            if not identity["virtual"] and self.grabbed(device):
                close_quietly(device)
                refusal = Refused("touch-node-grabbed")
                continue
            self.note("touch-witness: resolved %s node name=%r vendor=%#x product=%#x"
                      % ("virtual" if identity["virtual"] else "physical", identity["name"],
                         identity["vendor"], identity["product"]))
            return device
        raise refusal if refusal is not None else Refused("touch-node-missing")

    def _evdev(self):
        if self.evdev_module is None:
            import evdev  # deferred: the dev host has no python-evdev and needs none
            self.evdev_module = evdev
        return self.evdev_module

    def contact(self) -> bool:
        """Apply the preconditions to one observed contact and wake if they hold.

        Returns whether the power command ran.  Declines, with a reason on the
        journal, when a backlight is lit (ordinary input, owned by the
        compositor and host-attention.cjs) or when the previous wake is inside
        the rate limit.
        Implements: LLR-911
        """
        now = self.clock()
        if not backlight_dark(self.backlight_root):
            self.log("touch-witness: contact observed, backlight lit — declining")
            return False
        if self.last_wake is not None and now - self.last_wake < TOUCH_WAKE_MIN_INTERVAL_S:
            self.log("touch-witness: contact observed, inside the %.0fs rate limit — declining"
                     % TOUCH_WAKE_MIN_INTERVAL_S)
            return False
        self.log("touch-witness: contact observed, brightness=0, requesting touch-wake")
        try:
            self.helper.wake({"source": "touch"}, witness=True)
        except (Refused, OSError, subprocess.SubprocessError) as error:
            self.log("touch-witness: touch-wake failed (%s)" % error)
            return False
        self.last_wake = now
        self.log("touch-witness: touch-wake completed")
        return True

    def run(self, stop=None) -> None:
        """Block on the node for ever, re-resolving by identity when it vanishes.

        `stop` is a test seam: a callable that returns True to leave the loop.
        """
        while stop is None or not stop():
            device, backoff = None, self.retry_s
            try:
                device = self.resolve()
                self.last_note = None  # a real resolution ends the quiet period
                for event in device.read_loop():
                    if is_contact_start(event):
                        self.contact()
                    if stop is not None and stop():
                        return
            except Refused as error:
                if str(error) == "touch-node-grabbed":
                    # Steady state, not a fault: `adaptive` grabs the raw node
                    # and publishes accepted taps over its own bridge instead
                    # of a uinput device, so there is nothing here to witness
                    # until the mode changes or the daemon stops.
                    backoff = self.blocked_retry_s
                    self.note("touch-witness: the touchscreen node is grabbed by the touch "
                              "filter and this mode publishes no virtual node — touch wake "
                              "is unavailable until that changes")
                else:
                    self.note("touch-witness: node unavailable (%s) — re-resolving by identity"
                              % error)
            except (OSError, ValueError, ImportError) as error:
                self.note("touch-witness: node unavailable (%s) — re-resolving by identity"
                          % error)
            finally:
                if device is not None:
                    close_quietly(device)
            if stop is not None and stop():
                return
            self.sleep(backoff)


def start_touch_witness(helper, **kwargs) -> threading.Thread:
    """Start the witness as a daemon thread; it must never fail the helper."""
    witness = TouchWitness(helper, **kwargs)
    thread = threading.Thread(target=witness.run, name="touch-witness", daemon=True)
    thread.start()
    return thread


class Helper:
    """Execute validated requests; filesystem/network seams are injectable."""

    def __init__(self, host_path=HOST_PATH, state_path=STATE_PATH, auth_paths=LOCAL_AUTH_PATHS,
                 backup_path=HOST_BACKUP_PATH, panel_identity=None):
        self.host_path = Path(host_path)
        self.state_path = Path(state_path)
        self.auth_paths = tuple(Path(path) for path in auth_paths)
        self.backup_path = Path(backup_path)
        self.panel_identity = panel_identity

    def _panel(self):
        return self.panel_identity or __import__("pwd").getpwnam("panel")

    def _read(self) -> tuple[object, dict, dict]:
        panel = self._panel()
        host = _validate_host(private_object(self.host_path, absent={"enabled": False},
                                             group_read_gid=panel.pw_gid))
        state = _validate_state(private_object(self.state_path, absent={}))
        return panel, host, state

    def reconcile(self) -> tuple[object, dict, dict]:
        """Replay the host half when the durable local SSOT committed first."""
        panel, host, state = self._read()
        if state["localAccessEnabled"]:
            desired = merged_host(host)
            if host != desired:
                atomic_json(self.host_path, desired, mode=0o640, uid=0, gid=panel.pw_gid)
                host = desired
        return panel, host, state

    def wake(self, params: dict, *, witness: bool = False) -> dict:
        """Validate one wake and run the single power command.

        Contract:
          Inputs:  params: the socket peer's claim, or {"source": "touch"} from
                   the in-process witness; witness: keyword-only, True only for
                   that witness.
          Outputs: {"woke": True}.
          Raises:  Refused from validate_wake; subprocess errors unchanged.
        One place runs the power command for both paths; the arm differs so the
        journal attributes the restore to its evidence.
        Implements: LLR-912, LLR-913
        """
        if witness:
            validate_wake(params, {}, witness=True)
            command = TOUCH_POWER_COMMAND
        else:
            validate_wake(params, sensor_status())
            command = POWER_COMMAND
        subprocess.run(command, check=True, timeout=5, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"woke": True}

    def request(self, request: object) -> dict:
        if not isinstance(request, dict) or set(request) != {"method", "params"} or not isinstance(request["params"], dict):
            raise Refused("request-invalid")
        panel, host, state = self.reconcile()
        method, params = request["method"], request["params"]
        if method == "status":
            if params:
                raise Refused("request-invalid")
            return current_status(host, state, self.auth_paths)
        if method == "wake":
            # Never witness=True: a socket peer is not a witness (LLR-912).
            return self.wake(params)
        if method != "configure" or set(params) - {"expectedRevision", "accessMode", "session", "pin"}:
            raise Refused("request-invalid")
        if params.get("accessMode") != "local" or type(params.get("expectedRevision")) is not int:
            raise Refused("request-invalid")
        if params["expectedRevision"] != state["revision"]:
            raise Refused("stale-revision")
        status = current_status(host, state, self.auth_paths)
        if gateway_protected(host):
            verify_gateway(host, params.get("session"), params.get("pin"))
        elif not status["bootstrapAllowed"]:
            raise Refused("bootstrap-refused")
        new_state = {"schemaVersion": 1, "revision": state["revision"] + 1, "localAccessEnabled": True}
        if not self.backup_path.exists():
            atomic_json(self.backup_path, host, mode=0o600, uid=0, gid=0)
        atomic_json(self.state_path, new_state, mode=0o600, uid=0, gid=0)
        atomic_json(self.host_path, merged_host(host), mode=0o640, uid=0, gid=panel.pw_gid)
        return current_status(merged_host(host), new_state, self.auth_paths)


def serve(helper: Helper, socket_path: str = SETUP_SOCKET) -> None:
    """Serve the root-owned bounded local socket until systemd stops it."""
    # The state file is the commit record. Complete an interrupted host rename
    # before exposing the socket or allowing Electron to observe helper status.
    helper.reconcile()
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    panel = __import__("pwd").getpwnam("panel")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(socket_path)
        os.chown(socket_path, 0, panel.pw_gid)
        os.chmod(socket_path, 0o660)
        server.listen(8)
        while True:
            connection, _ = server.accept()
            with connection:
                try:
                    connection.settimeout(5)
                    peer = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                    _pid, uid, _gid = struct.unpack("3i", peer)
                    if uid != panel.pw_uid:
                        raise Refused("peer-refused")
                    data = bytearray()
                    while not data.endswith(b"\n") and len(data) <= MAX_REQUEST:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        data.extend(chunk)
                    if not data.endswith(b"\n") or len(data) > MAX_REQUEST:
                        raise Refused("request-invalid")
                    result = {"ok": True, "result": helper.request(json.loads(data))}
                except (Refused, OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
                    result = {"ok": False, "error": str(error) if isinstance(error, Refused) else "operation-failed"}
                connection.sendall(json.dumps(result, separators=(",", ":")).encode() + b"\n")


if __name__ == "__main__":
    _helper = Helper()
    # The witness is a thread of the existing power owner: no new privileged
    # process, no new socket, and no new path from the kiosk account to power.
    start_touch_witness(_helper)
    serve(_helper)
