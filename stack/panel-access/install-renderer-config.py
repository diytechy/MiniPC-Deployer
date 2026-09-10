#!/usr/bin/env python3
"""Validate and atomically install one nonsecret hub renderer config.

The bytes validated are the bytes installed. Errors never include source
contents, because a rejected legacy input may still contain a secret.
Implements: SR-016, SR-017.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import tempfile


VALIDATOR = Path(__file__).with_name("validate-panel-access.py")
SPEC = importlib.util.spec_from_file_location("validate_panel_access", VALIDATOR)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def install(env_path: Path, source: Path, destination: Path) -> None:
    """Validate source against hub policy, then atomically install it 0644."""
    env_text = env_path.read_text(encoding="utf-8-sig")
    source_bytes = source.read_bytes()
    document = json.loads(source_bytes.decode("utf-8-sig"))
    MODULE.validate(env_text, document)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".config.json.", dir=str(destination.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(source_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    try:
        install(args.env, args.source, args.destination)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise SystemExit("hub renderer config rejected; no configuration values were printed")
    print("PASS hub renderer config is nonsecret and policy-matched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
