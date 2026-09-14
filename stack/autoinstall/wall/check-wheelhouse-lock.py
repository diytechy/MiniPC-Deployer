#!/usr/bin/env python3
"""Validate an offline sensor wheelhouse before pip is ever invoked on the panel.

`install-wall-capabilities.sh` installs with
`pip --no-index --only-binary=:all: --require-hashes`. pip does enforce the
hashes, but its failure modes are opaque (a missing wheel and an unpinned
transitive dependency both surface as a resolver error after pip has already
built a temporary environment). This check reads the lock the build produced
and refuses, with a specific message, a lock that is not fully `==`-pinned and
sha256-hashed, that names an index or a remote URL, or whose pinned
distributions are not all present as wheels in the wheelhouse directory.

Prints only package names and counts: a lock file carries no secrets, but the
installer's convention is that a failing check never dumps a config value.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

# The five distributions install-wall-capabilities.sh smoke-imports. The lock
# may (and will) carry more; it may not carry fewer.
DEFAULT_REQUIRED = ("numpy", "Pillow", "cryptography", "onnxruntime", "dbus-next")

FORBIDDEN_OPTIONS = ("--index-url", "-i", "--extra-index-url", "--find-links", "-f", "-e", "--editable", "-r", "--requirement")
PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*(?P<version>[A-Za-z0-9][A-Za-z0-9.*+!-]*)$")
HASH_RE = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def logical_lines(text: str) -> list[str]:
    """Join pip's line continuations, drop comments and blank lines."""
    joined: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if " #" in raw else raw.rstrip()
        if line.lstrip().startswith("#"):
            line = ""
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        buffer += line
        if buffer.strip():
            joined.append(" ".join(buffer.split()))
        buffer = ""
    if buffer.strip():
        joined.append(" ".join(buffer.split()))
    return joined


def parse_lock(text: str) -> dict[str, str]:
    """Return {canonical name: version}, raising ValueError on anything unsafe."""
    pinned: dict[str, str] = {}
    for entry in logical_lines(text):
        tokens = entry.split()
        head, rest = tokens[0], tokens[1:]
        for token in tokens:
            option = token.split("=", 1)[0]
            if option in FORBIDDEN_OPTIONS:
                raise ValueError(f"lock entry uses the forbidden option {option!r}; the wheelhouse must be self-contained")
        if "://" in entry:
            raise ValueError("lock entry names a remote URL; the panel never fetches packages")
        match = PIN_RE.match(head)
        if match is None:
            raise ValueError(f"lock entry {head!r} is not an exact name==version pin")
        hashes = [token for token in rest if HASH_RE.match(token)]
        if not hashes:
            raise ValueError(f"lock entry {head!r} carries no --hash=sha256: pin")
        if len(hashes) != len(rest):
            raise ValueError(f"lock entry {head!r} carries a token that is not a sha256 hash")
        name = canonical(match.group("name"))
        if name in pinned:
            raise ValueError(f"lock pins {match.group('name')!r} more than once")
        pinned[name] = match.group("version")
    if not pinned:
        raise ValueError("lock file pins nothing")
    return pinned


def wheel_names(wheelhouse: str) -> set[str]:
    found = set()
    for entry in os.listdir(wheelhouse):
        if entry.endswith(".whl"):
            found.add(canonical(entry.split("-", 1)[0]))
    return found


def check(wheelhouse: str, required: "list[str] | tuple[str, ...]" = DEFAULT_REQUIRED) -> dict[str, str]:
    lock_path = os.path.join(wheelhouse, "requirements.lock")
    with open(lock_path, encoding="utf-8") as handle:
        pinned = parse_lock(handle.read())
    missing_required = sorted(name for name in required if canonical(name) not in pinned)
    if missing_required:
        raise ValueError("lock does not pin the sensor service's own dependencies: " + ", ".join(missing_required))
    present = wheel_names(wheelhouse)
    absent = sorted(name for name in pinned if name not in present)
    if absent:
        raise ValueError("wheelhouse has no wheel for: " + ", ".join(absent))
    return pinned


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheelhouse")
    parser.add_argument("--require", action="append", default=None, help="distribution that must be pinned (repeatable)")
    args = parser.parse_args(argv)
    try:
        pinned = check(args.wheelhouse, args.require or DEFAULT_REQUIRED)
    except (OSError, ValueError) as error:
        print(f"[wheelhouse-lock] REFUSED: {error}", file=sys.stderr)
        return 1
    print(f"[wheelhouse-lock] OK {len(pinned)} pinned distributions, every one hashed and present as a wheel")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
