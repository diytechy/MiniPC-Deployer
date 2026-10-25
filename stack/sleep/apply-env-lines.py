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
directory fsynced. On any failure up to and including the replace, the temp file is
removed and the original is untouched. The directory fsync comes AFTER the replace,
when the change is already committed: if only THAT fails, the helper prints a
WARNING that durability is uncertain and still exits 0 (the new file is in place).
At the start of every apply/restore, under the lock, it also deletes stale
.tmp-<name>-* files left beside the target by a hard-killed run (they hold secrets;
the prefix is scoped to the target's name, so a different env file is never touched). An exclusive flock on the original is held for the whole of it, and
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
Flag keys (*_ENABLED, SLEEP_PUBLISH) are written UNQUOTED: setup-sleep.sh and
firstboot compare the literal value, so 'true' / "true" are canonicalised to true.
Exit 0 ok (possibly with a durability WARNING on stderr), 1 --report found a
problem, 2 refused (the file is untouched).
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
        "HEART_ENABLED", "HEART_MIN_BPM", "HEART_MAX_BPM", "HEART_TARGET_BPM",
        # The heart gauge's samples mode (2026-10-25, the Owner's ruling).
        "HEART_SOURCE", "HEART_SAMPLE_DEVICE", "HEART_COLOUR_SPAN_BPM",
        "HEART_BASELINE_NIGHTS", "HEART_MIN_BASELINE_NIGHTS"]
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


def credential_problem(raw):
    """Why systemd's EnvironmentFile would NOT deliver this credential intact, or None.

    The RAW env value is parsed first, the way systemd does: one matched pair of
    surrounding quotes is syntax (the materialiser wraps a password containing
    whitespace in double quotes, so `"two words"` is normal and arrives as
    `two words`). The credential rules then apply to the DECODED value:
      * no backslash (systemd unescapes it: `\\"` becomes `"`);
      * no double quote inside the decoded value (it ends or garbles a quoted value);
      * no inner copy of the wrapping quote (`'a'b'` arrives as `ab'`).
    A leading quote in the DECODED value is fine when the raw value is wrapped
    (`"'a b"` arrives intact on systemd 255); an UNWRAPPED value starting with a
    quote is the unmatched case, refused before this is called.
    """
    wrapped = len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'"
    decoded = raw[1:-1] if wrapped else raw
    if not wrapped and raw[:1] in ("'", '"'):
        return "starts with a quote that has no matching closing quote (systemd drops it)"
    if "\\" in decoded:
        return "the decoded value contains a backslash (systemd would unescape it)"
    if '"' in decoded:
        return "the decoded value contains a double quote (systemd would alter it)"
    if wrapped and raw[0] in decoded:
        return "the decoded value contains its own wrapping quote"
    if wrapped and not (raw[0] == '"' and any(c.isspace() for c in decoded)):
        # The materialiser wraps in DOUBLE quotes, and only when the plaintext has
        # whitespace. Any other wrapped form is either not its output or a plaintext
        # that merely starts and ends with a quote, which systemd would silently strip.
        return "a quoted value is only accepted as the materialiser's double-quoted " \
               "whitespace form; this one would be altered or is ambiguous"
    return None


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
        stripped = value.strip()
        if stripped[:1] in ("'", '"') and not (len(stripped) >= 2 and stripped[-1] == stripped[0]):
            # systemd drops a LEADING quote even with no closing one, setup-sleep.sh and
            # firstboot require a matching pair: refuse the ambiguity rather than store it.
            refuse("%s starts with a quote that has no matching closing quote" % key)
        if key == "MYAIR_PASSWORD":
            problem = credential_problem(stripped)
            if problem:
                refuse("MYAIR_PASSWORD: " + problem)
        if key in REQUIRED and not unquote(value).strip():
            refuse("%s has a blank value; applying it would replace a good hub value "
                   "(import the missing secret and re-materialise first)" % key)
        if key.endswith("_ENABLED") or key == "SLEEP_PUBLISH":
            line = key + "=" + unquote(value)       # canonical: unquoted
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


TEMP_SUFFIX = re.compile(r"^[A-Za-z0-9_]{8}$")      # what tempfile.mkstemp appends


def temp_prefix(path):
    """Temp names are scoped to the TARGET's name, so two helpers updating different
    env files in one directory (each under its own lock) never sweep each other."""
    return ".tmp-%s-" % os.path.basename(path)


def atomic_write(path, data):
    """Replace `path` with `data` or change nothing. Caller holds the lock."""
    directory = os.path.dirname(os.path.abspath(path))
    original = os.stat(path)
    handle, temp = tempfile.mkstemp(prefix=temp_prefix(path), dir=directory)
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
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    # COMMITTED from here on. A failure to fsync the directory means the rename may
    # not survive a power cut, not that the file is unchanged: warn, do not raise.
    try:
        _fsync_dir(directory)
    except OSError as exc:
        print("WARNING: %s was replaced but the directory fsync failed (%s); durability "
              "is uncertain until the next sync" % (os.path.basename(path), type(exc).__name__),
              file=sys.stderr)


def sweep_stale_temps(path):
    """Delete this target's own stale temp files (and only those) beside `path`, owned by
    the same user. Caller holds the lock on `path`."""
    prefix = temp_prefix(path)
    directory = os.path.dirname(os.path.abspath(path))
    owner = os.stat(path).st_uid
    removed = 0
    for name in os.listdir(directory):
        full = os.path.join(directory, name)
        if (name.startswith(prefix) and TEMP_SUFFIX.match(name[len(prefix):])
                and os.path.isfile(full) and os.stat(full).st_uid == owner):
            try:
                os.unlink(full)
                removed += 1
            except OSError:
                pass
    if removed:
        print("removed %d stale %s* file(s) beside %s" % (removed, prefix, os.path.basename(path)),
              file=sys.stderr)


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
            sweep_stale_temps(path)
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
    # The heart gauge was ruled ON on 2026-10-25 (samples mode). Either literal is
    # fine; anything else (blank, "true", a quoted form) is a value the feeder and
    # firstboot may read differently.
    if values.get("HEART_ENABLED") not in ("0", "1"):
        print("NOTE: HEART_ENABLED must be the literal 0 or 1.")
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
