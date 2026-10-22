#!/usr/bin/env python3
"""The wall panel's low-disk alarm: measure free space, say LOW loudly, publish it.

WHY THIS EXISTS. On 2026-10-18 at 00:02 the panel's root filesystem filled for
about two minutes while the weekly logrotate gzipped a /var/log/syslog that had
grown to tens of gigabytes. Chromium's cookie store hit FILE_ERROR_NO_SPACE and
silently stopped persisting for the rest of that kiosk run, so the next restart
lost the Pandora sign-in. Nothing on the panel said the disk was nearly full,
before or after. The log cap (wall-logrotate-rsyslog, wall-journald.conf) closes
that one cause; this is the alarm for the next one, whatever it is.

WHAT IT DOES, once per invocation (wall-disk-check.timer runs it every 15 min):
  * runs `df` on / and on the media cache, deduplicated by mount point, so the
    cache is reported only when it really is a separate filesystem;
  * writes one journal line per mount -- `wall-disk: LOW <label> ...` at
    warning priority, `wall-disk: ok <label> ...` at info -- so
    `journalctl -t wall-disk --grep LOW` is the whole query;
  * replaces /run/wall-disk/status.json atomically with what it measured, for
    the panel app to surface later.

THE THRESHOLD. A mount is LOW when its available bytes are below the SMALLER of
10 % of its size and 5 GB (10**9): on any volume over 50 GB that is the 5 GB
arm, below 50 GB the 10 % arm. Taking the smaller keeps a big, comfortably
sized volume from alarming with tens of GB free, and a small one (a lab VM's
root) from alarming permanently for lack of 5 GB it never had. Both
numbers are overridable with --percent / --floor-bytes.

"Free" is df's Avail column -- what an unprivileged process such as the kiosk
can still write -- not the raw free count, which includes root's reserve.

PURE CORE -- parse_df(), threshold_bytes() and evaluate() do no I/O; main() is
the thin shell (subprocess, clock, file write). That split is what lets the
threshold logic be tested on Windows against fake df output.

Exit status: 0 whenever the disks were measured, LOW or not (a low disk is a
finding, not a failure of this check); 1 when df could not be run or parsed.
On failure the previous status file is left alone and simply goes stale, which
its ttlMs makes detectable.

Implements: SR-083, LLR-1141
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import tempfile

DEFAULT_PATHS = (("root", "/"), ("media", "/var/cache/wall-media"))
DEFAULT_PERCENT = 10.0
DEFAULT_FLOOR_BYTES = 5 * 10**9
DEFAULT_STATUS = "/run/wall-disk/status.json"
# Two timer periods: one missed tick is tolerated, two means nobody is
# measuring any more and a reader should stop believing the file.
DEFAULT_TTL_MS = 2 * 15 * 60 * 1000
SCHEMA_VERSION = 1

# sd-daemon(3) priority prefixes; journald parses them from stdout because the
# unit leaves SyslogLevelPrefix at its default (yes).
PRIORITY_WARNING = "<4>"
PRIORITY_INFO = "<6>"


def parse_df(text):
    """Parse `df -B1 --output=target,size,avail PATH...` into records, in order.

    Contract:
      Inputs:  text: str -- df's stdout: one header line, then one line per
               PATH argument, `<mount> <size bytes> <avail bytes>`.
      Outputs: list of {"mount": str, "totalBytes": int, "freeBytes": int},
               one per data line, in df's order (which is argument order).
      Raises:  ValueError on a line whose last two fields are not integers --
               a df that changed its output must not be read as "0 bytes free".
    The mount is everything before the two numbers, so a mount point that
    contains spaces still parses.
    """
    records = []
    lines = [line for line in text.splitlines() if line.strip()]
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 3:
            raise ValueError("unparseable df line: %r" % line)
        try:
            total, avail = int(fields[-2]), int(fields[-1])
        except ValueError:
            raise ValueError("unparseable df line: %r" % line) from None
        records.append({"mount": " ".join(fields[:-2]), "totalBytes": total, "freeBytes": avail})
    return records


def threshold_bytes(total_bytes, percent=DEFAULT_PERCENT, floor_bytes=DEFAULT_FLOOR_BYTES):
    """Free-space threshold for one mount: the smaller of percent-of-size and floor.

    Contract:
      Inputs:  total_bytes: int >= 0; percent: float in [0, 100];
               floor_bytes: int >= 0.
      Outputs: int -- a mount with fewer available bytes than this is LOW.
    """
    return int(min(total_bytes * percent / 100.0, floor_bytes))


def evaluate(labelled_paths, df_records, percent=DEFAULT_PERCENT, floor_bytes=DEFAULT_FLOOR_BYTES):
    """Pair each requested path with its df record and judge it.

    Contract:
      Inputs:  labelled_paths: sequence of (label, path), in the order the
               paths were handed to df; df_records: parse_df() output for
               exactly those paths, same order.
      Outputs: list of {"label", "path", "mount", "totalBytes", "freeBytes",
               "thresholdBytes", "low"}, one per DISTINCT mount -- a path whose
               mount was already reported (the media cache when it is still on
               root) is dropped, because it would be the same numbers twice.
      Raises:  ValueError when the two sequences differ in length.
    """
    if len(labelled_paths) != len(df_records):
        raise ValueError("df returned %d records for %d paths" % (len(df_records), len(labelled_paths)))
    results, seen = [], set()
    for (label, path), record in zip(labelled_paths, df_records):
        if record["mount"] in seen:
            continue
        seen.add(record["mount"])
        limit = threshold_bytes(record["totalBytes"], percent, floor_bytes)
        results.append({
            "label": label,
            "path": path,
            "mount": record["mount"],
            "totalBytes": record["totalBytes"],
            "freeBytes": record["freeBytes"],
            "thresholdBytes": limit,
            "low": record["freeBytes"] < limit,
        })
    return results


def journal_line(result):
    """One greppable journal line for one judged mount, priority prefix included."""
    total = result["totalBytes"]
    free_pct = (100.0 * result["freeBytes"] / total) if total else 0.0
    state = "LOW" if result["low"] else "ok"
    prefix = PRIORITY_WARNING if result["low"] else PRIORITY_INFO
    return "%swall-disk: %s %s mount=%s free=%d total=%d threshold=%d freePct=%.1f" % (
        prefix, state, result["label"], result["mount"], result["freeBytes"], total,
        result["thresholdBytes"], free_pct)


def status_document(results, observed_at, ttl_ms, percent, floor_bytes):
    """The /run status file's content (schemaVersion 1)."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "observedAt": observed_at,
        "ttlMs": ttl_ms,
        "thresholdPercent": percent,
        "thresholdFloorBytes": floor_bytes,
        "low": any(result["low"] for result in results),
        "mounts": results,
    }


# ── I/O shell ────────────────────────────────────────────────────────────────

def run_df(paths, df_command="df"):
    """df's stdout for these paths; raises on a non-zero exit or a hang."""
    completed = subprocess.run(
        [df_command, "-B1", "--output=target,size,avail", "--"] + list(paths),
        capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode != 0:
        raise RuntimeError("df exited %d: %s" % (completed.returncode, completed.stderr.strip()))
    return completed.stdout


def write_status(path, document):
    """Replace the status file atomically (temp file beside it, then rename)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".status.", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=1, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--path", action="append", metavar="LABEL=PATH",
                        help="a mount to check (repeatable); default root=/ and media=/var/cache/wall-media")
    parser.add_argument("--percent", type=float, default=DEFAULT_PERCENT)
    parser.add_argument("--floor-bytes", type=int, default=DEFAULT_FLOOR_BYTES)
    parser.add_argument("--status", default=DEFAULT_STATUS)
    parser.add_argument("--ttl-ms", type=int, default=DEFAULT_TTL_MS)
    parser.add_argument("--df", default="df", help="df executable (tests substitute a fake)")
    args = parser.parse_args(argv)
    if args.path:
        labelled = []
        for item in args.path:
            label, sep, path = item.partition("=")
            if not sep or not label or not path:
                parser.error("--path wants LABEL=PATH, got %r" % item)
            labelled.append((label, path))
        args.labelled_paths = tuple(labelled)
    else:
        args.labelled_paths = DEFAULT_PATHS
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    # A path that does not exist (no media cache on this panel yet) is skipped,
    # not passed to df, whose error would cost the root measurement too.
    present = [(label, path) for label, path in args.labelled_paths if os.path.exists(path)]
    if not present:
        print("%swall-disk: ERROR none of the paths exist: %s" % (
            PRIORITY_WARNING, " ".join(path for _, path in args.labelled_paths)))
        return 1
    try:
        records = parse_df(run_df([path for _, path in present], args.df))
        results = evaluate(present, records, args.percent, args.floor_bytes)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print("%swall-disk: ERROR could not measure free space: %s" % (PRIORITY_WARNING, error))
        return 1
    for result in results:
        print(journal_line(result))
    observed_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        write_status(args.status, status_document(
            results, observed_at, args.ttl_ms, args.percent, args.floor_bytes))
    except OSError as error:
        # The journal line above is the alarm; the file is a convenience. Say
        # so and fail the run, so `systemctl --failed` shows a broken publisher.
        print("%swall-disk: ERROR could not write %s: %s" % (PRIORITY_WARNING, args.status, error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
