#!/usr/bin/env python3
"""camera_clock.py - keep a cloud-less camera's clock right from the hub (SR-161).

The living-room camera (an EZVIZ, Hikvision family) takes its time only from
its vendor's cloud, which the router blocks, so it counts from 1970 at every
power-up and burns that date into every frame (measured 2026-11-09: after
power-up it asks DNS for its cloud login server and nothing else, no NTP). It
does accept a login on its device SDK port (8000) with its own username and
verification code, and a set-time there sticks until the next power cut
(proven 2026-11-09 15:33). So the hub sets it: homehub-camera-clock@<camera>.timer
runs `sync` every 10 minutes; `sync` logs in, reads the camera's clock, and
sets it to the hub's local time when it is off by more than MAX_DRIFT_SEC.
Nothing here depends on the vendor's cloud.

THE SDK is Hikvision's Device Network SDK for Linux 64 (HCNetSDK), a
proprietary library that is NOT in this public repository. sdk.manifest.json
pins the release zip's sha256 and every library file's; install-camera-clock-sdk.sh
puts the libraries in /opt/homehub-camera-clock/sdk, and `sync` refuses (78)
any other bytes. The unit gives the library no capabilities, a read-only
system, and IP access to private addresses only, so it cannot reach the
internet whatever it tries.

THE CREDENTIAL is the camera relay's (CAMERA_<C>_RELAY_UPSTREAM_USERNAME and
_PASSWORD; stack/camera-relay): read from the stack .env, handed to the library
in memory, never in argv, the environment or a log.

Verbs (all take --camera C):
    condition   exit 0 only when CAMERA_<C>_CLOCK_ENABLED is exactly true.
    check       the SDK pin and the settings; never a value.
    sync        log in, read, set when drifted, read back (the timer's job).
    read        log in and print the camera's clock (changes nothing).

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: SR-161, LLR-1512..LLR-1518.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import hashlib
import json
import os
import re
import sys
from typing import Callable, Dict, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "monitoring"))
sys.path.insert(0, os.path.join(HERE, "..", "door"))
sys.path.insert(0, os.path.join(HERE, "..", "camera-relay"))
import monitor_common as mc  # noqa: E402
import door_relay  # noqa: E402
import camera_relay  # noqa: E402

MANIFEST_FILE = os.path.join(HERE, "sdk.manifest.json")
DEFAULT_SDK_DIR = "/opt/homehub-camera-clock/sdk"
STATE_DIR = "/var/lib/homehub-camera-clock"
EXIT_REFUSED = 78
SANE_YEAR = 2024          # a camera clock before this is a fresh power-up, whatever the drift

# HCNetSDK (incEn/HCNetSDK.h, V6.1.11.40)
NET_DVR_GET_TIMECFG = 118
NET_DVR_SET_TIMECFG = 119
INIT_SDK_PATH, INIT_LIBEAY_PATH, INIT_SSLEAY_PATH = 2, 3, 4
LOGIN_OFFSET_V40_CALLBACK = 296   # the header pins this; a wrong layout hands the SDK a bad pointer


class Invalid(ValueError):
    """A configuration field is missing or malformed; the message names the KEY only."""


def knob(camera: str, suffix: str) -> str:
    return "CAMERA_%s_CLOCK_%s" % (camera.upper(), suffix)


class ClockSettings:
    """Camera C's clock knobs, read from the stack .env.

    Contract:
      Config:  CAMERA_<C>_CLOCK_ENABLED (false); _PORT (8000); _MAX_DRIFT_SEC
               (5, 1..3600); CAMERA_CLOCK_SDK_DIR (/opt/homehub-camera-clock/sdk);
               the host, username and password of camera C's relay
               (CAMERA_<C>_RELAY_UPSTREAM_*).  [stack/.env]
      Raises:  Invalid naming the key, never a value.
    Implements: LLR-1512.
    """

    def __init__(self, env_file: str, camera: str):
        self.camera = camera_relay.camera_name(camera)
        env = mc.read_knobs(env_file)
        self.enabled = mc.knob_bool(env, knob(camera, "ENABLED"), False)
        relay = camera_relay.CameraRelaySettings(env_file, camera)
        self.host, self.username, self.password = relay.host, relay.username, relay.password
        raw_port = env.get(knob(camera, "PORT"), "").strip() or "8000"
        if not re.fullmatch(r"[0-9]{1,5}", raw_port) or not 1 <= int(raw_port) <= 65535:
            raise Invalid("%s is not a port number" % knob(camera, "PORT"))
        self.port = int(raw_port)
        raw_drift = env.get(knob(camera, "MAX_DRIFT_SEC"), "").strip() or "5"
        if not re.fullmatch(r"[0-9]{1,4}", raw_drift) or not 1 <= int(raw_drift) <= 3600:
            raise Invalid("%s must be a whole number of seconds in 1..3600" % knob(camera, "MAX_DRIFT_SEC"))
        self.max_drift = int(raw_drift)
        self.sdk_dir = env.get("CAMERA_CLOCK_SDK_DIR", "").strip() or DEFAULT_SDK_DIR
        if not self.sdk_dir.startswith("/") or ".." in self.sdk_dir.split("/"):
            raise Invalid("CAMERA_CLOCK_SDK_DIR must be an absolute path without ..")
        if len(self.password.encode("utf-8")) >= 64 or len(self.username.encode("utf-8")) >= 64:
            raise Invalid("the camera's username or password is longer than the SDK accepts (63 bytes)")


# ── the SDK pin ──────────────────────────────────────────────────────────────


def read_manifest(path: str = MANIFEST_FILE) -> Dict[str, object]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_sdk(sdk_dir: str, manifest: Dict[str, object]) -> str:
    """The library directory when every pinned file is present with its bytes,
    and nothing unpinned sits beside them; Invalid otherwise. Implements: LLR-1513."""
    lib = os.path.join(sdk_dir, "lib")
    files = manifest["files"]  # type: ignore[index]
    for rel, want in sorted(files.items()):  # type: ignore[union-attr]
        path = os.path.join(lib, rel)
        try:
            got = sha256_file(path)
        except OSError:
            raise Invalid("SDK file %s is missing from %s (install-camera-clock-sdk.sh)" % (rel, lib))
        if got != want:
            raise Invalid("SDK file %s is not the pinned bytes (sdk.manifest.json)" % rel)
    present = set()
    for root, _dirs, names in os.walk(lib):
        for name in names:
            present.add(os.path.relpath(os.path.join(root, name), lib).replace(os.sep, "/"))
    extra = sorted(present - set(files))  # type: ignore[arg-type]
    if extra:
        raise Invalid("SDK directory holds %d unpinned file(s), e.g. %s" % (len(extra), extra[0]))
    return lib


# ── the SDK, through ctypes ──────────────────────────────────────────────────


class _SdkPath(ctypes.Structure):
    _fields_ = [("sPath", ctypes.c_char * 256), ("byRes", ctypes.c_ubyte * 128)]


class _LoginInfo(ctypes.Structure):
    _fields_ = [("sDeviceAddress", ctypes.c_char * 129), ("byUseTransport", ctypes.c_ubyte),
                ("wPort", ctypes.c_ushort), ("sUserName", ctypes.c_char * 64), ("sPassword", ctypes.c_char * 64),
                ("cbLoginResult", ctypes.c_void_p), ("pUser", ctypes.c_void_p), ("bUseAsynLogin", ctypes.c_int),
                ("byProxyType", ctypes.c_ubyte), ("byUseUTCTime", ctypes.c_ubyte), ("byLoginMode", ctypes.c_ubyte),
                ("byHttps", ctypes.c_ubyte), ("iProxyID", ctypes.c_int), ("byVerifyMode", ctypes.c_ubyte),
                ("byRes1", ctypes.c_ubyte * 3), ("cbLoginResultV40", ctypes.c_void_p),
                ("byRes3", ctypes.c_ubyte * 109)]


class _Time(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint) for n in ("dwYear", "dwMonth", "dwDay", "dwHour", "dwMinute", "dwSecond")]


class SdkError(RuntimeError):
    """An SDK call failed; carries the call's name and the SDK's error number only."""


class HikSdk:
    """The four calls this needs: login, get time, set time, logout. One
    instance per run; close() always cleans up. Implements: LLR-1514."""

    def __init__(self, lib_dir: str, loader: Callable[[str], object] = ctypes.CDLL):
        assert _LoginInfo.cbLoginResultV40.offset == LOGIN_OFFSET_V40_CALLBACK
        self.sdk = loader(os.path.join(lib_dir, "libhcnetsdk.so"))
        path = _SdkPath()
        path.sPath = lib_dir.encode()
        self.sdk.NET_DVR_SetSDKInitCfg(INIT_SDK_PATH, ctypes.byref(path))
        self.sdk.NET_DVR_SetSDKInitCfg(INIT_LIBEAY_PATH, ctypes.c_char_p(os.path.join(lib_dir, "libcrypto.so.3").encode()))
        self.sdk.NET_DVR_SetSDKInitCfg(INIT_SSLEAY_PATH, ctypes.c_char_p(os.path.join(lib_dir, "libssl.so.3").encode()))
        if not self.sdk.NET_DVR_Init():
            raise SdkError("NET_DVR_Init failed (SDK error %d)" % self.sdk.NET_DVR_GetLastError())
        # Level 0 closes the SDK's own file log (its HCNetSDK_Log_Switch.xml asks
        # for INFO into ./SDKLOG/): nothing of the camera's is written anywhere.
        self.sdk.NET_DVR_SetLogToFile(0, None, 1)
        self.sdk.NET_DVR_SetConnectTime(5000, 1)
        self.uid = -1

    def login(self, host: str, port: int, username: str, password: str) -> None:
        info = _LoginInfo()
        info.sDeviceAddress = host.encode()
        info.wPort = port
        info.sUserName = username.encode("utf-8")
        info.sPassword = password.encode("utf-8")
        info.bUseAsynLogin = 0
        info.byLoginMode = 0   # the private protocol on the SDK port
        device = (ctypes.c_ubyte * 1024)()
        self.uid = self.sdk.NET_DVR_Login_V40(ctypes.byref(info), ctypes.byref(device))
        ctypes.memset(ctypes.addressof(info), 0, ctypes.sizeof(info))
        if self.uid < 0:
            raise SdkError("login refused (SDK error %d)" % self.sdk.NET_DVR_GetLastError())

    def get_time(self) -> datetime.datetime:
        t = _Time()
        got = ctypes.c_uint(0)
        if not self.sdk.NET_DVR_GetDVRConfig(self.uid, NET_DVR_GET_TIMECFG, 0, ctypes.byref(t), ctypes.sizeof(t),
                                             ctypes.byref(got)):
            raise SdkError("get time failed (SDK error %d)" % self.sdk.NET_DVR_GetLastError())
        return datetime.datetime(t.dwYear, t.dwMonth, t.dwDay, t.dwHour, t.dwMinute, t.dwSecond)

    def set_time(self, when: datetime.datetime) -> None:
        t = _Time(when.year, when.month, when.day, when.hour, when.minute, when.second)
        if not self.sdk.NET_DVR_SetDVRConfig(self.uid, NET_DVR_SET_TIMECFG, 0, ctypes.byref(t), ctypes.sizeof(t)):
            raise SdkError("set time failed (SDK error %d)" % self.sdk.NET_DVR_GetLastError())

    def close(self) -> None:
        try:
            if self.uid >= 0:
                self.sdk.NET_DVR_Logout(self.uid)
        finally:
            self.uid = -1
            self.sdk.NET_DVR_Cleanup()


# ── the decision ─────────────────────────────────────────────────────────────


def needs_set(camera_time: datetime.datetime, hub_time: datetime.datetime, max_drift: int) -> Tuple[bool, float]:
    """(set it?, drift in seconds, camera minus hub). A camera before SANE_YEAR is
    a fresh power-up and is always set. Implements: LLR-1515."""
    drift = (camera_time - hub_time).total_seconds()
    return (camera_time.year < SANE_YEAR or abs(drift) > max_drift), drift


def local_now() -> datetime.datetime:
    """The hub's local wall time to the second (the camera keeps local time)."""
    return datetime.datetime.now().replace(microsecond=0)


def sync(s: ClockSettings, sdk, now: Callable[[], datetime.datetime] = local_now,
         log: Callable[..., None] = mc.log) -> Dict[str, object]:
    """One pass: log in, read, set when needed, read back. Returns the status
    record; raises SdkError. The caller closes the SDK. Never logs a
    credential. Implements: LLR-1516."""
    tag = "camera clock [%s]" % s.camera
    sdk.login(s.host, s.port, s.username, s.password)
    before = sdk.get_time()
    hub = now()
    set_it, drift = needs_set(before, hub, s.max_drift)
    record = {"camera": s.camera, "camera_time": before.isoformat(), "hub_time": hub.isoformat(),
              "drift_sec": round(drift, 1), "set": False}
    if set_it:
        target = now()
        sdk.set_time(target)
        after = sdk.get_time()
        residual = (after - now()).total_seconds()
        record.update(set=True, set_to=target.isoformat(), after=after.isoformat(), residual_sec=residual)
        if abs(residual) > s.max_drift:
            raise SdkError("the camera did not keep the time it was given (off by %ds after setting)" % residual)
        log("%s: was %s (%+.0fs from the hub), set to %s, reads %s"
            % (tag, before.isoformat(sep=" "), drift, target.isoformat(sep=" "), after.isoformat(sep=" ")))
    return record


def write_status(camera: str, record: Dict[str, object], state_dir: str = STATE_DIR) -> None:
    try:
        os.makedirs(state_dir, mode=0o750, exist_ok=True)
        path = os.path.join(state_dir, "%s.json" % camera)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        mc.log("camera clock [%s]: status not written (%s)" % (camera, type(exc).__name__), warning=True)


def main(argv: Optional[Sequence[str]] = None, sdk_factory: Optional[Callable[[str], object]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", default=mc.DEFAULT_ENV_FILE)
    p.add_argument("--camera", required=True)
    sub = p.add_subparsers(dest="verb")
    for verb in ("condition", "check", "sync", "read"):
        sub.add_parser(verb)
    args = p.parse_args(argv)
    try:
        camera = camera_relay.camera_name(args.camera)
    except door_relay.Invalid as exc:
        mc.log("camera clock: REFUSED - %s" % exc, warning=True)
        return 2
    tag = "camera clock [%s]" % camera
    if args.verb == "condition":
        on = mc.knob_bool(mc.read_knobs(args.env_file), knob(camera, "ENABLED"), False)
        mc.log("%s: %s is %s" % (tag, knob(camera, "ENABLED"), "true - running" if on else "not true - clean skip"))
        return 0 if on else 1
    try:
        s = ClockSettings(args.env_file, camera)
        lib = verify_sdk(s.sdk_dir, read_manifest())
    except (Invalid, door_relay.Invalid) as exc:
        if args.verb == "check":
            door_relay.emit({"camera": camera, "eligible": False, "reason": str(exc)})
            return 1
        mc.log("%s: REFUSED - %s" % (tag, exc), warning=True)
        return EXIT_REFUSED
    if args.verb == "check":
        door_relay.emit({"camera": camera, "eligible": True, "enabled": s.enabled, "port": s.port,
                         "max_drift_sec": s.max_drift, "sdk": read_manifest().get("release")})
        return 0
    factory = sdk_factory or HikSdk
    try:
        sdk = factory(lib)
        try:
            if args.verb == "read":
                sdk.login(s.host, s.port, s.username, s.password)
                door_relay.emit({"camera": camera, "camera_time": sdk.get_time().isoformat(),
                                 "hub_time": local_now().isoformat()})
                return 0
            record = sync(s, sdk)
        finally:
            sdk.close()
    except SdkError as exc:
        mc.log("%s: %s" % (tag, exc), warning=True)
        write_status(camera, {"camera": camera, "ok": False, "error": str(exc),
                              "at": local_now().isoformat()})
        return 1
    write_status(camera, dict(record, ok=True, at=local_now().isoformat()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
