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
SHIPPED_DEFAULT = {"slots": [], "unlisted": "append"}
MARKER = "derivedFrom"


# The feeder is the authority on which sources run. Its enabled_sources() is
# loaded by path so the renderer cannot drift from it.
FEEDER_PATH = Path(__file__).resolve().parent.parent / "ai-usage" / "ai_usage_feeder.py"
_ASSIGN = re.compile(r"^(\s*)(export\s+)?([A-Za-z_][A-Za-z0-9_]*)(\s*)=(.*)$")
READ_KEYS = ("AI_USAGE_ENABLED", "AI_USAGE_SOURCES")


def read_env(env_text: str, names):
    """{name: (value, problem)} from the FINAL assignment of each NAME.

    Supported form: `NAME=VALUE` at column 0, one line, no space before `=`, the
    value optionally wrapped in ONE matching pair of quotes (what the materialiser
    and apply-env-lines.py write). Anything else on the final assignment gives
    (None, reason). Earlier assignments are ignored, supported or not. An unquoted
    value is trimmed (as systemd delivers it); a quoted one is kept as written."""
    final = {}
    for line in env_text.replace("\r\n", "\n").split("\n"):
        match = _ASSIGN.match(line)
        if match and match.group(3) in names:
            final[match.group(3)] = match
    result = {}
    for name, match in final.items():
        indent, export, _, space, rest = match.groups()
        value = rest.strip()
        problem = None
        if indent:
            problem = "the assignment is indented"
        elif export:
            problem = "the assignment starts with export"
        elif space:
            problem = "there is a space before ="
        elif "\\" in rest:
            problem = "the value contains a backslash"
        elif value[:1] in ("'", '"'):
            quote = value[0]
            if len(value) < 2 or value[-1] != quote or quote in value[1:-1]:
                problem = "the value has an unmatched or multiline quote"
            else:
                value = value[1:-1]
        elif "'" in value or '"' in value:
            problem = "the value has a stray quote"
        result[name] = (None, problem) if problem else (value, None)
    return result


def feeder_sources(sources_value):
    """The feeder's own enabled_sources() for the value (None = knob unset)."""
    spec = importlib.util.spec_from_file_location("ai_usage_feeder_for_rings", FEEDER_PATH)
    feeder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(feeder)
    names = feeder.enabled_sources({} if sources_value is None else {"AI_USAGE_SOURCES": sources_value})
    listed = []
    for name in names:
        if name not in listed:
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
    existing = document.get("gaugeRings")
    marked = isinstance(existing, dict) and MARKER in existing
    owned = "gaugeRings" not in document or marked or existing == SHIPPED_DEFAULT

    def put(block):
        if not owned:
            print("gaugeRings: Owner-written block in the source left as is; not derived")
            return source_bytes
        changed = dict(document)
        changed["gaugeRings"] = block
        out = serialise(source_bytes.decode("utf-8-sig"), changed)
        MODULE.validate(env_text, json.loads(out.decode("ascii"), parse_constant=_no_constant))
        return out

    def fail_safe(reason):
        # Never keep a stale derived block, never hide anything we cannot account for.
        print("gaugeRings: WARNING %s; installing the append default so every served ring shows"
              % reason)
        return put(dict(SHIPPED_DEFAULT))

    values = read_env(env_text, READ_KEYS)
    enabled, problem = values.get("AI_USAGE_ENABLED", ("", None))
    if problem:
        return fail_safe("AI_USAGE_ENABLED is unsupported (%s)" % problem)
    if enabled != "true":
        if enabled not in ("", "false"):
            print("gaugeRings: AI_USAGE_ENABLED is not exactly true or false; not derived")
        return source_bytes
    raw, problem = values.get("AI_USAGE_SOURCES", (None, None))
    if problem:
        return fail_safe("AI_USAGE_SOURCES is unsupported (%s)" % problem)
    try:
        listed = feeder_sources(raw)
    except (SystemExit, Exception) as error:  # noqa: BLE001 - the feeder refuses or is absent
        return fail_safe("the feeder's source list could not be resolved (%s)" % type(error).__name__)
    return put(derive_rings(listed))


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
