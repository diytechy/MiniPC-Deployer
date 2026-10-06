"""C5 boot accounting and the brake - design §4 and §9 "Boot accounting".

Runs the real classifier against a temporary root: the stamp, boots.log,
last-boot, pstore records, the watchdog's bootstatus, kernel.panic and
/run/homehub-recovery/boot.json are all real files. Only the boot id and the
clock are chosen by the test.

Implements: TC-1056..TC-1060 (see docs/test/test-cases.csv)
"""

from __future__ import annotations

import errno
import json
import os

import pytest

from recovery_harness import BOOT_ID, RECOVERY, FakeDevice, Tree, config, make_supervisor, rc, recovery_boot

PREV = "a" * 32
NOW = 1_800_000_000.0
HOUR = 3600.0


def _tree(tmp_path, last_boot=PREV):
    tree = Tree(tmp_path)
    if last_boot:
        tree.write(tree.paths.last_boot_file, last_boot + "\n")
    return tree


def _stamp(tree, boot_id=PREV, intent=None):
    tree.write(tree.paths.stamp_file, json.dumps({"boot_id": boot_id, "t": NOW - 60, "intent": intent}))


def _bootstatus(tree, value):
    tree.write(os.path.join(tree.paths.watchdog_class_dir, "watchdog0", "bootstatus"), "{}\n".format(value))


def _classify(tree, cfg=None, now=NOW):
    return recovery_boot.classify(tree.paths, cfg or config(), now=now, journal_prev=lambda: None)


def _log(tree):
    return rc.parse_boots_log(open(tree.paths.boots_log, encoding="utf-8").read())


# ── the five classes ────────────────────────────────────────────────────────


def test_stamp_without_intent_is_clean_tc1056(tmp_path):
    tree = _tree(tmp_path)
    _stamp(tree)
    record, committed = _classify(tree)
    assert committed and record["class"] == "clean"


def test_stamp_with_intent_is_recovery_tc1056(tmp_path):
    tree = _tree(tmp_path)
    _stamp(tree, intent={"reason": "memory stall", "boot_id": PREV})
    record, _ = _classify(tree)
    assert record["class"] == "recovery" and "memory stall" in record["reason"]


def test_no_stamp_and_cardreset_is_watchdog_tc1056(tmp_path):
    tree = _tree(tmp_path)
    _bootstatus(tree, 0x20)
    assert _classify(tree)[0]["class"] == "watchdog"


def test_no_stamp_and_a_new_pstore_record_is_panic_tc1056(tmp_path):
    tree = _tree(tmp_path)
    tree.write(os.path.join(tree.paths.pstore_dirs[1], "1800000000", "dmesg.txt"), "Kernel panic")
    assert _classify(tree)[0]["class"] == "panic"


def test_no_stamp_and_no_evidence_is_unexplained_tc1056(tmp_path):
    tree = _tree(tmp_path)
    _bootstatus(tree, 0)
    assert _classify(tree)[0]["class"] == "unexplained"


def test_a_stamp_from_an_older_boot_is_not_this_boots_stamp_tc1056(tmp_path):
    tree = _tree(tmp_path)
    _stamp(tree, boot_id="9" * 32)
    assert _classify(tree)[0]["class"] == "unexplained"


def test_cardreset_wins_over_a_stamp_written_before_a_late_shutdown_hang_tc1056(tmp_path):
    tree = _tree(tmp_path)
    _stamp(tree, intent={"reason": "x"})
    _bootstatus(tree, 0x20)
    assert _classify(tree)[0]["class"] == "watchdog"


def test_an_old_pstore_record_is_not_a_new_panic_tc1056(tmp_path):
    tree = _tree(tmp_path)
    tree.write(os.path.join(tree.paths.pstore_dirs[1], "old", "dmesg.txt"), "x")
    tree.write(tree.paths.pstore_seen_file, json.dumps({"seen": ["old/dmesg.txt"]}))
    assert _classify(tree)[0]["class"] == "unexplained"


def test_the_capabilitys_first_boot_is_not_counted_tc1056(tmp_path):
    tree = _tree(tmp_path, last_boot=None)
    record, committed = _classify(tree)
    assert committed and record["class"] == "first" and record["counted_recent"] == 0


# ── the commit ──────────────────────────────────────────────────────────────


def test_the_commit_appends_rebinds_and_deletes_the_stamp_tc1057(tmp_path):
    tree = _tree(tmp_path)
    _stamp(tree)
    _classify(tree)
    assert [e["class"] for e in _log(tree)] == ["clean"]
    assert open(tree.paths.last_boot_file).read().strip() == BOOT_ID
    assert not os.path.exists(tree.paths.stamp_file)
    boot = json.load(open(tree.paths.boot_json))
    assert boot["boot_id"] == BOOT_ID and boot["committed"] is True


def test_a_second_run_in_the_same_boot_appends_nothing_tc1057(tmp_path):
    tree = _tree(tmp_path)
    _classify(tree)
    _classify(tree, now=NOW + 60)
    assert len(_log(tree)) == 1


def test_atomic_write_fsyncs_file_then_directory_and_never_tears_tc1057(tmp_path, monkeypatch):
    target = tmp_path / "boots.log"
    target.write_text("old\n")
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(rc, "_fsync_dir", lambda d: synced.append(("dir", d)))
    monkeypatch.setattr(rc.os, "fsync", lambda fd: (synced.append(("file", fd)), real_fsync(fd))[1])
    rc.atomic_write(str(target), b"new\n")
    assert [kind for kind, _ in synced] == ["file", "dir"], "the rename was not made durable"
    assert target.read_text() == "new\n"

    def enospc(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(rc.os, "fsync", enospc)
    with pytest.raises(OSError):
        rc.atomic_write(str(target), b"torn\n")
    assert target.read_text() == "new\n", "a failed write damaged the committed file"
    assert [p.name for p in tmp_path.iterdir()] == ["boots.log"], "a temp file was left behind"


def test_accounting_that_cannot_commit_makes_the_supervisor_report_only_tc1057(tmp_path, monkeypatch):
    tree = _tree(tmp_path)

    def full(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(rc, "append_boots_log", full)
    record, committed = _classify(tree)
    assert not committed and "No space" in record["error"]
    assert record["kernel_panic"] == 0
    assert json.load(open(tree.paths.boot_json))["committed"] is False
    sup = make_supervisor(tree)
    sup.setup()
    assert sup.mode == "report-only" and not FakeDevice.instances


# ── the brake and kernel.panic ──────────────────────────────────────────────


def _history(tree, classes, start=NOW - 3 * HOUR, step=HOUR):
    lines = [json.dumps({"t": start + i * step, "class": c, "boot_id": "%032x" % i})
             for i, c in enumerate(classes)]
    tree.write(tree.paths.boots_log, "\n".join(lines) + "\n")


def test_the_third_counted_boot_brakes_sets_panic_0_and_disarms_tc1058(tmp_path):
    tree = _tree(tmp_path)
    _history(tree, ["recovery", "watchdog"])
    _stamp(tree, intent={"reason": "stall"})
    record, _ = _classify(tree, cfg=config())
    assert record["braked"] and record["kernel_panic"] == 0
    assert open(tree.paths.kernel_panic_file).read().strip() == "0"
    sup = make_supervisor(tree)
    sup.setup()
    assert sup.mode == "braked" and not FakeDevice.instances


def test_below_the_brake_the_full_tier_sets_panic_10_tc1058(tmp_path):
    tree = _tree(tmp_path)
    _history(tree, ["recovery"])
    _stamp(tree)
    record, _ = _classify(tree, cfg=config())
    assert not record["braked"] and record["kernel_panic"] == 10
    assert open(tree.paths.kernel_panic_file).read().strip() == "10"


@pytest.mark.parametrize("env", [{"HUB_WATCHDOG_ENABLED": "false"}, {"HUB_WD_TIERS": "contain"}])
def test_panic_stays_0_unless_enabled_with_the_full_tier_tc1058(tmp_path, env):
    tree = _tree(tmp_path)
    cfg = rc.parse_config(dict({"HUB_WATCHDOG_ENABLED": "true", "HUB_WD_TIERS": "full"}, **env))
    assert _classify(tree, cfg=cfg)[0]["kernel_panic"] == 0


def test_the_brake_clears_only_after_24h_without_a_counted_boot_tc1058():
    day = 24 * HOUR
    entries = [{"t": 0, "class": "recovery"}, {"t": HOUR, "class": "recovery"},
               {"t": 2 * HOUR, "class": "panic"}]
    assert rc.brake_state(entries, 3 * HOUR, 3, False).braked
    # The oldest has aged out of 24 h but the last counted boot has not.
    assert rc.brake_state(entries, day + 30 * 60, 3, False).braked
    assert not rc.brake_state(entries, 2 * HOUR + day, 3, False).braked
    # A fourth counted boot 23 h after the third keeps it braked.
    entries.append({"t": 2 * HOUR + 23 * HOUR, "class": "watchdog"})
    assert rc.brake_state(entries, 2 * HOUR + 23 * HOUR + 60, 3, False).braked
    # Clean boots never count.
    assert not rc.brake_state([{"t": i, "class": "clean"} for i in range(10)], 20, 3, False).braked


def test_reset_brake_discards_everything_before_it_tc1058():
    entries = [{"t": i * 60.0, "class": "recovery"} for i in range(3)] + [{"t": 300.0, "event": "brake-reset"}]
    assert not rc.brake_state(entries, 400, 3, False).braked


# ── D10: who pays for a power cut ───────────────────────────────────────────


def test_d10_unexplained_counts_until_bootstatus_is_proven_tc1059():
    entries = [{"t": i * 60.0, "class": "unexplained"} for i in range(3)]
    assert rc.brake_state(entries, 200, 3, bootstatus_proven=False).braked
    assert not rc.brake_state(entries, 200, 3, bootstatus_proven=True).braked
    watchdog = [{"t": i * 60.0, "class": "watchdog"} for i in range(3)]
    assert rc.brake_state(watchdog, 200, 3, bootstatus_proven=True).braked


def test_d10_the_first_cardreset_latches_the_policy_switch_tc1059(tmp_path):
    tree = _tree(tmp_path)
    _bootstatus(tree, 0x20)
    record, _ = _classify(tree)
    assert record["bootstatus_proven"] and not record["counting_unexplained"]
    assert os.path.exists(tree.paths.bootstatus_proven_file)
    # A later power cut (unexplained) then costs nothing.
    tree.write(tree.paths.last_boot_file, PREV + "\n")
    _bootstatus(tree, 0)
    record, _ = _classify(tree, now=NOW + 60)
    assert record["class"] == "unexplained" and not record["counting_unexplained"]
    assert record["counted_recent"] == 1, "the power cut was charged to the brake"


def test_d10_without_a_latch_unexplained_boots_are_counted_tc1059(tmp_path):
    tree = _tree(tmp_path)
    _history(tree, ["unexplained", "unexplained"])
    record, _ = _classify(tree, cfg=config())
    assert record["class"] == "unexplained" and record["braked"] and record["counting_unexplained"]


# ── the stamp ───────────────────────────────────────────────────────────────


def test_the_stamp_carries_the_tmpfs_intent_to_disk_tc1060(tmp_path):
    tree = Tree(tmp_path)
    tree.write(tree.paths.intent_file, json.dumps({"reason": "memory stall", "boot_id": BOOT_ID}))
    stamp = recovery_boot.write_stamp(tree.paths, now=NOW)
    on_disk = json.load(open(tree.paths.stamp_file))
    assert on_disk == stamp and on_disk["boot_id"] == BOOT_ID
    assert on_disk["intent"]["reason"] == "memory stall"


def test_the_stamp_unit_stops_after_docker_and_before_unmount_tc1060():
    text = open(str(RECOVERY / "homehub-recovery-stamp.service"), encoding="utf-8").read()
    lines = {ln.split("=", 1)[0]: ln.split("=", 1)[1] for ln in text.splitlines()
             if "=" in ln and not ln.startswith("#")}
    # Reverse stop order: Before=docker.service means it STOPS after Docker.
    assert "docker.service" in lines["Before"].split()
    assert "docker.service" not in lines.get("After", "")
    assert "umount.target" in lines["Before"] and "shutdown.target" in lines["Before"]
    assert lines["Conflicts"] == "shutdown.target" and lines["DefaultDependencies"] == "no"
    assert "local-fs.target" in lines["After"]
    assert lines["ExecStop"].endswith("homehub-recovery-boot.py stamp")


# ── review C4: unreadable accounting fails closed ───────────────────────────


@pytest.mark.parametrize("damage", ["eio", "corrupt-line", "not-a-record"])
@pytest.mark.parametrize("rerun", [False, True])
def test_unreadable_accounting_fails_closed_even_on_a_same_boot_rerun_tc1075(tmp_path, damage, rerun):
    """A braked boot whose log then becomes unreadable must not release the brake."""
    tree = _tree(tmp_path)
    _history(tree, ["recovery", "watchdog"])
    _stamp(tree, intent={"reason": "stall"})
    first, committed = _classify(tree, cfg=config())
    assert committed and first["braked"]
    if not rerun:
        # damage before the FIRST classification of a boot instead
        tree.write(tree.paths.last_boot_file, PREV + "\n")
    if damage == "eio":
        os.unlink(tree.paths.boots_log)
        os.makedirs(tree.paths.boots_log)  # open() raises: the portable stand-in for EIO
    elif damage == "corrupt-line":
        with open(tree.paths.boots_log, "a", encoding="utf-8") as handle:
            handle.write('{"t": 1, "class": "recov\n')
    else:
        with open(tree.paths.boots_log, "a", encoding="utf-8") as handle:
            handle.write('["not", "a", "record"]\n')
    record, committed = _classify(tree, cfg=config(), now=NOW + 60)
    assert not committed and "unreadable" in record["error"]
    assert record["braked"] and record["kernel_panic"] == 0
    assert open(tree.paths.kernel_panic_file).read().strip() == "0"
    assert json.load(open(tree.paths.boot_json))["committed"] is False
    sup = make_supervisor(tree)
    sup.setup()
    assert sup.mode == "report-only" and not FakeDevice.instances


def test_an_absent_log_is_a_first_install_not_an_error_tc1075(tmp_path):
    tree = _tree(tmp_path)
    assert not os.path.exists(tree.paths.boots_log)
    record, committed = _classify(tree)
    assert committed and record["error"] == ""
