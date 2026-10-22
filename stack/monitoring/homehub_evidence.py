"""homehub_evidence - one bounded, redacted evidence bundle per liveness incident.

WHY THIS EXISTS (SN-048). On 2026-10-04 the hub wedged for hours and left no
account of what it was doing (HomeHub FOLLOWUP_HUB_UNRESPONSIVE_RCA_2026-10-04).
The panel's freeze bundles are how panel faults get diagnosed; this is the hub's
equivalent, captured by the liveness probe BEFORE anything else happens. It is
independent of recovery's critical path (HomeHub
HUB_HANG_WATCHDOG_DESIGN_2026-10-05.md, "evidence"): it acts on nothing.

WHAT A BUNDLE HOLDS, in /var/lib/homehub-evidence/<UTC ts>/ (root, 0700; files 0600):
  processes.txt    every process by comm (pid ppid state comm rss threads) -
                   never a command line or an environment
  top.txt          `top -b -n1`, which prints comm
  psi.txt          /proc/pressure/{cpu,memory,io}
  meminfo.txt      /proc/meminfo
  dstate.txt       every task in D state: comm, wchan and its kernel stack
  dmesg.txt        the kernel log's last 200 lines
  journal-units.txt / journal-tags.txt
                   the last 10 minutes of the journal for a NAMED list of units
                   and syslog tags only (HOMEHUB_EVIDENCE_JOURNAL_UNITS / _TAGS)
  docker-ps.txt    container names and states (a fixed --format: no labels,
                   no environment, no command)
  docker-stats.txt names, CPU, memory, pids (fixed --format)
  checks.json      what the probe saw fail
  manifest.json    per item: status (ok, timeout, failed, truncated,
                   skipped-deadline, write-failed), bytes, seconds, redactions;
                   the bundle's own totals; what pruning did

THE BOUNDS, all enforced here, never trusted to the commands:
  * per command: a timeout, and the whole process group killed at it;
  * per bundle: a deadline for the whole capture (default 90 s); items it
    cannot reach are recorded as skipped-deadline, not waited for;
  * bytes: a per-file cap and a per-bundle cap; output is cut, never buffered
    unbounded (a journal flood reads up to the cap and the reader stops);
  * total: at most KEEP bundles (20) and TOTAL_CAP bytes, oldest pruned first;
    the new bundle is never pruned; a prune that fails is recorded and the
    capture goes on.

SENSITIVE DATA. Every captured text passes monitor_common.redact (the privacy
scanner's secrets-floor patterns, plus bearer/JWT/password= shapes) before it is
written; the manifest counts the redactions per file.

Implements: SR-089, LLR-1162, LLR-1163, LLR-1164, LLR-1165
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import time
from typing import Callable, List, Optional, Tuple

import monitor_common as mc

DEFAULT_ROOT = "/var/lib/homehub-evidence"
KEEP = 20
FILE_CAP = 512 * 1024
BUNDLE_CAP = 4 * 1024 * 1024
TOTAL_CAP = 64 * 1024 * 1024
DEADLINE_S = 90.0
DSTATE_MAX = 50
DMESG_LINES = 200
JOURNAL_LINES = 2000
DIR_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z(?:-[0-9]+)?$")
DEFAULT_JOURNAL_UNITS = (
    "docker.service containerd.service homehub-firstboot.service homehub-rustdesk-check.service "
    "homehub-dns-console-check.service homehub-liveness.service homehub-watch.service "
    "homehub-disk-check.service homehub-telemetry.service"
)
DEFAULT_JOURNAL_TAGS = "kernel hub-pressure homehub-recovery-boot sshd systemd-journald"


# ── reading /proc without command lines ──────────────────────────────────────


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def process_table(proc_root: str) -> str:
    """pid ppid state comm rss_kb threads - from /proc/<pid>/stat and status only."""
    lines = [
        "%7s %7s %s %-16s %10s %7s" % ("PID", "PPID", "S", "COMM", "RSS_KB", "THREADS")
    ]
    try:
        pids = sorted((p for p in os.listdir(proc_root) if p.isdigit()), key=int)
    except OSError as exc:
        return "cannot list %s: %s\n" % (proc_root, exc)
    for pid in pids:
        stat = _read(os.path.join(proc_root, pid, "stat"))
        if not stat or ")" not in stat:
            continue
        comm = stat[stat.find("(") + 1 : stat.rfind(")")]
        rest = stat[stat.rfind(")") + 2 :].split()
        status = _read(os.path.join(proc_root, pid, "status")) or ""
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        rss = fields.get("VmRSS", "0 kB").split()[0]
        threads = fields.get("Threads", "?").strip()
        lines.append("%7s %7s %s %-16s %10s %7s" % (pid, rest[1] if len(rest) > 1 else "?",
                                                    rest[0] if rest else "?", comm, rss, threads))  # fmt: skip
    return "\n".join(lines) + "\n"


def dstate_tasks(proc_root: str, limit: int = DSTATE_MAX) -> str:
    """Every task (thread) in uninterruptible sleep: comm, wchan, kernel stack."""
    out, found = [], 0
    try:
        pids = sorted((p for p in os.listdir(proc_root) if p.isdigit()), key=int)
    except OSError as exc:
        return "cannot list %s: %s\n" % (proc_root, exc)
    for pid in pids:
        try:
            tids = sorted(
                os.listdir(os.path.join(proc_root, pid, "task")),
                key=lambda t: int(t) if t.isdigit() else 0,
            )
        except OSError:
            continue
        for tid in tids:
            base = os.path.join(proc_root, pid, "task", tid)
            stat = _read(os.path.join(base, "stat"))
            if not stat or ")" not in stat:
                continue
            state = stat[stat.rfind(")") + 2 :].split()[:1]
            if state != ["D"]:
                continue
            found += 1
            if found > limit:
                continue
            comm = stat[stat.find("(") + 1 : stat.rfind(")")]
            wchan = (_read(os.path.join(base, "wchan")) or "?").strip()
            stack = (
                _read(os.path.join(base, "stack")) or "(stack unreadable)\n"
            ).rstrip()
            out.append(
                "== pid %s tid %s comm %s wchan %s\n%s" % (pid, tid, comm, wchan, stack)
            )
    if found > limit:
        out.append("... %d more D-state task(s) not shown" % (found - limit))
    return ("\n".join(out) + "\n") if out else "no task in D state\n"


def files_text(paths: List[Tuple[str, str]]) -> str:
    chunks = []
    for label, path in paths:
        chunks.append("== %s\n%s" % (label, (_read(path) or "(unreadable)\n").rstrip()))
    return "\n".join(chunks) + "\n"


# ── the plan: what is captured, in order (pure) ──────────────────────────────


def capture_plan(proc_root: str, units: List[str], tags: List[str]) -> List[dict]:
    """The items of one bundle, in capture order.

    Each item is {"name", "argv"|"func", "timeout", "cap", "tail"}: argv items
    run bounded; func items read /proc. `tail` keeps only the last N lines.
    Ordered cheapest and most diagnostic first, so a deadline cuts the costly
    docker items rather than the process list.
    Implements: LLR-1162
    """
    clean = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LC_ALL": "C",
        "HOME": "/nonexistent",
    }
    since = [
        "--since",
        "-10min",
        "-n",
        str(JOURNAL_LINES),
        "--no-pager",
        "-o",
        "short-iso",
    ]
    unit_args = [a for u in units for a in ("-u", u)]
    tag_args = [a for t in tags for a in ("-t", t)]
    plan = [
        {"name": "processes.txt", "func": lambda: process_table(proc_root), "timeout": 10, "cap": FILE_CAP},
        {"name": "psi.txt", "func": lambda: files_text([(r, os.path.join(proc_root, "pressure", r)) for r in ("cpu", "memory", "io")]),
         "timeout": 5, "cap": 16 * 1024},
        {"name": "meminfo.txt", "func": lambda: files_text([("meminfo", os.path.join(proc_root, "meminfo"))]),
         "timeout": 5, "cap": 16 * 1024},
        {"name": "dstate.txt", "func": lambda: dstate_tasks(proc_root), "timeout": 10, "cap": 256 * 1024},
        {"name": "top.txt", "argv": ["top", "-b", "-n1", "-w", "200"], "env": clean, "timeout": 10, "cap": 256 * 1024},
        {"name": "dmesg.txt", "argv": ["dmesg", "-T"], "env": clean, "timeout": 10, "cap": 2 * 1024 * 1024,
         "tail": DMESG_LINES},
        {"name": "journal-units.txt", "argv": ["journalctl"] + since + unit_args, "env": clean, "timeout": 20,
         "cap": FILE_CAP},
        {"name": "journal-tags.txt", "argv": ["journalctl"] + since + tag_args, "env": clean, "timeout": 20,
         "cap": FILE_CAP},
        {"name": "docker-ps.txt", "argv": ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"],
         "env": clean, "timeout": 20, "cap": 64 * 1024},
        {"name": "docker-stats.txt", "argv": ["docker", "stats", "--no-stream", "--format",
                                               "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.PIDs}}"],
         "env": clean, "timeout": 30, "cap": 64 * 1024},
    ]  # fmt: skip
    if not units:
        plan = [i for i in plan if i["name"] != "journal-units.txt"]
    if not tags:
        plan = [i for i in plan if i["name"] != "journal-tags.txt"]
    return plan


# ── pruning (pure planner, thin deleter) ─────────────────────────────────────


def plan_prune(
    bundles: List[Tuple[str, int, int]], protect: str, keep: int, total_cap: int
) -> List[str]:
    """Bundles to delete, oldest first, so that at most `keep` remain and their
    total size fits `total_cap`. `bundles` = [(name, seq, bytes)]; age is the
    capture sequence number (immune to a clock jump), then the name. `protect`
    (the bundle just written) is never chosen.
    Implements: LLR-1164
    """
    ordered = sorted(bundles, key=lambda b: (b[1], b[0]))
    delete = []
    count, total = len(ordered), sum(b[2] for b in ordered)
    for name, _seq, size in ordered:
        if count <= keep and total <= total_cap:
            break
        if name == protect:
            continue
        delete.append(name)
        count -= 1
        total -= size
    return delete


def _dir_size(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def existing_bundles(root: str) -> List[Tuple[str, int, int]]:
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        path = os.path.join(root, name)
        if not DIR_RE.match(name) or not os.path.isdir(path):
            continue
        manifest = mc.load_json(os.path.join(path, "manifest.json"), {})
        seq = manifest.get("seq") if isinstance(manifest.get("seq"), int) else 0
        out.append((name, seq, _dir_size(path)))
    return out


def prune(
    root: str, protect: str, keep: int, total_cap: int, remover=shutil.rmtree
) -> Tuple[List[str], List[str]]:
    """Delete what plan_prune names. Returns (deleted, errors); never raises."""
    deleted, errors = [], []
    for name in plan_prune(existing_bundles(root), protect, keep, total_cap):
        try:
            remover(os.path.join(root, name))
            deleted.append(name)
        except OSError as exc:
            errors.append("%s: %s" % (name, exc))
    return deleted, errors


# ── capture ──────────────────────────────────────────────────────────────────


def _default_writer(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _bundle_name(root: str, now: float) -> str:
    base = _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name, n = base, 1
    while os.path.exists(os.path.join(root, name)):
        n += 1
        name = "%s-%d" % (base, n)
    return name


def capture(
    root: str,
    seq: int,
    plan: List[dict],
    checks: dict,
    now: Optional[float] = None,
    deadline_s: float = DEADLINE_S,
    bundle_cap: int = BUNDLE_CAP,
    keep: int = KEEP,
    total_cap: int = TOTAL_CAP,
    runner: Callable = mc.run_bounded,
    writer: Callable = _default_writer,
    remover=shutil.rmtree,
    clock: Callable = time.monotonic,
) -> dict:
    """Write one bundle and return its manifest (also written as manifest.json).

    Never raises for a capture problem: a command that fails or times out, a
    write that fails (ENOSPC), a prune that fails - each is a manifest entry and
    the rest of the bundle is still attempted.
    Implements: LLR-1163, LLR-1165
    """
    now = time.time() if now is None else now
    manifest = {"seq": seq, "items": [], "prune": {}, "complete": True}
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
    except OSError as exc:
        manifest.update(complete=False, error="cannot create %s: %s" % (root, exc))
        return manifest
    # Room first: leave space for this bundle under both caps before writing.
    deleted, errors = prune(root, "", keep - 1, max(0, total_cap - bundle_cap), remover)
    name = _bundle_name(root, now)
    path = os.path.join(root, name)
    manifest.update(bundle=name, path=path,
                    captured=_dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))  # fmt: skip
    try:
        os.mkdir(path, 0o700)
    except OSError as exc:
        manifest.update(complete=False, error="cannot create %s: %s" % (path, exc))
        return manifest
    started, used = clock(), 0
    items = [
        {
            "name": "checks.json",
            "data": json.dumps(checks, indent=2, sort_keys=True) + "\n",
            "cap": 64 * 1024,
        }
    ]
    items += plan
    for item in items:
        entry = {
            "name": item["name"],
            "status": "ok",
            "bytes": 0,
            "seconds": 0.0,
            "redactions": 0,
        }
        manifest["items"].append(entry)
        remaining = deadline_s - (clock() - started)
        if remaining <= 0:
            entry["status"] = "skipped-deadline"
            manifest["complete"] = False
            continue
        t0 = clock()
        cap = min(item["cap"], max(0, bundle_cap - used))
        if cap <= 0:
            entry["status"] = "skipped-bundle-cap"
            manifest["complete"] = False
            continue
        if "data" in item:
            text = item["data"]
        elif "func" in item:
            try:
                text = item["func"]()
            except Exception as exc:  # a /proc reader must never stop the bundle
                text, entry["status"] = "", "failed: %s" % exc
        else:
            done = runner(
                item["argv"], min(item["timeout"], remaining), cap, env=item.get("env")
            )
            text = done.output.decode("utf-8", "replace")
            if done.error is not None:
                entry["status"] = "failed: %s" % done.error
            elif done.timed_out:
                entry["status"] = "timeout after %ss" % min(
                    item["timeout"], int(remaining)
                )
            elif done.truncated:
                entry["status"] = "truncated"
            elif done.rc != 0:
                entry["status"] = "failed: exit %s" % done.rc
        if item.get("tail"):
            text = "\n".join(text.splitlines()[-item["tail"] :]) + "\n"
        text, entry["redactions"] = mc.redact(text)
        data = text.encode("utf-8", "replace")
        if len(data) > cap:
            data = data[:cap]
            if entry["status"] == "ok":
                entry["status"] = "truncated"
        try:
            writer(os.path.join(path, item["name"]), data)
            entry["bytes"] = len(data)
            used += len(data)
        except OSError as exc:
            entry["status"] = "write-failed: %s" % (exc.strerror or exc)
        entry["seconds"] = round(clock() - t0, 2)
        if entry["status"] != "ok":
            manifest["complete"] = False
    deleted2, errors2 = prune(root, name, keep, total_cap, remover)
    manifest["prune"] = {"deleted": deleted + deleted2, "errors": errors + errors2}
    manifest["bytes"] = used
    manifest["seconds"] = round(clock() - started, 2)
    try:
        writer(
            os.path.join(path, "manifest.json"),
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        )
    except OSError as exc:
        manifest["complete"] = False
        manifest["manifest_error"] = str(exc)
    return manifest
