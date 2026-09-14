#!/usr/bin/env python3
"""Validate an offline sensor wheelhouse before pip is ever invoked on the panel.

`install-wall-capabilities.sh` installs with
`pip --no-index --only-binary=:all: --require-hashes`. pip does enforce the
hashes, but its failure modes are opaque (a missing wheel and an unpinned
transitive dependency both surface as a resolver error after pip has already
built a temporary environment). This check reads the lock the build produced
and refuses, with a specific message, a lock that is not fully `==`-pinned and
sha256-hashed, that names an index or a remote URL, or whose pinned
distributions are not all present, at the pinned version, as wheels in the
wheelhouse directory. With `--expect PATH` it also refuses a lock that is not
byte-identical to the reviewed lock that shipped inside the panel image, which
is the only thing that makes substituted removable media detectable.

Prints only package names and counts: a lock file carries no secrets, but the
installer's convention is that a failing check never dumps a config value.
"""
from __future__ import annotations

import argparse
import hashlib
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


def wheel_names(wheelhouse: str) -> set[tuple[str, str]]:
    """{(canonical name, version)} of the wheels on the media.

    Version matters: a wheelhouse holding numpy-9.9.9 for a numpy==1.26.4 pin is
    the wrong media, and saying so here is the point of running before pip.
    """
    found = set()
    for entry in os.listdir(wheelhouse):
        if entry.endswith(".whl"):
            fields = entry[: -len(".whl")].split("-")
            if len(fields) >= 5:
                found.add((canonical(fields[0]), fields[1]))
    return found


def expected_lock(lock_bytes: bytes, expected_path: str) -> None:
    """Refuse a lock that is not the one reviewed and shipped with the image.

    Hash-pinning a lock the media itself supplies proves only that the wheels
    match that lock. The authenticity anchor is the copy of requirements.lock
    that travelled in the panel image next to this script: swapped media has to
    match it byte for byte.
    """
    with open(expected_path, "rb") as handle:
        expected = handle.read()
    # Byte for byte on both sides: reading either one in text mode would let a
    # line-ending rewrite pass as identical.
    if hashlib.sha256(lock_bytes).hexdigest() != hashlib.sha256(expected).hexdigest():
        raise ValueError(
            "the wheelhouse's requirements.lock is not the reviewed lock shipped with this panel image "
            f"({os.path.basename(expected_path)}); rebuild the media from the tracked lock"
        )


def check(wheelhouse: str, required: "list[str] | tuple[str, ...]" = DEFAULT_REQUIRED,
          expect: "str | None" = None) -> dict[str, str]:
    lock_path = os.path.join(wheelhouse, "requirements.lock")
    with open(lock_path, "rb") as handle:
        lock_bytes = handle.read()
    if expect:
        expected_lock(lock_bytes, expect)
    pinned = parse_lock(lock_bytes.decode("utf-8"))
    missing_required = sorted(name for name in required if canonical(name) not in pinned)
    if missing_required:
        raise ValueError("lock does not pin the sensor service's own dependencies: " + ", ".join(missing_required))
    present = wheel_names(wheelhouse)
    absent = sorted(f"{name}=={version}" for name, version in pinned.items() if (name, version) not in present)
    if absent:
        wrong = sorted(name for name in pinned if any(name == found for found, _ in present)
                       and (name, pinned[name]) not in present)
        detail = "; wrong version present for: " + ", ".join(wrong) if wrong else ""
        raise ValueError("wheelhouse has no wheel for: " + ", ".join(absent) + detail)
    return pinned


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheelhouse")
    parser.add_argument("--require", action="append", default=None, help="distribution that must be pinned (repeatable)")
    parser.add_argument("--expect", default=None,
                        help="path of the reviewed lock the media's lock must equal byte for byte")
    args = parser.parse_args(argv)
    try:
        pinned = check(args.wheelhouse, args.require or DEFAULT_REQUIRED, args.expect)
    except (OSError, ValueError) as error:
        print(f"[wheelhouse-lock] REFUSED: {error}", file=sys.stderr)
        return 1
    print(f"[wheelhouse-lock] OK {len(pinned)} pinned distributions, every one hashed and present as a wheel")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
