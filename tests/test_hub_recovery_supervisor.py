"""The hub recovery supervisor (C2), driven hermetically - design §9 "Hermetic".

Every clock, the procfs/cgroupfs tree, the watchdog device, the sshd probe and
the subprocess runner are injected (tests/recovery_harness.py). The cgroup.kill
write, the cgroup.events read and every file the supervisor leaves are REAL
files under a temporary root, so these tests watch what the code does rather
than what it says.

Grouped as §9 groups them: state machine, victims, kill, threads, shutdown and
disarm, envelope, evidence. Boot accounting is tests/test_hub_recovery_boot.py.

Implements: TC-1048..TC-1055, TC-1073..TC-1079, TC-1083 (see docs/test/test-cases.csv)
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from recovery_harness import (BOOT_ID, GiB, INVOCATION, RECOVERY, FakeClock, FakeDevice, Tree, actions_of, cid,
                              config, drain_telemetry, make_supervisor, rc, recoveryd, tick_for)

SM = recoveryd.MemoryStateMachine


def machine(**kw):
    args = dict(psi_full=40, kill_seconds=120, kill_measured_pct=75, reboot_seconds=600)
    args.update(kw)
    return SM(**args)


def feed(sm, start, seconds, psi, step=5.0):
    """Observe every `step` s from `start` for `seconds`; return (end, decisions)."""
    out, t = [], start
    while t < start + seconds:
        t += step
        out.extend(sm.observe(t, psi))
    return t, out


# ── state machine (§3.1) ────────────────────────────────────────────────────


def test_sustained_measured_pressure_kills_exactly_once_at_120s_tc1048():
    sm = machine()
    sm.observe(0, 90)
    t, out = feed(sm, 0, 115, 90)
    assert "kill" not in out, "killed before the 120 s window was complete"
    t, out = feed(sm, t, 5, 90)
    assert out.count("kill") == 1
    sm.note_kill(t)
    t, out = feed(sm, t, 55, 90)
    assert "kill" not in out, "a second kill inside the 60 s cooldown"


def test_a_single_gap_never_kills_tc1048():
    """One pressured sample, a 130 s scheduling gap, one more pressured sample."""
    sm = machine()
    assert sm.observe(0, 90) == ["episode-open"]
    out = sm.observe(130, 90)
    assert "gap" in out
    assert "kill" not in out, "a gap was counted as two minutes of measured pressure"


def test_gaps_never_count_as_measured_pressure_tc1048():
    """Unreadable PSI adds no sample: 3 minutes of it cannot reach a kill."""
    sm = machine()
    sm.observe(0, 90)
    _, out = feed(sm, 0, 180, None)
    assert "kill" not in out
    # And sparse measurements (every 16 s = every iteration a gap) are too few
    # to make a measured window, however pressured each one reads.
    sm2 = machine()
    sm2.observe(0, 90)
    _, out = feed(sm2, 0, 200, 90, step=16)
    assert "gap" in out and "kill" not in out


def test_75_percent_of_measured_samples_is_the_bar_tc1048():
    sm = machine()
    sm.observe(0, 90)
    t, out = 0, []
    # 2 of every 3 samples pressured = 67 % < 75 %
    for i in range(1, 40):
        t += 5
        out.extend(sm.observe(t, 90 if i % 3 else 10))
    assert "kill" not in out


def test_a_kill_does_not_reset_t0_and_600s_reboots_tc1048():
    sm = machine()
    sm.observe(0, 90)
    t, kills, reboots = 0, 0, 0
    while t < 700:
        t += 5
        out = sm.observe(t, 90)
        if "kill" in out:
            kills += 1
            sm.note_kill(t)
        reboots += out.count("reboot")
        if "reboot" in out:
            assert t == 600, "reboot clock did not run from the ORIGINAL t0"
    assert sm.t0 == 0, "a kill reset t0"
    assert kills == 3, "the 3-per-hour kill budget"
    assert reboots == 1, "the reboot decision repeated"


def test_gaps_count_as_elapsed_time_for_the_reboot_clock_tc1048():
    sm = machine()
    sm.observe(0, 90)
    assert "reboot" in sm.observe(601, 90), "a long gap inside an episode did not advance the reboot clock"


def test_sixty_seconds_of_measured_calm_clears_tc1048():
    sm = machine()
    sm.observe(0, 90)
    t, out = feed(sm, 0, 30, 90)
    t, out = feed(sm, t, 60, 5)  # calm samples at t=35..90: 55 s of calm
    assert "clear" not in out, "cleared before 60 s of calm"
    t, out = feed(sm, t, 5, 5)
    assert "clear" in out and sm.state == recoveryd.NORMAL and sm.t0 is None


def test_unmeasured_time_and_gaps_break_the_calm_streak_tc1073():
    """Review C8: 15 s of calm, then 50 s unmeasured, then calm, is NOT 60 s of calm."""
    sm = machine()
    sm.observe(0, 90)
    t, out = feed(sm, 0, 15, 5)
    t, out = feed(sm, t, 50, None)
    assert "clear" not in out, "unreadable PSI was counted as calm"
    t, out = feed(sm, t, 60, 5)  # calm at 70..125: 55 s since the streak restarted
    assert "clear" not in out, "the streak did not restart after the unmeasured stretch"
    t, out = feed(sm, t, 5, 5)
    assert "clear" in out
    # A scheduling gap breaks it the same way.
    sm2 = machine()
    sm2.observe(0, 90)
    t, _ = feed(sm2, 0, 15, 5)
    out = sm2.observe(t + 50, 5)
    assert out == ["gap"], "a gap was counted as calm"
    t, out = feed(sm2, t + 50, 55, 5)
    assert "clear" not in out
    _, out = feed(sm2, t, 5, 5)
    assert "clear" in out


def test_a_wall_clock_jump_changes_nothing_tc1048(tmp_path):
    """NTP stepping the wall clock by an hour mid-episode must not kill."""
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    tree.container(1, "hog", 3 * GiB)
    sup = make_supervisor(tree, clock=clock)
    sup.setup()
    tick_for(sup, clock, tree, 30, 90)
    clock.wall += 3600
    tick_for(sup, clock, tree, 30, 90)
    assert sup.sm.state == recoveryd.PRESSURED
    assert not sup.pending_confirm, "a wall-clock step produced a kill"
    assert sup.sm.t0 is not None and clock.mono - sup.sm.t0 == 55


def test_a_boottime_monotonic_divergence_resets_the_state_tc1048(tmp_path):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    sup = make_supervisor(tree, clock=clock)
    sup.setup()
    tick_for(sup, clock, tree, 60, 90)
    assert sup.sm.state == recoveryd.PRESSURED
    clock.boot += 3600  # suspended for an hour: BOOTTIME moved, MONOTONIC did not
    tree.psi(10)
    clock.advance(5)
    sup.safe_tick()
    assert sup.sm.state == recoveryd.NORMAL and sup.sm.t0 is None
    assert actions_of(drain_telemetry(sup), "suspend-detected")


# ── victims (§3.2) ──────────────────────────────────────────────────────────


def _inventory(tree):
    return rc.read_inventory(tree.paths.docker_containers_dir)


def test_protected_and_small_are_skipped_and_the_largest_is_chosen_tc1049(tmp_path):
    tree = Tree(tmp_path)
    tree.container(1, "caddy", 5 * GiB)
    tree.container(2, "technitium", 4 * GiB, service="technitium")
    tree.container(3, "tiny", 100 * 1024 * 1024)
    tree.container(4, "immich-ml", 2 * GiB)
    tree.container(5, "jellyfin", 3 * GiB)
    scopes = rc.scan_container_scopes(tree.paths.cgroup_root)
    choice = rc.choose_victim(scopes, _inventory(tree), ("caddy", "technitium"))
    assert choice.victim_info.name == "jellyfin"
    assert choice.would_have_killed == "caddy"
    assert any("tiny" in s for s in choice.skipped) or choice.victim_info.name != "tiny"


def test_host_processes_are_never_victims_tc1049(tmp_path):
    tree = Tree(tmp_path)
    host = os.path.join(tree.paths.cgroup_root, "system.slice", "cron.service")
    tree.write(os.path.join(host, "memory.current"), "{}\n".format(7 * GiB))
    tree.container(1, "small-but-container", 300 * 1024 * 1024)
    scopes = rc.scan_container_scopes(tree.paths.cgroup_root)
    assert {s.container_id for s in scopes} == {cid(1)}
    choice = rc.choose_victim(scopes, _inventory(tree), ())
    assert choice.victim.container_id == cid(1)


def test_an_unidentified_container_is_skipped_not_guessed_tc1049(tmp_path):
    tree = Tree(tmp_path)
    tree.container(1, "?", 6 * GiB, identified=False)
    tree.container(2, "known", 1 * GiB)
    choice = rc.choose_victim(rc.scan_container_scopes(tree.paths.cgroup_root), _inventory(tree), ())
    assert choice.victim.container_id == cid(2)
    assert any("unidentified" in s for s in choice.skipped)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "unreadable", "no-name", "no-hostconfig"])
def test_a_protected_container_with_damaged_metadata_is_never_chosen_tc1074(tmp_path, damage):
    """Review C5: caddy's metadata DIRECTORY exists but its identity cannot be read."""
    tree = Tree(tmp_path)
    tree.container(1, "caddy", 6 * GiB)
    tree.container(2, "jellyfin", 1 * GiB)
    meta = os.path.join(tree.paths.docker_containers_dir, cid(1))
    config_file = os.path.join(meta, "config.v2.json")
    if damage == "missing":
        os.unlink(config_file)
    elif damage == "corrupt":
        tree.write(config_file, '{"Name": "/caddy", "Config": ')
    elif damage == "unreadable":
        # A directory where the file should be: open() raises an OSError, the
        # portable stand-in for EIO.
        os.unlink(config_file)
        os.makedirs(config_file)
    elif damage == "no-name":
        tree.write(config_file, json.dumps({"Name": "", "Config": {"Labels": {}}}))
    else:
        os.unlink(os.path.join(meta, "hostconfig.json"))
    inventory = rc.read_inventory(tree.paths.docker_containers_dir)
    assert cid(1) not in inventory, "a blank or partial identity was invented"
    choice = rc.choose_victim(rc.scan_container_scopes(tree.paths.cgroup_root), inventory, ("caddy",))
    assert choice.victim.container_id == cid(2)
    assert any("unidentified" in s for s in choice.skipped)


def test_the_backup_slice_is_still_in_the_victim_set_tc1049(tmp_path):
    """C4 moves filebackup under homehub.slice; it must not leave L1's reach."""
    tree = Tree(tmp_path)
    tree.container(7, "stack-filebackup-run-1", 5 * GiB, service="filebackup",
                   slice_rel="homehub.slice/homehub-filebackup.slice", restart="no")
    choice = rc.choose_victim(rc.scan_container_scopes(tree.paths.cgroup_root), _inventory(tree), ())
    assert choice.victim_info.service == "filebackup"


def _pressure_until_kill(sup, clock, tree, limit=300):
    start = len(sup.kill_history), len(sup.quarantined)
    t = 0
    while t < limit:
        tick_for(sup, clock, tree, 5, 90)
        t += 5
        if (len(sup.kill_history), len(sup.quarantined)) != start:
            return True
    return False


def test_a_second_kill_of_one_service_within_an_hour_quarantines_it_tc1049(tmp_path):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    scope = tree.container(1, "immich-ml", 3 * GiB, service="immich-machine-learning")
    sup = make_supervisor(tree, clock=clock)
    sup.setup()
    sup.inventory = _inventory(tree)
    assert _pressure_until_kill(sup, clock, tree)
    assert _pressure_until_kill(sup, clock, tree)
    assert len(sup.kill_history) == 2 and not sup.quarantined
    budget = sup.sm.budget_left(clock.mono)
    assert _pressure_until_kill(sup, clock, tree)
    assert "immich-machine-learning" in sup.quarantined
    # The record names the compose project as well as the service, so a later
    # boot can find its current container without matching another project's
    # same-named service (TC-1084).
    record = sup.quarantined["immich-machine-learning"]
    assert (record["project"], record["service"], record["container"]) == (
        "stack", "immich-machine-learning", cid(1))
    assert sup.sm.budget_left(clock.mono) == budget, "quarantine spent kill budget"
    queued = list(sup.workers.actions["quarantine"])
    assert queued and queued[0]["container"] == cid(1)
    # And it is executed as update --restart=no + stop, so it stays down.
    calls = []
    sup.runner = lambda argv, timeout: (calls.append(argv) or (0, ""))
    sup.workers.run_one_action()
    assert ["docker", "update", "--restart=no", cid(1)] in calls
    assert ["docker", "stop", "-t", "10", cid(1)] in calls
    sup._write_status(clock.mono)
    status = json.load(open(tree.paths.status_json))
    assert status["health"] == "red" and status["quarantined"] == ["immich-machine-learning"]
    assert open(os.path.join(scope, "cgroup.kill")).read() == "1"


# ── kill, confirm, reconcile (§3.2) ─────────────────────────────────────────


def _armed_with_victim(tmp_path, runner=None, restart="unless-stopped"):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    scope = tree.container(1, "hog", 3 * GiB, restart=restart)
    sup = make_supervisor(tree, clock=clock, runner=runner)
    sup.setup()
    sup.inventory = _inventory(tree)
    return tree, clock, sup, scope


def test_kill_writes_cgroup_kill_and_confirms_populated_0_tc1050(tmp_path):
    tree, clock, sup, scope = _armed_with_victim(tmp_path)
    assert _pressure_until_kill(sup, clock, tree)
    assert open(os.path.join(scope, "cgroup.kill")).read() == "1"
    assert cid(1) in sup.pending_confirm
    tree.empty(scope)
    tick_for(sup, clock, tree, 5, 90)
    events = drain_telemetry(sup)
    assert actions_of(events, "kill-confirmed")
    assert [p["container"] for p in sup.workers.actions["reconcile"]] == [cid(1)]


def test_a_kill_that_does_not_take_is_recorded_and_the_episode_continues_tc1050(tmp_path):
    tree, clock, sup, scope = _armed_with_victim(tmp_path)
    assert _pressure_until_kill(sup, clock, tree)
    tick_for(sup, clock, tree, 10, 90)  # cgroup never empties
    assert actions_of(drain_telemetry(sup), "kill-did-not-take")
    assert sup.sm.t0 is not None


@pytest.mark.parametrize("restart,expected", [("unless-stopped", ["docker", "restart"]),
                                              ("no", ["docker", "rm", "-f"])])
def test_a_ghost_is_reconciled_tc1050(tmp_path, restart, expected):
    """Docker says running, the cgroup is empty: the 10-04 ghost."""
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            return 0, "true|{}|false|hog\n".format(restart)
        return 0, ""

    tree, clock, sup, scope = _armed_with_victim(tmp_path, runner=runner, restart=restart)
    tree.empty(scope)
    result = sup._execute_action("reconcile", {"container": cid(1), "path": scope, "key": cid(1)})
    assert result["ghost"] and result["ok"]
    assert calls[-1][:len(expected)] == expected and calls[-1][-1] == cid(1)


def test_docker_that_does_not_answer_is_retried_not_waited_on_tc1050(tmp_path):
    tree, clock, sup, scope = _armed_with_victim(tmp_path, runner=lambda a, t: (124, "timeout"))
    result = sup._execute_action("reconcile", {"container": cid(1), "path": scope, "key": cid(1)})
    assert not result["ok"]
    sup.pending_reconcile[cid(1)] = {"path": scope, "attempts": 1, "next": clock.mono + 60}
    sup.inflight.add(("reconcile", cid(1)))
    sup.workers.results.append(dict(result, kind="reconcile", key=cid(1)))
    tick_for(sup, clock, tree, 5, 0)
    assert cid(1) in sup.pending_reconcile, "an unanswered reconcile was dropped"
    tick_for(sup, clock, tree, 60, 0)
    assert [p["container"] for p in sup.workers.actions["reconcile"]] == [cid(1)], "not retried"


def test_a_hung_docker_delays_nothing_tc1050(tmp_path):
    """The action thread is stuck in `docker inspect`; ticks and pets go on."""
    hang = threading.Event()

    def runner(argv, timeout):
        hang.wait(30)
        return 124, "timeout"

    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    scope = tree.container(1, "hog", 3 * GiB)
    sup = make_supervisor(tree, clock=clock, runner=runner, start_threads=True)
    try:
        sup.setup()
        sup.inventory = _inventory(tree)
        assert _pressure_until_kill(sup, clock, tree)
        tree.empty(scope)
        pets = FakeDevice.instances[0].pets
        started = time.monotonic()
        tick_for(sup, clock, tree, 60, 90)
        assert time.monotonic() - started < 5, "a tick waited on Docker"
        assert FakeDevice.instances[0].pets >= pets + 12
    finally:
        hang.set()


# ── threads (§3.5) ──────────────────────────────────────────────────────────


def test_telemetry_blocked_forever_does_not_stop_actions_or_petting_tc1051(tmp_path):
    blocked = threading.Event()
    calls = []

    def journal(line):
        blocked.wait(30)  # journald is gone; both telemetry threads hang here

    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    sup = make_supervisor(tree, clock=clock, start_threads=True, journal=journal,
                          sshd_probe=lambda: False, runner=lambda a, t: (calls.append(a) or (0, "")),
                          cfg=config(HUB_WD_SSH_FAILS=1))
    try:
        sup.setup()
        for _ in range(400):  # far more telemetry than the queue holds
            sup.event("noise")
        tick_for(sup, clock, tree, 65, 0)
        deadline = time.monotonic() + 5
        while ["systemctl", "restart", "ssh"] not in calls and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ["systemctl", "restart", "ssh"] in calls, "an action waited on blocked telemetry"
        assert FakeDevice.instances[0].pets >= 13
        assert sup.workers.telemetry_dropped > 0, "the telemetry queue did not drop oldest"
    finally:
        blocked.set()


def test_a_reboot_request_is_never_dropped_or_duplicated_tc1051(tmp_path):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    sup = make_supervisor(tree, clock=clock)
    sup.setup()
    for _ in range(1000):
        sup.event("flood")
    sup._reboot(clock.mono, "test")
    sup._reboot(clock.mono, "test again")
    sup._enqueue("reboot", "reboot", {"key": "reboot"})
    assert len(sup.workers.actions["reboot"]) == 1
    calls = []
    sup.runner = lambda argv, timeout: (calls.append(argv) or (1, "failed"))
    sup.workers.run_one_action()
    clock.advance(5)
    sup.safe_tick()  # drains the FAILED result
    assert not sup._enqueue("reboot", "reboot", {"key": "reboot"}), "a failed reboot was re-queued"
    assert calls == [["systemctl", "reboot"]]
    intent = json.load(open(tree.paths.intent_file))
    assert intent["boot_id"] == BOOT_ID and intent["reason"] == "test"


def test_an_action_timeout_still_escalates_at_120s_tc1051(tmp_path):
    """`systemctl reboot` hangs on the action thread; the main thread stops petting."""
    hang = threading.Event()
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    sup = make_supervisor(tree, clock=clock, start_threads=True,
                          runner=lambda a, t: (hang.wait(30), (124, "timeout"))[1])
    try:
        sup.setup()
        sup._reboot(clock.mono, "test")
        tick_for(sup, clock, tree, 115, 0)
        assert sup.petting
        pets = FakeDevice.instances[0].pets
        tick_for(sup, clock, tree, 10, 0)
        assert not sup.petting
        assert FakeDevice.instances[0].pets <= pets + 1
        assert any(line.startswith("escalation") for line in sup.kmsg.lines)
    finally:
        hang.set()


def test_workers_drop_to_sched_other_and_main_does_not_tc1053(tmp_path):
    seen = []
    tree = Tree(tmp_path)
    tree.boot_json()
    sup = make_supervisor(tree, start_threads=True,
                          sched_other=lambda: seen.append(threading.current_thread().name))
    sup.setup()
    assert sorted(seen) == ["action", "telemetry-1", "telemetry-2"]
    assert sup.mode == "full"


# ── shutdown and disarm (§3.4, §3.6) ────────────────────────────────────────


def _armed(tmp_path):
    tree = Tree(tmp_path)
    tree.boot_json()
    sup = make_supervisor(tree)
    sup.setup()
    return tree, sup, FakeDevice.instances[0]


def test_sigterm_without_authorisation_hands_off_with_300s_and_no_magic_close_tc1052(tmp_path):
    tree, sup, dev = _armed(tmp_path)
    assert dev.calls[0] == ("set_timeout", 60)
    sup.on_sigterm()
    assert ("set_timeout", 300) in dev.calls
    assert ("magic_close",) not in dev.calls
    assert dev.calls[-2:] == [("keepalive",), ("close",)]
    assert sup.exits == [0]


def test_sigterm_never_disarms_even_with_a_valid_authorisation_present_tc1052(tmp_path):
    """A reboot is SIGTERM; only the SIGUSR1 disarm request may stop the device."""
    tree, sup, dev = _armed(tmp_path)
    tree.write(tree.paths.disarm_file, "{} {}\n".format(BOOT_ID, INVOCATION))
    sup.run(_signals(15))  # SIGTERM through the real loop
    assert ("magic_close",) not in dev.calls and ("set_timeout", 300) in dev.calls
    assert FakeDevice.state() == "active", "a SIGTERM stopped the timer"


def _ack(tree):
    return json.load(open(tree.paths.disarm_ack_file))


def test_a_disarm_request_stops_the_device_verifies_it_and_acknowledges_tc1077(tmp_path):
    tree, sup, dev = _armed(tmp_path)
    assert FakeDevice.state() == "active"
    tree.write(tree.paths.disarm_file, "{} {}\n".format(BOOT_ID, INVOCATION))
    sup.run(_signals(recoveryd.SIG_DISARM, None, 15))
    assert ("disable_card",) in dev.calls and ("magic_close",) in dev.calls
    assert FakeDevice.state() == "inactive"
    ack = _ack(tree)
    assert ack["ok"] is True and ack["boot_id"] == BOOT_ID and ack["invocation_id"] == INVOCATION
    assert ack["device_state"] == "inactive"
    assert sup.mode == "disarmed" and sup.device is None
    assert not os.path.exists(tree.paths.disarm_file), "the authorisation was not consumed"
    # The stop that follows is an ordinary SIGTERM with nothing left to hand off.
    assert ("set_timeout", 300) not in dev.calls and sup.exits == [0]


def test_a_disarm_that_does_not_stop_the_device_keeps_it_armed_and_says_so_tc1077(tmp_path):
    tree, sup, dev = _armed(tmp_path)
    FakeDevice.fail_disarm = True
    tree.write(tree.paths.disarm_file, "{} {}\n".format(BOOT_ID, INVOCATION))
    sup.on_disarm_request()
    ack = _ack(tree)
    assert ack["ok"] is False and ack["device_state"] == "active"
    assert sup.device is not None and sup.petting, "an active timer was abandoned"
    assert sup.mode == "full"
    reopened = FakeDevice.instances[-1]
    assert reopened is not dev and reopened.pets >= 1


@pytest.mark.parametrize("content", ["{} {}".format(BOOT_ID, "2" * 32), "{} {}".format("c" * 32, INVOCATION),
                                     "", "garbage"])
def test_a_stale_authorisation_is_ignored_and_consumed_tc1052(tmp_path, content):
    tree, sup, dev = _armed(tmp_path)
    tree.write(tree.paths.disarm_file, content)
    sup.on_disarm_request()
    assert ("magic_close",) not in dev.calls and FakeDevice.state() == "active"
    assert _ack(tree)["ok"] is False and sup.mode == "full"
    assert not os.path.exists(tree.paths.disarm_file)


def _signals(*sequence):
    """A fake sigtimedwait: returns each item once, then SIGTERM."""
    items = list(sequence)

    def wait(timeout):
        return items.pop(0) if items else 15

    return wait


# ── review C3: the device's real range, and failure after open ──────────────


@pytest.mark.parametrize("limits,knobs,fragment", [
    ((1, 30), {}, "HUB_WD_HW_TIMEOUT=60 is outside"),
    ((1, 120), {}, "HUB_WD_SHUTDOWN_SECONDS=300 is outside"),
    ((90, 0), {}, "HUB_WD_HW_TIMEOUT=60 is outside"),
])
def test_timeouts_outside_the_devices_range_never_open_it_tc1078(tmp_path, limits, knobs, fragment):
    tree = Tree(tmp_path)
    tree.boot_json()
    tree.watchdog(min_timeout=limits[0], max_timeout=limits[1])
    sup = make_supervisor(tree, cfg=config(**knobs))
    sup.setup()
    assert not FakeDevice.instances, "the device was opened (and so started) before validation"
    assert sup.mode == "report-only" and any(fragment in r for r in sup.reasons)


def test_an_unreadable_device_range_refuses_to_arm_tc1078(tmp_path):
    tree = Tree(tmp_path)
    tree.boot_json()
    os.unlink(os.path.join(tree.paths.watchdog_class_dir, "watchdog0", "max_timeout"))
    sup = make_supervisor(tree)
    sup.setup()
    assert not FakeDevice.instances and sup.mode == "report-only"


def test_a_failure_after_open_disarms_verifies_and_only_then_reports_only_tc1078(tmp_path):
    tree = Tree(tmp_path)
    tree.boot_json()
    sup = make_supervisor(tree)
    FakeDevice.fail_set_timeout = True
    sup.setup()
    dev = FakeDevice.instances[0]
    assert ("magic_close",) in dev.calls and FakeDevice.state() == "inactive"
    assert sup.mode == "report-only" and sup.device is None
    assert any("verified inactive" in r for r in sup.reasons)


def test_a_failure_after_open_that_cannot_disarm_keeps_petting_tc1078(tmp_path):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    sup = make_supervisor(tree, clock=clock)
    FakeDevice.fail_set_timeout = True
    FakeDevice.fail_disarm = True
    sup.setup()
    assert FakeDevice.state() == "active"
    assert sup.device is not None and sup.petting and sup.arm_error
    pets = sup.device.pets
    tick_for(sup, clock, tree, 30, 0)
    assert sup.device.pets >= pets + 6, "an active device was abandoned"
    status = json.load(open(tree.paths.status_json))
    assert status["health"] == "red" and status["arm_error"]
    assert any(line.startswith("arm-failed-device-retained") for line in sup.kmsg.lines)


# ── review C9: the running supervisor follows the brake ─────────────────────


def _log_entries(tree, entries):
    tree.write(tree.paths.boots_log, "\n".join(json.dumps(e) for e in entries) + "\n")


def _run_brake_check(sup, clock, tree):
    clock.advance(recoveryd.BRAKE_CHECK_SECONDS)
    sup.safe_tick()          # schedules the check
    drain_telemetry(sup)     # a telemetry thread reads the log
    clock.advance(5)
    sup.safe_tick()          # the main thread applies the verdict


def test_a_brake_that_expires_while_running_re_arms_through_validation_tc1079(tmp_path):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json(braked=True)
    day = 24 * 3600
    _log_entries(tree, [{"t": clock.wall - day - 3 * 60 * 60 + i * 60, "class": "recovery"} for i in range(3)])
    sup = make_supervisor(tree, clock=clock)
    sup.setup()
    assert sup.mode == "braked" and not FakeDevice.instances
    _run_brake_check(sup, clock, tree)
    assert sup.mode == "full" and FakeDevice.instances and FakeDevice.state() == "active"
    assert open(tree.paths.kernel_panic_file).read().strip() == "10"
    assert json.load(open(tree.paths.boot_json))["braked"] is False


def test_an_expired_brake_does_not_arm_a_device_outside_its_range_tc1079(tmp_path):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json(braked=True)
    tree.watchdog(min_timeout=1, max_timeout=30)
    sup = make_supervisor(tree, clock=clock)
    sup.setup()
    _run_brake_check(sup, clock, tree)
    assert sup.mode == "braked" and not FakeDevice.instances
    assert open(tree.paths.kernel_panic_file).read().strip() == "0"


@pytest.mark.parametrize("damage", ["counted", "unreadable"])
def test_a_brake_that_engages_while_running_releases_the_device_tc1079(tmp_path, damage):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    tree.write(tree.paths.kernel_panic_file, "10\n")
    sup = make_supervisor(tree, clock=clock)
    sup.setup()
    assert sup.mode == "full" and FakeDevice.state() == "active"
    if damage == "counted":
        _log_entries(tree, [{"t": clock.wall - 600 + i, "class": "watchdog"} for i in range(3)])
    else:
        os.makedirs(tree.paths.boots_log)  # unreadable: fail closed means braked
    _run_brake_check(sup, clock, tree)
    assert sup.mode == "braked" and sup.device is None and FakeDevice.state() == "inactive"
    assert open(tree.paths.kernel_panic_file).read().strip() == "0"
    sup._reboot(clock.mono, "test")
    assert not sup.workers.actions["reboot"]


def test_the_unit_has_no_execstop_and_the_envelope_settings_tc1052():
    unit = str(RECOVERY / "homehub-recoveryd.service")
    directives = [ln.split("=", 1)[0].strip() for ln in open(unit, encoding="utf-8")
                  if "=" in ln and not ln.lstrip().startswith("#")]
    text = open(unit, encoding="utf-8").read()
    assert not [d for d in directives if d.startswith("ExecStop")], "ExecStop would disarm every reboot"
    for line in ("OOMScoreAdjust=-1000", "CPUSchedulingPolicy=rr", "CPUSchedulingPriority=1",
                 "CPUSchedulingResetOnFork=yes", "LimitRTTIME=2s:4s", "LimitMEMLOCK=128M",
                 "MemoryMax=128M", "MemoryMin=64M", "Restart=no"):
        assert "\n" + line + "\n" in text, line


# ── envelope (§3.0) and modes (§4) ──────────────────────────────────────────


def test_a_setup_failure_leaves_it_unarmed_and_report_only_tc1053(tmp_path):
    tree = Tree(tmp_path)
    tree.boot_json()

    def fail():
        raise OSError(12, "Cannot allocate memory")

    sup = make_supervisor(tree, lock_memory=fail)
    sup.setup()
    assert sup.mode == "report-only" and sup.device is None and not FakeDevice.instances
    status = json.load(open(tree.paths.status_json))
    assert status["health"] == "red" and any("setup failed" in r for r in status["reasons"])


def test_a_worker_that_cannot_drop_priority_is_a_setup_failure_tc1053(tmp_path):
    tree = Tree(tmp_path)
    tree.boot_json()

    def refuse():
        raise PermissionError("EPERM")

    sup = make_supervisor(tree, start_threads=True, sched_other=refuse)
    sup.setup()
    assert sup.mode == "report-only" and not FakeDevice.instances


def test_a_memoryerror_in_the_main_loop_stops_petting_without_exiting_tc1053(tmp_path):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    sup = make_supervisor(tree, clock=clock)
    sup.setup()
    dev = FakeDevice.instances[0]
    tick_for(sup, clock, tree, 10, 0)
    pets = dev.pets

    def boom(now):
        raise MemoryError

    sup._sample = boom
    tick_for(sup, clock, tree, 30, 0)
    assert dev.pets == pets and not sup.petting and sup.dying
    assert sup.exits == [], "exiting would let a restarted instance re-arm and pet"


@pytest.mark.parametrize("boot,reason", [
    (None, "no boot accounting record"),
    ({"boot_id": "c" * 32, "committed": True}, "names another boot"),
    ({"boot_id": BOOT_ID, "committed": False, "error": "ENOSPC"}, "could not be committed"),
])
def test_unaccounted_boot_is_report_only_and_acts_on_nothing_tc1053(tmp_path, boot, reason):
    tree, clock = Tree(tmp_path), FakeClock()
    if boot is not None:
        tree.write(tree.paths.boot_json, json.dumps(boot))
    scope = tree.container(1, "hog", 3 * GiB)
    sup = make_supervisor(tree, clock=clock)
    sup.setup()
    sup.inventory = _inventory(tree)
    assert sup.mode == "report-only" and any(reason in r for r in sup.reasons)
    assert not FakeDevice.instances
    tick_for(sup, clock, tree, 700, 90)
    assert not os.path.exists(os.path.join(scope, "cgroup.kill"))
    events = drain_telemetry(sup)
    assert actions_of(events, "would-have-killed") and actions_of(events, "would-have-rebooted")
    assert not sup.workers.actions["reboot"]


def test_braked_opens_no_device_never_reboots_but_still_kills_tc1054(tmp_path):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json(braked=True)
    scope = tree.container(1, "hog", 3 * GiB)
    sup = make_supervisor(tree, clock=clock)
    sup.setup()
    sup.inventory = _inventory(tree)
    assert sup.mode == "braked" and not FakeDevice.instances
    tick_for(sup, clock, tree, 700, 90)
    assert open(os.path.join(scope, "cgroup.kill")).read() == "1"
    assert not sup.workers.actions["reboot"]
    assert actions_of(drain_telemetry(sup), "would-have-rebooted")


def test_contain_tier_kills_and_restarts_ssh_but_never_reboots_or_arms_tc1054(tmp_path):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    sup = make_supervisor(tree, clock=clock, cfg=config(HUB_WD_TIERS="contain", HUB_WD_SSH_FAILS=3),
                          sshd_probe=lambda: False)
    sup.setup()
    assert sup.mode == "contain" and not FakeDevice.instances
    tick_for(sup, clock, tree, 185, 0)
    assert len(sup.workers.actions["restart_ssh"]) == 1, "3 failed probes 60 s apart -> one restart"
    sup._reboot(clock.mono, "test")
    assert not sup.workers.actions["reboot"]


def test_ssh_probes_do_not_count_while_memory_is_pressured_tc1054(tmp_path):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    sup = make_supervisor(tree, clock=clock, cfg=config(HUB_WD_SSH_FAILS=1), sshd_probe=lambda: False)
    sup.setup()
    tick_for(sup, clock, tree, 100, 90)
    assert not sup.workers.actions["restart_ssh"]


# ── evidence (§3.7) ─────────────────────────────────────────────────────────


def test_every_action_writes_a_kmsg_line_and_an_evidence_file_tc1055(tmp_path):
    tree, clock, sup, scope = _armed_with_victim(tmp_path)
    assert _pressure_until_kill(sup, clock, tree)
    assert any(line.startswith("kill ") and "target=hog" in line for line in sup.kmsg.lines)
    drain_telemetry(sup)
    files = [n for n in os.listdir(tree.paths.events_dir) if n.endswith("-kill.json")]
    assert len(files) == 1 and BOOT_ID[:8] in files[0]
    evidence = json.load(open(os.path.join(tree.paths.events_dir, files[0])))
    assert evidence["details"]["target"] == "hog"
    assert len(evidence["samples"]) >= 24, "evidence must carry the samples already in memory"
    assert all(s["psi_full"] == 90 for s in evidence["samples"][-5:])


def test_a_kill_of_the_backup_names_the_backup_in_its_event_tc1055(tmp_path):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    tree.container(7, "stack-filebackup-run-1", 5 * GiB, service="filebackup", restart="no",
                   slice_rel="homehub.slice/homehub-filebackup.slice")
    sup = make_supervisor(tree, clock=clock)
    sup.setup()
    sup.inventory = _inventory(tree)
    assert _pressure_until_kill(sup, clock, tree)
    kill = actions_of(drain_telemetry(sup), "kill")[0]
    assert kill["details"]["interrupts_backup"] is True


# ── review C6: quarantine persistence under two telemetry threads ───────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename-over semantics; Windows refuses a "
                    "concurrent replace of one target. Runs on Linux (WSL / CI), as on the hub.")
def test_overlapping_atomic_writes_never_collide_tc1076(tmp_path):
    target = str(tmp_path / "quarantine.json")
    errors = []

    def writer(tag):
        for i in range(150):
            try:
                rc.atomic_write(target, json.dumps({"tag": tag, "i": i}).encode(), durable=False)
            except Exception as exc:  # noqa: BLE001 - any collision is the failure
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]
    assert json.load(open(target))["i"] == 149
    assert os.listdir(str(tmp_path)) == ["quarantine.json"], "a temp file was left behind"


def test_a_delayed_older_quarantine_snapshot_never_overwrites_a_newer_one_tc1076(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    tree.boot_json()
    sup = make_supervisor(tree)
    sup.setup()
    sup.quarantined = {"immich-ml": {"container": cid(1)}}
    sup._persist_quarantine()
    older = sup.workers.telemetry.pop()
    sup.quarantined = {}
    sup._persist_quarantine()
    newer = sup.workers.telemetry.pop()
    sup._execute_telemetry(newer)
    sup._execute_telemetry(older)  # the stalled one finally lands
    on_disk = json.load(open(tree.paths.quarantine_file))
    assert on_disk["quarantined"] == {} and on_disk["seq"] == newer["seq"]

    # And writers are serialised: while one is stuck inside its write, a second
    # does not write at the same time.
    gate, entered = threading.Event(), threading.Event()
    real = rc.atomic_write
    calls = []

    def slow(path, data, **kw):
        calls.append(json.loads(data)["seq"])
        if len(calls) == 1:
            entered.set()
            gate.wait(10)
        real(path, data, **kw)

    monkeypatch.setattr(rc, "atomic_write", slow)
    sup.quarantined = {"a": {}}
    sup._persist_quarantine()
    first = sup.workers.telemetry.pop()
    sup.quarantined = {"a": {}, "b": {}}
    sup._persist_quarantine()
    second = sup.workers.telemetry.pop()
    t1 = threading.Thread(target=sup._execute_telemetry, args=(first,))
    t1.start()
    assert entered.wait(5)
    t2 = threading.Thread(target=sup._execute_telemetry, args=(second,))
    t2.start()
    t2.join(0.5)
    assert t2.is_alive() and calls == [first["seq"]], "two quarantine writers ran at once"
    gate.set()
    t1.join(5)
    t2.join(5)
    assert sorted(json.load(open(tree.paths.quarantine_file))["quarantined"]) == ["a", "b"]


# ── review C1, supervisor side: a non-arming replacement takes over the orphan ─


@pytest.mark.parametrize("why", ["contain", "braked", "report-only"])
def test_a_replacement_that_will_not_arm_stops_its_predecessors_timer_tc1083(tmp_path, why):
    """`systemctl restart` handed the timer off at 300 s; nobody would pet it."""
    tree = Tree(tmp_path)
    tree.watchdog(state="active")
    cfg = config(HUB_WD_TIERS="contain") if why == "contain" else config()
    if why == "braked":
        tree.boot_json(braked=True)
    elif why == "contain":
        tree.boot_json()
    sup = make_supervisor(tree, cfg=cfg)
    sup.setup()
    assert sup.mode == why
    assert FakeDevice.instances and ("magic_close",) in FakeDevice.instances[0].calls
    assert FakeDevice.state() == "inactive" and sup.device is None


def test_an_orphan_that_will_not_stop_is_held_and_petted_tc1083(tmp_path):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.watchdog(state="active")
    sup = make_supervisor(tree, clock=clock)
    FakeDevice.fail_disarm = True
    sup.setup()  # no boot.json: report-only
    assert sup.mode == "report-only" and sup.device is not None and sup.petting and sup.arm_error
    pets = sup.device.pets
    tick_for(sup, clock, tree, 20, 0)
    assert sup.device.pets >= pets + 4


def test_full_tier_with_no_reset_device_still_reboots_tc1083(tmp_path):
    tree, clock = Tree(tmp_path), FakeClock()
    tree.boot_json()
    sup = make_supervisor(tree, clock=clock, device_path="")
    sup.setup()
    assert sup.mode == "full" and not FakeDevice.instances
    sup._reboot(clock.mono, "test")
    assert len(sup.workers.actions["reboot"]) == 1
