"""Tests for the hub's continuous monitoring (SN-048): the shared alert helper, the
episode machine, the telemetry record and its alarms, the low-disk alarm, the
watched jobs, and the liveness probe with its evidence bundle.

Verifies: SR-085, SR-086, SR-087, SR-088, SR-089
          LLR-1147 .. LLR-1166, LLR-1168, LLR-1290 .. LLR-1292
Cases:    TC-1275 .. TC-1305, TC-1313, TC-1460 .. TC-1465 (TC-1466 is in
          stack/monitoring/tests/monitoring.test.sh)

Everything here is hermetic: fake sysfs/procfs trees in tmp_path, a stub alert
helper or a stub curl, injected statfs/runner/writer seams. The Linux-only
halves (real sysfs symlinks for the NVMe test, a real ENOSPC from /dev/full, a
blocking docker killed by its timeout) are in
stack/monitoring/tests/monitoring.test.sh, run by run-hermetic-tests.sh.
"""

import errno
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MON = ROOT / "stack" / "monitoring"
FIRSTBOOT = ROOT / "stack" / "autoinstall" / "firstboot.sh"
ENVEX = ROOT / "stack" / ".env.example"
HELPER = MON / "homehub-alert.sh"

sys.path.insert(0, str(MON))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(MON / filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mc = _load("monitor_common", "monitor_common.py")
ev = _load("homehub_evidence", "homehub_evidence.py")
tel = _load("homehub_telemetry", "homehub-telemetry.py")
disk = _load("homehub_disk_check", "homehub-disk-check.py")
watch = _load("homehub_watch", "homehub-watch.py")
live = _load("homehub_liveness", "homehub-liveness.py")

_bash = shutil.which("bash")
needs_bash = pytest.mark.skipif(_bash is None, reason="needs bash")


def _w(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def _live(src):
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))  # noqa: E741


# ── the alert helper (SR-085, LLR-1147) ──────────────────────────────────────


class _HelperRig:
    """homehub-alert.sh against a stub curl that records its argv."""

    def __init__(self, tmp_path):
        self.d = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.env = _w(tmp_path / "env", "LAN_IP=192.0.2.10\nNTFY_PORT=8081\n")
        self.state = tmp_path / "state"
        self.calls = tmp_path / "curl.log"
        stub = _w(self.bin / "curl", '#!/usr/bin/env bash\necho "curl $*" | tr "\\n" " " >>"$CALLS"; echo >>"$CALLS"\n'
                                     '[ -e "$RIG/curl.fail" ] && exit 7\nexit 0\n')  # fmt: skip
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    def run(self, *extra, body=None):
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], RIG=str(self.d),
                   CALLS=str(self.calls))  # fmt: skip
        argv = [
            _bash,
            str(HELPER),
            "--env-file",
            str(self.env),
            "--state-dir",
            str(self.state),
        ] + list(extra)
        if self.calls.exists():
            self.calls.unlink()
        r = subprocess.run(
            argv, env=env, input=body, capture_output=True, text=True, timeout=60
        )
        calls = (
            self.calls.read_text(encoding="utf-8").splitlines()
            if self.calls.exists()
            else []
        )
        return r, calls


@pytest.fixture
def hrig(tmp_path):
    return _HelperRig(tmp_path)


BASIC = ("--key", "disk-low-root", "--priority", "high", "--title", "HomeHub: / is low")


@needs_bash
def test_helper_delivers_and_journals_tc1275(hrig):
    r, calls = hrig.run(*BASIC, "--body", "free 1 GB")
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(calls) == 1 and "http://192.0.2.10:8081/homehub-alerts" in calls[0]
    assert (
        "Priority: high" in calls[0]
        and "Title: HomeHub: / is low" in calls[0]
        and "free 1 GB" in calls[0]
    )
    assert "alert delivered" in r.stdout and "[disk-low-root]" in r.stdout
    assert (hrig.state / "alerted-disk-low-root").exists()


@needs_bash
def test_helper_rate_limits_per_key_tc1276(hrig):
    hrig.run(*BASIC)
    r, calls = hrig.run(*BASIC)
    assert r.returncode == 3 and not calls and "alert suppressed" in r.stdout
    other = (
        "--key",
        "disk-low-boot",
        "--priority",
        "high",
        "--title",
        "HomeHub: /boot is low",
    )
    r, calls = hrig.run(*other)
    assert r.returncode == 0 and len(calls) == 1, (
        "a different key must not be suppressed"
    )
    r, calls = hrig.run(*BASIC, "--repeat-sec", "0")
    assert r.returncode == 0 and len(calls) == 1, "the window is the caller's to set"


@needs_bash
def test_helper_ntfy_down_is_journaled_not_stamped_and_retried_tc1277(hrig):
    (hrig.d / "curl.fail").write_text("", encoding="utf-8")
    r, calls = hrig.run(*BASIC)
    assert r.returncode == 4 and len(calls) == 1
    assert (
        "ALERT NOT DELIVERED" in r.stdout
        and "this journal line is the record" in r.stdout
    )
    assert not (hrig.state / "alerted-disk-low-root").exists(), (
        "an undelivered alert must not stamp"
    )
    (hrig.d / "curl.fail").unlink()
    r, calls = hrig.run(*BASIC)
    assert r.returncode == 0 and len(calls) == 1, "the next attempt must go out"


@needs_bash
def test_helper_without_config_says_not_sent_tc1277(hrig):
    _w(hrig.env, "NTFY_PORT=8081\n")
    r, calls = hrig.run(*BASIC)
    assert r.returncode == 4 and not calls and "ALERT NOT SENT" in r.stdout


@needs_bash
def test_helper_ignores_a_stamp_from_the_future_tc1278(hrig):
    """A clock that moved backwards must not suppress the key until it catches up."""
    _w(hrig.state / "alerted-disk-low-root", "%d\n" % (2 * 10**9))
    r, calls = hrig.run(*BASIC)
    assert r.returncode == 0 and len(calls) == 1 and "in the future" in r.stdout


@needs_bash
def test_helper_refuses_bad_keys_and_strips_header_newlines_tc1278(hrig):
    r, _ = hrig.run("--key", "../etc/passwd", "--priority", "high", "--title", "x")
    assert r.returncode == 2
    r, _ = hrig.run("--key", "k", "--priority", "loud", "--title", "x")
    assert r.returncode == 2
    r, calls = hrig.run(
        "--key", "k", "--priority", "high", "--title", "a\r\nX-Injected: 1"
    )
    assert r.returncode == 0 and "Title: a  X-Injected: 1" in calls[0]


@needs_bash
def test_helper_reads_the_body_from_stdin_tc1275(hrig):
    r, calls = hrig.run(*BASIC, body="from stdin")
    assert r.returncode == 0 and "from stdin" in calls[0]


def test_helper_bounds_curl_and_is_lf_tc1278():
    live = _live(HELPER.read_text(encoding="utf-8"))
    assert re.search(r"timeout[^\n]*curl ", live)
    assert "--max-time" in live and "--connect-timeout" in live
    assert b"\r" not in HELPER.read_bytes() and HELPER.read_bytes().startswith(
        b"#!/usr/bin/env bash\n"
    )


@needs_bash
def test_send_alert_maps_the_helpers_exit_codes_tc1275(tmp_path, monkeypatch):
    log = tmp_path / "helper.log"
    stub = _w(tmp_path / "helper.sh", 'echo "$@" >>"%s"; echo "stub line"; exit "$(cat %s)"\n'
              % (log.as_posix(), (tmp_path / "rc").as_posix()))  # fmt: skip
    monkeypatch.setenv("HOMEHUB_ALERT_BASH", _bash or "bash")
    for rc, want in (
        ("0", mc.ALERT_DELIVERED),
        ("3", mc.ALERT_SUPPRESSED),
        ("4", mc.ALERT_FAILED),
    ):
        _w(tmp_path / "rc", rc)
        got = mc.send_alert(
            "k1", "high", "t", "b", "/env", str(tmp_path / "st"), helper=str(stub)
        )
        assert got == want
    assert "--state-dir %s" % os.path.join(
        str(tmp_path / "st"), "alerts"
    ) in log.read_text(encoding="utf-8")


# ── the episode machine (SR-085, LLR-1148) ───────────────────────────────────


class _Sender:
    def __init__(self, result=mc.ALERT_DELIVERED):
        self.result, self.sent = result, []

    def __call__(self, key, prio, title, body):
        self.sent.append((key, prio, title))
        return self.result


FIRE, CLEAR = ("high", "bad", "body"), ("good", "body")


def test_episode_fires_once_and_clears_once_tc1279():
    eps, s = {}, _Sender()
    for bad in (True, True, True, False, False, True):
        mc.run_episode(eps, "k", bad, FIRE, CLEAR, s)
    assert [k for k, _, _ in s.sent] == ["k", "k.clear", "k"]
    assert s.sent[1][1] == "default"


def test_episode_retries_an_undelivered_fire_and_clear_tc1279():
    eps, s = {}, _Sender(mc.ALERT_FAILED)
    mc.run_episode(eps, "k", True, FIRE, CLEAR, s)
    mc.run_episode(eps, "k", True, FIRE, CLEAR, s)
    s.result = mc.ALERT_DELIVERED
    mc.run_episode(eps, "k", True, FIRE, CLEAR, s)
    mc.run_episode(eps, "k", True, FIRE, CLEAR, s)
    assert [k for k, _, _ in s.sent] == ["k", "k", "k"], (
        "retried until delivered, then quiet"
    )
    s.result = mc.ALERT_FAILED
    mc.run_episode(eps, "k", False, FIRE, CLEAR, s)
    s.result = mc.ALERT_DELIVERED
    mc.run_episode(eps, "k", False, FIRE, CLEAR, s)
    mc.run_episode(eps, "k", False, FIRE, CLEAR, s)
    assert [k for k, _, _ in s.sent][-2:] == ["k.clear", "k.clear"]


def test_episode_treats_a_suppressed_fire_as_not_sent_tc1279():
    eps, s = {}, _Sender(mc.ALERT_SUPPRESSED)
    mc.run_episode(eps, "k", True, FIRE, CLEAR, s)
    assert eps["k"]["fired"] is False
    mc.run_episode(eps, "k", False, FIRE, CLEAR, s)
    assert len(s.sent) == 1, "no clear for an episode nobody was told about"


def test_read_env_text_matches_the_shell_reader_tc1280():
    text = "A=1\nA=2\r\nB=\"two words\" # note\nC='q'\n# D=no\nE=x#y\nbad line\n"
    env = mc.read_env_text(text)
    assert env == {"A": "2", "B": "two words", "C": "q", "E": "x#y"}


def test_override_file_lays_over_the_env_tc1280(tmp_path):
    _w(tmp_path / "env", "K=1\nL=2\n")
    _w(tmp_path / "ov", "K=9\n")
    assert mc.read_knobs(str(tmp_path / "env"), str(tmp_path / "ov")) == {
        "K": "9",
        "L": "2",
    }
    assert mc.read_knobs(str(tmp_path / "env"), str(tmp_path / "absent")) == {
        "K": "1",
        "L": "2",
    }


def test_save_json_atomic_survives_a_failed_write_tc1280(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    assert mc.save_json_atomic(str(path), {"a": 1})

    def boom(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(mc.os, "replace", boom)
    assert mc.save_json_atomic(str(path), {"a": 2}) is False
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"], (
        "the temp file must be cleaned up"
    )
    _w(path, "{torn")
    assert mc.load_json(str(path), {}) == {}


def test_parse_mountinfo_decodes_escapes_tc1283():
    text = ("29 1 253:0 / / rw,relatime shared:1 - ext4 /dev/mapper/vg-root rw\n"
            "40 29 8:17 / /srv/my\\040disk rw - ext4 /dev/sdb1 rw\n")  # fmt: skip
    m = mc.parse_mountinfo(text)
    assert m[0] == {
        "mount": "/",
        "majmin": "253:0",
        "fstype": "ext4",
        "source": "/dev/mapper/vg-root",
    }
    assert m[1]["mount"] == "/srv/my disk"


# ── the bounded runner (LLR-1149) ────────────────────────────────────────────


def test_run_bounded_kills_at_the_timeout_tc1300():
    import time

    t0 = time.monotonic()
    r = mc.run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], 1.0, 1024)
    assert r.timed_out and not r.ok and time.monotonic() - t0 < 10


def test_run_bounded_stops_a_flood_at_the_cap_tc1300():
    r = mc.run_bounded(
        [sys.executable, "-c", "import sys\nwhile True: sys.stdout.write('x' * 4096)"],
        20,
        10000,
    )
    assert r.truncated and len(r.output) == 10000


def test_run_bounded_reports_a_missing_command_tc1300():
    r = mc.run_bounded(["/nonexistent/definitely-not-here"], 5, 100)
    assert r.error and not r.ok


# ── telemetry (SR-086, LLR-1150..LLR-1153) ───────────────────────────────────


def _fake_machine(
    base,
    cpu_c=50.0,
    nvme_c=40.0,
    mhz=(2000, 2000),
    throttle=(0, 0),
    stat_line=None,
    boot="b1",
):
    sysr, procr = base / "sys", base / "proc"
    _w(sysr / "class/thermal/thermal_zone0/type", "x86_pkg_temp\n")
    if cpu_c is None:
        (sysr / "class/thermal/thermal_zone0/temp").unlink() if (
            sysr / "class/thermal/thermal_zone0/temp"
        ).exists() else None
    else:
        _w(sysr / "class/thermal/thermal_zone0/temp", "%d\n" % int(cpu_c * 1000))
    _w(sysr / "class/thermal/thermal_zone1/type", "iwlwifi_1\n")
    _w(sysr / "class/hwmon/hwmon1/name", "nvme\n")
    _w(sysr / "class/hwmon/hwmon1/temp1_label", "Composite\n")
    _w(sysr / "class/hwmon/hwmon1/temp1_input", "%d\n" % int(nvme_c * 1000))
    for i, m in enumerate(mhz):
        _w(
            sysr / ("devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq" % i),
            "%d\n" % (m * 1000),
        )
        _w(
            sysr
            / ("devices/system/cpu/cpu%d/thermal_throttle/core_throttle_count" % i),
            "%d\n" % throttle[0],
        )
        _w(
            sysr
            / ("devices/system/cpu/cpu%d/thermal_throttle/package_throttle_count" % i),
            "%d\n" % throttle[1],
        )
    _w(procr / "stat", stat_line or "cpu  100 0 100 800 0 0 0 0 0 0\n")
    _w(procr / "loadavg", "0.50 0.40 0.30 1/200 999\n")
    _w(procr / "pressure/cpu", "some avg10=1.00 avg60=0.50 avg300=0.1 total=1\n")
    _w(procr / "self/mountinfo", "29 1 253:0 / / rw - ext4 /dev/mapper/vg-root rw\n")
    _w(procr / "sys/kernel/random/boot_id", boot + "\n")
    return sysr, procr


def _record(base, **kw):
    sysr, procr = _fake_machine(base, **kw)
    rec, _ = tel.collect(
        str(sysr), str(procr), None, lambda mm: False, None, 1_800_000_000.0
    )
    return rec


CFG = tel.config_from_env({})


def test_collect_reads_every_sensor_and_marks_missing_ones_tc1281(tmp_path):
    rec = _record(tmp_path, cpu_c=55.5, nvme_c=41.0, mhz=(2380, 1200))
    assert tel.cpu_temp(rec) == 55.5 and tel.nvme_temp(rec) == 41.0
    assert rec["cpu_mhz"] == [2380, 1200] and rec["throttle"] == {
        "core_count": 0,
        "package_count": 0,
    }
    assert {z["type"]: z["temp_c"] for z in rec["thermal_zones"]}["iwlwifi_1"] is None
    assert rec["load"] == [0.5, 0.4, 0.3] and rec["psi"]["cpu"]["some_avg10"] == 1.0
    assert rec["fs"] == [], "a filesystem not on the NVMe is never measured"


def test_collect_never_statfs_a_filesystem_off_the_nvme_tc1281(tmp_path):
    sysr, procr = _fake_machine(tmp_path)
    _w(procr / "self/mountinfo", "29 1 253:0 / / rw - ext4 /dev/mapper/vg-root rw\n"
                                 "40 29 8:17 / /mnt/backup-drive rw - ext4 /dev/sdb1 rw\n")  # fmt: skip
    asked = []

    def statfs(path):
        asked.append(path)
        return 100, 40

    rec, _ = tel.collect(
        str(sysr), str(procr), None, lambda mm: mm == "253:0", statfs, 1.0
    )
    assert asked == ["/"] and rec["fs"][0]["used_pct"] == 60.0


def _run_rules(tmp_path, readings, cfg=CFG, step=30.0, boot="b1"):
    state, verdicts = {}, None
    history = []
    for i, reading in enumerate(readings):
        kw = dict(reading)
        busy = kw.pop("busy", None)
        rec = _record(tmp_path / str(i), boot=kw.pop("boot", boot), **kw)
        rec["cpu_busy_pct"] = busy
        state, verdicts, _ = tel.evaluate(state, rec, cfg, 1000.0 + i * step)
        history.append(dict(verdicts))
    return history


def test_cpu_temperature_rule_fires_after_n_samples_and_clears_with_hysteresis_tc1282(
    tmp_path,
):
    hot, warm, cool = {"cpu_c": 95}, {"cpu_c": 87}, {"cpu_c": 80}
    h = _run_rules(
        tmp_path, [hot, hot, hot, hot, warm, warm, warm, warm, cool, cool, cool, cool]
    )
    assert [v["cpu-temp"] for v in h[:4]] == [False, False, False, True]
    assert all(v["cpu-temp"] for v in h[4:8]), "inside the hysteresis band it stays bad"
    assert [v["cpu-temp"] for v in h[8:]] == [True, True, True, False]


def test_nvme_temperature_rule_fires_and_clears_tc1282(tmp_path):
    h = _run_rules(tmp_path, [{"nvme_c": 80}] * 4 + [{"nvme_c": 60}] * 4)
    assert h[3]["nvme-temp"] and not h[7]["nvme-temp"] and not h[2]["nvme-temp"]


def test_a_missing_sensor_neither_fires_nor_clears_tc1282(tmp_path):
    h = _run_rules(tmp_path, [{"cpu_c": 95}] * 4 + [{"cpu_c": None}] * 6)
    assert all(v["cpu-temp"] for v in h[3:])


def test_throttle_rule_fires_on_an_increase_and_clears_when_quiet_tc1282(tmp_path):
    cfg = dict(CFG, throttle_clear=3)
    seq = [{"throttle": (5, 2)}, {"throttle": (5, 2)}, {"throttle": (9, 2)}] + [
        {"throttle": (9, 2)}
    ] * 3
    h = _run_rules(tmp_path, seq, cfg)
    assert [v["throttle"] for v in h] == [False, False, True, True, True, False]


def test_a_reset_throttle_counter_is_a_baseline_not_an_episode_tc1282(tmp_path):
    h = _run_rules(
        tmp_path, [{"throttle": (50, 9)}, {"throttle": (0, 0)}, {"throttle": (0, 0)}]
    )
    assert not any(v["throttle"] for v in h)


def test_frequency_floor_needs_load_and_n_samples_tc1282(tmp_path):
    cfg = dict(CFG, freq_samples=3)
    slow_busy = {"mhz": (600, 700), "busy": 90.0}
    slow_idle = {"mhz": (600, 700), "busy": 5.0}
    h = _run_rules(tmp_path, [slow_idle] * 4 + [slow_busy] * 3 + [slow_idle] * 3, cfg)
    assert not any(v["freq-floor"] for v in h[:6])
    assert h[6]["freq-floor"] and h[8]["freq-floor"] and not h[9]["freq-floor"]


def test_a_reboot_or_a_gap_restarts_the_consecutive_count_tc1283(tmp_path):
    hot = {"cpu_c": 95}
    h = _run_rules(tmp_path, [hot, hot, hot, dict(hot, boot="b2"), hot, hot])
    assert not h[3]["cpu-temp"] and not h[5]["cpu-temp"], "a reboot restarts the count"
    h = _run_rules(tmp_path / "gap", [hot, hot, hot, hot], step=600.0)
    assert not any(v["cpu-temp"] for v in h), (
        "samples ten minutes apart are not consecutive"
    )


def test_plan_rotation_keeps_seven_files_and_the_cap_never_the_active_tc1284():
    files = [("telemetry-202610%02d.jsonl" % d, 4) for d in range(1, 13)] + [
        ("other.txt", 999)
    ]
    delete = tel.plan_rotation(files, "telemetry-20261012.jsonl", 7, 10**9)
    assert delete == ["telemetry-202610%02d.jsonl" % d for d in range(1, 6)]
    delete = tel.plan_rotation(files, "telemetry-20261012.jsonl", 7, 9)
    assert "telemetry-20261012.jsonl" not in delete and len(delete) == 10
    # The clock jumped back: the active file sorts oldest and is still kept.
    delete = tel.plan_rotation(files, "telemetry-20261001.jsonl", 7, 10**9)
    assert "telemetry-20261001.jsonl" not in delete


def test_append_rotates_and_refuses_past_the_cap_tc1284(tmp_path):
    for d in range(1, 10):
        _w(tmp_path / ("telemetry-202609%02d.jsonl" % d), "{}\n")
    ok, why = tel.append_record(str(tmp_path), {"epoch": 1792652484.0, "v": 1})
    assert ok and why == "recorded"
    names = sorted(p.name for p in tmp_path.iterdir())
    assert len(names) == 7 and names[-1] == "telemetry-20261022.jsonl"
    ok, why = tel.append_record(
        str(tmp_path), {"epoch": 1792652484.0, "pad": "x" * 200}, cap_bytes=100
    )
    assert not ok and "cap" in why


def test_export_skips_torn_lines_and_filters_by_time_tc1284(tmp_path):
    _w(
        tmp_path / "telemetry-20261022.jsonl",
        '{"epoch": 10, "a": 1}\n{"epo\n{"epoch": 20}\nnull\n',
    )
    assert [r["epoch"] for r in tel.iter_records(str(tmp_path))] == [10, 20]
    assert [r["epoch"] for r in tel.iter_records(str(tmp_path), since=15)] == [20]
    assert "2 records" in tel.summarise(list(tel.iter_records(str(tmp_path))))[0]


def test_sample_end_to_end_records_and_alerts_once_tc1285(tmp_path, monkeypatch):
    sysr, procr = _fake_machine(tmp_path / "m", cpu_c=99)
    sent = []
    monkeypatch.setattr(
        tel.mc,
        "send_alert",
        lambda key, *a, **k: sent.append(key) or mc.ALERT_DELIVERED,
    )
    args = ["--data-dir", str(tmp_path / "data"), "--state-dir", str(tmp_path / "st"), "--env-file",
            str(tmp_path / "env"), "--sys-root", str(sysr), "--proc-root", str(procr), "sample"]  # fmt: skip
    _w(tmp_path / "env", "HOMEHUB_THERMAL_SAMPLES=2\n")
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))
    for _ in range(4):
        assert tel.main(args) == 0
    lines = (
        (tmp_path / "data")
        .glob("telemetry-*.jsonl")
        .__next__()
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(lines) == 4 and json.loads(lines[-1])["alarms"] == ["cpu-temp"]
    assert sent == ["telemetry-cpu-temp"]


def test_sample_fails_loudly_when_it_cannot_write_tc1285(tmp_path, monkeypatch):
    sysr, procr = _fake_machine(tmp_path / "m")
    monkeypatch.setattr(
        tel, "append_record", lambda *a, **k: (False, "No space left on device")
    )
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))
    rc = tel.main(["--data-dir", str(tmp_path / "d"), "--state-dir", str(tmp_path / "s"), "--env-file",
                   str(tmp_path / "env"), "--sys-root", str(sysr), "--proc-root", str(procr), "sample"])  # fmt: skip
    assert rc == 1


# ── disk (SR-087, LLR-1154, LLR-1155) ────────────────────────────────────────

MOUNTS = mc.parse_mountinfo(
    "29 1 253:0 / / rw - ext4 /dev/mapper/vg-root rw\n"
    "30 29 253:1 / /var/lib/docker rw - ext4 /dev/mapper/vg-docker rw\n"
    "31 29 259:2 / /boot rw - ext4 /dev/nvme0n1p2 rw\n"
    "40 29 8:17 / /mnt/backup-drive rw - ext4 /dev/sdb1 rw\n"
)


def _nvme(mm):
    return mm != "8:17"


def test_parse_mount_spec_and_threshold_tc1286():
    assert disk.parse_mount_spec("/") == ("/", None)
    assert disk.parse_mount_spec("/srv/scratch/=99%") == ("/srv/scratch", ("pct", 99.0))
    assert disk.parse_mount_spec("/boot=1G") == ("/boot", ("bytes", 1e9))
    for bad in ("relative", "/x=5", "/x=0%", "/a/../b", "/x=100%"):
        assert disk.parse_mount_spec(bad) is None, bad
    assert disk.threshold_bytes(100 * 10**9, None) == 5 * 10**9
    assert disk.threshold_bytes(2 * 10**9, None) == 2 * 10**8


def test_disk_never_statfs_a_usb_drive_and_reports_absent_tc1287():
    asked = []

    def statfs(path):
        asked.append(path)
        return 100 * 10**9, 50 * 10**9

    specs = [("/", None), ("/mnt/backup-drive", None), ("/srv/scratch", None)]
    res = disk.evaluate(specs, MOUNTS, _nvme, statfs)
    assert asked == ["/"]
    assert [r["state"] for r in res] == ["ok", "skipped", "absent"]


def _disk_main(tmp_path, monkeypatch, free, mounts_knob='"/ /boot"'):
    sent = []
    monkeypatch.setattr(disk.mc, "send_alert", lambda key, prio, title, *a, **k: sent.append((key, title))
                        or mc.ALERT_DELIVERED)  # fmt: skip
    monkeypatch.setattr(disk.mc, "is_nvme_backed", lambda mm, root: True)
    monkeypatch.setattr(disk.os, "statvfs", lambda p: type("S", (), {"f_blocks": 100, "f_frsize": 10**8,
                                                                    "f_bavail": free[p]})(), raising=False)  # fmt: skip
    _w(
        tmp_path / "proc/self/mountinfo",
        "29 1 253:0 / / rw - ext4 /dev/vg rw\n31 29 259:2 / /boot rw - ext4 /dev/n rw\n",
    )
    _w(tmp_path / "env", "HOMEHUB_DISK_MOUNTS=%s\n" % mounts_knob)
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))
    rc = disk.main(["--state-dir", str(tmp_path / "st"), "--env-file", str(tmp_path / "env"),
                    "--proc-root", str(tmp_path / "proc")])  # fmt: skip
    return rc, sent


def test_disk_alerts_once_per_crossing_and_on_recovery_tc1288(tmp_path, monkeypatch):
    rc, sent = _disk_main(tmp_path, monkeypatch, {"/": 50, "/boot": 50})
    assert rc == 0 and sent == []
    rc, sent = _disk_main(tmp_path, monkeypatch, {"/": 2, "/boot": 50})
    assert [k for k, _ in sent] == ["disk-low-root"] and "low on space" in sent[0][1]
    rc, sent = _disk_main(tmp_path, monkeypatch, {"/": 1, "/boot": 50})
    assert sent == [], "one alert per crossing, not per run"
    rc, sent = _disk_main(tmp_path, monkeypatch, {"/": 60, "/boot": 50})
    assert [k for k, _ in sent] == ["disk-low-root.clear"] and "space again" in sent[0][
        1
    ]
    status = json.loads(
        (tmp_path / "st" / "disk-status.json").read_text(encoding="utf-8")
    )
    assert status["ts"] > 0 and [m["state"] for m in status["mounts"]] == ["ok", "ok"]


def test_disk_threshold_override_drill_tc1288(tmp_path, monkeypatch):
    rc, sent = _disk_main(
        tmp_path, monkeypatch, {"/": 50, "/boot": 50}, '"/ /boot=99%"'
    )
    assert [k for k, _ in sent] == ["disk-low-boot"]
    rc, sent = _disk_main(tmp_path, monkeypatch, {"/": 50, "/boot": 50}, '"/ /boot"')
    assert [k for k, _ in sent] == ["disk-low-boot.clear"]


def test_disk_alerts_a_configured_mount_that_is_not_mounted_tc1287(
    tmp_path, monkeypatch
):
    rc, sent = _disk_main(
        tmp_path, monkeypatch, {"/": 50, "/boot": 50}, '"/ /srv/scratch"'
    )
    assert rc == 0 and [k for k, _ in sent] == ["disk-absent-srv-scratch"]


# ── watches (SR-088, LLR-1156..LLR-1158) ─────────────────────────────────────

TABLE = (MON / "watch.table").read_text(encoding="utf-8")


def test_the_shipped_table_parses_and_honours_its_conditions_tc1289():
    rows, problems = watch.parse_table(TABLE, {})
    assert problems == []
    assert [r["name"] for r in rows] == ["library-backup", "containers", "restart-loops"]
    env = {"ICEDRIVE_MODE": "appimage", "REMOTE_UI_ENABLED": "true", "HOMEHUB_WATCH_SYNTHETIC_PROCESS": "hh-drill",
           "HOMEHUB_WATCH_SYNTHETIC_ABSENT_MIN": "5", "HOMEHUB_WATCH_DISABLE": "library-backup"}  # fmt: skip
    rows, problems = watch.parse_table(TABLE, env)
    by = {r["name"]: r for r in rows}
    assert problems == [] and set(by) == {
        "icedrive-client",
        "icedrive-profile",
        "containers",
        "restart-loops",
        "synthetic",
    }
    assert (
        by["icedrive-client"]["threshold_s"] == 900
        and by["icedrive-profile"]["threshold_s"] == 26 * 3600
    )
    assert by["synthetic"] == {
        "name": "synthetic",
        "kind": "process",
        "target": "hh-drill",
        "threshold_s": 300,
    }


def test_a_malformed_row_is_a_problem_not_a_guess_tc1289():
    rows, problems = watch.parse_table(
        "a process x 5x\nb nonsense x 5m\nc process x 5m\nc process y 5m\n", {}
    )
    assert [r["name"] for r in rows] == ["c"] and len(problems) == 3


def test_process_watch_fires_after_the_threshold_and_clears_tc1290():
    row = {
        "name": "w",
        "kind": "process",
        "target": "Icedrive.AppImage",
        "threshold_s": 900,
    }
    st = {}
    bad, st, _ = watch.judge_process(row, st, ["Icedrive.AppIma"], "b", 0.0)
    assert bad is False
    bad, st, what = watch.judge_process(row, st, ["bash"], "b", 600.0)
    assert bad is False and what == "absent for 600s"
    bad, st, _ = watch.judge_process(row, st, ["bash"], "b", 901.0)
    assert bad is True
    bad, st, _ = watch.judge_process(row, st, ["Icedrive.AppIma"], "b", 960.0)
    assert bad is False
    bad, st, _ = watch.judge_process(row, st, ["bash"], "b2", 10.0)
    assert bad is False, "a reboot restarts the count"
    assert watch.judge_process(row, st, None, "b2", 2000.0)[0] is None


def _show(
    result="success",
    status="0",
    exit_ts=None,
    start_ts=None,
    active="inactive",
    load="loaded",
):
    d = {
        "Result": result,
        "ExecMainStatus": status,
        "ActiveState": active,
        "LoadState": load,
    }
    d["ExecMainExitTimestamp"] = "@%d" % exit_ts if exit_ts else ""
    d["ExecMainStartTimestamp"] = "@%d" % start_ts if start_ts else ""
    return d


def test_unit_success_watch_fires_on_age_and_clears_on_success_tc1291():
    row = {
        "name": "lb",
        "kind": "unit-success",
        "target": "x.service",
        "threshold_s": 26 * 3600,
    }
    now = 2_000_000_000
    bad, st, _ = watch.judge_unit_success(
        row, {}, _show(exit_ts=now - 3600, start_ts=now - 3700), now
    )
    assert bad is False and st["last_success"] == now - 3600
    # systemd forgot (a reboot): the stored success still counts, and ages.
    bad, st, _ = watch.judge_unit_success(
        row, st, _show(result="", status=""), now + 26 * 3600
    )
    assert bad is True
    bad, st, _ = watch.judge_unit_success(
        row, st, _show(exit_ts=now + 27 * 3600), now + 27 * 3600 + 5
    )
    assert bad is False


def test_a_failed_last_run_older_than_the_threshold_is_stale_at_once_tc1291():
    row = {
        "name": "lb",
        "kind": "unit-success",
        "target": "x.service",
        "threshold_s": 26 * 3600,
    }
    now = 2_000_000_000
    show = _show(
        result="exit-code",
        status="1",
        exit_ts=now - 17 * 86400,
        start_ts=now - 18 * 86400,
        active="failed",
    )
    bad, _, what = watch.judge_unit_success(row, {}, show, now)
    assert bad is True and "no success seen" in what
    bad, _, _ = watch.judge_unit_success(
        row, {}, _show(result="", status="", load="not-found"), now
    )
    assert bad is False, "no record at all: judged from when the watch first saw it"
    assert watch.judge_unit_success(row, {}, None, now)[0] is None


def test_container_watch_is_per_container_and_unknown_when_docker_hangs_tc1292():
    row = {"name": "c", "kind": "docker-health", "target": "*", "threshold_s": 600}
    v, st, _ = watch.judge_containers(row, {}, ["immich"], 0.0, "b")
    assert v == {"immich": False}
    v, st, _ = watch.judge_containers(row, st, ["immich", "sure-web"], 601.0, "b")
    assert v == {"immich": True, "sure-web": False}
    v, st2, _ = watch.judge_containers(row, st, None, 700.0, "b")
    assert v is None and st2 == st
    v, st, _ = watch.judge_containers(row, st, ["sure-web"], 1300.0, "b")
    assert v == {"sure-web": True, "immich": False}


def test_tc1368_a_restart_loop_fires_per_container_and_clears_after_a_quiet_window_llr1211():
    """C58: Immich died every ~14 s while `docker ps` said Up and its health
    said starting - so docker-health never fired. Only RestartCount told."""
    row = {"name": "r", "kind": "docker-restarts", "target": "*", "threshold_s": 1800}
    v, st, _ = watch.judge_restarts(row, {}, {"immich-server": 10, "caddy": 0}, 0.0, "b")
    assert v == {"immich-server": False, "caddy": False}, "first sight is a baseline, never an alert"
    v, st, what = watch.judge_restarts(row, st, {"immich-server": 31, "caddy": 0}, 300.0, "b")
    assert v == {"immich-server": True, "caddy": False} and "immich-server +21" in what
    v, st, _ = watch.judge_restarts(row, st, {"immich-server": 31, "caddy": 2}, 600.0, "b")
    assert v == {"immich-server": True, "caddy": False}, "two restarts is not a loop"
    # Thirty quiet minutes later the old samples have aged out: cleared.
    v, st, _ = watch.judge_restarts(row, st, {"immich-server": 31, "caddy": 2}, 2500.0, "b")
    assert v == {"immich-server": False, "caddy": False}


def test_tc1368_a_recreated_container_a_new_boot_and_a_hung_docker_llr1211():
    row = {"name": "r", "kind": "docker-restarts", "target": "*", "threshold_s": 1800}
    _, st, _ = watch.judge_restarts(row, {}, {"x": 50}, 0.0, "b")
    v, st, _ = watch.judge_restarts(row, st, {"x": 0}, 300.0, "b")
    assert v == {"x": False}, "a count that went down is a fresh container, not -50"
    v, st, _ = watch.judge_restarts(row, st, {"x": 4}, 600.0, "b")
    assert v == {"x": True}
    v, st2, _ = watch.judge_restarts(row, st, None, 700.0, "b")
    assert v is None and st2 == st, "docker not answering changes nothing"
    v, st, _ = watch.judge_restarts(row, st, {"x": 9}, 5.0, "b2")
    # A new boot restarts every history, but a loop that was alerting is not
    # declared over by a reboot: it clears only after a whole window of the new
    # boot with fewer than three restarts (W10-sA review F3).
    assert v == {"x": True} and st["samples"] == {"x": [[5.0, 9]]}
    v, st, _ = watch.judge_restarts(row, st, {"x": 10}, 1000.0, "b2")
    assert v == {"x": True}, "less than a whole window of the new boot: no clear yet"
    v, st, _ = watch.judge_restarts(row, st, {"x": 10}, 1805.0, "b2")
    assert v == {"x": False}, "a whole window with one restart clears"
    v, st, _ = watch.judge_restarts(row, st, {}, 1810.0, "b2")
    assert v == {"x": False} and st["samples"] == {} and st["looping"] == [], "a container that is gone is cleared"


def test_tc1368_the_baseline_brackets_the_window_so_no_restart_inside_it_is_lost_llr1211():
    """The review's sequence (poll second, count), restarts really at 100, 800
    and 1700 s, polled about every 301 s (the timer's AccuracySec=30s allows the
    drift). All three lie inside the 30-minute window ending at 1806; pruning
    every sample older than the window threw away the zero baseline and the
    watch reported +2 and never fired."""
    row = {"name": "r", "kind": "docker-restarts", "target": "*", "threshold_s": 1800}
    st, seen = {}, []
    for mono, count in ((0, 0), (301, 1), (602, 1), (903, 2), (1204, 2), (1505, 2), (1806, 3)):
        v, st, what = watch.judge_restarts(row, st, {"immich-server": count}, float(mono), "b")
        seen.append(v["immich-server"])
    assert seen == [False] * 6 + [True], seen
    assert "immich-server +3" in what
    # The baseline kept is the newest sample at or before 1806 - 1800 = 6: (0, 0).
    assert st["samples"]["immich-server"][0] == [0.0, 0]
    # 2107: the window (307, 2107] holds 800 and 1700 only, from the baseline
    # (301, 1), a whole window old: fewer than three, so it clears.
    v, st, _ = watch.judge_restarts(row, st, {"immich-server": 3}, 2107.0, "b")
    assert v == {"immich-server": False}


def _restart_loop_verdicts(period, poll, until, stop_at=None, jitter=()):
    """Drive judge_restarts with a container that restarts every `period`
    seconds (until `stop_at`), polled every `poll` seconds (plus `jitter`)."""
    row = {"name": "r", "kind": "docker-restarts", "target": "*", "threshold_s": 1800}
    st, out, t, i = {}, [], 0.0, 0
    while t <= until:
        last = t if stop_at is None else min(t, stop_at)
        count = int(last // period)
        v, st, _ = watch.judge_restarts(row, st, {"c": count}, t, "b")
        out.append((t, v["c"]))
        t += poll + (jitter[i % len(jitter)] if jitter else 0.0)
        i += 1
    return out


def test_tc1368_a_steady_loop_at_the_threshold_rate_fires_and_stays_fired_llr1211():
    """Restarts every 600 s, polled every 301 s: exactly three per window. The
    old pruning flapped fire/clear on every poll from 1505 s on, sending
    'stopped restarting' while the loop ran."""
    verdicts = _restart_loop_verdicts(period=600.0, poll=301.0, until=4 * 3600.0)
    flips = [(a[0], b[0]) for a, b in zip(verdicts, verdicts[1:]) if a[1] and not b[1]]
    assert flips == [], "cleared while the loop ran: %r" % flips[:5]
    first = next(t for t, bad in verdicts if bad)
    assert first <= 1800.0 + 301.0, first
    assert all(bad for t, bad in verdicts if t >= first)
    # the same loop with the timer's +-30 s drift
    drift = _restart_loop_verdicts(period=600.0, poll=301.0, until=4 * 3600.0, jitter=(25.0, -20.0, 29.0, -30.0))
    first = next(t for t, bad in drift if bad)
    assert all(bad for t, bad in drift if t >= first), drift


def test_tc1368_a_loop_clears_only_after_a_whole_window_with_fewer_llr1211():
    """The loop stops at 7200 s (last restart there). While any whole 30-minute
    window ending at the poll still holds three restarts the verdict stays
    fired; it clears once one does not."""
    verdicts = _restart_loop_verdicts(period=600.0, poll=301.0, until=4 * 3600.0, stop_at=7200.0)
    restarts = [600.0 * k for k in range(1, 13)]
    for t, bad in verdicts:
        inside = sum(1 for r in restarts if t - 1800.0 < r <= t)
        if t > 1800.0 + 301.0 and inside >= 3:
            assert bad, (t, inside)
    cleared = [t for t, bad in verdicts if t > 7200.0 and not bad]
    assert cleared, "never cleared after the loop stopped"
    assert cleared[0] >= 7200.0 - 1200.0 + 1800.0, cleared[0]   # 6000 s restart left the window
    assert all(not bad for t, bad in verdicts if t >= cleared[0])


def test_tc1369_the_watch_alerts_a_loop_once_and_clears_it_once_llr1211(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(watch.mc, "send_alert", lambda key, prio, title, *a, **k: sent.append((key, title))
                        or mc.ALERT_DELIVERED)  # fmt: skip
    monkeypatch.setattr(watch, "systemctl_show", lambda unit: _show(exit_ts=2_000_000_000))
    monkeypatch.setattr(watch, "docker_unhealthy", lambda: [])
    counts = {"immich-server": 0}
    monkeypatch.setattr(watch, "docker_restart_counts", lambda: dict(counts))
    clock = {"mono": 100.0}
    monkeypatch.setattr(watch.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(watch.time, "time", lambda: 2_000_000_100.0)
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))
    _w(tmp_path / "env", "")
    _w(tmp_path / "proc/sys/kernel/random/boot_id", "b1\n")
    args = ["--state-dir", str(tmp_path / "st"), "--env-file", str(tmp_path / "env"), "--proc-root",
            str(tmp_path / "proc")]  # fmt: skip
    for mono, count in ((100.0, 0), (400.0, 21), (700.0, 42), (2900.0, 42), (3200.0, 42)):
        clock["mono"], counts["immich-server"] = mono, count
        assert watch.main(args) == 0
    loops = [t for k, t in sent if k.startswith("watch-restart-loops-immich-server")]
    assert len(loops) == 2, sent
    assert "restarting in a loop" in loops[0] and "stopped restarting" in loops[1]
    status = json.loads((tmp_path / "st" / "watch-status.json").read_text(encoding="utf-8"))
    assert {r["name"]: r["verdict"] for r in status["rows"]}["restart-loops"] == "ok"


def test_tc1369_the_restart_counts_reader_parses_docker_and_refuses_garbage_llr1211(monkeypatch):
    outputs = iter([mc_done(b"aaaaaaaaaaaa\nbbbbbbbbbbbb\n"),
                    mc_done(b"/immich-server 2327\n/caddy 0\nnot a line\n")])
    monkeypatch.setattr(watch.mc, "run_bounded", lambda *a, **k: next(outputs))
    assert watch.docker_restart_counts() == {"immich-server": 2327, "caddy": 0}
    monkeypatch.setattr(watch.mc, "run_bounded", lambda *a, **k: mc_done(b"", ok=False))
    assert watch.docker_restart_counts() is None


def mc_done(output, ok=True):
    import types as _types
    return _types.SimpleNamespace(ok=ok, output=output)


def test_watch_main_drill_fires_and_clears_the_synthetic_watch_tc1293(
    tmp_path, monkeypatch
):
    sent = []
    monkeypatch.setattr(watch.mc, "send_alert", lambda key, prio, title, *a, **k: sent.append((key, title))
                        or mc.ALERT_DELIVERED)  # fmt: skip
    monkeypatch.setattr(
        watch, "systemctl_show", lambda unit: _show(exit_ts=2_000_000_000)
    )
    monkeypatch.setattr(watch, "docker_unhealthy", lambda: [])
    clock = {"mono": 100.0}
    monkeypatch.setattr(watch.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(watch.time, "time", lambda: 2_000_000_100.0)
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "override"))
    _w(tmp_path / "env", "")
    _w(
        tmp_path / "override",
        "HOMEHUB_WATCH_SYNTHETIC_PROCESS=hh-drill\nHOMEHUB_WATCH_SYNTHETIC_ABSENT_MIN=5\n",
    )
    _w(tmp_path / "proc/sys/kernel/random/boot_id", "b1\n")
    args = ["--state-dir", str(tmp_path / "st"), "--env-file", str(tmp_path / "env"), "--proc-root",
            str(tmp_path / "proc")]  # fmt: skip

    def tick(mono, running):
        clock["mono"] = mono
        comm = tmp_path / "proc" / "4242" / "comm"
        if running:
            _w(comm, "hh-drill\n")
        elif comm.exists():
            comm.unlink()
        assert watch.main(args) == 0

    tick(100.0, True)
    tick(300.0, False)
    tick(399.0, False)
    assert sent == [], "absent 299 s of a 300 s threshold"
    tick(401.0, False)
    assert [k for k, _ in sent] == [
        "watch-synthetic"
    ] and "hh-drill is not running" in sent[0][1]
    tick(1000.0, False)
    tick(1300.0, True)
    assert [k for k, _ in sent] == ["watch-synthetic", "watch-synthetic.clear"]
    status = json.loads(
        (tmp_path / "st" / "watch-status.json").read_text(encoding="utf-8")
    )
    assert {r["name"]: r["verdict"] for r in status["rows"]}["synthetic"] == "ok"


# ── plan W3b: door segments, scratch identity, meeting pull (LLR-1290..LLR-1292) ──

W3B_ON = {"DOOR_RECORDER_ENABLED": "true", "HOMEHUB_SCRATCH_ENABLED": "true", "MEETING_PULL_ENABLED": "true"}
T0 = 2_000_000_000.0
# A synthetic filesystem UUID: this repo is public, so never a real deployment value.
SCRATCH_UUID = "00000000-0000-4000-8000-000000000001"


def _w3b_rows(env):
    rows, problems = watch.parse_table(TABLE, env)
    assert problems == []
    return {r["name"]: r for r in rows if r["name"] in ("door-segments", "scratch-mount", "meeting-pull")}


def test_tc1460_the_w3b_rows_are_gated_by_their_knobs_and_take_drill_overrides_llr1292():
    assert _w3b_rows({}) == {}, "nothing W3b watches on a hub without the components"
    assert _w3b_rows({k: "false" for k in W3B_ON}) == {}
    by = _w3b_rows(W3B_ON)
    assert by == {
        "door-segments": {"name": "door-segments", "kind": "newest-file",
                          "target": "/srv/scratch/door/*/door-*.ts*", "threshold_s": 900},
        "scratch-mount": {"name": "scratch-mount", "kind": "scratch-identity",
                          "target": "/opt/homehub/stack/scratch/homehub_scratch.py", "threshold_s": 300},
        "meeting-pull": {"name": "meeting-pull", "kind": "unit-success",
                         "target": "homehub-meeting-pull.service", "threshold_s": 1800},
    }  # fmt: skip
    for name, knob in (("door-segments", "DOOR_RECORDER_ENABLED"), ("scratch-mount", "HOMEHUB_SCRATCH_ENABLED"),
                       ("meeting-pull", "MEETING_PULL_ENABLED")):  # fmt: skip
        assert set(_w3b_rows(dict(W3B_ON, **{knob: "false"}))) == set(by) - {name}, knob
    drill = _w3b_rows(dict(W3B_ON, HOMEHUB_WATCH_DOOR_FRESH_MIN="0", HOMEHUB_WATCH_SCRATCH_MIN="0",
                           HOMEHUB_WATCH_MEETING_PULL_MIN="0", HOMEHUB_WATCH_SCRATCH_TOOL="/nonexistent/x.py"))  # fmt: skip
    assert {n: r["threshold_s"] for n, r in drill.items()} == {"door-segments": 0, "scratch-mount": 0,
                                                               "meeting-pull": 0}  # fmt: skip
    assert drill["scratch-mount"]["target"] == "/nonexistent/x.py"
    assert "door-segments" not in _w3b_rows(dict(W3B_ON, HOMEHUB_WATCH_DISABLE="door-segments"))
    # the default tool is the one the deploy installs, the path every scratch writer's guard runs
    assert (ROOT / "stack" / "scratch" / "homehub_scratch.py").is_file()
    assert "/opt/homehub/stack/scratch/homehub_scratch.py" in (
        ROOT / "stack" / "door" / "homehub-door-recorder.service").read_text(encoding="utf-8")


def test_tc1460_newest_file_and_scratch_targets_are_validated_llr1290_llr1291():
    assert watch.split_glob("/srv/scratch/door/*/door-*.ts*") == ("/srv/scratch/door", ["*", "door-*.ts*"])
    assert watch.split_glob("/srv/scratch/door/x.ts") == ("/srv/scratch/door", ["x.ts"])
    for bad in ("srv/door/*", "/srv/../etc/*", "/srv/**/x", "/*/x", "/srv//door/*", "/srv/./door/*"):
        assert watch.split_glob(bad) is None, bad
    rows, problems = watch.parse_table(
        "d newest-file relative/*.ts 15m\ne newest-file /srv/**/x 15m\ns scratch-identity tool.py 5m\n"
        "ok newest-file /srv/scratch/door/*/door-*.ts* 0m\n", {})  # fmt: skip
    assert [r["name"] for r in rows] == ["ok"] and rows[0]["threshold_s"] == 0 and len(problems) == 3
    assert watch.parse_threshold("0m") == 0 and watch.parse_threshold("m") is None


def _seg(path, mtime):
    path = _w(path, "x")
    os.utime(str(path), (mtime, mtime))
    return path


def _door_row(tmp_path, threshold=900):
    """The SHIPPED door-segments row, its tree moved into tmp_path."""
    row = dict(_w3b_rows(W3B_ON)["door-segments"], threshold_s=threshold)
    row["target"] = row["target"].replace("/srv/scratch/door", (tmp_path / "door").as_posix(), 1)
    return row


def _judge_door(row, st, now):
    return watch.judge_newest_file(row, st, watch.newest_match(row["target"]), now)


def test_tc1461_door_segment_freshness_fires_on_a_stale_tree_and_counts_the_open_part_llr1290(tmp_path):
    row, day = _door_row(tmp_path), tmp_path / "door" / "2026-10-22"
    closed = _seg(day / "door-20261022T230000Z.ts", T0 - 1200)
    part = _seg(day / "door-20261022T231000Z.ts.part", T0 - 30)
    _seg(day / "notes.txt", T0)  # not a segment: never counts
    _seg(tmp_path / "door" / "door-20261022T231000Z.ts", T0)  # not one level down: never counts
    bad, st, what = _judge_door(row, {}, T0)
    assert bad is False and "door-20261022T231000Z.ts.part written 30s ago (2 matching)" in what
    part.unlink()  # the recorder stopped: the newest is the closed segment, 1200 s old
    bad, st, what = _judge_door(row, st, T0)
    assert bad is True and "door-20261022T230000Z.ts written 1200s ago" in what
    os.utime(str(closed), (T0 - 899, T0 - 899))
    assert _judge_door(row, st, T0)[0] is False, "899 s of a 900 s threshold"
    _seg(day / "door-20261022T232000Z.ts.part", T0 - 1)
    bad, st, _ = _judge_door(row, st, T0)
    assert bad is False, "a new segment clears it"
    # the drill: 0m fires on the healthy tree, the normal threshold clears it
    assert _judge_door(_door_row(tmp_path, 0), st, T0)[0] is True
    assert _judge_door(row, st, T0)[0] is False


def test_tc1461_no_file_or_no_tree_fires_after_the_threshold_from_first_sight_llr1290(tmp_path):
    row = _door_row(tmp_path)
    bad, st, what = _judge_door(row, {}, T0)  # the root does not even exist
    assert bad is False and st["empty_since"] == T0 and "no file matches" in what and "ENOENT" in what
    (tmp_path / "door" / "2026-10-22").mkdir(parents=True)  # an empty tree
    bad, st, what = _judge_door(row, st, T0 + 899)
    assert bad is False and "seen empty for 899s" in what
    bad, st, _ = _judge_door(row, st, T0 + 900)
    assert bad is True
    # wall clock, kept in the state file: a reboot does not restart the count
    st = json.loads(json.dumps(st))
    assert _judge_door(row, st, T0 + 2000)[0] is True
    # a clock that went back past the stamp restarts the count (of an incident not yet firing)
    bad, st2, _ = _judge_door(row, {"empty_since": T0}, T0 - 5000)
    assert bad is False and st2["empty_since"] == T0 - 5000
    assert _judge_door(row, st, T0 - 5000)[0] is True, "a firing incident is not cleared by a clock jump"
    _seg(tmp_path / "door" / "2026-10-22" / "door-20261022T000000Z.ts.part", T0 + 1990)
    bad, st, _ = _judge_door(row, st, T0 + 2000)
    assert bad is False and "empty_since" not in st and st["firing"] is False


def test_tc1461_stale_segments_deleted_while_firing_send_no_false_clear_llr1290(tmp_path, monkeypatch):
    """main(): stale alert delivered -> the stale files are deleted -> still empty -> a fresh
    segment: exactly one fire and one clear, the clear only when recording resumes."""
    sent = []
    monkeypatch.setattr(watch.mc, "send_alert", lambda key, prio, title, *a, **k: sent.append(key)
                        or mc.ALERT_DELIVERED)  # fmt: skip
    clock = {"t": T0}
    monkeypatch.setattr(watch.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(watch.time, "time", lambda: clock["t"])
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))
    row = [line for line in TABLE.splitlines() if line.startswith("door-segments ")]
    door = (tmp_path / "door").as_posix()
    table = _w(tmp_path / "watch.table", "\n".join(row).replace("/srv/scratch/door", door) + "\n")
    _w(tmp_path / "env", "DOOR_RECORDER_ENABLED=true\n")
    _w(tmp_path / "proc/sys/kernel/random/boot_id", "b1\n")
    args = ["--table", str(table), "--state-dir", str(tmp_path / "st"), "--env-file", str(tmp_path / "env"),
            "--proc-root", str(tmp_path / "proc")]  # fmt: skip
    day = tmp_path / "door" / "2026-10-22"
    stale = _seg(day / "door-20261022T200000Z.ts", T0 - 3600)
    assert watch.main(args) == 0 and sent == ["watch-door-segments"]
    stale.unlink()  # the operator frees space; the recorder is still stopped
    for step in (300, 600, 900, 1200):
        clock["t"] = T0 + step
        assert watch.main(args) == 0
    assert sent == ["watch-door-segments"], "no 'being written again' while nothing is written"
    _seg(day / "door-20261022T232000Z.ts.part", T0 + 1490)
    clock["t"] = T0 + 1500
    assert watch.main(args) == 0 and sent == ["watch-door-segments", "watch-door-segments.clear"]


def test_tc1461_a_clock_that_went_back_does_not_clear_a_stale_recorder_llr1290(tmp_path, monkeypatch):
    """main(): a segment last written 1200 s ago fires -> the clock goes back an hour with the
    file untouched (its mtime now 2400 s in the future) -> still firing, no 'being written
    again' -> a genuinely fresh segment is the only clear (Sol W3b review round 1, F2)."""
    sent = []
    monkeypatch.setattr(watch.mc, "send_alert", lambda key, prio, title, *a, **k: sent.append(key)
                        or mc.ALERT_DELIVERED)  # fmt: skip
    clock = {"t": T0}
    monkeypatch.setattr(watch.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(watch.time, "time", lambda: clock["t"])
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))
    row = [line for line in TABLE.splitlines() if line.startswith("door-segments ")]
    door = (tmp_path / "door").as_posix()
    table = _w(tmp_path / "watch.table", "\n".join(row).replace("/srv/scratch/door", door) + "\n")
    _w(tmp_path / "env", "DOOR_RECORDER_ENABLED=true\n")
    _w(tmp_path / "proc/sys/kernel/random/boot_id", "b1\n")
    args = ["--table", str(table), "--state-dir", str(tmp_path / "st"), "--env-file", str(tmp_path / "env"),
            "--proc-root", str(tmp_path / "proc")]  # fmt: skip
    day = tmp_path / "door" / "2026-10-22"
    _seg(day / "door-20261022T200000Z.ts", T0 - 1200)
    assert watch.main(args) == 0 and sent == ["watch-door-segments"]
    for back in (3600, 3300, 3000):  # the clock went back an hour and runs on from there
        clock["t"] = T0 - back
        assert watch.main(args) == 0
    assert sent == ["watch-door-segments"], "an unchanged stale file is no recovery"
    # held, not judged, until the clock passes the file again: then a fresh segment is the clear
    clock["t"] = T0 + 300
    assert watch.main(args) == 0 and sent == ["watch-door-segments"], "the file is 1500 s old again"
    _seg(day / "door-20261022T210000Z.ts.part", T0 + 590)
    clock["t"] = T0 + 600
    assert watch.main(args) == 0 and sent == ["watch-door-segments", "watch-door-segments.clear"]
    # the pure judgement: a row that was not firing stays ok, and says why it did not judge
    bad, st, what = _judge_door(_door_row(tmp_path), {}, T0 - 7200)
    assert bad is False and st["firing"] is False and "in the future (clock went back?)" in what
    bad, st, what = _judge_door(_door_row(tmp_path), {"firing": True}, T0 - 7200)
    assert bad is True and st["firing"] is True and "not judged as recovery" in what


def _can_symlink(tmp_path):
    try:
        os.symlink(str(tmp_path), str(tmp_path / "probe-link"))
    except (OSError, NotImplementedError):
        return False
    os.unlink(str(tmp_path / "probe-link"))
    return True


def test_tc1462_a_symlink_out_of_the_tree_is_never_followed_llr1290(tmp_path):
    if not _can_symlink(tmp_path):
        pytest.skip("this OS account cannot create symlinks (monitoring.test.sh runs the same case on Linux)")
    row, outside = _door_row(tmp_path), tmp_path / "elsewhere"
    fresh = _seg(outside / "door-20261022T235900Z.ts", T0)
    _seg(tmp_path / "door" / "2026-10-22" / "door-20261022T200000Z.ts", T0 - 3600)
    os.symlink(str(outside), str(tmp_path / "door" / "2026-10-23"))  # a "day" that is a link out
    os.symlink(str(fresh), str(tmp_path / "door" / "2026-10-22" / "door-20261022T235959Z.ts"))  # a file link out
    bad, _, what = _judge_door(row, {}, T0)
    assert bad is True and "door-20261022T200000Z.ts written 3600s ago (1 matching)" in what
    linked_root = tmp_path / "linked"
    os.symlink(str(outside), str(linked_root))
    row = {"name": "x", "kind": "newest-file", "threshold_s": 900, "target": linked_root.as_posix() + "/door-*.ts"}
    bad, _, what = _judge_door(row, {}, T0)
    assert bad is False and "is a symlink (not followed)" in what and "no file matches" in what


def test_tc1463_the_walk_is_bounded_in_entries_and_time_and_a_limit_fires_llr1290(tmp_path, monkeypatch):
    row, day = _door_row(tmp_path), tmp_path / "door" / "2026-10-22"
    for i in range(12):
        _seg(day / ("door-20261022T23%02d00Z.ts" % i), T0 - 10)
    assert watch.newest_match(row["target"], max_entries=13)["limit"] is None
    found = watch.newest_match(row["target"], max_entries=12)
    assert found["limit"] == "more than 12 entries under %s" % (tmp_path / "door").as_posix()
    bad, _, what = watch.judge_newest_file(row, {}, found, T0)
    assert bad is True and "not judged from part of the tree" in what, "fresh files, but the walk was cut"
    import threading

    gate = threading.Event()
    monkeypatch.setattr(watch, "_walk_newest", lambda *a: gate.wait(30) and {})
    t = __import__("time").monotonic()
    found = watch.newest_match(row["target"], timeout=0.3)
    took = __import__("time").monotonic() - t
    gate.set()
    assert took < 5 and "did not finish in 0.3s" in found["limit"]
    assert watch.judge_newest_file(row, {}, found, T0)[0] is True


def _status(**over):
    d = {"mountpoint": "/srv/scratch", "expected_uuid": SCRATCH_UUID, "mounted": True,
         "source": "/dev/mapper/vg-hub--scratch", "identity_ok": True, "prjquota": True}  # fmt: skip
    d.update(over)
    return d


def test_tc1464_scratch_identity_reads_the_tools_own_verdict_and_fires_after_two_bad_runs_llr1291():
    assert watch.scratch_verdict(_status(), "") == (False, "/srv/scratch mounted from /dev/mapper/vg-hub--scratch, "
                                                         "identity and prjquota ok")  # fmt: skip
    for status, says in ((_status(mounted=False), "is not mounted"),
                         ({"mountpoint": "/srv/scratch", "mounted": False}, "is not mounted"),
                         (_status(identity_ok=False), "is not the filesystem HOMEHUB_SCRATCH_UUID names"),
                         (_status(identity_ok=False, expected_uuid=""), "expected unset"),
                         (_status(prjquota=False), "without prjquota"),
                         (_status(identity_ok=None), "is not the filesystem"),
                         (None, "scratch status did not run: boom")):  # fmt: skip
        bad, what = watch.scratch_verdict(status, "boom")
        assert bad is True and says in what, (status, what)
    row = {"name": "scratch-mount", "kind": "scratch-identity", "target": "/x.py", "threshold_s": 300}
    bad, st, what = watch.judge_scratch(row, {}, True, "not mounted", "b", 100.0)
    assert bad is False and "bad for 0s" in what, "one bad run is not five minutes"
    bad, st, _ = watch.judge_scratch(row, st, True, "not mounted", "b", 399.7)
    assert bad is True, "the next timer run, a fraction of a second under 300 s later, fires"
    bad, st, _ = watch.judge_scratch(row, st, False, "ok", "b", 700.0)
    assert bad is False and st == {}
    _, st, _ = watch.judge_scratch(row, st, True, "x", "b", 800.0)
    assert watch.judge_scratch(row, st, True, "x", "b", 1000.0)[0] is False, "200 s of bad is not 300"
    assert watch.judge_scratch(row, st, True, "x", "b2", 5.0)[0] is False, "a new boot restarts the count"
    assert watch.judge_scratch(dict(row, threshold_s=0), {}, True, "x", "b", 1.0)[0] is True, "0m: the drill"
    # an incident that fired survives a reboot while the volume is still wrong
    bad, st, _ = watch.judge_scratch(row, {}, True, "x", "b", 0.0)
    bad, st, _ = watch.judge_scratch(row, st, True, "x", "b", 300.0)
    assert bad is True
    bad, st, _ = watch.judge_scratch(row, json.loads(json.dumps(st)), True, "x", "b3", 40.0)
    assert bad is True, "still bad after the reboot: no false clear"
    assert watch.judge_scratch(row, st, False, "ok", "b3", 340.0)[:2] == (False, {})


def test_tc1464_a_reboot_with_the_volume_still_missing_sends_no_false_clear_llr1291(tmp_path, monkeypatch):
    """main(): outage delivered -> reboot -> still missing -> healthy: one fire, one clear, in that order."""
    sent = []
    monkeypatch.setattr(watch.mc, "send_alert", lambda key, prio, title, *a, **k: sent.append(key)
                        or mc.ALERT_DELIVERED)  # fmt: skip
    clock = {"mono": 100.0}
    monkeypatch.setattr(watch.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(watch.time, "time", lambda: T0)
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))
    good = _tool(tmp_path, "good.py", "print(json.dumps({'mounted': True, 'identity_ok': True, 'prjquota': True}))\n")
    gone = _tool(tmp_path, "gone.py", "print(json.dumps({'mountpoint': '/srv/scratch', 'mounted': False}))\n")
    row = [line for line in TABLE.splitlines() if line.startswith("scratch-mount ")]
    table = _w(tmp_path / "watch.table", "\n".join(row) + "\n")
    args = ["--table", str(table), "--state-dir", str(tmp_path / "st"), "--env-file", str(tmp_path / "env"),
            "--proc-root", str(tmp_path / "proc")]  # fmt: skip

    def tick(boot, mono, tool):
        clock["mono"] = mono
        _w(tmp_path / "proc/sys/kernel/random/boot_id", boot + "\n")
        _w(tmp_path / "env", "HOMEHUB_SCRATCH_ENABLED=true\nHOMEHUB_WATCH_SCRATCH_TOOL=%s\n" % tool)
        assert watch.main(args) == 0

    tick("b1", 100.0, gone)
    tick("b1", 400.0, gone)
    assert sent == ["watch-scratch-mount"]
    tick("b2", 200.0, gone)  # rebooted, the volume is still missing
    tick("b2", 500.0, gone)
    assert sent == ["watch-scratch-mount"], "no false 'mounted again', no second fire"
    tick("b2", 800.0, good)
    assert sent == ["watch-scratch-mount", "watch-scratch-mount.clear"]


def _tool(tmp_path, name, body):
    return str(_w(tmp_path / name, "import sys, time, json\n" + body))


def test_tc1464_a_status_that_cannot_run_is_bad_never_a_crash_llr1291(tmp_path, monkeypatch):
    env = str(_w(tmp_path / "env", ""))
    ok = _tool(tmp_path, "ok.py", "assert sys.argv[1:] == ['--env-file', %r, 'status']\n"
               "print(json.dumps({'mounted': True, 'identity_ok': True, 'prjquota': True}))\n" % env)  # fmt: skip
    assert watch.scratch_status(ok, env) == ({"mounted": True, "identity_ok": True, "prjquota": True}, "")
    cases = [
        (_tool(tmp_path, "crash.py", "open('/proc/self/mountinfo-nope')\n"), "status exited 1: FileNotFoundError"),
        (_tool(tmp_path, "garbage.py", "print('not json')\n"), "printed no JSON: not json"),
        (_tool(tmp_path, "list.py", "print('[1, 2]')\n"), "printed no JSON object"),
        (str(tmp_path / "missing.py"), "status exited 2"),
    ]
    for tool, says in cases:
        status, why = watch.scratch_status(tool, env)
        assert status is None and says in why, (tool, why)
        assert watch.scratch_verdict(status, why)[0] is True
    monkeypatch.setattr(watch, "SCRATCH_STATUS_SEC", 1)
    status, why = watch.scratch_status(_tool(tmp_path, "hang.py", "time.sleep(60)\n"), env)
    assert status is None and "timed out after 1s" in why
    monkeypatch.setattr(watch.sys, "executable", str(tmp_path / "no-python"))
    status, why = watch.scratch_status(ok, env)
    assert status is None and why.startswith("cannot run")


def test_tc1464_the_watch_reads_the_keys_homehub_scratch_status_really_writes_llr1291(tmp_path, monkeypatch):
    """The other side of the contract: homehub_scratch.status() itself, over a
    fake mountinfo, judged by the watch. Not a hand-written dict."""
    spec = importlib.util.spec_from_file_location("homehub_scratch_w3b", str(ROOT / "stack" / "scratch" /
                                                                              "homehub_scratch.py"))  # fmt: skip
    hs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hs)
    uuid = SCRATCH_UUID
    paths = hs.Paths()
    paths.mountinfo = str(tmp_path / "mountinfo")
    monkeypatch.setattr(hs, "majmin_of_rdev", lambda p: "253:7" if p.endswith(uuid) else None)
    monkeypatch.setattr(hs, "usable_bytes", lambda p: 18 * 1024 ** 3)
    monkeypatch.setattr(hs, "get_project_quota", lambda dev, pid: (0, 512 * 1024 ** 2))
    monkeypatch.setattr(hs.os, "statvfs", lambda p: __import__("types").SimpleNamespace(f_bavail=1, f_frsize=4096),
                        raising=False)  # fmt: skip
    settings = hs.Settings({"HOMEHUB_SCRATCH_UUID": uuid})

    def judged(majmin, opts):
        _w(paths.mountinfo, "40 29 %s / /srv/scratch rw,noatime - ext4 /dev/mapper/vg-hub--scratch %s\n" % (majmin, opts))
        return watch.scratch_verdict(json.loads(json.dumps(hs.status(settings, paths))), "")

    assert judged("253:7", "rw,prjquota")[0] is False
    bad, what = judged("253:9", "rw,prjquota")
    assert bad is True and "is not the filesystem HOMEHUB_SCRATCH_UUID names" in what
    bad, what = judged("253:7", "rw")
    assert bad is True and "without prjquota" in what
    _w(paths.mountinfo, "29 1 253:0 / / rw - ext4 /dev/mapper/vg-root rw\n")
    assert watch.scratch_verdict(hs.status(settings, paths), "") == (True, "/srv/scratch is not mounted")
    assert watch.scratch_verdict(hs.status(hs.Settings({}), paths), "")[0] is True


def test_tc1465_the_w3b_drills_fire_once_and_clear_once_through_main_llr1292(tmp_path, monkeypatch):
    """The live drill procedure (README "Drills" d-f), run against the real main():
    knobs from the override file only, nothing stopped, one alert and one clear."""
    sent = []
    monkeypatch.setattr(watch.mc, "send_alert", lambda key, prio, title, *a, **k: sent.append((key, title))
                        or mc.ALERT_DELIVERED)  # fmt: skip
    monkeypatch.setattr(watch, "systemctl_show", lambda unit: _show(exit_ts=int(T0) - 60))
    monkeypatch.setattr(watch, "docker_unhealthy", lambda: [])
    monkeypatch.setattr(watch, "docker_restart_counts", lambda: {})
    monkeypatch.setattr(watch.time, "monotonic", lambda: 500.0)
    monkeypatch.setattr(watch.time, "time", lambda: T0)
    override = tmp_path / "override"
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(override))
    door = (tmp_path / "door").as_posix()
    rows = [line for line in TABLE.splitlines() if line.split()[:1] in (["door-segments"], ["scratch-mount"], ["meeting-pull"])]
    assert len(rows) == 3
    table = _w(tmp_path / "watch.table", "\n".join(rows).replace("/srv/scratch/door", door) + "\n")
    ok_tool = _tool(tmp_path, "ok.py", "print(json.dumps({'mounted': True, 'identity_ok': True, 'prjquota': True}))\n")
    _w(tmp_path / "env", "".join("%s=true\n" % k for k in W3B_ON) + "HOMEHUB_WATCH_SCRATCH_TOOL=%s\n" % ok_tool)
    _w(tmp_path / "proc/sys/kernel/random/boot_id", "b1\n")
    _seg(tmp_path / "door" / "2026-10-22" / "door-20261022T231000Z.ts.part", T0 - 20)
    args = ["--table", str(table), "--state-dir", str(tmp_path / "st"), "--env-file", str(tmp_path / "env"),
            "--proc-root", str(tmp_path / "proc")]  # fmt: skip

    def run(override_text=None):
        if override_text is None:
            if override.exists():
                override.unlink()
        else:
            _w(override, override_text)
        del sent[:]
        assert watch.main(args) == 0
        status = json.loads((tmp_path / "st" / "watch-status.json").read_text(encoding="utf-8"))
        return [k for k, _ in sent], [t for _, t in sent], {r["name"]: r for r in status["rows"]}

    keys, _, rows = run()
    assert keys == [] and {n: r["verdict"] for n, r in rows.items()} == {
        "door-segments": "ok", "scratch-mount": "ok", "meeting-pull": "ok"}  # fmt: skip
    # (d) door freshness at 0m on a healthy recorder, then restored
    keys, titles, rows = run("HOMEHUB_WATCH_DOOR_FRESH_MIN=0\n")
    assert keys == ["watch-door-segments"] and rows["door-segments"]["verdict"] == "FIRING"
    assert titles[0] == "HomeHub: nothing new matching %s/*/door-*.ts* in 0m (watch door-segments)" % door
    keys, titles, _ = run()
    assert keys == ["watch-door-segments.clear"] and "is being written again" in titles[0]
    # (e) the scratch status pointed at a tool that does not exist, at 0m
    keys, titles, rows = run("HOMEHUB_WATCH_SCRATCH_TOOL=%s\nHOMEHUB_WATCH_SCRATCH_MIN=0\n" % (tmp_path / "nope.py"))
    assert keys == ["watch-scratch-mount"] and "not mounted as expected" in titles[0]
    assert rows["scratch-mount"]["verdict"] == "FIRING" and "scratch status did not run" in rows["scratch-mount"]["detail"]
    keys, titles, _ = run()
    assert keys == ["watch-scratch-mount.clear"] and "mounted as expected again" in titles[0]
    # (f) the meeting pull at 0m: its last success (60 s ago) is older than 0
    keys, titles, _ = run("HOMEHUB_WATCH_MEETING_PULL_MIN=0\n")
    assert keys == ["watch-meeting-pull"] and "homehub-meeting-pull.service has not succeeded in 0m" in titles[0]
    keys, _, rows = run()
    assert keys == ["watch-meeting-pull.clear"] and rows["meeting-pull"]["verdict"] == "ok"
    # a component switched off takes its row with it
    _w(tmp_path / "env", "DOOR_RECORDER_ENABLED=false\n")
    keys, _, rows = run()
    assert keys == [] and rows == {}


# ── liveness and evidence (SR-089, LLR-1159..LLR-1166) ───────────────────────


def test_dns_packet_round_trip_tc1294():
    pkt = mc.dns_query_packet("tracker.example.test", 0x1234)
    assert pkt[:2] == b"\x12\x34" and b"\x07tracker\x07example\x04test\x00" in pkt
    reply = bytearray(pkt)
    reply[2:4] = b"\x81\x80"
    assert mc.dns_answer_ok(bytes(reply), 0x1234) == (True, "NOERROR")
    reply[3] = 0x83
    assert mc.dns_answer_ok(bytes(reply), 0x1234) == (True, "NXDOMAIN")
    reply[3] = 0x82
    assert mc.dns_answer_ok(bytes(reply), 0x1234) == (False, "SERVFAIL")
    assert mc.dns_answer_ok(bytes(reply), 0x9999)[0] is False
    assert mc.dns_answer_ok(b"\x00", 1)[0] is False


def test_liveness_targets_default_to_the_hubs_own_names_tc1294():
    t = live.targets_from_env({"DOMAIN": "example.test", "LAN_IP": "192.0.2.10"})
    assert (t["dns_server"], t["dns_name"], t["https_host"], t["https_port"]) == (
        "192.0.2.10", "tracker.example.test", "127.0.0.1", 443)  # fmt: skip
    t = live.targets_from_env(
        {"DOMAIN": "example.test", "HOMEHUB_LIVENESS_HTTPS_PORT": "1"}
    )
    assert t["https_port"] == 1 and t["failures"] == 3


def test_https_check_accepts_any_status_and_fails_a_closed_port_tc1294():
    ok, why = live.check_https("127.0.0.1", 1, "localhost", timeout=2)
    assert not ok


def test_incident_opens_once_and_closes_on_the_first_pass_tc1295():
    st, events = {}, []
    for ok in (False, False, False, False, False, True, False):
        st, e = live.advance(st, ok, 3)
        events.append(e)
    assert events == [None, None, "open", None, None, "close", None]
    assert st["seq"] == 1 and st["failures"] == 1


class _Done:
    def __init__(self, out=b"", rc=0, timed_out=False, truncated=False, error=None):
        self.output, self.rc, self.timed_out, self.truncated, self.error = (
            out,
            rc,
            timed_out,
            truncated,
            error,
        )

    @property
    def ok(self):
        return self.error is None and not self.timed_out and self.rc == 0


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _plan(tmp_path):
    proc = tmp_path / "proc"
    _w(proc / "1/stat", "1 (systemd) S 0 1 1 0 -1\n")
    _w(proc / "1/status", "Name:\tsystemd\nVmRSS:\t 1234 kB\nThreads:\t1\n")
    _w(proc / "1/task/1/stat", "1 (systemd) S 0 1 1\n")
    _w(proc / "77/stat", "77 (jbd2/dm-0-8) D 2 0 0\n")
    _w(proc / "77/status", "Name:\tjbd2\nThreads:\t1\n")
    _w(proc / "77/task/77/stat", "77 (jbd2/dm-0-8) D 2 0 0\n")
    _w(proc / "77/task/77/wchan", "jbd2_journal_commit_transaction")
    _w(proc / "77/task/77/stack", "[<0>] jbd2_journal_commit_transaction+0x1\n")
    _w(proc / "meminfo", "MemTotal: 8000000 kB\n")
    _w(proc / "pressure/memory", "some avg10=50.0\nfull avg10=40.0\n")
    return ev.capture_plan(str(proc), ["docker.service"], ["kernel"])


def test_bundle_holds_every_item_by_comm_with_root_only_modes_tc1296(tmp_path):
    runner = lambda argv, t, cap, env=None: _Done(("ran %s\n" % argv[0]).encode())  # noqa: E731
    m = ev.capture(str(tmp_path / "ev"), 1, _plan(tmp_path), {"dns": {"ok": False}}, now=1792652484.0,
                   runner=runner)  # fmt: skip
    assert m["complete"] and m["bundle"] == "20261022T070124Z"
    names = sorted(p.name for p in (tmp_path / "ev" / m["bundle"]).iterdir())
    assert names == sorted(["checks.json", "processes.txt", "psi.txt", "meminfo.txt", "dstate.txt", "top.txt",
                            "dmesg.txt", "journal-units.txt", "journal-tags.txt", "docker-ps.txt",
                            "docker-stats.txt", "manifest.json"])  # fmt: skip
    bundle = tmp_path / "ev" / m["bundle"]
    assert "systemd" in (bundle / "processes.txt").read_text(encoding="utf-8")
    assert "jbd2_journal_commit_transaction" in (bundle / "dstate.txt").read_text(
        encoding="utf-8"
    )
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "ev").stat().st_mode) == 0o700
        assert stat.S_IMODE((bundle / "top.txt").stat().st_mode) == 0o600


def test_the_plan_reads_no_command_lines_and_names_its_journal_sources_tc1296(tmp_path):
    plan = _plan(tmp_path)
    argvs = [" ".join(i["argv"]) for i in plan if "argv" in i]
    assert not any("cmdline" in a or "environ" in a for a in argvs)
    journal = [a for a in argvs if a.startswith("journalctl")]
    assert "-u docker.service" in journal[0] and "-t kernel" in journal[1]
    assert all("--since -10min" in a for a in journal)
    docker = [i["argv"] for i in plan if "argv" in i and i["argv"][0] == "docker"]
    assert all("--format" in a for a in docker), (
        "docker output is names and states only"
    )
    src = (MON / "homehub_evidence.py").read_text(encoding="utf-8")
    assert "cmdline" not in _live(src).replace("command line", "")


def test_a_blocked_docker_is_a_timeout_and_the_bundle_finishes_tc1297(tmp_path):
    clock = _Clock()

    def runner(argv, timeout, cap, env=None):
        if argv[0] == "docker":
            clock.t += timeout
            return _Done(b"", rc=None, timed_out=True)
        return _Done(b"ok\n")

    m = ev.capture(
        str(tmp_path / "ev"), 1, _plan(tmp_path), {}, runner=runner, clock=clock
    )
    status = {i["name"]: i["status"] for i in m["items"]}
    assert (
        status["docker-ps.txt"].startswith("timeout")
        and status["processes.txt"] == "ok"
    )
    assert (
        m["complete"] is False
        and (tmp_path / "ev" / m["bundle"] / "manifest.json").exists()
    )


def test_the_whole_bundle_deadline_skips_what_it_cannot_reach_tc1297(tmp_path):
    clock = _Clock()

    def runner(argv, timeout, cap, env=None):
        clock.t += 50
        return _Done(b"x\n")

    m = ev.capture(
        str(tmp_path / "ev"),
        1,
        _plan(tmp_path),
        {},
        runner=runner,
        clock=clock,
        deadline_s=90,
    )
    status = [i["status"] for i in m["items"]]
    assert "skipped-deadline" in status and status.count("ok") >= 5


def test_a_journal_flood_is_cut_at_the_file_and_bundle_caps_tc1298(tmp_path):
    def runner(argv, timeout, cap, env=None):
        return _Done(b"y" * cap, truncated=True)

    m = ev.capture(
        str(tmp_path / "ev"),
        1,
        _plan(tmp_path),
        {},
        runner=runner,
        bundle_cap=600 * 1024,
    )
    items = {i["name"]: i for i in m["items"]}
    assert items["top.txt"]["status"] == "truncated"
    assert m["bytes"] <= 600 * 1024
    assert any(i["status"] == "skipped-bundle-cap" for i in m["items"])
    for f in (tmp_path / "ev" / m["bundle"]).iterdir():
        assert f.stat().st_size <= ev.FILE_CAP + 64 * 1024


def test_enospc_is_recorded_and_the_rest_is_still_attempted_tc1299(tmp_path):
    attempts = []

    def writer(path, data):
        attempts.append(os.path.basename(path))
        if os.path.basename(path) in ("top.txt", "dmesg.txt"):
            raise OSError(errno.ENOSPC, "No space left on device")
        ev._default_writer(path, data)

    runner = lambda argv, t, cap, env=None: _Done(b"z\n")  # noqa: E731
    m = ev.capture(
        str(tmp_path / "ev"), 1, _plan(tmp_path), {}, runner=runner, writer=writer
    )
    status = {i["name"]: i["status"] for i in m["items"]}
    assert status["top.txt"] == "write-failed: No space left on device"
    assert status["docker-stats.txt"] == "ok" and "manifest.json" in attempts
    assert m["complete"] is False


def test_pruning_keeps_twenty_and_the_total_cap_and_never_the_new_one_tc1300():
    bundles = [("20261001T0000%02dZ" % i, i, 10) for i in range(25)]
    delete = ev.plan_prune(bundles, "20261001T000024Z", 20, 10**9)
    assert delete == ["20261001T0000%02dZ" % i for i in range(5)]
    delete = ev.plan_prune(bundles, "20261001T000000Z", 20, 50)
    assert "20261001T000000Z" not in delete and len(delete) == 20, (
        "five bundles of 10 fit a cap of 50"
    )
    # Ordered by capture sequence, not name: a clock jump cannot make the newest look oldest.
    jumped = [("19700101T000000Z", 30, 10)] + bundles
    assert "19700101T000000Z" not in ev.plan_prune(jumped, "", 20, 10**9)


def test_failed_pruning_is_recorded_and_the_capture_goes_on_tc1300(tmp_path):
    root = tmp_path / "ev"
    for i in range(22):
        _w(root / ("20261001T0000%02dZ" % i) / "manifest.json", json.dumps({"seq": i}))

    def remover(path):
        raise OSError(errno.EACCES, "Permission denied")

    runner = lambda argv, t, cap, env=None: _Done(b"z\n")  # noqa: E731
    m = ev.capture(str(root), 30, _plan(tmp_path), {}, runner=runner, remover=remover)
    assert m["prune"]["errors"] and m["prune"]["deleted"] == []
    assert (root / m["bundle"] / "processes.txt").exists()


def test_a_planted_token_is_redacted_before_it_is_written_tc1301(tmp_path):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    secret = "sk-" + "x" * 30

    def runner(argv, timeout, cap, env=None):
        return _Done(("Oct 22 svc[1]: using %s and Authorization: Bearer abcdef123456789 password=hunter22x %s\n"
                      % (token, secret)).encode())  # fmt: skip

    m = ev.capture(str(tmp_path / "ev"), 1, _plan(tmp_path), {}, runner=runner)
    text = (tmp_path / "ev" / m["bundle"] / "journal-units.txt").read_text(
        encoding="utf-8"
    )
    assert (
        token not in text
        and secret not in text
        and "hunter22x" not in text
        and "abcdef123456789" not in text
    )
    assert "[REDACTED:github-token]" in text and "password=[REDACTED]" in text
    assert {i["name"]: i["redactions"] for i in m["items"]}["journal-units.txt"] == 4


def test_redact_covers_the_privacy_scanners_secrets_floor_tc1301():
    samples = ["-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
               "github_pat_" + "a" * 30, "xoxb-" + "1" * 12, "AKIA" + "A" * 16, "sk-" + "b" * 30,
               "eyJabcdefghij.eyJabcdefghij.abcdefghijkl"]  # fmt: skip
    for s in samples:
        out, n = mc.redact("before %s after" % s)
        assert n == 1 and s not in out, s
    assert mc.redact("an ordinary line")[1] == 0


def test_liveness_main_writes_one_bundle_per_incident_then_alerts_tc1302(
    tmp_path, monkeypatch
):
    sent, captures = [], []
    monkeypatch.setattr(live.mc, "send_alert", lambda key, prio, title, *a, **k: sent.append((key, prio, title))
                        or mc.ALERT_DELIVERED)  # fmt: skip
    monkeypatch.setattr(live.ev, "capture", lambda root, seq, plan, checks: captures.append(seq)
                        or {"path": root + "/b%d" % seq, "complete": True, "prune": {"errors": []}})  # fmt: skip
    state = {"https": False}
    monkeypatch.setattr(live, "check_dns", lambda s, n: (True, "ok"))
    monkeypatch.setattr(live, "check_https", lambda h, p, n: (state["https"], "https"))
    monkeypatch.setattr(live, "check_docker", lambda: (True, "ok"))
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))
    _w(tmp_path / "env", "HOMEHUB_LIVENESS_FAILURES=3\n")
    args = ["--state-dir", str(tmp_path / "st"), "--env-file", str(tmp_path / "env"), "--evidence-dir",
            str(tmp_path / "ev")]  # fmt: skip
    for _ in range(6):
        assert live.main(args) == 0
    assert captures == [1], "exactly one bundle for the incident"
    assert [(k, p) for k, p, _ in sent] == [("liveness", "urgent")]
    state["https"] = True
    live.main(args)
    assert [k for k, _, _ in sent] == ["liveness", "liveness.clear"]
    state["https"] = False
    for _ in range(3):
        live.main(args)
    assert captures == [1, 2], "a new incident gets its own bundle"


def test_no_monitor_acts_on_the_box_tc1302():
    """Record and alert only: every systemctl/docker argv in the monitors is a
    query, and no code string names a reboot, a shutdown or a kill verb."""
    import ast

    for f in ("homehub-liveness.py", "homehub_evidence.py", "homehub-watch.py", "homehub-disk-check.py",
              "homehub-telemetry.py", "monitor_common.py"):  # fmt: skip
        tree = ast.parse((MON / f).read_text(encoding="utf-8"))
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                      and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}  # fmt: skip
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.List)
                and node.elts
                and isinstance(node.elts[0], ast.Constant)
            ):
                head = node.elts[0].value
                if head in ("systemctl", "docker") and len(node.elts) > 1:
                    # `inspect` is a query too (homehub-watch's restart counts).
                    assert node.elts[1].value in ("show", "ps", "stats", "inspect"), (
                        f,
                        head,
                        node.elts[1].value,
                    )
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                assert node.value not in ("restart", "stop", "kill", "reboot", "shutdown", "poweroff",
                                          "--force-recreate", "isolate"), (f, node.value)  # fmt: skip
                for phrase in (
                    "systemctl restart",
                    "systemctl stop",
                    "docker kill",
                    "docker restart",
                ):
                    assert phrase not in node.value, (f, node.value)


# ── wiring: units, timers, firstboot, knobs (TC-1303, TC-1304) ───────────────

UNITS = {
    "homehub-telemetry": "30s",
    "homehub-disk-check": "15min",
    "homehub-watch": "5min",
    "homehub-liveness": "60s",
}


def test_units_are_bounded_oneshots_and_timers_have_their_cadence_tc1303():
    for name, every in UNITS.items():
        svc = _live((MON / (name + ".service")).read_text(encoding="utf-8"))
        tim = _live((MON / (name + ".timer")).read_text(encoding="utf-8"))
        assert "Type=oneshot" in svc
        assert (
            "ExecStart=/usr/bin/python3 /opt/homehub/stack/monitoring/%s.py" % name
            in svc
        )
        for directive in ("CPUQuota=", "MemoryMax=", "TimeoutStartSec=", "TasksMax="):
            assert re.search("^" + directive + r"\S+$", svc, re.M), (name, directive)
        assert "OnUnitActiveSec=%s" % every in tim and "WantedBy=timers.target" in tim
        assert "Restart=" not in svc, "a monitor is a timer, never a respawning daemon"
    tel_svc = (MON / "homehub-telemetry.service").read_text(encoding="utf-8")
    assert "StateDirectory=homehub-telemetry" in tel_svc
    assert "homehub-evidence" in (MON / "homehub-liveness.service").read_text(
        encoding="utf-8"
    )


def test_monitoring_files_are_lf_and_scripts_have_shebangs_tc1303():
    for p in MON.iterdir():
        if p.is_file():
            assert b"\r" not in p.read_bytes(), p.name
    for name in (
        "homehub-telemetry.py",
        "homehub-disk-check.py",
        "homehub-watch.py",
        "homehub-liveness.py",
    ):
        assert (MON / name).read_bytes().startswith(b"#!/usr/bin/env python3\n"), name


def test_firstboot_installs_every_unit_and_converges_each_knob_tc1304():
    src = FIRSTBOOT.read_text(encoding="utf-8")
    block = src[src.index("# BEGIN hub-monitoring") : src.index("# END hub-monitoring")]
    for name in UNITS:
        for kind in ("service", "timer"):
            line = 'install -m 0644 "$_mon/%s.%s"' % (name, kind)
            assert line in block and "/etc/systemd/system/%s.%s" % (name, kind) in block
    for timer, knob in (("homehub-telemetry.timer", "HOMEHUB_TELEMETRY_ENABLED"),
                        ("homehub-disk-check.timer", "HOMEHUB_DISK_CHECK_ENABLED"),
                        ("homehub-watch.timer", "HOMEHUB_WATCH_ENABLED"),
                        ("homehub-liveness.timer", "HOMEHUB_LIVENESS_ENABLED")):  # fmt: skip
        assert re.search(r"_mon_converge %s +%s\n" % (re.escape(timer), knob), block)
    assert 'case "${_v:-true}" in' in block, "an unset knob means on"
    assert "systemctl disable --now" in block and "systemctl enable --now" in block
    assert src.index("# BEGIN hub-monitoring") < src.index("# ── 7. done")


@needs_bash
@pytest.mark.parametrize(
    "value,want",
    [("", "enable"), ("true", "enable"), ("false", "disable"), ("yes", "none")],
)
def test_the_firstboot_converger_obeys_its_knob_tc1304(tmp_path, value, want):
    """Run the block's converger for real, against a stub systemctl."""
    src = FIRSTBOOT.read_text(encoding="utf-8")
    start = src.index("    _mon_converge() {")
    end = src.index('    log "hub monitoring (SN-048', start)
    fn = src[start:end]
    calls = tmp_path / "calls"
    script = ('log() { echo "$*"; }\n_env_val() { sed -n "s/^$1=//p" "%s" | head -1; }\n'
              'systemctl() { echo "$*" >>"%s"; }\n%s\n_mon_converge homehub-watch.timer HOMEHUB_WATCH_ENABLED\n'
              % ((tmp_path / "env").as_posix(), calls.as_posix(), fn))  # fmt: skip
    _w(
        tmp_path / "env",
        ("HOMEHUB_WATCH_ENABLED=%s\n" % value) if value else "OTHER=1\n",
    )
    _w(tmp_path / "t.sh", script)
    r = subprocess.run(
        [_bash, str(tmp_path / "t.sh")], capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, r.stderr
    got = calls.read_text(encoding="utf-8") if calls.exists() else ""
    if want == "none":
        assert got == "" and "is not true or false" in r.stdout
    else:
        assert got.startswith(want + " --now homehub-watch.timer"), got


def test_env_example_declares_every_monitoring_knob_tc1304():
    env_keys = set(
        re.findall(r"^([A-Z][A-Z0-9_]*)=", ENVEX.read_text(encoding="utf-8"), re.M)
    )
    env_comment = ENVEX.read_text(encoding="utf-8")
    read = set()
    for f in MON.glob("*.py"):
        read |= set(
            re.findall(r'"(HOMEHUB_[A-Z0-9_]+)"', f.read_text(encoding="utf-8"))
        )
    read |= set(re.findall(r"\$\{(HOMEHUB_[A-Z0-9_]+)", TABLE))
    process_env = {"HOMEHUB_MONITOR_STATE_DIR", "HOMEHUB_MONITOR_ENV_FILE", "HOMEHUB_ALERT_HELPER",
                   "HOMEHUB_MONITOR_SYS_ROOT", "HOMEHUB_MONITOR_PROC_ROOT", "HOMEHUB_TELEMETRY_DIR",
                   "HOMEHUB_EVIDENCE_DIR", "HOMEHUB_WATCH_TABLE", "HOMEHUB_MONITOR_OVERRIDE_FILE",
                   "HOMEHUB_ALERT_BASH"}  # fmt: skip
    missing = sorted(
        k
        for k in read - process_env
        if k not in env_keys and "# %s=" % k not in env_comment
    )
    assert missing == []
    for k in ("HOMEHUB_TELEMETRY_ENABLED", "HOMEHUB_DISK_CHECK_ENABLED", "HOMEHUB_WATCH_ENABLED",
              "HOMEHUB_LIVENESS_ENABLED"):  # fmt: skip
        assert re.search("^%s=true$" % k, ENVEX.read_text(encoding="utf-8"), re.M)


# ── delivery budget: record first, alerts within the unit's timeout (SR-085, LLR-1168) ──

HOT = dict(cpu_c=99, nvme_c=99, mhz=(400, 400), throttle=(5, 5),
           stat_line="cpu  1100 0 1100 810 0 0 0 0 0 0\n")  # fmt: skip
RULE_KEYS = ["telemetry-cpu-temp", "telemetry-nvme-temp", "telemetry-throttle", "telemetry-freq-floor"]


class _FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _tel_rig(tmp_path, monkeypatch):
    """telemetry main() over a fake machine where sample 2 raises all four alarms at once."""
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))
    _w(tmp_path / "env", "HOMEHUB_THERMAL_SAMPLES=1\nHOMEHUB_FREQ_SAMPLES=1\nLAN_IP=192.0.2.10\nNTFY_PORT=8081\n")
    data, st = tmp_path / "data", tmp_path / "st"

    def run(helper=None, **machine):
        sysr, procr = _fake_machine(tmp_path / "m", **machine)
        argv = ["--data-dir", str(data), "--state-dir", str(st), "--env-file", str(tmp_path / "env"),
                "--sys-root", str(sysr), "--proc-root", str(procr)]  # fmt: skip
        if helper:
            argv += ["--alert-helper", str(helper)]
        return tel.main(argv + ["sample"])

    def lines():
        files = sorted(data.glob("telemetry-*.jsonl"))
        return files[0].read_text(encoding="utf-8").splitlines() if files else []

    def episodes():
        p = st / "telemetry-state.json"
        return json.loads(p.read_text(encoding="utf-8")).get("episodes", {}) if p.exists() else {}

    return run, lines, episodes


def _pending(eps, keys):
    return all(eps.get(k, {}).get("active") is True and eps[k].get("fired") is False for k in keys)


def _hung_helper(tmp_path):
    """An alert helper that records the call and then never returns (ntfy hung)."""
    calls = tmp_path / "calls"
    hung = _w(tmp_path / "hung.sh", 'echo "$@" >>"%s"\nexec sleep 30\n' % calls.as_posix())
    return hung, calls


def test_alert_budget_defers_an_alert_without_running_the_helper_tc1313(tmp_path):
    clock = _FakeClock()
    budget = mc.AlertBudget(10, clock=clock)
    assert budget.remaining() == 10
    clock.t += 10 - mc.MIN_ALERT_SEC + 0.5
    got = mc.send_alert("k", "high", "t", "b", "/env", str(tmp_path), helper=str(tmp_path / "absent.sh"),
                        budget=budget)  # fmt: skip
    assert got == mc.ALERT_DEFERRED, "too little left: not attempted, and not a delivery"
    eps = {}
    item = mc.plan_episode(eps, "k", True, FIRE, CLEAR)
    assert eps["k"] == {"active": True, "fired": False, "clear_pending": False}, "planned = stored as pending"
    assert mc.deliver_pending(eps, [item], lambda *a: mc.ALERT_DEFERRED) == 1
    assert eps["k"]["fired"] is False, "a deferred alert stays pending for the next run"


@needs_bash
def test_send_alert_cuts_the_helper_to_the_budget_and_stops_it_there_tc1313(tmp_path, monkeypatch):
    import time as _t

    hung, calls = _hung_helper(tmp_path)
    monkeypatch.setenv("HOMEHUB_ALERT_BASH", _bash)
    t0 = _t.monotonic()
    got = mc.send_alert("k", "high", "t", "b", "/env", str(tmp_path / "st"), helper=str(hung),
                        budget=mc.AlertBudget(5))  # fmt: skip
    took = _t.monotonic() - t0
    assert got == mc.ALERT_FAILED and took < 8, took
    assert re.search(r"--timeout [12]( |$)", calls.read_text(encoding="utf-8")), (
        "curl's --max-time is cut to what the budget leaves"
    )


def test_telemetry_records_and_saves_every_alert_pending_before_sending_tc1313(tmp_path, monkeypatch):
    run, lines, episodes = _tel_rig(tmp_path, monkeypatch)
    seen = []

    def sender(key, *a, **k):
        seen.append((key, len(lines()), episodes()))
        return mc.ALERT_FAILED

    monkeypatch.setattr(tel.mc, "send_alert", sender)
    assert run() == 0 and seen == []
    assert run(**HOT) == 0
    assert [k for k, _, _ in seen] == RULE_KEYS
    _, n_lines, saved = seen[0]
    assert n_lines == 2, "the sample is on disk before the first alert is attempted"
    assert json.loads(lines()[-1])["alarms"] == ["cpu-temp", "freq-floor", "nvme-temp", "throttle"]
    assert _pending(saved, RULE_KEYS), "every owed alert is saved as pending before the first attempt"
    assert _pending(seen[-1][2], RULE_KEYS[-1:]) and seen[-1][2][RULE_KEYS[0]]["fired"] is False
    assert _pending(episodes(), RULE_KEYS), "undelivered: all four still pending"
    monkeypatch.setattr(tel.mc, "send_alert", lambda key, *a, **k: seen.append((key, 0, {})) or mc.ALERT_DELIVERED)
    assert run(**HOT) == 0
    assert [k for k, _, _ in seen[4:]] == RULE_KEYS, "the next run retries all four"
    assert all(episodes()[k]["fired"] for k in RULE_KEYS)


@needs_bash
def test_simultaneous_alarms_and_a_hung_ntfy_fit_the_telemetry_deadline_tc1313(tmp_path, monkeypatch, capsys):
    """The real send_alert, a helper that hangs, four alarms at once: the run ends
    at its deadline (scaled down to 6 s here) with the sample and every alert
    saved as pending, instead of being killed by its unit with nothing saved."""
    import time as _t

    hung, calls = _hung_helper(tmp_path)
    good = _w(tmp_path / "good.sh", 'echo "stub delivered"\nexit 0\n')
    monkeypatch.setenv("HOMEHUB_ALERT_BASH", _bash)
    monkeypatch.setattr(tel, "ALERT_DEADLINE_SEC", 6)
    run, lines, episodes = _tel_rig(tmp_path, monkeypatch)
    assert run(helper=hung) == 0
    t0 = _t.monotonic()
    assert run(helper=hung, **HOT) == 0
    took = _t.monotonic() - t0
    out = capsys.readouterr().out
    assert took < 6 + 3, "the run must end at its delivery deadline (took %.1fs)" % took
    assert len(lines()) == 2 and json.loads(lines()[-1])["alarms"]
    assert _pending(episodes(), RULE_KEYS)
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1, "one attempt, then the budget is spent"
    assert out.count("ALERT DEFERRED") == 3 and "stopped after" in out
    assert run(helper=good, **HOT) == 0
    assert all(episodes()[k]["fired"] for k in RULE_KEYS), "delivered on the next run"


@needs_bash
def test_many_unhealthy_containers_and_a_hung_ntfy_fit_the_watch_deadline_tc1313(tmp_path, monkeypatch, capsys):
    import time as _t

    hung, calls = _hung_helper(tmp_path)
    # The helper copies the state file as it stands when the first alert goes out.
    hung = _w(tmp_path / "hung-snap.sh", 'cp "$SNAP_SRC" "$SNAP_DST" 2>/dev/null\nexec bash "%s" "$@"\n' % hung.as_posix())
    monkeypatch.setenv("SNAP_SRC", str(tmp_path / "st" / "watch-state.json"))
    monkeypatch.setenv("SNAP_DST", str(tmp_path / "snap.json"))
    monkeypatch.setenv("HOMEHUB_ALERT_BASH", _bash)
    monkeypatch.setattr(watch, "ALERT_DEADLINE_SEC", 6)
    names = ["c%d" % i for i in range(8)]
    monkeypatch.setattr(watch, "docker_unhealthy", lambda: list(names))
    monkeypatch.setattr(watch, "systemctl_show", lambda unit: _show(exit_ts=2_000_000_000))
    clock = {"mono": 100.0}
    monkeypatch.setattr(watch.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(watch.time, "time", lambda: 2_000_000_100.0)
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))
    _w(tmp_path / "env", "LAN_IP=192.0.2.10\nNTFY_PORT=8081\n")
    _w(tmp_path / "proc/sys/kernel/random/boot_id", "b1\n")
    args = ["--state-dir", str(tmp_path / "st"), "--env-file", str(tmp_path / "env"), "--proc-root",
            str(tmp_path / "proc"), "--alert-helper", str(hung)]  # fmt: skip
    assert watch.main(args) == 0
    clock["mono"] = 100.0 + 11 * 60
    t0 = _t.perf_counter()
    assert watch.main(args) == 0
    took = _t.perf_counter() - t0
    keys = ["watch-containers-" + n for n in names]
    saved = json.loads((tmp_path / "st" / "watch-state.json").read_text(encoding="utf-8"))
    assert took < 6 + 3, took
    assert _pending(saved["episodes"], keys), "eight unhealthy containers, all saved as pending"
    snap = json.loads((tmp_path / "snap.json").read_text(encoding="utf-8"))
    assert _pending(snap["episodes"], keys), "saved as pending BEFORE the first alert was attempted"
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1
    assert capsys.readouterr().out.count("ALERT DEFERRED") == 7
    assert (tmp_path / "st" / "watch-status.json").exists()


def test_every_monitor_runs_its_alerts_on_one_budget_that_fits_its_unit_tc1313(tmp_path, monkeypatch):
    """Each monitor builds ONE AlertBudget at the start of main() and passes it to
    every send; its deadline plus the save reserve fits the unit's TimeoutStartSec;
    and the disk and liveness state is on disk, pending, before the first send."""
    import time as _t

    for module, unit in ((tel, "homehub-telemetry"), (disk, "homehub-disk-check"),
                         (watch, "homehub-watch"), (live, "homehub-liveness")):  # fmt: skip
        svc = (MON / (unit + ".service")).read_text(encoding="utf-8")
        limit = int(re.search(r"^TimeoutStartSec=(\d+)$", svc, re.M).group(1))
        assert 0 < module.ALERT_DEADLINE_SEC and module.ALERT_DEADLINE_SEC + mc.SAVE_RESERVE_SEC <= limit, unit

    def capture(module, state_file, into):
        def sender(key, prio, title, body, env_file, state_dir, **k):
            p = tmp_path / "st" / state_file
            into.append((key, k.get("budget"), json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}))
            return mc.ALERT_FAILED

        monkeypatch.setattr(module.mc, "send_alert", sender)

    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))
    got = []
    capture(disk, "disk-state.json", got)
    monkeypatch.setattr(disk.mc, "is_nvme_backed", lambda mm, root: True)
    monkeypatch.setattr(disk.os, "statvfs", lambda p: type("S", (), {"f_blocks": 100, "f_frsize": 10**8,
                                                                    "f_bavail": 1})(), raising=False)  # fmt: skip
    _w(tmp_path / "proc/self/mountinfo", "29 1 253:0 / / rw - ext4 /dev/vg rw\n31 29 259:2 / /boot rw - ext4 /dev/n rw\n")
    _w(tmp_path / "env", 'HOMEHUB_DISK_MOUNTS="/ /boot"\n')
    t0 = _t.monotonic()
    disk.main(["--state-dir", str(tmp_path / "st"), "--env-file", str(tmp_path / "env"),
               "--proc-root", str(tmp_path / "proc")])  # fmt: skip
    assert [k for k, _, _ in got] == ["disk-low-root", "disk-low-boot"]
    assert got[0][1] is got[1][1] and isinstance(got[0][1], mc.AlertBudget), "one budget for the run"
    assert abs(got[0][1].deadline - (t0 + disk.ALERT_DEADLINE_SEC)) < 2
    assert _pending(got[0][2]["episodes"], ["disk-low-root", "disk-low-boot"])

    got = []
    capture(live, "liveness-state.json", got)
    monkeypatch.setattr(live.ev, "capture", lambda root, seq, plan, checks: {"path": root + "/b", "complete": True,
                                                                            "prune": {"errors": []}})  # fmt: skip
    monkeypatch.setattr(live, "check_dns", lambda s, n: (False, "down"))
    monkeypatch.setattr(live, "check_https", lambda h, p, n: (True, "ok"))
    monkeypatch.setattr(live, "check_docker", lambda: (True, "ok"))
    _w(tmp_path / "env", "HOMEHUB_LIVENESS_FAILURES=1\n")
    live.main(["--state-dir", str(tmp_path / "st"), "--env-file", str(tmp_path / "env"),
               "--evidence-dir", str(tmp_path / "ev")])  # fmt: skip
    assert [k for k, _, _ in got] == ["liveness"] and isinstance(got[0][1], mc.AlertBudget)
    assert _pending(got[0][2]["episodes"], ["liveness"]) and got[0][2]["incident"]["incident"]
