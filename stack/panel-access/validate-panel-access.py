#!/usr/bin/env python3
"""Validate the hub/renderer access cutover without printing configuration values.

Inputs: --env literal compose env file, --config renderer JSON. No writes.
Exit 0 means policies agree; 1 means deployment must not proceed.
Implements: SR-016 (panel route boundary), SR-017 (provisioned thin client).
"""
import argparse
import json
from pathlib import Path
import sys


def validate(env_text, config):
    """Refuse split policy and renderer access credentials. Implements: SR-016."""
    enabled = "false"
    for line in env_text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "PANEL_ACCESS_ENABLED":
            enabled = value.split(" #", 1)[0].strip().strip("\"'")
    if enabled not in ("true", "false"):
        raise ValueError("PANEL_ACCESS_ENABLED must be true or false")
    if not isinstance(config, dict) or type(config.get("ACCESS_ENABLED", False)) is not bool:
        raise ValueError("ACCESS_ENABLED must be a JSON boolean")
    if config.get("ACCESS_ENABLED", False) != (enabled == "true"):
        raise ValueError("Hub and renderer access policies disagree")
    forbidden = {
        "feed_token", "heartbeat_url", "devicecredential", "session",
        "wrappingkey", "pinverifier",
    }
    def inspect(value, in_subsonic=False):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = key.lower()
                if ((normalized in forbidden)
                        or (in_subsonic and normalized in {"user", "password"})) and child:
                    raise ValueError("Hub renderer config contains access credentials")
                inspect(child, in_subsonic or normalized == "subsonic")
        elif isinstance(value, list):
            for child in value:
                inspect(child, in_subsonic)
    inspect(config)


def main():
    """Read candidate files and print only the verdict. Implements: SR-016."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        validate(Path(args.env).read_text(encoding="utf-8-sig"), json.loads(Path(args.config).read_text(encoding="utf-8-sig")))
    except ValueError as error:
        # JSON syntax errors can echo document text; deliberately avoid them.
        message = "Invalid renderer JSON" if isinstance(error, json.JSONDecodeError) else str(error)
        print("FAIL panel access: " + message, file=sys.stderr)
        return 1
    except OSError:
        print("FAIL panel access: candidate configuration is unreadable", file=sys.stderr)
        return 1
    print("PASS panel access: hub and renderer policy agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
