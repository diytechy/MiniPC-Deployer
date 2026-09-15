#!/usr/bin/env python3
"""Derive whether saved sensor wake settings require an awake CPU.

Contract:
  Input: sensor config JSON path.
  Output: exactly KEEP_AWAKE=true|false|unknown.
Implements: SR-020, LLR-020
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def keep_awake(value: object) -> bool:
    """Return true when a configured software wake source needs the CPU."""
    if not isinstance(value, dict) or value.get("schemaVersion") != 2:
        raise ValueError("unsupported sensor config")
    booleans = ("cameraEnabled", "presenceFace", "presenceMotion", "faceLoginEnabled", "bluetoothEnabled")
    if any(type(value.get(key)) is not bool for key in booleans) or value.get("cameraConsentVersion") not in (0, 1):
        raise ValueError("invalid sensor config")
    camera = value["cameraEnabled"] and value["cameraConsentVersion"] == 1 and any(
        value[key] for key in ("presenceFace", "presenceMotion", "faceLoginEnabled")
    )
    return camera or value["bluetoothEnabled"]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("KEEP_AWAKE=unknown")
        return 0
    try:
        value = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        answer = "true" if keep_awake(value) else "false"
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        answer = "unknown"
    print("KEEP_AWAKE=" + answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
