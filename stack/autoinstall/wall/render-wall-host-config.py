#!/usr/bin/env python3
"""Merge panel-local renderer secrets into the private Electron host JSON.

The resulting JSON is written to stdout for wall-firstboot.sh to capture in a
0600 temporary file.  Existing access-broker registration is preserved; the
renderer subset is replaced on every run so removing a wall.env value revokes
it instead of retaining stale credentials. Implements: SR-017.

WALL_ACCESS_MODE additionally owns the ACCESS half of the same file, and only
the two fields a panel-local lock needs (Owner ruling 2026-09-13, "local
authentication mode"):

  unset / "gateway"  the access half is left exactly as it is -- today's
                     behaviour, and the reversal path: a file that currently
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


def render(existing_path: Path, environ: dict[str, str]) -> dict:
    """Return host config with the exact wall.env-owned renderer allowlist."""
    if existing_path.exists():
        info = existing_path.stat()
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
            raise ValueError("existing host config is not a private regular file")
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
    mode = environ.get("WALL_ACCESS_MODE", "").strip().lower() or "gateway"
    if mode not in ("gateway", "local"):
        raise ValueError("WALL_ACCESS_MODE must be empty, gateway or local")
    if mode == "local":
        document["accessMode"] = "local"
        document["enabled"] = True
    elif document.get("accessMode") == "local":
        # Reversal. The panel goes back to being enabled only if it still holds
        # a real gateway registration; otherwise it returns to the unprovisioned
        # posture rather than to an `enabled` flag with nothing behind it.
        document.pop("accessMode", None)
        document["enabled"] = bool(document.get("gatewayUrl")) and bool(document.get("deviceCredential"))
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
    if len(argv) != 2:
        raise SystemExit("usage: render-wall-host-config.py EXISTING_OR_TARGET_PATH")
    try:
        document = render(Path(argv[1]), os.environ)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise SystemExit("private host config could not be rendered; no values were printed")
    json.dump(document, sys.stdout, separators=(",", ":"), sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
