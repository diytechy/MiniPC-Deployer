#!/usr/bin/env python3
"""The one thing the hub may do on the panel: collect finished meeting
recordings from the spool (WSN-097; SR-110).

This is the FORCED COMMAND of the `wall-meeting` account. The hub's key is
installed in that account's authorized_keys as

    restrict,command="/usr/local/lib/wall-panel/wall-meeting-spool" ssh-ed25519 ...

so whatever the hub asks for arrives here in SSH_ORIGINAL_COMMAND, and nothing
else can run: no shell, no forwarding, no pty (`restrict`). The verbs reach
only /var/spool/wall-meeting/ready/ and the spool's hub-state.json:

  list              one JSON object per line, oldest first:
                    {"name", "size", "sha256"} for each FINISHED recording
                    (its .sha256 sidecar exists; a file still being written
                    or finalised is never offered)
  get NAME          the file's bytes on stdout
  ack NAME SHA256   delete NAME (and its sidecar) only if its sha256 is SHA256:
                    prints "deleted NAME" (exit 0); "absent NAME" (exit 0: an
                    ack repeated after a lost reply is already done);
                    "mismatch NAME" (exit 3) and the file is KEPT
  report ok|full    record the hub's meetings/ quota state for the recorder
                    and the kiosk (hub-state.json, 0644)

NAME must be a recording name (meeting-<UTC>.opus); anything else -- a path, a
dot, an unknown verb, extra words -- is exit 2 with one line on stderr.

Every verb is safe to repeat, which is what makes the hub's pull retryable at
any point: the hub deletes nothing here, and asks for a delete only after it
has the file under its final name with the same hash.

Implements: SR-110, LLR-1247, LLR-1248
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time

SPOOL = "/var/spool/wall-meeting"
NAME = re.compile(r"^meeting-[0-9]{8}T[0-9]{6}Z(-[0-9]{1,3})?\.opus$")
SHA = re.compile(r"^[0-9a-f]{64}$")
EXIT_USAGE, EXIT_MISMATCH = 2, 3
MAX_COMMAND = 256


def ready_dir(root: str) -> str:
    return os.path.join(root, "ready")


def sidecar_hash(root: str, name: str):
    """The recorded sha256 of a finished recording, or None."""
    try:
        with open(os.path.join(ready_dir(root), name + ".sha256"), encoding="ascii") as handle:
            text = handle.read(200)
    except (OSError, UnicodeDecodeError):
        return None
    digest = text.split()[0] if text.split() else ""
    return digest if SHA.match(digest) else None


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def listing(root: str):
    """Finished recordings, oldest first (the names sort by time)."""
    try:
        names = sorted(n for n in os.listdir(ready_dir(root)) if NAME.match(n))
    except OSError:
        return []
    rows = []
    for name in names:
        digest = sidecar_hash(root, name)
        if digest is None:
            continue
        try:
            size = os.lstat(os.path.join(ready_dir(root), name)).st_size
        except OSError:
            continue
        rows.append({"name": name, "size": size, "sha256": digest})
    return rows


def parse(command: str):
    """(verb, args) when `command` is one of the four verbs, else None."""
    if not command or len(command) > MAX_COMMAND:
        return None
    words = command.split()
    if words == ["list"]:
        return "list", []
    if len(words) == 2 and words[0] == "get" and NAME.match(words[1]):
        return "get", words[1:]
    if len(words) == 3 and words[0] == "ack" and NAME.match(words[1]) and SHA.match(words[2]):
        return "ack", words[1:]
    if len(words) == 2 and words[0] == "report" and words[1] in ("ok", "full"):
        return "report", words[1:]
    return None


def do_get(root: str, name: str, out) -> int:
    path = os.path.join(ready_dir(root), name)
    if sidecar_hash(root, name) is None:
        print("wall-meeting-spool: %s is not a finished recording" % name, file=sys.stderr)
        return EXIT_USAGE
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        print("wall-meeting-spool: cannot open %s (%s)" % (name, exc), file=sys.stderr)
        return EXIT_USAGE
    with os.fdopen(fd, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            out.write(chunk)
    out.flush()
    return 0


def do_ack(root: str, name: str, expected: str, out) -> int:
    path = os.path.join(ready_dir(root), name)
    if not os.path.lexists(path):
        out.write(("absent %s\n" % name).encode())
        return 0
    try:
        actual = file_hash(path)
    except OSError as exc:
        print("wall-meeting-spool: cannot hash %s (%s)" % (name, exc), file=sys.stderr)
        return EXIT_MISMATCH
    if actual != expected:
        out.write(("mismatch %s\n" % name).encode())
        return EXIT_MISMATCH
    os.unlink(path)
    try:
        os.unlink(path + ".sha256")
    except OSError:
        pass
    out.write(("deleted %s\n" % name).encode())
    return 0


def do_report(root: str, quota: str, now=time.time) -> int:
    document = {"schemaVersion": 1, "quota": quota, "reportedAt": int(now() * 1000)}
    fd, temporary = tempfile.mkstemp(dir=root, prefix=".hub-state.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(document, sort_keys=True) + "\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, os.path.join(root, "hub-state.json"))
    return 0


def main(argv=None, environ=None, out=None, root=None) -> int:
    environ = os.environ if environ is None else environ
    out = out or sys.stdout.buffer
    root = root or SPOOL
    parsed = parse(environ.get("SSH_ORIGINAL_COMMAND", ""))
    if parsed is None:
        print("wall-meeting-spool: refused (verbs: list | get NAME | ack NAME SHA256 | report ok|full)",
              file=sys.stderr)
        return EXIT_USAGE
    verb, args = parsed
    if verb == "list":
        for row in listing(root):
            out.write((json.dumps(row, sort_keys=True) + "\n").encode())
        out.flush()
        return 0
    if verb == "get":
        return do_get(root, args[0], out)
    if verb == "ack":
        return do_ack(root, args[0], args[1], out)
    return do_report(root, args[0])


if __name__ == "__main__":
    sys.exit(main())
