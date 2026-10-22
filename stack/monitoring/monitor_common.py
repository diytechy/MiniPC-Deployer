"""monitor_common - the shared core of the hub's continuous monitors (SN-048).

ONE RESPONSIBILITY: the pieces every SN-048 monitor needs and must not grow its
own copy of - reading knobs from the stack .env, raising an alert through the
one alert helper, the once-per-episode alert state machine, atomic state files,
the mount table, the "is this filesystem on the NVMe" test, a bounded
subprocess runner and the secrets-floor redactor.

PURE WHERE IT DECIDES. read_env_text, knob_*, episode_step/episode_record,
parse_mountinfo, redact and the DNS packet helpers do no I/O and are what the
tests exercise directly. The thin shells (send_alert, run_bounded, load/save
state, is_nvme_backed) take their roots and paths as arguments so a test can
point them at a fake tree.

THE CONSTRAINTS THIS MODULE EXISTS TO HOLD (SN-048, HomeHub plan W3):
  * Never wake a spun-down disk. Mounts come from /proc/self/mountinfo, and the
    only filesystems anyone may statfs are those is_nvme_backed() says live on
    the NVMe. The USB backup and library drives are never touched.
  * Record and alert only. Nothing here acts on the box.
  * Network: ntfy (through the helper) and the liveness probe's named local
    targets; nothing in this module opens a socket except the DNS probe helper.

Python 3.8 compatible: the dev PC's gate interpreter is 3.8, the hub's is 3.12.

Implements: SR-085, LLR-1147, LLR-1148, LLR-1149
"""

from __future__ import annotations

import json
import os
import re
import signal
import struct
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
DEFAULT_ENV_FILE = "/opt/homehub/stack/.env"
# Read AFTER the stack .env, by the monitors only: where a drill puts its knob,
# so a drill never edits the stack's own .env (stack/monitoring/README.md).
DEFAULT_OVERRIDE_FILE = "/etc/homehub-monitor/override.env"
DEFAULT_STATE_DIR = "/var/lib/homehub-monitor"
DEFAULT_ALERT_HELPER = os.path.join(HERE, "homehub-alert.sh")

# The helper's documented exit codes (homehub-alert.sh header, LLR-1147).
ALERT_DELIVERED = "delivered"
ALERT_SUPPRESSED = "suppressed"
ALERT_FAILED = "failed"


# ── knobs ────────────────────────────────────────────────────────────────────


def read_env_text(text: str) -> Dict[str, str]:
    """Parse a stack .env AS TEXT, the way the shell helpers read it.

    Contract:
      Inputs:  text: the file's content.
      Outputs: {KEY: value} - last assignment wins; a CR, an unquoted trailing
               ` # comment` and one layer of surrounding quotes are stripped.
               Comment and blank lines are ignored. Never evaluated as shell.
    """
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = re.sub(r"\s#.*$", "", value).rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def read_env_file(path: str) -> Dict[str, str]:
    """read_env_text over a file; an unreadable file is an empty mapping."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return read_env_text(handle.read())
    except OSError:
        return {}


def read_knobs(env_file: str, override_file: Optional[str] = None) -> Dict[str, str]:
    """The stack .env with the monitors' override file laid over it.

    `override_file` defaults to $HOMEHUB_MONITOR_OVERRIDE_FILE, else
    DEFAULT_OVERRIDE_FILE; a missing override is simply nothing to lay over.
    """
    if override_file is None:
        override_file = os.environ.get(
            "HOMEHUB_MONITOR_OVERRIDE_FILE", DEFAULT_OVERRIDE_FILE
        )
    knobs = read_env_file(env_file)
    knobs.update(read_env_file(override_file))
    return knobs


def knob_bool(env: Dict[str, str], key: str, default: bool) -> bool:
    """`true`/`false` exactly (the repo's knob convention); anything else is the default."""
    value = env.get(key, "")
    if value == "true":
        return True
    if value == "false":
        return False
    return default


def knob_int(env: Dict[str, str], key: str, default: int, low: int, high: int) -> int:
    """A whole number within [low, high]; empty, malformed or out of range is the default."""
    value = env.get(key, "")
    if not re.fullmatch(r"[0-9]{1,12}", value):
        return default
    number = int(value)
    return number if low <= number <= high else default


def knob_words(env: Dict[str, str], key: str, default: str) -> List[str]:
    """A space-separated list; an unset key is the default, an EMPTY one is empty."""
    value = env[key] if key in env else default
    return value.split()


# ── logging ──────────────────────────────────────────────────────────────────

# sd-daemon(3) priority prefixes; journald reads them from stdout because the
# units leave SyslogLevelPrefix at its default.
PRIORITY_WARNING = "<4>"
PRIORITY_INFO = "<6>"


def log(message: str, warning: bool = False) -> None:
    """One journal line. A failure to log is not allowed to fail the monitor."""
    try:
        sys.stdout.write(
            (PRIORITY_WARNING if warning else PRIORITY_INFO) + message + "\n"
        )
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


# ── state files ──────────────────────────────────────────────────────────────


def load_json(path: str, default):
    """The decoded file, or `default` when it is absent, unreadable or corrupt.

    A corrupt state file (a torn write on a full disk) must not wedge a monitor:
    it starts again from nothing, which at worst repeats one alert.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def save_json_atomic(path: str, data) -> bool:
    """Replace `path` atomically (temp file in the same directory, then rename).

    Returns False instead of raising when the write fails (ENOSPC is the case
    this exists for): the previous file stays whole and the caller logs it.
    """
    directory = os.path.dirname(path) or "."
    tmp = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


# ── alerts: the episode state machine ────────────────────────────────────────
#
# An EPISODE is one stretch of a condition being bad. The monitor alerts once
# when it starts and once when it clears, never per sample. Delivery can fail
# (ntfy down) or be suppressed (the helper's per-key rate limit): either way the
# alert is NOT marked sent, so the next run tries again while the episode lasts.
# A clear is only sent for an episode whose start was delivered: a reader who
# never heard "X is bad" gains nothing from "X is fine again".


def episode_new() -> Dict[str, bool]:
    return {"active": False, "fired": False, "clear_pending": False}


def episode_step(episode: Optional[dict], bad: bool) -> Tuple[dict, Optional[str]]:
    """Advance one episode by one observation.

    Contract:
      Inputs:  episode: the stored dict (None = never seen); bad: this run's verdict.
      Outputs: (new_episode, action) where action is "fire", "clear" or None.
               Call episode_record with the delivery result of that action.
    """
    ep = dict(episode_new(), **(episode or {}))
    if bad:
        ep["clear_pending"] = False
        if not ep["active"]:
            ep["active"] = True
            ep["fired"] = False
        return ep, (None if ep["fired"] else "fire")
    if ep["active"]:
        ep["active"] = False
        ep["clear_pending"] = bool(ep["fired"])
        ep["fired"] = False
    return ep, ("clear" if ep["clear_pending"] else None)


def episode_record(episode: dict, action: Optional[str], delivered: bool) -> dict:
    """Fold the delivery result of `action` back into the episode."""
    ep = dict(episode)
    if action == "fire" and delivered:
        ep["fired"] = True
    if action == "clear" and delivered:
        ep["clear_pending"] = False
    return ep


def send_alert(
    key: str,
    priority: str,
    title: str,
    body: str,
    env_file: str,
    state_dir: str,
    helper: str = DEFAULT_ALERT_HELPER,
    repeat_sec: int = 1800,
    timeout: int = 6,
    tags: str = "warning",
    log_tag: str = "homehub-monitor",
) -> str:
    """Raise one alert through homehub-alert.sh; returns delivered|suppressed|failed.

    The helper writes the journal line itself (its stdout is passed through), so
    every alert is journaled whether or not ntfy took it. Bounded: the helper's
    own curl timeout plus this outer one.
    """
    # The interpreter is overridable for the dev-PC tests, where a bare "bash"
    # can resolve to the WSL launcher instead of the shell the suite runs under.
    argv = [
        os.environ.get("HOMEHUB_ALERT_BASH", "bash"), helper, "--key", key, "--priority", priority, "--title", title,
        "--env-file", env_file, "--state-dir", os.path.join(state_dir, "alerts"),
        "--repeat-sec", str(repeat_sec), "--timeout", str(timeout), "--tags", tags,
        "--log-tag", log_tag,
    ]  # fmt: skip
    try:
        done = subprocess.run(
            argv,
            input=body.encode("utf-8", "replace")[:4096],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout + 15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(
            "ALERT NOT SENT (the alert helper could not run: %s) [%s]: %s"
            % (exc, key, title),
            True,
        )
        return ALERT_FAILED
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        log(line, warning=done.returncode != 0)
    if done.returncode == 0:
        return ALERT_DELIVERED
    if done.returncode == 3:
        return ALERT_SUPPRESSED
    return ALERT_FAILED


def run_episode(
    episodes: dict,
    key: str,
    bad: bool,
    fire: Tuple[str, str, str],
    clear: Tuple[str, str],
    sender,
) -> None:
    """Step one keyed episode and send what it asks for, in place.

    fire = (priority, title, body); clear = (title, body), sent at `default`.
    `sender(key, priority, title, body)` returns delivered|suppressed|failed.
    The clear goes out under its own key (`<key>.clear`) so the rate limit on
    the start of an episode never swallows its end.
    """
    episode, action = episode_step(episodes.get(key), bad)
    delivered = False
    if action == "fire":
        delivered = sender(key, fire[0], fire[1], fire[2]) == ALERT_DELIVERED
    elif action == "clear":
        delivered = (
            sender(key + ".clear", "default", clear[0], clear[1]) == ALERT_DELIVERED
        )
    episodes[key] = episode_record(episode, action, delivered)


def drop_stale_episodes(episodes: dict, live_keys) -> None:
    """Forget episodes whose subject no longer exists and that owe nothing."""
    for key in list(episodes):
        ep = episodes[key]
        if (
            key not in live_keys
            and not ep.get("active")
            and not ep.get("clear_pending")
        ):
            del episodes[key]


# ── mounts, and the one test that keeps a parked disk asleep ────────────────


def _unescape_mount(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text: str) -> List[Dict[str, str]]:
    """Parse /proc/self/mountinfo into [{mount, majmin, fstype, source}], in order.

    proc(5): `id parent maj:min root mountpoint options [optional...] - fstype
    source superopts`. Octal escapes (\\040 for a space) are decoded.
    """
    out = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields, tail = left.split(), right.split()
        if len(fields) < 5 or len(tail) < 2:
            continue
        out.append({
            "mount": _unescape_mount(fields[4]),
            "majmin": fields[2],
            "fstype": tail[0],
            "source": _unescape_mount(tail[1]),
        })  # fmt: skip
    return out


def mount_entry(mounts: List[Dict[str, str]], path: str) -> Optional[Dict[str, str]]:
    """The entry whose mount point IS `path` (the last one wins, as the kernel stacks)."""
    found = None
    for entry in mounts:
        if entry["mount"] == path:
            found = entry
    return found


def is_nvme_backed(majmin: str, sys_root: str = "/sys", _depth: int = 0) -> bool:
    """True only when the block device `majmin` is an NVMe namespace or partition,
    or a device-mapper/md device built ENTIRELY from such devices.

    Reads sysfs links only, which never touch the disk. Anything unresolvable -
    a virtual filesystem (0:NN), a USB disk, a loop device - is False, and False
    means "do not statfs it".
    """
    if _depth > 4 or not re.fullmatch(r"[0-9]+:[0-9]+", majmin):
        return False
    link = os.path.join(sys_root, "dev", "block", majmin)
    try:
        real = os.path.realpath(link)
    except OSError:
        return False
    if not os.path.exists(real):
        return False
    name = os.path.basename(real)
    if re.match(r"nvme[0-9]+n[0-9]+", name):
        return True
    slaves_dir = os.path.join(real, "slaves")
    if re.match(r"(dm|md)-?[0-9]+", name) and os.path.isdir(slaves_dir):
        slaves = sorted(os.listdir(slaves_dir))
        if not slaves:
            return False
        for slave in slaves:
            dev_file = os.path.join(slaves_dir, slave, "dev")
            try:
                with open(dev_file, encoding="ascii") as handle:
                    slave_majmin = handle.read().strip()
            except OSError:
                return False
            if not is_nvme_backed(slave_majmin, sys_root, _depth + 1):
                return False
        return True
    return False


def read_boot_id(proc_root: str = "/proc") -> str:
    """This boot's id (proc/sys/kernel/random/boot_id), or "unknown"."""
    try:
        with open(
            os.path.join(proc_root, "sys", "kernel", "random", "boot_id"),
            encoding="ascii",
        ) as h:
            return h.read().strip() or "unknown"
    except (OSError, ValueError):
        return "unknown"


# ── processes, by comm only ──────────────────────────────────────────────────


def process_comms(proc_root: str = "/proc") -> Optional[List[str]]:
    """Every live process's comm (15 bytes at most), or None if /proc is unreadable.

    comm only - never cmdline or environ, which carry arguments and secrets.
    """
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return None
    out = []
    for pid in pids:
        try:
            with open(
                os.path.join(proc_root, pid, "comm"), encoding="utf-8", errors="replace"
            ) as h:
                out.append(h.read().strip())
        except OSError:
            continue
    return out


# ── a subprocess that cannot hang the monitor or flood its memory ───────────


class Bounded:
    """What run_bounded observed: the bytes it kept and how the command ended."""

    def __init__(self, rc, output, truncated, timed_out, error):
        self.rc = rc
        self.output = output
        self.truncated = truncated
        self.timed_out = timed_out
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.rc == 0


def _kill_tree(proc) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass


def run_bounded(argv: List[str], timeout: float, max_bytes: int, env=None) -> Bounded:
    """Run `argv` with stdout+stderr captured, killed at `timeout` seconds or as
    soon as it has produced `max_bytes`, whichever comes first.

    The whole process GROUP is killed, so a `docker` stuck on a dead daemon or a
    `journalctl` flooding a pipe cannot outlive its budget. A reader thread
    drains the pipe incrementally, so a flood never sits in memory beyond
    `max_bytes`, and the deadline holds even while the child writes nothing.
    """
    import threading  # local: the pure callers never need it

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        return Bounded(None, b"", False, False, str(exc))
    kept = {"chunks": [], "size": 0, "truncated": False}

    def _drain():
        reader = getattr(proc.stdout, "read1", proc.stdout.read)
        while True:
            try:
                data = reader(65536)
            except (OSError, ValueError):
                return
            if not data:
                return
            room = max_bytes - kept["size"]
            if len(data) >= room:
                kept["chunks"].append(data[:room])
                kept["size"] = max_bytes
                kept["truncated"] = len(data) > room or kept["truncated"]
                if len(data) > room:
                    return
                continue
            kept["chunks"].append(data)
            kept["size"] += len(data)

    thread = threading.Thread(target=_drain, daemon=True)
    thread.start()
    thread.join(timeout)
    timed_out = thread.is_alive()
    if timed_out or kept["truncated"]:
        _kill_tree(proc)
    try:
        rc = proc.wait(
            timeout=2 if (timed_out or kept["truncated"]) else max(0.1, timeout)
        )
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        timed_out = True
        try:
            rc = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            rc = None
    thread.join(2)
    # A descendant that left the process group can still hold the pipe open, and
    # closing a buffered reader another thread is blocked in would wait for that
    # thread for ever. So the pipe is closed only once the reader has finished;
    # otherwise the daemon reader is abandoned and dies with this process.
    if not thread.is_alive():
        try:
            proc.stdout.close()
        except OSError:
            pass
    return Bounded(rc, b"".join(kept["chunks"]), kept["truncated"], timed_out, None)


# ── the secrets floor, applied to evidence before it is written ─────────────
#
# The privacy scanner's secrets-floor classes (HomeHub scripts/privacy/
# check_privacy.py: KEY_RE and TOKEN_RES), plus the three shapes a system log
# actually leaks - a bearer header, a JWT and a `password=`-style assignment.
# Anything matching is replaced before a single byte reaches the bundle.
SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|$)", re.S)),
    ("github-token", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("github-fine-grained-token", re.compile(r"github_pat_[A-Za-z0-9_]{22,}")),
    ("slack-token", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("aws-access-key-id", re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])")),
    ("api-secret-key", re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_-])")),
    ("bearer", re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("assignment", re.compile(r"(?i)\b((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s,;\"']{4,}")),
]  # fmt: skip


def redact(text: str) -> Tuple[str, int]:
    """Return (text with every secrets-floor match replaced, number of matches)."""
    count = 0
    for label, pattern in SECRET_PATTERNS:

        def _sub(match, label=label):
            nonlocal count
            count += 1
            if label in ("bearer", "assignment"):
                return match.group(1) + "[REDACTED]"
            return "[REDACTED:%s]" % label

        text = pattern.sub(_sub, text)
    return text, count


# ── a DNS question, with no dependency beyond the socket module ─────────────


def dns_query_packet(name: str, query_id: int) -> bytes:
    """A standard recursive A query for `name` (RFC 1035 section 4.1)."""
    header = struct.pack(">HHHHHH", query_id & 0xFFFF, 0x0100, 1, 0, 0, 0)
    labels = b"".join(
        bytes([len(part)]) + part.encode("ascii")
        for part in name.strip(".").split(".")
        if part
    )
    return header + labels + b"\x00" + struct.pack(">HH", 1, 1)


def dns_answer_ok(packet: bytes, query_id: int) -> Tuple[bool, str]:
    """Is `packet` a response to our query that a WORKING server would give?

    NOERROR and NXDOMAIN both prove the server is answering; SERVFAIL and
    REFUSED do not (a wedged resolver says SERVFAIL). Returns (ok, why).
    """
    if len(packet) < 12:
        return False, "short reply (%d bytes)" % len(packet)
    rid, flags = struct.unpack(">HH", packet[:4])
    if rid != query_id & 0xFFFF:
        return False, "reply id mismatch"
    if not flags & 0x8000:
        return False, "not a response"
    rcode = flags & 0x000F
    names = {
        0: "NOERROR",
        1: "FORMERR",
        2: "SERVFAIL",
        3: "NXDOMAIN",
        4: "NOTIMP",
        5: "REFUSED",
    }
    label = names.get(rcode, "rcode %d" % rcode)
    return rcode in (0, 3), label
