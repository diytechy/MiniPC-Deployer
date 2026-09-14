#!/usr/bin/env python3
"""Merge panel-local renderer secrets into the private Electron host JSON.

The resulting JSON is written to stdout for wall-firstboot.sh to capture in a
0600 temporary file.  Existing access-broker registration is preserved; the
renderer subset is replaced on every run so removing a wall.env value revokes
it instead of retaining stale credentials. Implements: SR-017.

WALL_ACCESS_MODE additionally owns the ACCESS half of the same file, and only
the two fields a panel-local lock needs (Owner ruling 2026-09-13, "local
authentication mode"):

  unset              the access half is left exactly as it is.
  "gateway"          explicit reversal: a file that currently
                     says accessMode "local" is returned to its gateway
                     registration, enabled only if that registration is
                     actually present.
  "local"            accessMode "local", enabled true. NO credential is
                     written, because local mode has none: the PIN is set at
                     the wall, in Settings, and lives scrypt-hashed inside the
                     panel's own encrypted access state under /var/lib.

A gatewayUrl/deviceId/deviceCredential left over from an earlier registration
is PRESERVED and ignored while local mode is selected, so flipping back is this
one knob rather than a re-registration.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path


def render(existing_path: Path, environ: dict[str, str], local_state_path: Path | None = None) -> dict:
    """Return host config with renderer values and the local capability overlay."""
    if existing_path.exists() or existing_path.is_symlink():
        info = existing_path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and mode not in (0o600, 0o640)):
            raise ValueError("existing host config is not a private regular file")
        if os.name != "nt":
            import pwd
            panel = pwd.getpwnam("panel")
            parent = existing_path.parent.stat()
            valid_owner = ((mode == 0o640 and info.st_uid == 0 and info.st_gid == panel.pw_gid)
                           or (mode == 0o600 and info.st_uid in (0, panel.pw_uid)))
            if not valid_owner or parent.st_uid != 0 or parent.st_mode & 0o022:
                raise ValueError("existing host config has an invalid owner")
        document = json.loads(existing_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("existing host config is not an object")
    else:
        document = {"enabled": False}
    def value(name: str) -> str:
        result = environ.get(name, "")
        if not isinstance(result, str) or len(result) > 4096 or "\0" in result:
            raise ValueError("wall.env renderer value is invalid")
        return result
    raw_mode = environ.get("WALL_ACCESS_MODE", "").strip().lower()
    mode = raw_mode or None
    if mode not in (None, "gateway", "local"):
        raise ValueError("WALL_ACCESS_MODE must be empty, gateway or local")
    prior_access_mode = document.get("accessMode")
    prior_enabled = document.get("enabled", False)
    was_local = prior_access_mode == "local"
    gateway_was_protected = prior_enabled is True and not was_local
    if mode == "local":
        document["accessMode"] = "local"
        document["enabled"] = True
        document["localAccessEnabled"] = True
    elif mode == "gateway" and document.get("accessMode") == "local":
        # Reversal. The panel goes back to being enabled only if it still holds
        # a real gateway registration; otherwise it returns to the unprovisioned
        # posture rather than to an `enabled` flag with nothing behind it.
        document.pop("accessMode", None)
        # ALL THREE registration fields, not two: access-broker readConfig
        # refuses a config with a gatewayUrl and a credential but no deviceId,
        # and the panel then boots into its locked-and-unavailable fallback
        # while firstboot cheerfully reports access as enabled. Terra round 1.
        document["enabled"] = all(bool(document.get(field)) for field in ("gatewayUrl", "deviceId", "deviceCredential"))
    document.update({
        "localCapabilitiesVersion": 1,
        "localFacePolicyVersion": 1,
        "sensorSocket": "/run/wall-sensors/service.sock",
        "sensorModelManifest": "/opt/wall-sensors/models/manifest.json",
        "localSetupSocket": "/run/wall-local-setup/service.sock",
    })
    if local_state_path is not None and local_state_path.exists():
        state = json.loads(local_state_path.read_text(encoding="utf-8"))
        if set(state) != {"schemaVersion", "revision", "localAccessEnabled"} or state.get("schemaVersion") != 1:
            raise ValueError("local capability state is invalid")
        if type(state.get("revision")) is not int or state["revision"] < 0 or type(state.get("localAccessEnabled")) is not bool:
            raise ValueError("local capability state is invalid")
        document["localAccessEnabled"] = state["localAccessEnabled"]
        if not state["localAccessEnabled"] and mode == "local":
            if prior_access_mode is None:
                document.pop("accessMode", None)
            else:
                document["accessMode"] = prior_access_mode
            document["enabled"] = prior_enabled
        if state["localAccessEnabled"] and not gateway_was_protected:
            document["accessMode"] = "local"
            document["enabled"] = True
    document["rendererConfig"] = {
        "FEED_TOKEN": value("WALL_SHELL_FEED_TOKEN"),
        "HEARTBEAT_URL": value("WALL_SHELL_HEARTBEAT_URL"),
        "SUBSONIC": {
            "user": value("WALL_SHELL_SUBSONIC_USER"),
            "password": value("WALL_SHELL_SUBSONIC_PASSWORD"),
        },
    }
    return document


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        raise SystemExit("usage: render-wall-host-config.py EXISTING_OR_TARGET_PATH [LOCAL_CAPABILITY_STATE]")
    try:
        state_path = Path(argv[2]) if len(argv) == 3 else None
        document = render(Path(argv[1]), os.environ, state_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise SystemExit("private host config could not be rendered; no values were printed")
    json.dump(document, sys.stdout, separators=(",", ":"), sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
