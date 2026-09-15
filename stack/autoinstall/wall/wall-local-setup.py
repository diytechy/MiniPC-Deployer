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


def validate_wake(params: dict, status: dict, now_ms: int | None = None) -> None:
    """Require the broker claim to match a fresh positive sensor observation."""
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
            status = sensor_status()
            validate_wake(params, status)
            subprocess.run(POWER_COMMAND, check=True, timeout=5, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"woke": True}
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
    serve(Helper())
