"""C1's pressure sample, the knob validation, and the homehub-recovery CLI.

The sampler runs against a fake procfs/cgroupfs/Docker tree; the CLI's
systemctl and docker calls are substituted at its single `_run` seam so a
test can see exactly what disarm and release ask the system to do.

Implements: TC-1061..TC-1063, TC-1080, TC-1083, TC-1084 (see docs/test/test-cases.csv)
"""

from __future__ import annotations

import json
import os

import pytest

from recovery_harness import REPO, GiB, Tree, cid, load_script, rc

sampler = load_script("homehub_pressure_sample", "homehub-pressure-sample.py")
cli = load_script("homehub_recovery_cli", "homehub-recovery")


def _proc(tree, pid, name, rss_kb, state="S (sleeping)"):
    tree.write(os.path.join(tree.paths.proc_dir, str(pid), "status"),
               "Name:\t{}\nState:\t{}\nVmRSS:\t{} kB\n".format(name, state, rss_kb))


def test_a_sample_logs_protected_items_in_full_top5_and_dstate_tc1061(tmp_path):
    tree = Tree(tmp_path)
    tree.psi(12.5)
    tree.container(1, "caddy", 50 * 1024 * 1024)  # small, but protected: always logged
    tree.container(2, "immich-ml", 3 * GiB)
    tree.container(3, "stack-filebackup-run-x", 2 * GiB, service="filebackup",
                   slice_rel="homehub.slice/homehub-filebackup.slice")
    for i in range(1, 8):
        _proc(tree, 100 + i, "proc%d" % i, i * 1000)
    _proc(tree, 200, "pwsh", 900000, state="D (disk sleep)")
    _proc(tree, 300, "tailscaled", 40000)
    unit = os.path.join(tree.paths.cgroup_root, "system.slice", "tailscaled.service")
    tree.write(os.path.join(unit, "memory.current"), "52428800\n")
    tree.write(os.path.join(unit, "cgroup.procs"), "300\n")

    record = sampler.sample(tree.paths, ("caddy", "technitium"))
    json.dumps(record)  # it is one journal line of JSON
    assert record["psi"]["memory"]["full_avg10"] == 12.5
    assert record["mem"]["MemAvailable"] == 4000000
    assert [c["name"] for c in record["containers"]] == ["immich-ml", "stack-filebackup-run-x", "caddy"]
    assert record["containers"][1]["slice"] == "homehub-filebackup"
    assert [c["name"] for c in record["protected"]["containers"]] == ["caddy"]
    assert record["protected"]["host_units"] == [
        {"unit": "tailscaled.service", "memory_current": 52428800, "rss_kb": 40000}]
    assert [p["comm"] for p in record["top"]] == ["pwsh", "tailscaled", "proc7", "proc6", "proc5"]
    assert record["dstate_count"] == 1 and record["dstate"][0]["comm"] == "pwsh"


@pytest.mark.parametrize("env,fragment", [
    ({"HUB_WATCHDOG_ENABLED": "yes"}, "HUB_WATCHDOG_ENABLED"),
    ({"HUB_WD_TIERS": "everything"}, "HUB_WD_TIERS"),
    ({"HUB_WD_PSI_FULL": "0"}, "HUB_WD_PSI_FULL"),
    ({"HUB_WD_KILL_SECONDS": "600", "HUB_WD_REBOOT_SECONDS": "600"}, "must exceed"),
    ({"HUB_WD_KILL_MEASURED_PCT": "40"}, "HUB_WD_KILL_MEASURED_PCT"),
    ({"HUB_WD_PROTECT": "caddy,$(reboot)"}, "HUB_WD_PROTECT"),
    ({"HUB_OOM_MEM_PCT": "2,5"}, "KILL <= TERM"),
    ({"HUB_OOM_SWAP_PCT": "10"}, "TERM,KILL"),
    ({"HUB_WD_RESET_DEVICE": "iTCO"}, "HUB_WD_RESET_DEVICE"),
])
def test_a_bad_knob_is_refused_by_name_tc1062(env, fragment):
    with pytest.raises(rc.ConfigError) as err:
        rc.parse_config(env)
    assert fragment in str(err.value)


def test_empty_knobs_take_the_design_defaults_tc1062():
    cfg = rc.parse_config({k: "" for k in rc.KNOB_DEFAULTS})
    assert (cfg.enabled, cfg.tiers, cfg.psi_full, cfg.kill_seconds, cfg.kill_measured_pct,
            cfg.reboot_seconds, cfg.ssh_fails, cfg.max_recovery_boots, cfg.protect, cfg.hw_timeout,
            cfg.shutdown_seconds, cfg.oom_mem_pct, cfg.oom_swap_pct) == (
        False, "contain", 40.0, 120, 75, 600, 3, 3, ("caddy", "technitium"), 60, 300, (5, 2), (10, 5))
    assert rc.parse_config({}) == cfg, "an absent knob and an empty one must agree"


def test_check_config_exits_2_on_a_bad_knob_and_normalises_a_good_one_tc1062(tmp_path, capsys):
    bad = tmp_path / "bad.env"
    bad.write_text("HUB_WD_TIERS=most\n")
    assert cli.main(["check-config", str(bad)]) == 2
    good = tmp_path / "good.env"
    good.write_text('HUB_WATCHDOG_ENABLED="true"\nHUB_WD_PROTECT=caddy, technitium\n')
    capsys.readouterr()
    assert cli.main(["check-config", str(good)]) == 0
    out = capsys.readouterr().out
    assert "HUB_WATCHDOG_ENABLED=true\n" in out and "HUB_WD_PROTECT=caddy,technitium\n" in out
    assert rc.parse_config(rc.read_env_file(str(good))) == rc.parse_config(
        dict(line.split("=", 1) for line in out.splitlines() if "=" in line and not line.startswith("#")))


def _fake_run(monkeypatch, answers):
    calls = []

    def run(argv, timeout=60):
        calls.append(argv)
        for prefix, answer in answers:
            if argv[:len(prefix)] == prefix:
                return answer
        return 0, "", ""

    monkeypatch.setattr(cli, "_run", run)
    return calls


def _supervisor_answering(monkeypatch, tree, ack_ok=True, stops=True, ack=True):
    """Model the running supervisor's side of the disarm protocol.

    On `systemctl kill -s SIGUSR1` it reads the authorisation, and - like the
    real one - stops the device (sysfs inactive) only when `stops`, then writes
    the acknowledgement it would write (`ack_ok`), unless `ack` is False (it
    never answers)."""
    state = tree.watchdog(state="active")
    monkeypatch.setenv("RECOVERY_ACK_WAIT", "0.5")
    calls = []

    def run(argv, timeout=60):
        calls.append(argv)
        if argv[:2] == ["systemctl", "is-active"]:
            return 0, "active", ""
        if argv[:2] == ["systemctl", "show"]:
            return 0, "f" * 32, ""
        if argv[:3] == ["systemctl", "kill", "-s"]:
            boot, invocation = open(tree.paths.disarm_file).read().split()
            if stops:
                tree.write(state, "inactive\n")
            if ack:
                tree.write(tree.paths.disarm_ack_file, json.dumps(
                    {"boot_id": boot, "invocation_id": invocation, "ok": ack_ok, "detail": "x"}))
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(cli, "_run", run)
    return calls, state


STOP = ["systemctl", "stop", "homehub-recoveryd.service"]
SIGUSR1 = ["systemctl", "kill", "-s", "SIGUSR1", "--kill-whom=main", "homehub-recoveryd.service"]


def test_disarm_signals_waits_for_a_verified_ack_then_stops_tc1063(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    calls, state = _supervisor_answering(monkeypatch, tree)
    assert cli.main(["--root", str(tmp_path), "disarm"]) == 0
    assert calls.index(SIGUSR1) < calls.index(STOP)
    assert open(state).read().strip() == "inactive"


@pytest.mark.parametrize("ack_ok,stops,ack", [(False, False, True), (True, False, True), (True, True, False)])
def test_disarm_that_is_not_verified_never_stops_the_supervisor_tc1080(tmp_path, monkeypatch, ack_ok, stops, ack):
    """Review C2: a nack, an ack contradicted by sysfs, or no ack at all."""
    tree = Tree(tmp_path)
    calls, _ = _supervisor_answering(monkeypatch, tree, ack_ok=ack_ok, stops=stops, ack=ack)
    assert cli.main(["--root", str(tmp_path), "disarm"]) == 1
    assert SIGUSR1 in calls and STOP not in calls, "a supervisor was stopped with its device unverified"


def test_disarm_refuses_to_stop_without_an_invocation_id_tc1063(tmp_path, monkeypatch):
    Tree(tmp_path)
    calls = _fake_run(monkeypatch, [(["systemctl", "is-active"], (0, "active", "")),
                                    (["systemctl", "show"], (1, "", "boom"))])
    assert cli.main(["--root", str(tmp_path), "disarm"]) == 1
    assert STOP not in calls and SIGUSR1 not in calls


def test_disarm_with_nothing_running_and_nothing_armed_does_nothing_tc1080(tmp_path, monkeypatch):
    Tree(tmp_path)
    calls = _fake_run(monkeypatch, [(["systemctl", "is-active"], (3, "failed", ""))])
    opened = []
    monkeypatch.setattr(cli, "_open_device", lambda p: opened.append(p))
    assert cli.main(["--root", str(tmp_path), "disarm"]) == 0
    assert calls == [["systemctl", "is-active", "homehub-recoveryd.service"]] and not opened


@pytest.mark.parametrize("stops,nowayout,expected", [(True, "0", 0), (False, "0", 1), (True, "1", 1)])
def test_an_active_device_with_no_supervisor_is_disarmed_and_verified_tc1080(
        tmp_path, monkeypatch, stops, nowayout, expected):
    """Review C2: a failed or killed supervisor left the timer running."""
    tree = Tree(tmp_path)
    state = tree.watchdog(state="active")
    tree.write(os.path.join(os.path.dirname(state), "nowayout"), nowayout + "\n")
    _fake_run(monkeypatch, [(["systemctl", "is-active"], (3, "failed", ""))])
    sink = tmp_path / "devnode"
    opened = []

    def fake_open(path):
        opened.append(path)
        fd = os.open(str(sink), os.O_WRONLY | os.O_CREAT)
        if stops:
            tree.write(state, "inactive\n")  # the kernel stops it on 'V' + close
        return fd

    monkeypatch.setattr(cli, "_open_device", fake_open)
    assert cli.main(["--root", str(tmp_path), "disarm"]) == expected
    if nowayout == "1":
        assert not opened, "tried to open a nowayout device"
    else:
        assert opened and sink.read_bytes() == b"V"


def test_release_restores_the_policy_starts_it_and_tells_the_supervisor_tc1063(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    tree.write(tree.paths.quarantine_file, json.dumps(
        {"quarantined": {"immich-machine-learning": {"container": cid(1), "restart": "unless-stopped"}}}))
    calls = _fake_run(monkeypatch, [])
    assert cli.main(["--root", str(tmp_path), "release", "immich-machine-learning"]) == 0
    assert ["docker", "update", "--restart=unless-stopped", cid(1)] in calls
    assert ["docker", "start", cid(1)] in calls
    assert json.load(open(tree.paths.quarantine_file)) == {"quarantined": {}}
    assert open(tree.paths.released_file).read() == "immich-machine-learning\n"


def _compose_ps(service, ids):
    return (["docker", "ps", "-a", "-q", "--no-trunc",
             "--filter", "label=com.docker.compose.service={}".format(service)],
            (0, "\n".join(ids), ""))


def test_a_boot_keeps_a_quarantined_service_stopped_after_compose_up_tc1084(tmp_path, monkeypatch):
    """Review 2026-10-10: firstboot's `docker compose up -d` started it again.

    The service was recreated under a new id by that `up` (a new image), so the
    stop must find it by its compose label, not by the id the record holds.
    A service with no current container falls back to the recorded id."""
    tree = Tree(tmp_path)
    tree.write(tree.paths.quarantine_file, json.dumps({"quarantined": {
        "immich-machine-learning": {"container": cid(1), "restart": "unless-stopped"},
        "old-hog": {"container": cid(3), "restart": "always"}}}))
    calls = _fake_run(monkeypatch, [_compose_ps("immich-machine-learning", [cid(2)])])
    assert cli.main(["--root", str(tmp_path), "enforce-quarantine"]) == 0
    assert ["docker", "update", "--restart=no", cid(2)] in calls
    assert ["docker", "stop", "-t", "10", cid(2)] in calls
    assert ["docker", "stop", "-t", "10", cid(3)] in calls
    assert ["docker", "stop", "-t", "10", cid(1)] not in calls, "stopped a stale id, not the running one"
    # Nothing is released by enforcing: the record still names both.
    assert set(json.load(open(tree.paths.quarantine_file))["quarantined"]) == {"immich-machine-learning",
                                                                                "old-hog"}


def test_enforcing_quarantine_names_a_stop_that_failed_tc1084(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    tree.write(tree.paths.quarantine_file, json.dumps(
        {"quarantined": {"hog": {"container": cid(1), "restart": "unless-stopped"}}}))
    _fake_run(monkeypatch, [(["docker", "stop"], (1, "", "daemon not answering"))])
    assert cli.main(["--root", str(tmp_path), "enforce-quarantine"]) == 1


def test_a_released_service_is_not_stopped_again_at_the_next_boot_tc1084(tmp_path, monkeypatch):
    """The Owner's way out: release removes the entry, so enforcement leaves it."""
    tree = Tree(tmp_path)
    tree.write(tree.paths.quarantine_file, json.dumps(
        {"quarantined": {"hog": {"container": cid(1), "restart": "unless-stopped"}}}))
    _fake_run(monkeypatch, [_compose_ps("hog", [cid(2)])])
    assert cli.main(["--root", str(tmp_path), "release", "hog"]) == 0
    calls = _fake_run(monkeypatch, [_compose_ps("hog", [cid(2)])])
    assert cli.main(["--root", str(tmp_path), "enforce-quarantine"]) == 0
    assert not [c for c in calls if c[:2] in (["docker", "stop"], ["docker", "update"])]


def test_release_starts_the_recreated_container_not_the_recorded_id_tc1084(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    tree.write(tree.paths.quarantine_file, json.dumps(
        {"quarantined": {"hog": {"container": cid(1), "restart": "unless-stopped"}}}))
    calls = _fake_run(monkeypatch, [_compose_ps("hog", [cid(2)])])
    assert cli.main(["--root", str(tmp_path), "release", "hog"]) == 0
    assert ["docker", "update", "--restart=unless-stopped", cid(2)] in calls
    assert ["docker", "start", cid(2)] in calls


def test_firstboot_enforces_quarantine_right_after_the_bulk_compose_up_tc1084():
    """STATIC ORDER: the enforcement is the next command after `docker compose up -d`
    and comes before anything else starts or judges containers."""
    lines = [ln for ln in (REPO / "stack" / "autoinstall" / "firstboot.sh").read_text(
        encoding="utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    up = lines.index("docker compose up -d")
    assert "quarantine.json" in lines[up + 1] and lines[up + 1].startswith("if ")
    assert 'homehub-recovery" enforce-quarantine' in lines[up + 2]
    assert "|| log" in lines[up + 3], "a failed enforcement must be logged, never fail the boot"


def test_reset_brake_records_a_reset_the_brake_honours_tc1063(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    tree.write(tree.paths.boots_log, "\n".join(json.dumps({"t": 1.0 + i, "class": "recovery"})
                                               for i in range(3)) + "\n")
    _fake_run(monkeypatch, [])
    assert cli.main(["--root", str(tmp_path), "reset-brake"]) == 0
    entries = rc.parse_boots_log(open(tree.paths.boots_log).read())
    assert entries[-1]["event"] == "brake-reset"
    assert not rc.brake_state(entries, entries[-1]["t"] + 1, 3, False).braked


@pytest.mark.parametrize("stops", [True, False])
def test_reset_brake_replaces_a_running_supervisor_only_through_the_disarm_tc1083(tmp_path, monkeypatch, stops):
    """Review C1: a bare restart would hand off a timer the replacement may not take."""
    tree = Tree(tmp_path)
    calls, state = _supervisor_answering(monkeypatch, tree, ack_ok=stops, stops=stops)
    rc_ = cli.main(["--root", str(tmp_path), "reset-brake"])
    assert not [c for c in calls if c[:2] in (["systemctl", "try-restart"], ["systemctl", "restart"])
                and c[-1] == "homehub-recoveryd.service"]
    if stops:
        assert rc_ == 0 and calls.index(SIGUSR1) < calls.index(STOP) < calls.index(
            ["systemctl", "start", "homehub-recoveryd.service"])
    else:
        assert rc_ == 1 and STOP not in calls
    assert rc.read_boots_log_strict(tree.paths.boots_log)[-1]["event"] == "brake-reset"
