"""C1's pressure sample, the knob validation, and the homehub-recovery CLI.

The sampler runs against a fake procfs/cgroupfs/Docker tree; the CLI's
systemctl and docker calls are substituted at its single `_run` seam so a
test can see exactly what disarm and release ask the system to do.

Implements: TC-1061..TC-1063 (see docs/test/test-cases.csv)
"""

from __future__ import annotations

import json
import os

import pytest

from recovery_harness import BOOT_ID, GiB, Tree, cid, load_script, rc

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


def test_disarm_writes_this_boot_and_invocation_then_stops_tc1063(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    calls = _fake_run(monkeypatch, [(["systemctl", "is-active"], (0, "active", "")),
                                    (["systemctl", "show"], (0, "f" * 32, ""))])
    written = {}
    real = rc.atomic_write

    def capture(path, data, **kw):
        written[path] = data
        real(path, data, **kw)

    monkeypatch.setattr(rc, "atomic_write", capture)
    assert cli.main(["--root", str(tmp_path), "disarm"]) == 0
    assert written[tree.paths.disarm_file].decode().split() == [BOOT_ID, "f" * 32]
    assert calls[-1] == ["systemctl", "stop", "homehub-recoveryd.service"]


def test_disarm_refuses_to_stop_without_an_invocation_id_tc1063(tmp_path, monkeypatch):
    Tree(tmp_path)
    calls = _fake_run(monkeypatch, [(["systemctl", "is-active"], (0, "active", "")),
                                    (["systemctl", "show"], (1, "", "boom"))])
    assert cli.main(["--root", str(tmp_path), "disarm"]) == 1
    assert ["systemctl", "stop", "homehub-recoveryd.service"] not in calls


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


def test_reset_brake_records_a_reset_the_brake_honours_tc1063(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    tree.write(tree.paths.boots_log, "\n".join(json.dumps({"t": 1.0 + i, "class": "recovery"})
                                               for i in range(3)) + "\n")
    _fake_run(monkeypatch, [])
    assert cli.main(["--root", str(tmp_path), "reset-brake"]) == 0
    entries = rc.parse_boots_log(open(tree.paths.boots_log).read())
    assert entries[-1]["event"] == "brake-reset"
    assert not rc.brake_state(entries, entries[-1]["t"] + 1, 3, False).braked
