#!/usr/bin/env python3
"""homehub-watch - notice, within minutes, that a job which must stay up is not.

WHY THIS EXISTS (SN-048). The offsite sync client was down for most of
2026-09-19..10-21 and nothing noticed for a month. Every watch here is a row in
watch.table (one table, in the payload, edited as data); this script reads it
every 5 minutes (homehub-watch.timer), judges each row, and alerts once when a
watch fires and once when it clears.

THE KINDS (watch.table header has the row syntax):
  process        absent for THRESHOLD, counted on the monotonic clock within a
                 boot. A reboot restarts the count: a client that has not come
                 back 15 minutes after boot is the alert, not the reboot.
  unit-success   no successful finish for THRESHOLD. systemd's record
                 (Result, ExecMainStatus, ExecMainExitTimestamp) is read every
                 run and the newest success is kept in the state file, so the
                 age survives a reboot. A unit whose LAST run failed has not
                 succeeded since that run started, so a failure older than the
                 threshold is stale at once; a unit with no record at all is
                 judged from when this watch first saw it.
  docker-health  each container `unhealthy` for THRESHOLD, one alert each.
                 `docker ps` runs under a timeout; when docker cannot answer the
                 row is "unknown" and nothing changes (the liveness probe owns
                 "docker is not answering").
  docker-restarts each container whose RestartCount rose by RESTART_LOOP_MIN or
                 more within THRESHOLD, one alert each; clear once a whole
                 THRESHOLD passes with fewer. The rise is measured from the
                 newest sample at or before now - THRESHOLD, so the span is the
                 whole window plus at most one poll (judge_restarts has the
                 boundary rule). A crash-looping container is
                 INVISIBLE to docker-health: between deaths it is "starting",
                 never "unhealthy", and `docker ps` says "Up". Immich ran like
                 that for 2,327 restarts in 2026-08 (open-items C58) and only
                 the counter told the truth (LLR-1211).
  newest-file    the newest regular file matching TARGET (an absolute glob, one
                 path component per level, no `**`) has an mtime THRESHOLD old
                 or older, or no file has matched for THRESHOLD since the watch
                 first saw the tree empty (kept in the state file, wall clock).
                 Read-only and bounded: the walk never follows a symlink (not
                 the root, not a directory, not a file), looks at no more than
                 NEWEST_FILE_MAX_ENTRIES entries and gives up after
                 NEWEST_FILE_SCAN_SEC; either limit is a firing verdict, never
                 a guess from part of the tree. The door recorder's open
                 `.ts.part` is the recording in progress and counts (LLR-1290).
  scratch-identity
                 the scratch volume is not mounted as the one its guard expects
                 for THRESHOLD. The judgement is homehub_scratch.py's own:
                 TARGET is that script, run as `TARGET --env-file ENV status`
                 under a timeout, and the row is bad unless its JSON says
                 mounted, identity_ok and prjquota are all true. A status that
                 does not run, times out or prints no JSON is bad too - it must
                 alert, never crash the watch or go quiet (LLR-1291).

THRESHOLD 0m is allowed and means "fires at the first run that sees the
condition": a drill sets it through the override file on a healthy job and
restores it (README "Drills").

NEVER ACTS. It reads /proc, asks systemd and docker, and alerts. It starts and
stops nothing, including the synthetic drill process.

State: /var/lib/homehub-monitor/watch-state.json. Status (what each row looked
like, with `ts`): /var/lib/homehub-monitor/watch-status.json.

Exit: 0 when the table was read and every row judged (fired or not); 1 when the
table is missing or unreadable.

Implements: SR-088, LLR-1156, LLR-1157, LLR-1158, LLR-1211, LLR-1290, LLR-1291
"""

from __future__ import annotations

import argparse
import errno
import fnmatch
import json
import os
import re
import stat
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import monitor_common as mc  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
DEFAULT_TABLE = os.path.join(HERE, "watch.table")
KINDS = ("process", "unit-success", "docker-health", "docker-restarts",
         "newest-file", "scratch-identity")
# docker-restarts: this many restarts inside the row's window is a loop. Three,
# because one restart is a crash that recovered and two can be a deploy; the
# loop C58 recorded added ~20 every five minutes.
RESTART_LOOP_MIN = 3
# Every alert of one run must have finished this long after the run started;
# with monitor_common.SAVE_RESERVE_SEC it fits the unit's TimeoutStartSec=120,
# however many containers turn unhealthy at once while ntfy (itself a
# container) hangs.
ALERT_DEADLINE_SEC = 100
# newest-file: the most directory entries one run looks at, and how long the
# walk may take. The door tree holds about 150 segments at its cap (10-minute
# segments under DOOR_RECORDER_CAP_MIB); a tree past either limit is reported,
# never judged from the part that was read.
NEWEST_FILE_MAX_ENTRIES = 10000
NEWEST_FILE_SCAN_SEC = 10.0
# scratch-identity: the status command's bound. It reads mountinfo, one by-uuid
# link, statvfs and three quotactl calls; well under a second when healthy.
SCRATCH_STATUS_SEC = 10
# scratch-identity counts "bad for THRESHOLD" from the first bad run. Runs are
# OnUnitActiveSec=5min apart, measured from each activation, so two consecutive
# runs can sample the monotonic clock a fraction of a second less than 300 s
# apart; this much early still counts the period (the timer's AccuracySec).
PERIOD_SLACK_SEC = 30
GLOB_CHARS = re.compile(r"[*?\[]")
SUBST = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


# ── the table (pure) ─────────────────────────────────────────────────────────


def parse_threshold(text: str) -> Optional[int]:
    """`Nm` / `Nh` -> seconds; None when malformed. `0m` is 0: the drill value,
    which fires at the first run that sees the condition."""
    m = re.fullmatch(r"([0-9]{1,5})([mh])", text)
    if not m:
        return None
    return int(m.group(1)) * (60 if m.group(2) == "m" else 3600)


def split_glob(pattern: str) -> Optional[Tuple[str, List[str]]]:
    """An absolute glob -> (root, [one fnmatch pattern per level below it]).

    The root is every leading component with no wildcard; at least one must
    exist, so no row ever walks from `/`. With no wildcard at all the last
    component is the (literal) name. None when the pattern is relative, has
    an empty, `.` or `..` component, or uses `**` (no recursion: the work is
    bounded by the pattern's depth). On Windows (the gate's test runner) a
    drive-letter path is accepted with either slash.
    Implements: LLR-1290
    """
    norm = pattern.replace("\\", "/") if os.sep == "\\" else pattern
    if not os.path.isabs(norm):
        return None
    head, _, rest = norm.partition("/")
    parts = rest.rstrip("/").split("/")
    if not rest or any(p in ("", ".", "..") or "**" in p for p in parts):
        return None
    first = next((i for i, p in enumerate(parts) if GLOB_CHARS.search(p)), len(parts) - 1)
    if first == 0:
        return None
    return head + "/" + "/".join(parts[:first]), parts[first:]


def _subst(text: str, env: Dict[str, str]) -> str:
    return SUBST.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), text)


def condition_holds(when: str, env: Dict[str, str]) -> bool:
    """Every comma-separated KEY, KEY=VALUE, KEY!=VALUE term holds (`-` = always)."""
    if when in ("", "-"):
        return True
    for term in when.split(","):
        if "!=" in term:
            key, _, value = term.partition("!=")
            if env.get(key, "") == value:
                return False
        elif "=" in term:
            key, _, value = term.partition("=")
            if env.get(key, "") != value:
                return False
        elif not env.get(term, ""):
            return False
    return True


def parse_table(text: str, env: Dict[str, str]) -> Tuple[List[dict], List[str]]:
    """The rows that apply on this box, and a list of problems.

    Contract:
      Inputs:  text: watch.table; env: the stack .env.
      Outputs: ([{"name","kind","target","threshold_s"}], [problem strings]).
               A malformed row is a problem, never a guess. A row whose `when`
               does not hold, or that HOMEHUB_WATCH_DISABLE names, is left out.
    Implements: LLR-1156
    """
    rows, problems, seen = [], [], set()
    disabled = set(mc.knob_words(env, "HOMEHUB_WATCH_DISABLE", ""))
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not line:
            continue
        cols = line.split()
        if len(cols) not in (4, 5):
            problems.append(
                "line %d: expected 4 or 5 columns, got %d" % (number, len(cols))
            )
            continue
        name, kind, target, threshold = cols[:4]
        when = cols[4] if len(cols) == 5 else "-"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", name) or name in seen:
            problems.append("line %d: bad or duplicate name %r" % (number, name))
            continue
        seen.add(name)
        if kind not in KINDS:
            problems.append("line %d: unknown kind %r" % (number, kind))
            continue
        if name in disabled or not condition_holds(when, env):
            continue
        target, seconds = _subst(target, env), parse_threshold(_subst(threshold, env))
        if seconds is None or not target:
            problems.append(
                "line %d (%s): empty target or bad threshold %r"
                % (number, name, threshold)
            )
            continue
        if kind == "newest-file" and split_glob(target) is None:
            problems.append("line %d (%s): newest-file target %r is not an absolute glob with a fixed root"
                            % (number, name, target))  # fmt: skip
            continue
        if kind == "scratch-identity" and not os.path.isabs(target):
            problems.append("line %d (%s): scratch-identity target %r is not an absolute path"
                            % (number, name, target))  # fmt: skip
            continue
        rows.append(
            {"name": name, "kind": kind, "target": target, "threshold_s": seconds}
        )
    return rows, problems


# ── the judgements (pure; each takes what was observed and the row's state) ──


def judge_process(
    row: dict, st: dict, comms: Optional[List[str]], boot: str, mono: float
) -> Tuple[Optional[bool], dict, str]:
    """(bad|None if unknown, new state, description)."""
    st = dict(st)
    if comms is None:
        return None, st, "cannot read /proc"
    want = row["target"][:15]
    if (
        st.get("boot_id") != boot
        or st.get("since_mono") is None
        or mono < st["since_mono"]
    ):
        st.update(boot_id=boot, since_mono=mono)
    if want in comms:
        st["since_mono"] = mono
        return False, st, "running"
    absent = int(mono - st["since_mono"])
    return absent >= row["threshold_s"], st, "absent for %ds" % absent


def parse_systemctl_show(text: str) -> Dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key] = value
    return out


def _unix(value: str) -> Optional[float]:
    m = re.fullmatch(r"@([0-9]+)(?:\.[0-9]+)?", (value or "").strip())
    return float(m.group(1)) if m else None


def judge_unit_success(
    row: dict, st: dict, show: Optional[Dict[str, str]], now: float
) -> Tuple[Optional[bool], dict, str]:
    """Age of the newest observed success against the threshold (wall clock).

    `show` is `systemctl show --timestamp=unix` output parsed, or None when
    systemctl could not answer (unknown: nothing changes).
    """
    st = dict(st)
    if show is None:
        return None, st, "systemctl did not answer"
    st.setdefault("first_seen", now)
    if st["first_seen"] > now + 60:  # the clock went backwards past it
        st["first_seen"] = now
    exit_ts = _unix(show.get("ExecMainExitTimestamp", ""))
    start_ts = _unix(show.get("ExecMainStartTimestamp", ""))
    succeeded = (
        show.get("Result") == "success" and show.get("ExecMainStatus", "") == "0"
    )
    if succeeded and exit_ts is not None:
        st["last_success"] = max(st.get("last_success") or 0, exit_ts)
    baseline = st["first_seen"]
    if (
        not succeeded
        and start_ts is not None
        and show.get("ActiveState") != "activating"
    ):
        # The last run failed: no success since it STARTED.
        baseline = min(baseline, start_ts)
    reference = st.get("last_success") or baseline
    age = int(now - reference)
    what = (
        ("last success %ds ago" % age)
        if st.get("last_success")
        else ("no success seen for %ds" % age)
    )
    if show.get("LoadState") == "not-found":
        what += " (unit not installed)"
    return age > row["threshold_s"], st, what


def judge_containers(
    row: dict, st: dict, unhealthy: Optional[List[str]], mono: float, boot: str
) -> Tuple[Optional[Dict[str, bool]], dict, str]:
    """Per-container verdicts {name: bad}; None when docker could not answer."""
    st = dict(st)
    if unhealthy is None:
        return None, st, "docker did not answer"
    if st.get("boot_id") != boot:
        st = {"boot_id": boot, "since": {}}
    since = dict(st.get("since", {}))
    verdicts = {}
    for name in unhealthy:
        since.setdefault(name, mono)
        if mono < since[name]:
            since[name] = mono
        verdicts[name] = mono - since[name] >= row["threshold_s"]
    for name in list(since):
        if name not in unhealthy:
            del since[name]
            verdicts[name] = False
    st["since"] = since
    return (
        verdicts,
        st,
        ("unhealthy: " + ", ".join(sorted(unhealthy))) if unhealthy else "all healthy",
    )


def judge_restarts(
    row: dict, st: dict, counts: Optional[Dict[str, int]], mono: float, boot: str
) -> Tuple[Optional[Dict[str, bool]], dict, str]:
    """Per-container verdicts {name: looping}; None when docker could not answer.

    `counts` is {container: RestartCount} for the containers docker lists now.
    Each container keeps (monotonic time, count) samples, and the RISE is the
    count now minus the count of its BASELINE: the newest sample taken at or
    before `mono - THRESHOLD`. The measured span therefore always covers the
    whole window, and by at most one poll interval more - a restart inside the
    window is never lost to pruning (dropping every sample older than the window
    threw that baseline away, and three restarts at 100, 800 and 1700 s, polled
    every 301 s, never fired; W10-sA review F3). Until a container has a sample
    that old, the baseline is its oldest sample.

    Looping FIRES when the rise reaches RESTART_LOOP_MIN, over whatever span the
    samples cover. It CLEARS only when a whole THRESHOLD has passed with fewer:
    a rise below RESTART_LOOP_MIN measured from a baseline at least THRESHOLD
    old. A shorter history keeps the verdict it had, so a steady loop at the
    threshold rate stays fired instead of flapping fire/clear every poll.

    A count that went DOWN is a recreated container (a fresh one starts at 0),
    so its history restarts; a new boot restarts every history (the monotonic
    clock restarts). Either way a container that was looping stays looping
    until a whole THRESHOLD of the new history has passed with fewer. A
    container no longer listed is cleared. Pure: no I/O.
    Implements: SR-088, LLR-1211
    """
    st = dict(st)
    if counts is None:
        return None, st, "docker did not answer"
    if st.get("boot_id") != boot:
        st = {"boot_id": boot, "samples": {}, "looping": list(st.get("looping", []))}
    window = row["threshold_s"]
    cutoff = mono - window
    samples = {k: [list(x) for x in v] for k, v in st.get("samples", {}).items()}
    was_looping = set(st.get("looping", []))
    verdicts, rising = {}, []
    for name, count in counts.items():
        kept = [x for x in samples.get(name, []) if x[0] <= mono]
        if kept and count < kept[-1][1]:
            kept = []
        older = [x for x in kept if x[0] <= cutoff]
        history = older[-1:] + [x for x in kept if x[0] > cutoff]
        history.append([mono, count])
        samples[name] = history
        rise = count - history[0][1]
        if rise >= RESTART_LOOP_MIN:
            verdicts[name] = True
        elif history[0][0] <= cutoff:
            verdicts[name] = False
        else:
            verdicts[name] = name in was_looping
        if rise > 0:
            rising.append("%s +%d" % (name, rise))
    for name in list(samples):
        if name not in counts:
            del samples[name]
            verdicts[name] = False
    st["samples"] = samples
    st["looping"] = sorted(n for n, bad in verdicts.items() if bad)
    return (
        verdicts,
        st,
        ("restarts in the last %dm: %s" % (window // 60, ", ".join(sorted(rising))))
        if rising else "no restarts",
    )


def _span(seconds: int) -> str:
    """A threshold for a title: whole hours as `Nh`, anything else as `Nm`."""
    if seconds >= 3600 and seconds % 3600 == 0:
        return "%dh" % (seconds // 3600)
    return "%dm" % (seconds // 60)


def judge_newest_file(row: dict, st: dict, found: dict, now: float) -> Tuple[bool, dict, str]:
    """(bad, new state, description) from one walk (newest_match's result).

    Bad when the newest matching file's mtime is THRESHOLD old or older; when
    nothing matched, bad once the tree has been empty for THRESHOLD since this
    watch first saw it so (`empty_since`, wall clock, kept across reboots - a
    recorder that has not written since before a reboot is still not writing);
    and at once when the walk hit a limit, because a partial walk cannot vouch
    for the newest file. `now` must be read AFTER the walk, or the segment being
    written can carry an mtime later than `now`. Never unknown: a tree that
    cannot be read is a recorder that is not recording there. Pure.
    Implements: LLR-1290
    """
    st = dict(st)
    limit = row["threshold_s"]
    problem = ("; " + found["problem"]) if found.get("problem") else ""
    if found.get("limit"):
        st["firing"] = True
        return True, st, "%s: %s - not judged from part of the tree" % (row["target"], found["limit"])
    newest = found.get("newest")
    if newest is not None:
        st.pop("empty_since", None)
        st["firing"] = now - newest[0] >= limit
        return (st["firing"], st, "newest %s written %ds ago (%d matching)%s"
                % (newest[1], int(now - newest[0]), found.get("files", 0), problem))  # fmt: skip
    since = st.get("empty_since")
    if not isinstance(since, (int, float)) or since > now + 60:  # unset, or the clock went back past it
        since = now
    st["empty_since"] = since
    # A firing incident whose stale files were deleted is still an incident:
    # only a fresh matching file clears it (Sol W3b review 2).
    st["firing"] = bool(st.get("firing")) or now - since >= limit
    return (st["firing"], st,
            "no file matches %s (seen empty for %ds)%s" % (row["target"], int(now - since), problem))  # fmt: skip


def scratch_verdict(status: Optional[dict], error: str) -> Tuple[bool, str]:
    """(bad, description) from homehub_scratch.py status's JSON - ITS judgement
    of mounted / identity_ok / prjquota, read back, never re-derived. None (the
    status did not run or printed no JSON) is bad, with `error` as the reason.
    Pure.
    Implements: LLR-1291
    """
    if status is None:
        return True, "scratch status did not run: %s" % error
    where = str(status.get("mountpoint") or "/srv/scratch")
    if status.get("mounted") is not True:
        return True, "%s is not mounted" % where
    failed = []
    if status.get("identity_ok") is not True:
        failed.append("is not the filesystem HOMEHUB_SCRATCH_UUID names (expected %s, mounted from %s)"
                      % (status.get("expected_uuid") or "unset", status.get("source", "?")))  # fmt: skip
    if status.get("prjquota") is not True:
        failed.append("is mounted without prjquota (quotas not enforced)")
    if failed:
        return True, "%s %s" % (where, " and ".join(failed))
    return False, "%s mounted from %s, identity and prjquota ok" % (where, status.get("source", "?"))


def judge_scratch(
    row: dict, st: dict, bad_now: bool, what: str, boot: str, mono: float
) -> Tuple[bool, dict, str]:
    """(bad, new state, description). Bad once every run for THRESHOLD has been
    bad, counted on the monotonic clock from the FIRST bad run of this boot
    (PERIOD_SLACK_SEC early still counts), so the default 5m fires on the
    second bad run in a row and one slow status does not page anyone; 0m fires
    on the first. Once firing it stays firing until a good run - a reboot with
    the volume still wrong restarts the count but not the incident, so it never
    sends a false "mounted again" (Sol W3b review 1). One good run clears. Pure.
    Implements: LLR-1291
    """
    st = dict(st)
    if not bad_now:
        return False, {}, what
    since = st.get("bad_since_mono")
    if st.get("boot_id") != boot or not isinstance(since, (int, float)) or mono < since:
        st.update(boot_id=boot, bad_since_mono=mono)
        since = mono
    lasted = mono - since
    bad = bool(st.get("firing")) or lasted >= max(0, row["threshold_s"] - PERIOD_SLACK_SEC)
    st["firing"] = bad
    return bad, st, "%s (bad for %ds)" % (what, int(lasted))


# ── the shell ────────────────────────────────────────────────────────────────


def _errname(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, str(exc.errno))


def _walk_newest(root: str, levels: List[str], max_entries: int) -> dict:
    """Walk root/levels without following any symlink; see newest_match."""
    out = {"newest": None, "files": 0, "problem": None, "limit": None}
    try:
        top = os.lstat(root)
    except OSError as exc:
        out["problem"] = "%s: %s" % (root, _errname(exc))
        return out
    if not stat.S_ISDIR(top.st_mode):
        out["problem"] = "%s is %s (not followed)" % (
            root, "a symlink" if stat.S_ISLNK(top.st_mode) else "not a directory")
        return out
    here, seen, problems = [root], 0, []
    for depth, pattern in enumerate(levels):
        last, below = depth == len(levels) - 1, []
        for directory in here:
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > max_entries:
                            out["limit"] = "more than %d entries under %s" % (max_entries, root)
                            return out
                        if not fnmatch.fnmatchcase(entry.name, pattern):
                            continue
                        try:
                            if not last:
                                if entry.is_dir(follow_symlinks=False):
                                    below.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                mtime = entry.stat(follow_symlinks=False).st_mtime
                                out["files"] += 1
                                if out["newest"] is None or mtime > out["newest"][0]:
                                    out["newest"] = (mtime, entry.name)
                        except OSError:
                            continue  # gone between the listing and the stat (a segment closing)
            except OSError as exc:
                problems.append("%s: %s" % (directory, _errname(exc)))
        here = below
    out["problem"] = "; ".join(problems[:3]) or None
    return out


def newest_match(pattern: str, max_entries: int = NEWEST_FILE_MAX_ENTRIES,
                 timeout: float = NEWEST_FILE_SCAN_SEC) -> dict:  # fmt: skip
    """The newest regular file matching `pattern`, read-only and bounded.

    Returns {"newest": (mtime, name) or None, "files": N, "problem": str|None,
    "limit": str|None}. The walk runs in a daemon thread joined for `timeout`
    seconds, so a filesystem stuck in I/O costs this row its verdict ("limit"),
    never the run its record. Symlinks are never followed: a symlinked root,
    directory or file is not part of the tree.
    Implements: LLR-1290
    """
    split = split_glob(pattern)
    if split is None:
        return {"newest": None, "files": 0, "problem": "not an absolute glob", "limit": None}
    box = {}

    def _run():
        box["out"] = _walk_newest(split[0], split[1], max_entries)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout)
    if "out" not in box:
        return {"newest": None, "files": 0, "problem": None,
                "limit": "the walk of %s did not finish in %gs" % (split[0], timeout)}  # fmt: skip
    return box["out"]


def scratch_status(tool: str, env_file: str) -> Tuple[Optional[dict], str]:
    """Run `python3 TOOL --env-file ENV status`, bounded: (its JSON, "") or
    (None, why). The tool's own exit code and output decide; a crash, a
    timeout, a non-zero exit or a last line that is not a JSON object is None.
    Implements: LLR-1291
    """
    done = mc.run_bounded([sys.executable, tool, "--env-file", env_file, "status"], SCRATCH_STATUS_SEC, 65536)
    text = done.output.decode("utf-8", "replace").strip()
    tail = (text.splitlines() or [""])[-1][-200:]
    if done.error is not None:
        return None, "cannot run %s (%s)" % (tool, done.error)
    if done.timed_out:
        return None, "%s status timed out after %ds" % (tool, SCRATCH_STATUS_SEC)
    if done.rc != 0:
        return None, "%s status exited %s: %s" % (tool, done.rc, tail)
    try:
        status = json.loads((text.splitlines() or [""])[-1])
    except ValueError:
        return None, "%s status printed no JSON: %s" % (tool, tail)
    if not isinstance(status, dict):
        return None, "%s status printed no JSON object" % tool
    return status, ""


def systemctl_show(unit: str) -> Optional[Dict[str, str]]:
    props = "Result,ExecMainStatus,ExecMainExitTimestamp,ExecMainStartTimestamp,ActiveState,LoadState"
    done = mc.run_bounded(
        ["systemctl", "show", "--timestamp=unix", "-p", props, unit], 20, 65536
    )
    if not done.ok:
        return None
    return parse_systemctl_show(done.output.decode("utf-8", "replace"))


def docker_unhealthy() -> Optional[List[str]]:
    done = mc.run_bounded(
        ["docker", "ps", "--filter", "health=unhealthy", "--format", "{{.Names}}"],
        20,
        65536,
    )
    if not done.ok:
        return None
    return [
        n
        for n in done.output.decode("utf-8", "replace").split()
        if re.fullmatch(r"[A-Za-z0-9_.-]+", n)
    ]


def docker_restart_counts() -> Optional[Dict[str, int]]:
    """{name: RestartCount} for every container, stopped ones included (`-a`):
    one caught between a death and its restart must not drop out of the
    history and clear; a stopped one's count simply stops rising. None when
    docker cannot answer within its bound."""
    ids = mc.run_bounded(["docker", "ps", "-a", "-q", "--no-trunc"], 20, 65536)
    if not ids.ok:
        return None
    wanted = [i for i in ids.output.decode("utf-8", "replace").split() if re.fullmatch(r"[0-9a-f]{12,64}", i)]
    if not wanted:
        return {}
    done = mc.run_bounded(
        ["docker", "inspect", "--format", "{{.Name}} {{.RestartCount}}"] + wanted, 20, 262144
    )
    if not done.ok:
        return None
    counts = {}
    for line in done.output.decode("utf-8", "replace").splitlines():
        m = re.fullmatch(r"/?([A-Za-z0-9_.-]+) ([0-9]+)", line.strip())
        if m:
            counts[m.group(1)] = int(m.group(2))
    return counts


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="HomeHub watched jobs (SN-048)")
    p.add_argument(
        "--table", default=os.environ.get("HOMEHUB_WATCH_TABLE", DEFAULT_TABLE)
    )
    p.add_argument(
        "--state-dir",
        default=os.environ.get("HOMEHUB_MONITOR_STATE_DIR", mc.DEFAULT_STATE_DIR),
    )
    p.add_argument(
        "--env-file",
        default=os.environ.get("HOMEHUB_MONITOR_ENV_FILE", mc.DEFAULT_ENV_FILE),
    )
    p.add_argument(
        "--alert-helper",
        default=os.environ.get("HOMEHUB_ALERT_HELPER", mc.DEFAULT_ALERT_HELPER),
    )
    p.add_argument(
        "--proc-root", default=os.environ.get("HOMEHUB_MONITOR_PROC_ROOT", "/proc")
    )
    args = p.parse_args(argv)
    budget = mc.AlertBudget(ALERT_DEADLINE_SEC)
    env = mc.read_knobs(args.env_file)
    try:
        with open(args.table, encoding="utf-8") as handle:
            rows, problems = parse_table(handle.read(), env)
    except OSError as exc:
        mc.log("watch: cannot read %s (%s) - nothing watched" % (args.table, exc), True)
        return 1
    for problem in problems:
        mc.log("watch: table problem: " + problem, True)

    state_path = os.path.join(args.state_dir, "watch-state.json")
    state = mc.load_json(state_path, {})
    rows_state, episodes = state.get("rows", {}), state.get("episodes", {})
    now, mono = time.time(), time.monotonic()
    boot = mc.read_boot_id(args.proc_root)
    comms = mc.process_comms(args.proc_root)
    repeat = mc.knob_int(env, "HOMEHUB_ALERT_REPEAT_SEC", 1800, 0, 86400)

    def sender(key, prio, title, body):
        return mc.send_alert(key, prio, title, body, args.env_file, args.state_dir, helper=args.alert_helper,
                             repeat_sec=repeat, log_tag="homehub-watch", budget=budget)  # fmt: skip

    status, live_keys, pending = [], set(), []
    for row in rows:
        name, st = row["name"], rows_state.get(row["name"], {})
        if row["kind"] in ("docker-health", "docker-restarts"):
            looping = row["kind"] == "docker-restarts"
            if looping:
                verdicts, st, what = judge_restarts(row, st, docker_restart_counts(), mono, boot)
            else:
                verdicts, st, what = judge_containers(
                    row, st, docker_unhealthy(), mono, boot
                )
            if verdicts is not None:
                # A container that recovered (or vanished) while its clear could
                # not be delivered still owes that clear: keep stepping it.
                prefix = "watch-%s-" % name
                for key in episodes:
                    if key.startswith(prefix) and key[len(prefix) :] not in verdicts:
                        verdicts[key[len(prefix) :]] = False
            for cname, bad in (verdicts or {}).items():
                key = "watch-%s-%s" % (name, cname)
                live_keys.add(key)
                if looping:
                    fire = ("high", "HomeHub: container %s is restarting in a loop (%d+ restarts in %dm)"
                            % (cname, RESTART_LOOP_MIN, row["threshold_s"] // 60),
                            "%s was restarted at least %d times in %dm while `docker ps` can still say Up. Look at: "
                            "docker inspect -f '{{.RestartCount}}' %s; docker logs --tail 50 %s"
                            % (cname, RESTART_LOOP_MIN, row["threshold_s"] // 60, cname, cname))
                    clear = ("HomeHub: container %s stopped restarting" % cname,
                             "%s has not restarted %d times in the last %dm." % (cname, RESTART_LOOP_MIN, row["threshold_s"] // 60))
                else:
                    fire = ("high", "HomeHub: container %s unhealthy for %dm" % (cname, row["threshold_s"] // 60),
                            "docker reports %s unhealthy. Look at: docker inspect --format '{{json .State.Health}}' %s"
                            % (cname, cname))
                    clear = ("HomeHub: container %s is healthy again" % cname, "%s is no longer unhealthy." % cname)
                pending.append(mc.plan_episode(episodes, key, bad, fire, clear))  # fmt: skip
            bad = None if verdicts is None else any(verdicts.values())
        else:
            look = "journalctl -u homehub-watch; the watch table is stack/monitoring/watch.table"
            if row["kind"] == "newest-file":
                found = newest_match(row["target"])
                # read after the walk: the file being written may be newer than `now`
                bad, st, what = judge_newest_file(row, st, found, time.time())
                fire_title = "HomeHub: nothing new matching %s in %s (watch %s)" % (
                    row["target"], _span(row["threshold_s"]), name)
                clear_title = "HomeHub: %s is being written again (watch %s)" % (row["target"], name)
                look = "ls -lt %s; journalctl -u homehub-watch" % split_glob(row["target"])[0]
            elif row["kind"] == "scratch-identity":
                bad_now, what = scratch_verdict(*scratch_status(row["target"], args.env_file))
                bad, st, what = judge_scratch(row, st, bad_now, what, boot, mono)
                fire_title = "HomeHub: the scratch volume is not mounted as expected (watch %s)" % name
                clear_title = "HomeHub: the scratch volume is mounted as expected again (watch %s)" % name
                look = "python3 %s status; journalctl -u homehub-watch" % row["target"]
            elif row["kind"] == "process":
                bad, st, what = judge_process(row, st, comms, boot, mono)
                fire_title = "HomeHub: %s is not running (watch %s)" % (
                    row["target"],
                    name,
                )
                clear_title = "HomeHub: %s is running again (watch %s)" % (
                    row["target"],
                    name,
                )
            else:
                bad, st, what = judge_unit_success(
                    row, st, systemctl_show(row["target"]), now
                )
                fire_title = "HomeHub: %s has not succeeded in %s (watch %s)" % (
                    row["target"], "%dh" % (row["threshold_s"] // 3600) if row["threshold_s"] >= 3600
                    else "%dm" % (row["threshold_s"] // 60), name)  # fmt: skip
                clear_title = "HomeHub: %s succeeded again (watch %s)" % (
                    row["target"],
                    name,
                )
            key = "watch-" + name
            live_keys.add(key)
            if bad is not None:
                pending.append(mc.plan_episode(episodes, key, bad,
                               ("high", fire_title, "%s: %s. Look at: %s" % (row["target"], what, look)),
                               (clear_title, "%s: %s." % (row["target"], what))))  # fmt: skip
        rows_state[name] = st
        verdict = "unknown" if bad is None else ("FIRING" if bad else "ok")
        mc.log("watch: %-17s %-8s %s" % (name, verdict, what), warning=bool(bad))
        status.append(
            {"name": name, "kind": row["kind"], "verdict": verdict, "detail": what}
        )
    mc.drop_stale_episodes(episodes, live_keys)
    for name in list(rows_state):
        if name not in {r["name"] for r in rows}:
            del rows_state[name]
    # State (every owed alert pending) and status are written before any alert
    # is attempted, and the state again after each attempt: a slow ntfy can cost
    # this run its alerts, never its record.
    state = {"rows": rows_state, "episodes": episodes}
    saved = {"ok": mc.save_json_atomic(state_path, state)}
    mc.save_json_atomic(os.path.join(args.state_dir, "watch-status.json"),
                        {"ts": int(now), "rows": status, "problems": problems})  # fmt: skip

    def checkpoint():
        saved["ok"] = mc.save_json_atomic(state_path, state)

    mc.deliver_pending(episodes, pending, sender, checkpoint)
    if not saved["ok"]:
        mc.log("watch: WARN could not save %s" % state_path, True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
