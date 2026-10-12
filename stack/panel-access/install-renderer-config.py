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


DEFAULT_SOURCES = "codex,claude,opencode"  # the feeder's own default; a test reads it from the feeder
_ASSIGN = re.compile(r"^\s*(export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")


def logical_lines(env_text: str):
    """Physical lines joined across a trailing backslash, as systemd does."""
    pending = ""
    for line in env_text.replace("\r\n", "\n").split("\n"):
        stripped = line.rstrip()
        trailing = len(stripped) - len(stripped.rstrip("\\"))
        if trailing % 2 == 1:
            pending += stripped[:-1]
            continue
        yield pending + line
        pending = ""
    if pending:
        yield pending


def read_env(env_text: str, names):
    """({name: value}, unsupported) for the NAMES asked for, the way systemd's
    EnvironmentFile reads them: a comment is '#' or ';' at line start only (an
    inline ' # note' is part of the value), the LAST assignment wins, one pair of
    matching quotes is stripped. `export NAME=` and indented `NAME=` are not read
    by systemd as written, so they are reported in `unsupported` (fail closed)."""
    values = {}
    unsupported = set()
    for line in logical_lines(env_text):
        match = _ASSIGN.match(line)
        if not match or match.group(2) not in names:
            continue
        name = match.group(2)
        if match.group(1) or line != line.lstrip():
            unsupported.add(name)
            continue
        value = line.partition("=")[2].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if "\\" in value:
            unsupported.add(name)
            continue
        values[name] = value
    return values, unsupported


def listed_sources(sources_text: str):
    """Usable provider ids from a comma list (lower-cased, de-duplicated)."""
    listed = []
    for raw in sources_text.split(","):
        name = raw.strip().lower()
        if PROVIDER_KEY.match(name) and name not in listed:
            listed.append(name)
    return listed


def derive_rings(listed):
    """Build the gaugeRings block for usable source names (maybe an empty block)."""
    ordered = [n for n in PROVIDER_ORDER if n in listed]
    ordered += [n for n in listed if n not in PROVIDER_ORDER]
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


def _no_constant(token):
    raise ValueError("non-finite number")


def serialise(source_text: str, document) -> bytes:
    """Re-serialise with the source's indentation and final newline. The result is
    value-faithful, not byte-faithful: non-ASCII is written as \\u escapes, line
    endings become LF and a BOM is dropped."""
    match = re.search(r"\n([ \t]+)\S", source_text)
    indent = match.group(1) if match else None
    text = json.dumps(document, indent=indent, ensure_ascii=True, allow_nan=False)
    if source_text.rstrip(" \t").endswith("\n"):
        text += "\n"
    return text.encode("ascii")


def derive(env_text: str, source_bytes: bytes, document) -> bytes:
    values, unsupported = read_env(env_text, ("AI_USAGE_ENABLED", "AI_USAGE_SOURCES"))
    if unsupported:
        print("gaugeRings: %s is written in a form systemd does not read; not derived"
              % ", ".join(sorted(unsupported)))
        return source_bytes
    enabled = values.get("AI_USAGE_ENABLED", "")
    if enabled != "true":
        if enabled not in ("", "false"):
            print("gaugeRings: AI_USAGE_ENABLED is not exactly true or false; not derived")
        return source_bytes
    raw = values.get("AI_USAGE_SOURCES", "")
    if not raw.replace(",", "").strip():
        raw = DEFAULT_SOURCES  # the feeder's default for an unset or empty list
    listed = listed_sources(raw)
    existing = document.get("gaugeRings")
    marked = isinstance(existing, dict) and MARKER in existing
    if not listed and not marked:
        print("gaugeRings: AI_USAGE_SOURCES lists no usable source; nothing derived")
        return source_bytes
    owned = "gaugeRings" not in document or marked or existing == SHIPPED_DEFAULT
    if not owned:
        print("gaugeRings: Owner-written block in the source left as is; not derived")
        return source_bytes
    document = dict(document)
    document["gaugeRings"] = derive_rings(listed)
    out = serialise(source_bytes.decode("utf-8-sig"), document)
    checked = json.loads(out.decode("ascii"), parse_constant=_no_constant)
    MODULE.validate(env_text, checked)
    return out


def render(env_text: str, source_bytes: bytes, document) -> bytes:
    """The bytes to install: the source itself unless a derivation applies, and
    the source itself again if the derivation fails for ANY reason (a derivation
    problem must never abort firstboot)."""
    try:
        return derive(env_text, source_bytes, document)
    except Exception:  # noqa: BLE001 - deliberate: fall back, never abort
        print("gaugeRings: WARNING derivation failed; installing the source config unchanged")
        return source_bytes


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
