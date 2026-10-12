#!/usr/bin/env python3
"""apply-env-lines.py - set sleep/heart/myAir keys in the hub's .env, ATOMICALLY.

For the in-place install on a RUNNING hub (stack/sleep/README.md). The obvious
ways of doing this are each wrong:

  * `sudo tee -a` echoes its stdin, which prints MYAIR_PASSWORD;
  * "append what is missing" leaves a template default in force: a hub .env that
    already says SLEEP_ENABLED=false, a blank SLEEP_USER or blank myAir values
    makes setup-sleep.sh exit "disabled" having installed nothing;
  * appending a second copy of a key gives readers that disagree (systemd and
    setup take the LAST line, firstboot's _env_val takes the FIRST);
  * rewriting the file in place (open + truncate) is NOT crash-safe: a full
    disk, a killed ssh session or a power cut half-way leaves every hub secret
    corrupt. Nothing bind-mounts stack/.env (compose reads it for interpolation
    only, systemd as an EnvironmentFile), so keeping the inode buys nothing.

So the new content is written to a temp file in the SAME directory with the
original's mode and owner, fsynced, os.replace()d over the original, and the
directory fsynced. On any failure the temp file is removed and the original is
untouched. An exclusive flock on the original is held for the whole of it, and
the inode is re-checked after the lock is taken (a second helper that waited
re-reads the file the first one produced instead of overwriting it).

For every KEY=VALUE line on stdin: the FIRST existing line for that key is
replaced and further copies are removed; a key not present is appended.
`export KEY=...` and indented `  KEY=...` lines of a TARGET key are recognised as
copies of that key and collapsed the same way; --report flags any that remain
for the other keys as UNSUPPORTED (systemd does not read them as written).

NOTHING THAT IS A VALUE IS EVER PRINTED, except the literal value of the
*_ENABLED flags in --report. Everything else prints a key name and "set" or
"empty". Only an exact set of key names is accepted (a typo is refused), a blank
value for a REQUIRED key is refused (it would replace a good hub value), and
stdin must be valid UTF-8 with no BOM.

Usage:
  sudo -n python3 apply-env-lines.py --apply   [--env PATH] < lines
  sudo -n python3 apply-env-lines.py --report  [--env PATH]
  sudo -n python3 apply-env-lines.py --restore BACKUP [--env PATH]
Exit 0 ok, 1 --report found a problem, 2 refused (the file is untouched).
"""
import argparse
import os
import re
import sys
import tempfile

try:                                    # the hub is Linux; the tests also run on Windows
    import fcntl
except ImportError:                     # pragma: no cover
    fcntl = None

DEFAULT_ENV = "/opt/homehub/stack/.env"
KEYS = ["SLEEP_ENABLED", "SLEEP_USER", "SLEEP_USER_ACCOUNT", "SLEEP_FEED_URL",
        "SLEEP_FEED_TOKEN", "SLEEP_PUBLISH", "SLEEP_STATE_FILE",
        "SLEEP_TIMEOUT_SECONDS", "MYAIR_EMAIL", "MYAIR_PASSWORD", "TIMEZONE",
        "HEART_ENABLED", "HEART_MIN_BPM", "HEART_MAX_BPM", "HEART_TARGET_BPM"]
# EXACT names: SLEEP_USR is a typo, not a key. The panel's display-sleep knobs
# (SLEEP_MODE, SLEEP_START, SLEEP_END) share the word and are not in the set.
ALLOWED = frozenset(KEYS)
# Keys that MUST be non-empty for the feeder to install and run.
REQUIRED = ["SLEEP_ENABLED", "SLEEP_USER", "SLEEP_FEED_URL", "MYAIR_EMAIL",
            "MYAIR_PASSWORD", "TIMEZONE"]
LINE_KEY = re.compile(r"^\s*(export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")


def refuse(message):
    print("REFUSED: " + message, file=sys.stderr)
    sys.exit(2)


def unquote(value):
    """systemd EnvironmentFile and compose both strip one pair of matching quotes."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_incoming(raw):
    """[(key, line)] from stdin BYTES; refuses anything unexpected, echoing no value."""
    if raw.startswith(b"\xef\xbb\xbf"):
        refuse("stdin starts with a UTF-8 BOM; send the bytes without one")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        refuse("stdin is not valid UTF-8 (an encoding was applied on the way); "
               "send the file's bytes unchanged")
    out = []
    for line in text.replace("\r", "").split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in ALLOWED:
            # Neither the line nor the unknown name is echoed: it may be a secret.
            refuse("an input line is not KEY=VALUE for an allowed key")
        if key in REQUIRED and not unquote(value):
            refuse("%s has a blank value; applying it would replace a good hub value "
                   "(import the missing secret and re-materialise first)" % key)
        out.append((key, line))
    if not out:
        refuse("no KEY=VALUE lines on stdin")
    return out


def merge(data, incoming):
    """(new_bytes, {key: 'replaced'|'added'|'unchanged'})."""
    wanted = {}
    for key, line in incoming:
        wanted[key] = line                  # a repeat on stdin: the last one wins
    lines = data.decode("utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    status, seen, result = {}, set(), []
    for line in lines:
        match = None if line.lstrip().startswith("#") else LINE_KEY.match(line)
        key = match.group(2) if match else None
        if key in wanted:
            if key in seen:
                continue                    # a duplicate: drop it
            seen.add(key)
            status[key] = "unchanged" if line == wanted[key] else "replaced"
            result.append(wanted[key])
        else:
            result.append(line)
    for key in wanted:
        if key not in seen:
            result.append(wanted[key])
            status[key] = "added"
    return ("\n".join(result) + "\n").encode("utf-8"), status


def effective(data):
    """{key: value} as systemd reads it: plain KEY=VALUE only, the LAST line wins,
    one pair of matching quotes stripped. export / indented forms are not values."""
    values = {}
    for line in data.decode("utf-8").split("\n"):
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            values[key] = unquote(value)
    return values


def unsupported_forms(data):
    found = set()
    for line in data.decode("utf-8").split("\n"):
        if line.lstrip().startswith("#"):
            continue
        match = LINE_KEY.match(line)
        if match and match.group(2) in ALLOWED and (match.group(1) or line != line.lstrip()):
            found.add(match.group(2))
    return sorted(found)


def duplicates(data):
    seen, dup = set(), set()
    for line in data.decode("utf-8").split("\n"):
        match = None if line.lstrip().startswith("#") else LINE_KEY.match(line)
        if match and match.group(2) in ALLOWED:
            if match.group(2) in seen:
                dup.add(match.group(2))
            seen.add(match.group(2))
    return sorted(dup)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _fsync_dir(directory):
    if os.name == "nt":                     # no directory handles to fsync on Windows
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data):
    """Replace `path` with `data` or change nothing. Caller holds the lock."""
    directory = os.path.dirname(os.path.abspath(path))
    original = os.stat(path)
    handle, temp = tempfile.mkstemp(prefix=".env.tmp-", dir=directory)
    try:
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(handle, original.st_mode & 0o7777)
            if hasattr(os, "fchown"):
                os.fchown(handle, original.st_uid, original.st_gid)
            _write_all(handle, data)
            os.fsync(handle)
        finally:
            os.close(handle)
        os.replace(temp, path)
        _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def locked_update(path, transform):
    """Run transform(current_bytes) -> (new_bytes, result) under an exclusive flock on
    `path`, writing atomically only if the bytes changed. Returns result."""
    for _attempt in range(5):
        pending = None
        with open(path, "rb") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            if os.name != "nt" and os.fstat(handle.fileno()).st_ino != os.stat(path).st_ino:
                continue                    # another helper replaced it while we waited
            data = handle.read()
            new_bytes, result = transform(data)
            if new_bytes != data:
                if fcntl is not None:
                    atomic_write(path, new_bytes)   # still holding the lock
                else:
                    pending = new_bytes             # Windows cannot replace an open file
        if pending is not None:
            atomic_write(path, pending)
        return result
    refuse("the file kept being replaced by another process; try again")


def report(path):
    with open(path, "rb") as handle:
        data = handle.read()
    values = effective(data)
    problems = 0
    for key in KEYS:
        if key.endswith("_ENABLED"):
            shown = values.get(key)
            print("%-24s %s" % (key, "(absent)" if shown is None else (shown or "(empty)")))
        else:
            state = "set" if values.get(key) else ("empty" if key in values else "absent")
            print("%-24s %s" % (key, state))
        if key in REQUIRED and not values.get(key):
            problems += 1
    dup = duplicates(data)
    if dup:
        print("DUPLICATE KEYS (readers disagree): " + ", ".join(dup))
        problems += 1
    bad = unsupported_forms(data)
    if bad:
        print("UNSUPPORTED FORM (export / indented, not read as written): " + ", ".join(bad))
        problems += 1
    if values.get("SLEEP_ENABLED") != "true":
        print("NOTE: SLEEP_ENABLED is not the literal true - setup-sleep.sh installs nothing.")
        problems += 1
    if values.get("HEART_ENABLED") != "0":
        print("NOTE: HEART_ENABLED must be the literal 0 for this rollout (no heart device).")
        problems += 1
    return 0 if problems == 0 else 1


def read_backup(backup, env):
    if not os.path.isfile(backup):
        refuse("backup %s does not exist or is not a regular file" % backup)
    if os.path.abspath(backup) == os.path.abspath(env):
        refuse("the backup is the live file")
    try:
        with open(backup, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        refuse("backup %s is not readable (%s)" % (backup, type(exc).__name__))
    if not data.strip():
        refuse("backup %s is empty" % backup)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        refuse("backup %s is not valid UTF-8" % backup)
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--report", action="store_true")
    mode.add_argument("--restore", metavar="BACKUP")
    parser.add_argument("--env", default=DEFAULT_ENV)
    args = parser.parse_args(argv)
    if not os.path.isfile(args.env):
        refuse("%s does not exist; this edits an existing .env" % args.env)
    if args.report:
        return report(args.env)
    if args.restore:
        data = read_backup(args.restore, args.env)      # validated BEFORE anything is touched
        locked_update(args.env, lambda _current: (data, None))
        print("restored %s from the backup (atomic replace)" % os.path.basename(args.env))
        return 0
    incoming = parse_incoming(sys.stdin.buffer.read())
    status = locked_update(args.env, lambda current: merge(current, incoming))
    for key in sorted(status):
        print("%-24s %s" % (key, status[key]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
