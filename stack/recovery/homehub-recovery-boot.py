#!/usr/bin/env python3
"""homehub-recovery-boot — C5 boot accounting: the shutdown stamp and the boot classifier.

Two subcommands, two units, one file (they share every format):

  stamp     ExecStop= of homehub-recovery-stamp.service. Runs late in shutdown
            (after Docker has stopped, before filesystems go read-only) and
            writes the clean-shutdown stamp atomically: this boot's id, UTC, and
            the contents of /run/homehub-recovery/intent if the supervisor left
            one. The intent is written to tmpfs at request time precisely so a
            slow disk cannot lose it; it rides to disk HERE.

  classify  ExecStart= of homehub-recovery-boot.service, early in every boot.
            Classifies the PREVIOUS boot into one of five classes, commits that
            to boots.log, computes the brake, sets kernel.panic, and leaves
            /run/homehub-recovery/boot.json for the supervisor. If the commit
            fails (ENOSPC, read-only root) boot.json says committed=false and the
            supervisor runs REPORT-ONLY: an unwritable disk must not become a way
            round the brake.

ALWAYS ON (record-only) whatever HUB_WATCHDOG_ENABLED says: counting reboots is
evidence, and evidence is P1. The knob only changes kernel.panic (via the full
tier) and whether anything acts on the brake.

Exit: 0 committed (or stamp written); 1 accounting could not be committed (the
boot.json still says so); 2 bad arguments.

Implements: SR-049, SR-051, LLR-994, LLR-995, LLR-996, LLR-997, LLR-1017
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import recovery_common as rc  # noqa: E402


def _log(message: str) -> None:
    # stdout -> journal (SyslogIdentifier=homehub-recovery-boot). This unit runs
    # early and alone; nothing here needs the telemetry discipline of the
    # supervisor's main loop.
    print(message, flush=True)


def _utc_iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# ── stamp ────────────────────────────────────────────────────────────────────


def write_stamp(paths: rc.Paths, now: Optional[float] = None) -> dict:
    """Write the clean-shutdown stamp (§4). Returns what was written.

    Contract:
      Outputs: stamp.json = {boot_id, utc, t, intent: <intent JSON or null>}.
      Raises:  OSError if it cannot be made durable; the unit then fails, the
               next boot finds no stamp and classifies this shutdown as
               unexplained — the conservative reading.
    Implements: SR-049, LLR-996
    """
    now = time.time() if now is None else now
    intent = rc.read_json(paths.intent_file)
    stamp = {
        "boot_id": rc.read_boot_id(paths),
        "utc": _utc_iso(now),
        "t": now,
        "intent": intent,
    }
    os.makedirs(paths.state_dir, exist_ok=True)
    rc.atomic_write(paths.stamp_file, json.dumps(stamp, sort_keys=True).encode("utf-8"))
    return stamp


# ── classify: the impure edges, each small and separately overridable ────────


def read_bootstatus(paths: rc.Paths) -> Optional[int]:
    """OR of every watchdog's bootstatus, or None if there is no watchdog at all.

    The module is loaded by modules-load.d before this unit runs (the unit is
    After=systemd-modules-load.service). Decimal, as watchdog_dev.c emits it."""
    values = []
    for path in glob.glob(os.path.join(paths.watchdog_class_dir, "watchdog*", "bootstatus")):
        try:
            with open(path, encoding="ascii") as handle:
                values.append(int(handle.read().strip() or "0"))
        except (OSError, ValueError):
            continue
    if not values:
        return None
    out = 0
    for v in values:
        out |= v
    return out


def cardreset_identities(paths: rc.Paths) -> List[str]:
    """Identities of every watchdog whose bootstatus carries WDIOF_CARDRESET.

    What D10's proof is bound to (rc.bootstatus_proven): a device reporting a
    reset proves THAT device can, not whichever one is selected later. A
    reporting device with no readable identity proves nothing.
    Implements: SR-051, LLR-1017
    """
    out: List[str] = []
    for path in sorted(glob.glob(os.path.join(paths.watchdog_class_dir, "watchdog*", "bootstatus"))):
        try:
            with open(path, encoding="ascii") as handle:
                value = int(handle.read().strip() or "0")
        except (OSError, ValueError):
            continue
        if value & rc.WDIOF_CARDRESET:
            identity = rc.watchdog_identity(paths, os.path.basename(os.path.dirname(path)))
            if identity and identity not in out:
                out.append(identity)
    return out


def list_pstore_records(paths: rc.Paths) -> List[str]:
    """Every record name under the live pstore fs and systemd-pstore's archive."""
    names: List[str] = []
    for base in paths.pstore_dirs:
        try:
            for name in sorted(os.listdir(base)):
                full = os.path.join(base, name)
                if os.path.isdir(full):
                    try:
                        names.extend("{}/{}".format(name, sub) for sub in sorted(os.listdir(full)))
                    except OSError:
                        names.append(name)
                else:
                    names.append(name)
        except OSError:
            continue
    return names


def journal_previous_boot_id(runner=subprocess.run) -> Optional[str]:
    """Boot id at index -1 of `journalctl --list-boots`, or None.

    A FALLBACK only. The primary record of which boot came before is our own
    last-boot file, because the journal is exactly what a hard reset is most
    likely to leave dirty, and it is bounded by retention."""
    try:
        done = runner(["journalctl", "--list-boots", "--no-pager", "-q"],
                      capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "-1":
            return parts[1].replace("-", "")
    return None


def write_kernel_panic(paths: rc.Paths, value: int) -> bool:
    try:
        with open(paths.kernel_panic_file, "w", encoding="ascii") as handle:
            handle.write("{}\n".format(value))
        return True
    except OSError:
        return False


# ── classify: the orchestration ──────────────────────────────────────────────


def classify(paths: rc.Paths, cfg: rc.Config, now: Optional[float] = None,
             journal_prev=journal_previous_boot_id) -> Tuple[dict, bool]:
    """Classify the previous boot, commit it, compute the brake, set kernel.panic.

    Contract:
      Outputs: (boot_record, committed). boot_record is also written to
               /run/homehub-recovery/boot.json for the supervisor, whatever
               happened to the disk.
      Idempotent within one boot: a second run (unit restart, `reset-brake`)
      finds last-boot == this boot, appends nothing and only recomputes.
    Implements: SR-049, SR-051, LLR-994, LLR-995, LLR-997
    """
    now = time.time() if now is None else now
    boot_id = rc.read_boot_id(paths)
    os.makedirs(paths.run_dir, exist_ok=True)
    committed = True
    error = ""
    record = {"boot_id": boot_id, "utc": _utc_iso(now), "t": now}

    last_boot = None
    try:
        with open(paths.last_boot_file, encoding="ascii") as handle:
            last_boot = handle.read().strip() or None
    except FileNotFoundError:
        pass
    except OSError as exc:
        committed, error = False, "cannot read last-boot: {}".format(exc)

    stamp = rc.read_json(paths.stamp_file)
    already = last_boot == boot_id
    # FAIL CLOSED (review C4): an existing log that cannot be read or parsed is
    # NOT an empty history. Nothing is committed, the brake is taken as ON,
    # kernel.panic stays 0 and the supervisor runs report-only - on a same-boot
    # re-run exactly as on the first run.
    try:
        entries = rc.read_boots_log_strict(paths.boots_log)
        log_ok = True
    except rc.AccountingError as exc:
        entries, log_ok = [], False
        committed, error = False, "boot accounting unreadable: {}".format(exc)
    known = rc.proven_identities(paths)
    newly: List[str] = []

    if already:
        mine = [e for e in entries if e.get("boot_id") == boot_id and "class" in e]
        record.update(mine[-1] if mine else {"class": "?", "reason": "already classified this boot"})
        record["t"], record["utc"] = now, _utc_iso(now)
    else:
        previous = last_boot
        if previous is None and stamp:
            # Never ran before but a stamp exists: bind it through the journal.
            previous = journal_prev()
        bootstatus = read_bootstatus(paths)
        seen = set((rc.read_json(paths.pstore_seen_file) or {}).get("seen", []))
        records = list_pstore_records(paths)
        fresh = [r for r in records if r not in seen]
        cls, reason = rc.classify_previous_boot(stamp, previous, bootstatus, fresh)
        newly = [i for i in cardreset_identities(paths) if i not in known]
        record.update({"class": cls, "reason": reason, "previous_boot_id": previous,
                       "bootstatus": bootstatus})
        try:
            if not committed:
                # A read already failed: committing on top of a history we could
                # not read would rewrite it from a partial view.
                raise OSError(error)
            os.makedirs(paths.state_dir, exist_ok=True)
            os.makedirs(paths.events_dir, exist_ok=True)
            if newly:
                # D10's proof, latched PER DEVICE IDENTITY the first time that
                # device reports a watchdog reset. It applies only while the
                # selected device is one of these (rc.bootstatus_proven), so a
                # fall-back to a device that cannot report counts power cuts
                # again.
                rc.atomic_write(paths.bootstatus_proven_file, json.dumps(
                    {"identities": known + newly, "utc": _utc_iso(now)},
                    sort_keys=True).encode("utf-8"))
            entry = {"t": now, "utc": record["utc"], "boot_id": boot_id,
                     "previous_boot_id": previous, "class": cls, "reason": reason}
            rc.append_boots_log(paths.boots_log, entry)
            entries.append(entry)
            if fresh:
                rc.atomic_write(paths.pstore_seen_file,
                                json.dumps({"seen": sorted(seen | set(records))}).encode("utf-8"))
            rc.atomic_write(paths.last_boot_file, (boot_id + "\n").encode("ascii"))
            rc.atomic_delete(paths.stamp_file)
        except OSError as exc:
            if committed:
                committed, error = False, "accounting could not be committed: {}".format(exc)

    proven = rc.bootstatus_proven(paths, extra=newly)
    brake = rc.brake_state(entries, now, cfg.max_recovery_boots, proven)
    if not log_ok:
        brake = rc.BrakeState(braked=True, counted_recent=-1, last_counted_utc=None,
                              detail="UNKNOWN - the accounting could not be read, so braked")
    panic = rc.kernel_panic_value(cfg, committed, brake.braked)
    panic_set = write_kernel_panic(paths, panic)
    record.update({
        "committed": committed,
        "error": error,
        "braked": brake.braked,
        "counted_recent": brake.counted_recent,
        "brake_detail": brake.detail,
        "bootstatus_proven": proven,
        "watchdog_identity": rc.selected_watchdog_identity(paths),
        "proven_identities": known + newly,
        "counting_unexplained": rc.CLASS_UNEXPLAINED in rc.counted_classes(proven),
        "enabled": cfg.enabled,
        "tiers": cfg.tiers,
        "kernel_panic": panic,
        "kernel_panic_set": panic_set,
    })
    # tmpfs: no fsync, and it must be written even when the disk refused.
    rc.atomic_write(paths.boot_json, json.dumps(record, sort_keys=True).encode("utf-8"), durable=False)
    return record, committed


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("stamp", "classify"))
    parser.add_argument("--root", default="/", help=argparse.SUPPRESS)  # tests only
    args = parser.parse_args(argv)
    paths = rc.Paths(args.root)
    if args.command == "stamp":
        stamp = write_stamp(paths)
        _log("clean-shutdown stamp written for boot {} (intent: {})".format(
            stamp["boot_id"], "yes" if stamp["intent"] else "no"))
        return 0
    try:
        cfg = rc.load_config(paths, os.environ)
    except rc.ConfigError as exc:
        # A bad knob must not stop the COUNTING. Classify with the disabled
        # defaults, which also forces kernel.panic=0.
        _log("WARNING: {} - classifying with the capability treated as disabled".format(exc))
        cfg = rc.parse_config({})
    record, committed = classify(paths, cfg)
    _log("previous boot: {class} ({reason}); brake: {brake_detail}{b}; kernel.panic={kernel_panic}".format(
        b=" - BRAKED" if record["braked"] else "", **{k: record.get(k, "?") for k in
                                                     ("class", "reason", "brake_detail", "kernel_panic")}))
    if not committed:
        _log("ERROR: {} - the supervisor will run REPORT-ONLY this boot".format(record["error"]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
