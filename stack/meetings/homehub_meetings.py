#!/usr/bin/env python3
"""homehub_meetings - collect the wall panel's meeting recordings onto the
scratch volume, and age them out (WSN-097; SR-110, SR-111).

WHY A PULL OVER SSH AND NOT A SHARE. /srv/scratch is in no share and must stay
in none (HomeHub storage-map 3c), and the panel must not hold a credential that
can write the hub. So the HUB reaches the PANEL, with a key of its own that
the panel's `wall-meeting` account accepts only for one forced command
(stack/autoinstall/wall/wall-meeting-spool.py: list, get, ack, report).

ONE FILE, END TO END (`pull`):
  1. the scratch guard for the `meetings` writer (W4, IF-071) -- before the run
     and again before every new file; a refusal stops the run (exit 76);
  2. `list` on the panel: finished recordings with size and sha256;
  3. for each, oldest first: already here under its final name with that hash
     -> just `ack` again (a lost ack is repaired); otherwise check the quota
     room, stream `get` into meetings/.incoming/<name>.part while hashing,
     and only when size AND hash match fsync it and RENAME it to
     meetings/YYYY-MM-DD/<name> (publish by rename: a reader never sees a
     partial file under a final name); then `ack` it, and the panel deletes
     its copy only if the hash it computes is the same;
  4. a short or failed stream, or a hash that does not match, removes the
     .part and leaves the panel's copy alone: the next run retries;
  5. no room (the quota is the wall: statvfs on meetings/ reports the project
     quota) or EDQUOT/ENOSPC while writing -> `report full`, alert, exit 75;
     otherwise `report ok`.
Every step is safe to repeat, so the timer simply runs it again.

RETENTION (`retention`): recordings older than MEETING_RETENTION_DAYS (default
14) are deleted, oldest first, and then -- the quota is a hard 2 GiB -- the
oldest are deleted while usage is above MEETING_QUOTA_HIGH_PCT (90) of it.
Stale .part files (over an hour) go too. `--dry-run` deletes nothing and
prints what it would; `--days N` overrides the period for a short dry run.
At 48 kbit/s stereo a recording is ~21.6 MB an hour, so 2 GiB holds about 99
hours, 89 below the 90 % high-water: 14 days fits about 6 hours of meetings a
day before the high-water rule starts taking the oldest early.

Contract:
  Inputs:  the stack .env (MEETING_PULL_ENABLED, MEETING_PANEL_HOST or
           PANEL_IP, MEETING_PANEL_USER, MEETING_RETENTION_DAYS,
           MEETING_QUOTA_HIGH_PCT); the panel's forced command over ssh.
  Outputs: /srv/scratch/meetings/YYYY-MM-DD/meeting-<UTC>.opus;
           /var/lib/homehub-meetings/{id_ed25519,known_hosts,status.json}.
  Raises:  nothing; exit 0 done, 1 the panel could not be reached or a
           transfer failed (retried next run), 2 usage, 75 quota full,
           76 the scratch guard refused.
Implements: SR-110, SR-111, LLR-1249, LLR-1250, LLR-1251, LLR-1252

Python 3.8 compatible: the dev PC's gate interpreter is 3.8, the hub's 3.12.
"""
from __future__ import annotations

import argparse
import calendar
import errno
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "monitoring"))
import monitor_common as mc  # noqa: E402

ENV_FILE = "/opt/homehub/stack/.env"
SCRATCH = "/srv/scratch"
MEETINGS = os.path.join(SCRATCH, "meetings")
STATE_DIR = "/var/lib/homehub-meetings"
GUARD = ["/usr/bin/python3", "/opt/homehub/stack/scratch/homehub_scratch.py", "guard", "--writer", "meetings"]
NAME = re.compile(r"^meeting-([0-9]{8})T([0-9]{6})Z(-[0-9]{1,3})?\.opus$")
SHA = re.compile(r"^[0-9a-f]{64}$")
EXIT_OK, EXIT_TRANSFER, EXIT_USAGE, EXIT_QUOTA, EXIT_GUARD = 0, 1, 2, 75, 76
# Room kept above the file itself, so the last file cannot fill the quota to
# the byte (the directory entry and the date directory need blocks too).
MARGIN = 1024 * 1024
STALE_PART_S = 3600
LIST_TIMEOUT_S = 60
# A recording streams at LAN speed; the bound is generous so a busy panel is
# not a failure, and still finite so a wedged link cannot hold the run.
GET_BASE_TIMEOUT_S = 60
GET_BYTES_PER_S = 256 * 1024


def log(message: str, warning: bool = False) -> None:
    mc.log("meetings: " + message, warning)


# ── pure helpers ─────────────────────────────────────────────────────────────

def parse_listing(text: str) -> List[Dict[str, object]]:
    """The panel's `list` output: valid rows only, oldest first."""
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        name, size, digest = row.get("name"), row.get("size"), row.get("sha256")
        if (isinstance(name, str) and NAME.match(name) and isinstance(size, int)
                and not isinstance(size, bool) and size > 0 and isinstance(digest, str) and SHA.match(digest)):
            rows.append({"name": name, "size": size, "sha256": digest})
    return sorted(rows, key=lambda r: r["name"])


def recorded_at(name: str) -> Optional[float]:
    """The UTC start time a recording's name carries, as epoch seconds."""
    match = NAME.match(name)
    if not match:
        return None
    try:
        return float(calendar.timegm(time.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")))
    except ValueError:
        return None


def final_path(root: str, name: str) -> str:
    """meetings/YYYY-MM-DD/<name>, the date being the recording's own (UTC)."""
    match = NAME.match(name)
    day = "%s-%s-%s" % (match.group(1)[:4], match.group(1)[4:6], match.group(1)[6:8])
    return os.path.join(root, day, name)


def retention_plan(files: List[Tuple[str, int, float]], now: float, days: int, limit: int,
                   high_pct: int) -> List[str]:
    """Which recordings to delete (LLR-1252). Pure.

    Contract:
      Inputs:  files: (path, size bytes, recorded-at epoch s); now: epoch s;
               days: retention period; limit: the quota in bytes (0 = unknown,
               no size pruning); high_pct: 1..100.
      Outputs: paths, oldest first: everything older than `days`, then the
               oldest of the rest while the total is above high_pct of limit.
    """
    ordered = sorted(files, key=lambda f: (f[2], f[0]))
    cutoff = now - days * 86400
    doomed = [f for f in ordered if f[2] < cutoff]
    kept = [f for f in ordered if f[2] >= cutoff]
    if limit > 0:
        ceiling = limit * high_pct // 100
        total = sum(f[1] for f in kept)
        while kept and total > ceiling:
            oldest = kept.pop(0)
            total -= oldest[1]
            doomed.append(oldest)
    return [f[0] for f in doomed]


# ── the transport ────────────────────────────────────────────────────────────

class Transport:
    """ssh to the panel's forced command. `argv` is the ssh prefix; each call
    appends the verb's words, which arrive there as SSH_ORIGINAL_COMMAND."""

    def __init__(self, argv: List[str]):
        self.argv = list(argv)

    def run(self, words: List[str], timeout: float) -> Tuple[int, bytes]:
        try:
            done = subprocess.run(self.argv + words, stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return 124, b""
        except OSError as exc:
            log("cannot run ssh (%s)" % exc, True)
            return 127, b""
        if done.stderr:
            for line in done.stderr.decode("utf-8", "replace").splitlines()[:5]:
                log("panel: " + line)
        return done.returncode, done.stdout

    def stream(self, words: List[str], sink: Callable[[bytes], None], timeout: float) -> int:
        """Run a verb whose stdout is a file; feed it to `sink` chunk by chunk."""
        try:
            proc = subprocess.Popen(self.argv + words, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
        except OSError as exc:
            log("cannot run ssh (%s)" % exc, True)
            return 127
        deadline = time.monotonic() + timeout
        try:
            while True:
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    return 124
                chunk = proc.stdout.read(1 << 16)
                if not chunk:
                    break
                sink(chunk)
            return proc.wait(timeout=max(1.0, deadline - time.monotonic()))
        except BaseException:
            proc.kill()
            proc.wait()
            raise


def ssh_argv(settings: Dict[str, str], state_dir: str = STATE_DIR) -> List[str]:
    """The ssh prefix: key only, never a prompt, the panel's host key pinned
    in the pull's own known_hosts on first contact, every wait bounded."""
    return ["ssh", "-i", posixpath.join(state_dir, "id_ed25519"),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=" + posixpath.join(state_dir, "known_hosts"),
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
            "-o", "LogLevel=ERROR",
            "%s@%s" % (settings["user"], settings["host"])]


# ── the pull (LLR-1249, LLR-1250) ────────────────────────────────────────────

class QuotaFull(Exception):
    pass


class Puller:
    """One pull run. Every external effect is a constructor argument."""

    def __init__(self, transport, root: str = MEETINGS, guard: Callable[[], Tuple[bool, str]] = None,
                 room: Callable[[str], int] = None, alert: Callable[[str, str, str], None] = None):
        self.transport = transport
        self.root = root
        self.incoming = os.path.join(root, ".incoming")
        self.guard = guard or run_guard
        self.room = room or statvfs_room
        self.alert = alert or (lambda key, title, body: None)
        self.collected: List[str] = []

    def run(self) -> int:
        ok, reason = self.guard()
        if not ok:
            log("REFUSING to pull - scratch guard: %s" % reason, True)
            self.alert("meetings-guard", "HomeHub: meeting pull refused (scratch volume)", reason)
            return EXIT_GUARD
        rc, out = self.transport.run(["list"], LIST_TIMEOUT_S)
        if rc != 0:
            log("the panel did not answer `list` (exit %d); retrying next run" % rc, True)
            return EXIT_TRANSFER
        status = EXIT_OK
        try:
            for row in parse_listing(out.decode("utf-8", "replace")):
                result = self.one(row)
                if result == EXIT_GUARD:
                    return EXIT_GUARD
                if result != EXIT_OK:
                    status = EXIT_TRANSFER
        except QuotaFull as exc:
            log("meetings/ is full (%s); the panel keeps its recordings" % exc, True)
            self.alert("meetings-full", "HomeHub: meeting recordings quota full",
                       "meetings/ on the scratch volume is full; the panel keeps its copies "
                       "and refuses new recordings once its own spool is full too.")
            self.transport.run(["report", "full"], LIST_TIMEOUT_S)
            return EXIT_QUOTA
        self.transport.run(["report", "ok"], LIST_TIMEOUT_S)
        return status

    def one(self, row: Dict[str, object]) -> int:
        name, size, digest = str(row["name"]), int(row["size"]), str(row["sha256"])
        target = final_path(self.root, name)
        if os.path.exists(target):
            if file_sha256(target) == digest:
                return self.ack(name, digest)
            log("%s is already here with a DIFFERENT hash; leaving both copies" % name, True)
            return EXIT_TRANSFER
        ok, reason = self.guard()
        if not ok:
            log("STOPPING before %s - scratch guard: %s" % (name, reason), True)
            self.alert("meetings-guard", "HomeHub: meeting pull stopped (scratch volume)", reason)
            return EXIT_GUARD
        os.makedirs(self.incoming, exist_ok=True)
        if self.room(self.root) < size + MARGIN:
            raise QuotaFull("%d bytes needed for %s" % (size + MARGIN, name))
        part = os.path.join(self.incoming, name + ".part")
        received = self.receive(name, size, part)
        if received is None:
            return EXIT_TRANSFER
        if received != digest:
            log("%s arrived with sha256 %s, the panel lists %s; discarded, the panel keeps it"
                % (name, received[:12], digest[:12]), True)
            self.alert("meetings-hash", "HomeHub: a meeting recording failed its hash check",
                       "%s was discarded on the hub and kept on the panel; the next pull retries it." % name)
            os.unlink(part)
            return EXIT_TRANSFER
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(part, target)
        fsync_dir(os.path.dirname(target))
        log("collected %s (%d bytes)" % (name, size))
        self.collected.append(name)
        return self.ack(name, digest)

    def receive(self, name: str, size: int, part: str) -> Optional[str]:
        """Stream `get NAME` into `part`; the sha256 when exactly `size` bytes
        arrived from a clean exit, else None (the part is removed)."""
        digest = hashlib.sha256()
        count = {"n": 0}
        timeout = GET_BASE_TIMEOUT_S + size / GET_BYTES_PER_S
        try:
            with open(part, "wb") as handle:
                def sink(chunk: bytes) -> None:
                    count["n"] += len(chunk)
                    if count["n"] > size:
                        raise ValueError("more bytes than listed")
                    handle.write(chunk)
                    digest.update(chunk)
                rc = self.transport.stream(["get", name], sink, timeout)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            _unlink(part)
            if exc.errno in (errno.EDQUOT, errno.ENOSPC):
                raise QuotaFull("%s while writing %s" % (os.strerror(exc.errno), name))
            log("could not write %s (%s)" % (part, exc), True)
            return None
        except ValueError as exc:
            _unlink(part)
            log("%s: %s; discarded, retrying next run" % (name, exc), True)
            return None
        if rc != 0 or count["n"] != size:
            _unlink(part)
            log("%s: the transfer was interrupted (exit %d, %d of %d bytes); retrying next run"
                % (name, rc, count["n"], size), True)
            return None
        return digest.hexdigest()

    def ack(self, name: str, digest: str) -> int:
        rc, out = self.transport.run(["ack", name, digest], LIST_TIMEOUT_S)
        answer = out.decode("utf-8", "replace").strip()
        if rc == 0 and answer in ("deleted " + name, "absent " + name):
            return EXIT_OK
        log("the panel did not confirm the delete of %s (exit %d, %r); it keeps its copy"
            % (name, rc, answer[:80]), True)
        return EXIT_TRANSFER


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_dir(path: str) -> None:
    """Make a rename durable. Best effort: a platform that cannot open a
    directory (the tests' Windows host) simply skips it."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def statvfs_room(path: str) -> int:
    """Free bytes as the writer sees them: on an ext4 project directory with
    prjquota this is what is left of the project's quota."""
    return shutil.disk_usage(path).free


def quota_limit(path: str) -> int:
    """The project quota's size, by the same statfs view (f_blocks)."""
    return shutil.disk_usage(path).total


def run_guard(argv: List[str] = None) -> Tuple[bool, str]:
    """W4's mount guard for the meetings writer (IF-071), as a command, so
    this file does not import the scratch module and a missing one refuses."""
    argv = argv or GUARD
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "the guard could not run (%s)" % exc
    reason = (done.stdout + done.stderr).strip().splitlines()
    return done.returncode == 0, reason[-1] if reason else "exit %d" % done.returncode


# ── retention (LLR-1252) ─────────────────────────────────────────────────────

def recordings(root: str) -> List[Tuple[str, int, float]]:
    found = []
    try:
        days = sorted(os.listdir(root))
    except OSError:
        return found
    for day in days:
        directory = os.path.join(root, day)
        if day.startswith(".") or not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            at = recorded_at(name)
            if at is None:
                continue
            path = os.path.join(directory, name)
            try:
                found.append((path, os.lstat(path).st_size, at))
            except OSError:
                continue
    return found


def retention(root: str, days: int, high_pct: int, *, dry_run: bool, now: float = None,
              limit: Optional[int] = None, guard: Callable[[], Tuple[bool, str]] = None) -> int:
    now = time.time() if now is None else now
    ok, reason = (guard or run_guard)()
    if not ok:
        log("REFUSING retention - scratch guard: %s" % reason, True)
        return EXIT_GUARD
    if limit is None:
        try:
            limit = quota_limit(root)
        except OSError:
            limit = 0
    files = recordings(root)
    doomed = retention_plan(files, now, days, limit, high_pct)
    verb = "would delete" if dry_run else "deleted"
    for path in doomed:
        if not dry_run:
            _unlink(path)
        log("%s %s" % (verb, os.path.relpath(path, root)))
    incoming = os.path.join(root, ".incoming")
    try:
        parts = os.listdir(incoming)
    except OSError:
        parts = []
    for name in parts:
        path = os.path.join(incoming, name)
        try:
            stale = now - os.lstat(path).st_mtime > STALE_PART_S
        except OSError:
            continue
        if stale:
            if not dry_run:
                _unlink(path)
            log("%s the stale partial transfer %s" % (verb, name))
    if not dry_run:
        for day in os.listdir(root) if os.path.isdir(root) else []:
            directory = os.path.join(root, day)
            if not day.startswith(".") and os.path.isdir(directory) and not os.listdir(directory):
                os.rmdir(directory)
    kept = len(files) - len(doomed)
    log("retention: %d recording(s) kept, %d %s (period %d days, high-water %d%% of %d bytes)"
        % (kept, len(doomed), verb, days, high_pct, limit))
    return EXIT_OK


# ── the command line ─────────────────────────────────────────────────────────

def settings_from(knobs: Dict[str, str]) -> Dict[str, object]:
    return {
        "enabled": mc.knob_bool(knobs, "MEETING_PULL_ENABLED", False),
        "host": knobs.get("MEETING_PANEL_HOST") or knobs.get("PANEL_IP", ""),
        "user": knobs.get("MEETING_PANEL_USER") or "wall-meeting",
        "days": mc.knob_int(knobs, "MEETING_RETENTION_DAYS", 14, 1, 365),
        "high_pct": mc.knob_int(knobs, "MEETING_QUOTA_HIGH_PCT", 90, 10, 100),
    }


def init_key(state_dir: str = STATE_DIR) -> int:
    """Make the hub's pull key if there is none, and print its PUBLIC half,
    which is what wall.env's WALL_MEETING_PULL_PUBKEY must carry."""
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    key = os.path.join(state_dir, "id_ed25519")
    if not os.path.exists(key):
        done = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "homehub-meeting-pull",
                               "-f", key], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            log("ssh-keygen failed: %s" % done.stderr.strip(), True)
            return EXIT_TRANSFER
        log("made a new pull key; the panel needs its public half (WALL_MEETING_PULL_PUBKEY)")
    with open(key + ".pub", encoding="ascii") as handle:
        print(handle.read().strip())
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="homehub_meetings.py")
    parser.add_argument("--env-file", default=ENV_FILE)
    sub = parser.add_subparsers(dest="verb")
    sub.add_parser("condition")
    sub.add_parser("pull")
    sub.add_parser("init-key")
    keep = sub.add_parser("retention")
    keep.add_argument("--dry-run", action="store_true")
    keep.add_argument("--days", type=int)
    args = parser.parse_args(argv)
    knobs = mc.read_env_file(args.env_file)
    settings = settings_from(knobs)
    if args.verb == "condition":
        return 0 if settings["enabled"] else 1
    if args.verb == "init-key":
        return init_key()
    if args.verb == "retention":
        days = args.days if args.days is not None else settings["days"]
        return retention(MEETINGS, days, int(settings["high_pct"]), dry_run=args.dry_run)
    if args.verb == "pull":
        if not re.match(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$", str(settings["host"])):
            log("no panel address (MEETING_PANEL_HOST or PANEL_IP); nothing to pull from", True)
            return EXIT_USAGE

        def alert(key, title, body):
            mc.send_alert(key, "high", title, body, args.env_file, STATE_DIR, log_tag="homehub-meetings")

        puller = Puller(Transport(ssh_argv(settings)), alert=alert)
        rc = puller.run()
        mc.save_json_atomic(os.path.join(STATE_DIR, "status.json"),
                            {"ts": int(time.time()), "exit": rc, "collected": puller.collected})
        return rc
    parser.print_usage(sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
