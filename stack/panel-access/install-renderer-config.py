#!/usr/bin/env python3
"""Validate and atomically install one nonsecret hub renderer config.

The bytes validated are the bytes installed. Errors never include source
contents, because a rejected legacy input may still contain a secret.
Also derives the panel's AI `gaugeRings` block from AI_USAGE_SOURCES (see
README.md, "Derived AI rings"). The SOURCE file is never modified.
Implements: SR-016, SR-017, SR-081, LLR-1121, LLR-1122, LLR-1123, LLR-1124, LLR-1125.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile


VALIDATOR = Path(__file__).with_name("validate-panel-access.py")
SPEC = importlib.util.spec_from_file_location("validate_panel_access", VALIDATOR)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


# Mirrors USAGE_PROVIDER_ORDER in OfficeWallNaglight js/views/gauges.js; the
# tests read that file and compare, so drift fails there rather than here.
PROVIDER_ORDER = ("claude", "claude2", "codex", "grok", "gemini", "opencode", "freellmapi")
PROVIDER_KEY = re.compile(r"^[a-z0-9]+$")  # same rule normalizeGaugeRings applies
SHIPPED_DEFAULT = {"slots": [], "unlisted": "append"}
MARKER = "derivedFrom"


def env_value(env_text: str, name: str) -> str:
    """Last assignment wins; strips whitespace, a trailing ' #' comment and quotes."""
    found = ""
    for line in env_text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            found = value.split(" #", 1)[0].strip().strip("\"'")
    return found


def derive_rings(sources_text: str):
    """Build the gaugeRings block for a comma list of usage sources, or None if empty."""
    listed = []
    for raw in sources_text.split(","):
        name = raw.strip().lower()
        if PROVIDER_KEY.match(name) and name not in listed:
            listed.append(name)
    ordered = [n for n in PROVIDER_ORDER if n in listed]
    ordered += [n for n in listed if n not in PROVIDER_ORDER]
    if not ordered:
        return None
    paired = "claude" in ordered and "claude2" in ordered
    slots = []
    for name in ordered:
        slot = {"provider": name}
        if name in ("claude", "claude2"):
            slot["mark"] = "claude"
            if paired:
                slot["badge"] = "1" if name == "claude" else "2"
        slots.append(slot)
    return {"slots": slots, "unlisted": "hide", MARKER: "AI_USAGE_SOURCES"}


def serialise(source_text: str, document) -> bytes:
    """Re-serialise with the indentation the source uses (and its final newline)."""
    match = re.search(r"\n([ \t]+)\S", source_text)
    indent = match.group(1) if match else None
    text = json.dumps(document, indent=indent, ensure_ascii=False)
    if source_text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def render(env_text: str, source_bytes: bytes, document) -> bytes:
    """The bytes to install: the source itself unless a derivation applies."""
    if env_value(env_text, "AI_USAGE_ENABLED") != "true":
        return source_bytes
    block = derive_rings(env_value(env_text, "AI_USAGE_SOURCES"))
    if block is None:
        print("gaugeRings: AI_USAGE_SOURCES lists no usable source; nothing derived")
        return source_bytes
    existing = document.get("gaugeRings") if isinstance(document, dict) else None
    owned = (
        "gaugeRings" not in document
        or (isinstance(existing, dict) and MARKER in existing)
        or existing == SHIPPED_DEFAULT
    )
    if not owned:
        print("gaugeRings: Owner-written block in the source left as is; not derived")
        return source_bytes
    document["gaugeRings"] = block
    return serialise(source_bytes.decode("utf-8-sig"), document)


def install(env_path: Path, source: Path, destination: Path) -> None:
    """Validate source against hub policy, then atomically install it 0644."""
    env_text = env_path.read_text(encoding="utf-8-sig")
    source_bytes = source.read_bytes()
    document = json.loads(source_bytes.decode("utf-8-sig"))
    MODULE.validate(env_text, document)
    source_bytes = render(env_text, source_bytes, document)
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
