#!/usr/bin/env python3
"""apply-env-lines.py - set sleep/heart/myAir keys in the hub's .env, IN PLACE.

For the in-place install on a RUNNING hub (stack/sleep/README.md). It exists
because the obvious ways of doing this are each wrong:

  * `sudo tee -a` echoes its stdin, which prints MYAIR_PASSWORD;
  * "append what is missing" leaves a template default in force: a hub .env that
    already says SLEEP_ENABLED=false, a blank SLEEP_USER or blank myAir values
    makes setup-sleep.sh exit "disabled" having installed nothing;
  * appending a second copy of a key gives readers that disagree (systemd and
    setup-sleep.sh take the LAST line, firstboot's _env_val takes the FIRST);
  * sed -i, mv and cp --remove-destination write a new inode and can detach a
    bind mount that something else holds on the file.

So: for every KEY=VALUE line on stdin, the FIRST existing line for that key is
replaced and any further copies are removed; a key not present is appended. The
file is opened r+b and truncated, never replaced, so its inode is kept.

NOTHING THAT IS A VALUE IS EVER PRINTED, except the literal value of the
*_ENABLED flags in --report. Everything else prints a key name and "set" or
"empty".

Usage:
  sudo -n python3 apply-env-lines.py --apply  [--env PATH] < lines
  sudo -n python3 apply-env-lines.py --report [--env PATH]
Exit 0 ok, 2 refused (bad input; the file is untouched).
"""
import argparse
import os
import re
import sys

DEFAULT_ENV = "/opt/homehub/stack/.env"
ALLOWED = re.compile(r"^(SLEEP_[A-Z_]+|MYAIR_[A-Z_]+|HEART_[A-Z_]+|TIMEZONE)$")
# The panel's display-sleep policy shares the word and is NOT ours.
FORBIDDEN = {"SLEEP_MODE", "SLEEP_START", "SLEEP_END"}
REPORT_KEYS = ["SLEEP_ENABLED", "SLEEP_USER", "SLEEP_USER_ACCOUNT", "SLEEP_FEED_URL",
               "SLEEP_FEED_TOKEN", "SLEEP_PUBLISH", "SLEEP_STATE_FILE",
               "SLEEP_TIMEOUT_SECONDS", "MYAIR_EMAIL", "MYAIR_PASSWORD", "TIMEZONE",
               "HEART_ENABLED", "HEART_MIN_BPM", "HEART_MAX_BPM", "HEART_TARGET_BPM"]
# Keys that MUST be non-empty for the feeder to install and run.
REQUIRED = ["SLEEP_ENABLED", "SLEEP_USER", "SLEEP_FEED_URL", "MYAIR_EMAIL",
            "MYAIR_PASSWORD", "TIMEZONE"]


def refuse(message):
    print("REFUSED: " + message, file=sys.stderr)
    sys.exit(2)


def parse_incoming(text):
    """[(key, line_without_newline)] from stdin text; refuses anything unexpected."""
    out = []
    for raw in text.replace("\r", "").split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        key, sep, _value = raw.partition("=")
        if not sep or not ALLOWED.match(key) or key in FORBIDDEN:
            # The line itself is NOT echoed: it may be a secret.
            refuse("an input line is not KEY=VALUE for an allowed key (%s)"
                   % (key if ALLOWED.match(key) else "name not shown"))
        out.append((key, raw))
    if not out:
        refuse("no KEY=VALUE lines on stdin")
    return out


def merge(data, incoming):
    """(new_bytes, {key: 'replaced'|'added'|'unchanged'}) - first occurrence replaced,
    later duplicates dropped, absent keys appended."""
    keys = {}
    for key, line in incoming:
        keys[key] = line            # a repeat on stdin: last one wins
    lines = data.decode("utf-8").split("\n")
    trailing_newline = lines and lines[-1] == ""
    if trailing_newline:
        lines.pop()
    status, seen, result = {}, set(), []
    for line in lines:
        key = line.partition("=")[0]
        if key in keys and not line.lstrip().startswith("#"):
            if key in seen:
                continue            # a duplicate: drop it
            seen.add(key)
            status[key] = "unchanged" if line == keys[key] else "replaced"
            result.append(keys[key])
        else:
            result.append(line)
    for key in keys:
        if key not in seen:
            result.append(keys[key])
            status[key] = "added"
    return ("\n".join(result) + "\n").encode("utf-8"), status


def effective(data):
    """{key: value} the way systemd and setup-sleep.sh read it: the LAST line wins,
    surrounding double quotes stripped."""
    values = {}
    for line in data.decode("utf-8").split("\n"):
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        values[key] = value
    return values


def duplicates(data, keys):
    seen, dup = {}, set()
    for line in data.decode("utf-8").split("\n"):
        key = line.partition("=")[0]
        if key in keys and not line.lstrip().startswith("#"):
            if key in seen:
                dup.add(key)
            seen[key] = True
    return sorted(dup)


def rewrite_in_place(path, new_bytes):
    with open(path, "r+b") as handle:      # same inode: truncate and rewrite
        handle.seek(0)
        handle.write(new_bytes)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def report(path):
    with open(path, "rb") as handle:
        data = handle.read()
    values = effective(data)
    problems = 0
    for key in REPORT_KEYS:
        if key.endswith("_ENABLED"):
            print("%-24s %s" % (key, values.get(key, "(absent)") or "(empty)"))
        else:
            state = "set" if values.get(key) else ("empty" if key in values else "absent")
            print("%-24s %s" % (key, state))
        if key in REQUIRED and not values.get(key):
            problems += 1
    dup = duplicates(data, set(REPORT_KEYS))
    if dup:
        print("DUPLICATE KEYS (readers disagree): " + ", ".join(dup))
        problems += 1
    if values.get("SLEEP_ENABLED") != "true":
        print("NOTE: SLEEP_ENABLED is not the literal true - setup-sleep.sh installs nothing.")
        problems += 1
    return 0 if problems == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--report", action="store_true")
    parser.add_argument("--env", default=DEFAULT_ENV)
    args = parser.parse_args(argv)
    if not os.path.isfile(args.env):
        refuse("%s does not exist; this edits an existing .env in place" % args.env)
    if args.report:
        return report(args.env)
    incoming = parse_incoming(sys.stdin.read())
    with open(args.env, "rb") as handle:
        data = handle.read()
    new_bytes, status = merge(data, incoming)
    if new_bytes != data:
        rewrite_in_place(args.env, new_bytes)
    for key in sorted(status):
        print("%-24s %s" % (key, status[key]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
