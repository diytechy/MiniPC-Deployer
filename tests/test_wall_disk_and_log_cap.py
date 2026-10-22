"""SR-083: the panel's classic syslog is capped, journald's cap is explicit, and
a low-disk alarm reports root and the media cache.

Three layers, each against the real artefact rather than a description of it:

* TC-1262 -- wall-disk-check.py's pure core (parse_df / threshold_bytes /
  evaluate / journal_line) against FAKE df OUTPUT, and main() end to end with
  df replaced in-process, asserting the status file it actually wrote.
* TC-1263 -- the shipped config files themselves: the logrotate stanza, the
  logrotate.timer drop-in (list reset before the new value), the journald
  drop-in and the two units.
* TC-1264 -- the firstboot block, cut out of wall-firstboot.sh between its
  BEGIN/END markers and EXECUTED in bash with install/cmp/systemctl and the
  enable helpers replaced by recording shell functions. Needs bash.

WHAT THIS DOES NOT PROVE. Nothing here runs logrotate, journald or systemd.
That the drop-in really makes logrotate.timer hourly, and that the units load,
was checked by hand under WSL systemd 255 when this was written
(`systemd-analyze verify`, `logrotate -d`); on the panel it is proven by the
paired release's own verification and `systemctl list-timers`.

Implements tests: TC-1262, TC-1263, TC-1264.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parents[1] / "stack" / "autoinstall" / "wall"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")

GB = 10**9


def _load():
    spec = importlib.util.spec_from_file_location("wall_disk_check", WALL / "wall-disk-check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


disk = _load()


def fake_df(*rows):
    """df -B1 --output=target,size,avail output for (mount, size, avail) rows."""
    lines = ["Mounted on        1B-blocks        Avail"]
    lines += ["%-16s %12d %12d" % row for row in rows]
    return "\n".join(lines) + "\n"


# ── TC-1262: the threshold logic, against fake df output ─────────────────────

def test_tc1262_parse_df_reads_mount_size_and_avail_in_order():
    text = fake_df(("/", 15 * GB, 4 * GB), ("/var/cache/wall-media", 120 * GB, 60 * GB))
    assert disk.parse_df(text) == [
        {"mount": "/", "totalBytes": 15 * GB, "freeBytes": 4 * GB},
        {"mount": "/var/cache/wall-media", "totalBytes": 120 * GB, "freeBytes": 60 * GB},
    ]


def test_tc1262_parse_df_keeps_a_mount_point_with_spaces():
    text = "Mounted on 1B-blocks Avail\n/mnt/a b 100 7\n"
    assert disk.parse_df(text) == [{"mount": "/mnt/a b", "totalBytes": 100, "freeBytes": 7}]


@pytest.mark.parametrize("line", ["/ 100 -", "/ 1.5G 2G", "/ 100"])
def test_tc1262_parse_df_refuses_output_it_does_not_understand(line):
    # A df whose output changed must not be read as "0 bytes free".
    with pytest.raises(ValueError):
        disk.parse_df("Mounted on 1B-blocks Avail\n" + line + "\n")


@pytest.mark.parametrize("total, expected", [
    (15 * GB, int(1.5 * GB)),     # a small root: the 10 % arm is smaller
    (50 * GB, 5 * GB),            # exactly where the two arms meet
    (120 * GB, 5 * GB),           # the media cache: the 5 GB arm is smaller
    (0, 0),
])
def test_tc1262_threshold_is_the_smaller_of_ten_percent_and_five_gb(total, expected):
    assert disk.threshold_bytes(total) == expected


def test_tc1262_root_below_ten_percent_is_low_and_media_above_five_gb_is_not():
    records = disk.parse_df(fake_df(("/", 15 * GB, 1 * GB), ("/var/cache/wall-media", 120 * GB, 6 * GB)))
    results = disk.evaluate((("root", "/"), ("media", "/var/cache/wall-media")), records)
    assert [(r["label"], r["low"]) for r in results] == [("root", True), ("media", False)]
    assert results[0]["thresholdBytes"] == int(1.5 * GB)
    assert results[1]["thresholdBytes"] == 5 * GB


def test_tc1262_media_below_five_gb_is_low_even_at_three_percent_used():
    records = disk.parse_df(fake_df(("/", 15 * GB, 8 * GB), ("/var/cache/wall-media", 120 * GB, 4 * GB)))
    results = disk.evaluate((("root", "/"), ("media", "/var/cache/wall-media")), records)
    assert [(r["label"], r["low"]) for r in results] == [("root", False), ("media", True)]


def test_tc1262_exactly_at_the_threshold_is_not_low():
    records = disk.parse_df(fake_df(("/", 15 * GB, int(1.5 * GB))))
    assert disk.evaluate((("root", "/"),), records)[0]["low"] is False
    records = disk.parse_df(fake_df(("/", 15 * GB, int(1.5 * GB) - 1)))
    assert disk.evaluate((("root", "/"),), records)[0]["low"] is True


def test_tc1262_a_media_cache_still_on_root_is_reported_once():
    # No separate LV yet: df resolves the cache path to "/" and the numbers are
    # root's. Reporting them twice would double every alarm.
    records = disk.parse_df(fake_df(("/", 15 * GB, 1 * GB), ("/", 15 * GB, 1 * GB)))
    results = disk.evaluate((("root", "/"), ("media", "/var/cache/wall-media")), records)
    assert [r["label"] for r in results] == ["root"]


def test_tc1262_evaluate_refuses_a_record_count_that_does_not_match():
    with pytest.raises(ValueError):
        disk.evaluate((("root", "/"), ("media", "/m")), disk.parse_df(fake_df(("/", 1, 1))))


def test_tc1262_the_journal_line_is_greppable_and_carries_its_priority():
    low = disk.evaluate((("root", "/"),), disk.parse_df(fake_df(("/", 15 * GB, 1 * GB))))[0]
    ok = disk.evaluate((("root", "/"),), disk.parse_df(fake_df(("/", 15 * GB, 8 * GB))))[0]
    assert disk.journal_line(low) == (
        "<4>wall-disk: LOW root mount=/ free=1000000000 total=15000000000 "
        "threshold=1500000000 freePct=6.7")
    assert disk.journal_line(ok).startswith("<6>wall-disk: ok root mount=/ free=8000000000 ")


def test_tc1262_main_writes_the_status_file_and_logs_each_mount(tmp_path, monkeypatch, capsys):
    media = tmp_path / "media"
    media.mkdir()
    status = tmp_path / "run" / "status.json"
    seen = {}

    def run_df(paths, df_command="df"):
        seen["paths"] = paths
        return fake_df(("/", 15 * GB, 1 * GB), (str(media), 120 * GB, 60 * GB))

    monkeypatch.setattr(disk, "run_df", run_df)
    code = disk.main(["--path", "root=" + str(tmp_path), "--path", "media=" + str(media),
                      "--status", str(status)])
    assert code == 0
    assert seen["paths"] == [str(tmp_path), str(media)]
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("<4>wall-disk: LOW root mount=/ ")
    assert out[1].startswith("<6>wall-disk: ok media mount=%s " % media)
    doc = json.loads(status.read_text(encoding="utf-8"))
    assert doc["schemaVersion"] == 1 and doc["ttlMs"] == 30 * 60 * 1000 and doc["low"] is True
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", doc["observedAt"])
    assert [(m["path"], m["freeBytes"], m["totalBytes"], m["low"]) for m in doc["mounts"]] == [
        (str(tmp_path), 1 * GB, 15 * GB, True), (str(media), 60 * GB, 120 * GB, False)]
    # Replaced atomically: no temporary is left beside it.
    assert sorted(p.name for p in status.parent.iterdir()) == ["status.json"]


def test_tc1262_a_missing_path_is_skipped_not_passed_to_df(tmp_path, monkeypatch):
    seen = {}

    def run_df(paths, df_command="df"):
        seen["paths"] = paths
        return fake_df(("/", 15 * GB, 8 * GB))

    monkeypatch.setattr(disk, "run_df", run_df)
    code = disk.main(["--path", "root=" + str(tmp_path), "--path", "media=" + str(tmp_path / "absent"),
                      "--status", str(tmp_path / "status.json")])
    assert code == 0 and seen["paths"] == [str(tmp_path)]


def test_tc1262_a_failed_df_exits_1_and_leaves_the_previous_status(tmp_path, monkeypatch, capsys):
    status = tmp_path / "status.json"
    status.write_text('{"previous": true}\n', encoding="utf-8")

    def run_df(paths, df_command="df"):
        raise RuntimeError("df exited 1: boom")

    monkeypatch.setattr(disk, "run_df", run_df)
    assert disk.main(["--path", "root=" + str(tmp_path), "--status", str(status)]) == 1
    assert "<4>wall-disk: ERROR could not measure free space" in capsys.readouterr().out
    assert json.loads(status.read_text(encoding="utf-8")) == {"previous": True}


# ── TC-1263: the shipped configuration ───────────────────────────────────────

def _directives(path):
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def test_tc1263_logrotate_caps_syslog_daily_at_100m_keeping_seven():
    lines = _directives(WALL / "wall-logrotate-rsyslog")
    body = lines[lines.index("{") + 1:lines.index("}")]
    for directive in ("daily", "maxsize 100M", "rotate 7", "compress", "delaycompress",
                      "missingok", "notifempty", "sharedscripts"):
        assert directive in body, directive
    assert "weekly" not in body and not any(d.startswith("size ") for d in body)
    # The stock postrotate, unchanged.
    assert body[body.index("postrotate") + 1] == "/usr/lib/rsyslog/rsyslog-rotate"
    # The stock file list: replacing the stock file must not drop any log it rotated.
    assert lines[:lines.index("{")] == ["/var/log/syslog", "/var/log/mail.log", "/var/log/kern.log",
                                        "/var/log/auth.log", "/var/log/user.log", "/var/log/cron.log"]


def test_tc1263_the_logrotate_timer_drop_in_resets_the_list_before_going_hourly():
    lines = _directives(WALL / "wall-logrotate-hourly.conf")
    timer = lines[lines.index("[Timer]") + 1:]
    # OnCalendar is a list: without the empty assignment the stock `daily`
    # stays armed beside `hourly` (systemd-dropin-directives-accumulate).
    assert timer.index("OnCalendar=") < timer.index("OnCalendar=hourly")
    assert [d for d in timer if d.startswith("OnCalendar")] == ["OnCalendar=", "OnCalendar=hourly"]
    assert not any(d.startswith(("Persistent", "WantedBy")) for d in lines)


def test_tc1263_journald_cap_is_explicit():
    lines = _directives(WALL / "wall-journald.conf")
    assert lines[0] == "[Journal]"
    assert "SystemMaxUse=500M" in lines and "SystemKeepFree=2G" in lines


def test_tc1263_disk_check_units():
    service = _directives(WALL / "wall-disk-check.service")
    for directive in ("Type=oneshot", "ExecStart=/usr/bin/python3 /usr/local/sbin/wall-disk-check.py",
                      "RuntimeDirectory=wall-disk", "RuntimeDirectoryMode=0755",
                      # A oneshot stops when the script exits; without this the
                      # status file is deleted every run.
                      "RuntimeDirectoryPreserve=yes", "SyslogIdentifier=wall-disk"):
        assert directive in service, directive
    assert not any(d.startswith("RemainAfterExit") for d in service)
    timer = _directives(WALL / "wall-disk-check.timer")
    for directive in ("OnBootSec=2min", "OnUnitActiveSec=15min", "Unit=wall-disk-check.service",
                      "WantedBy=timers.target"):
        assert directive in timer, directive
    # The status file's TTL is two timer periods.
    assert disk.DEFAULT_TTL_MS == 2 * 15 * 60 * 1000
    assert disk.DEFAULT_STATUS == "/run/wall-disk/status.json"


def test_tc1263_new_files_are_lf_and_the_script_is_executable_in_git():
    for name in ("wall-disk-check.py", "wall-disk-check.service", "wall-disk-check.timer",
                 "wall-logrotate-rsyslog", "wall-logrotate-hourly.conf", "wall-journald.conf"):
        assert b"\r" not in (WALL / name).read_bytes(), name
    out = subprocess.run(["git", "-C", str(WALL), "ls-files", "-s", "wall-disk-check.py"],
                         capture_output=True, text=True, check=True).stdout
    assert out.startswith("100755"), out


# ── TC-1264: the firstboot block, executed ───────────────────────────────────

def block():
    text = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    found = re.search(r"# BEGIN log-cap-and-disk-alarm\n(.*?)# END log-cap-and-disk-alarm\n", text, re.S)
    assert found, "wall-firstboot.sh lost its log-cap-and-disk-alarm block"
    return found.group(1)


def run_block(tmp_path, payload, journald_same=False, enable_status=0):
    calls = tmp_path / "calls"
    record = 'printf "%s\\n" "$*" >> "{}"'.format(calls.as_posix())
    script = "\n".join([
        "set -euo pipefail",
        'PAYLOAD="{}"'.format(Path(payload).as_posix()),
        'log() { echo "[wall-firstboot] $*"; }',
        'warn() { echo "[wall-firstboot] WARNING: $*" >&2; }',
        'fail_step() { echo "[wall-firstboot] ERROR: $*" >&2; %s; }' % record.replace('"$*"', '"fail_step $*"'),
        'install() { %s; }' % record.replace('"$*"', '"install $*"'),
        'cmp() { return %d; }' % (0 if journald_same else 1),
        'systemctl() { %s; }' % record.replace('"$*"', '"systemctl $*"'),
        'enable_unit() { shift; %s; return %d; }' % (record.replace('"$*"', '"enable_unit $*"'), enable_status),
        'enable_unit_now() { shift; %s; return %d; }' % (record.replace('"$*"', '"enable_unit_now $*"'), enable_status),
        block(),
        "echo BLOCK-DONE",
    ]) + "\n"
    done = subprocess.run([BASH, "-s"], input=script, capture_output=True, timeout=60,
                          env=dict(os.environ), text=True, encoding="utf-8", errors="replace")
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return done, lines


INSTALLS = [
    "install -m 0644 {p}/wall-logrotate-rsyslog /etc/logrotate.d/rsyslog",
    "install -m 0644 {p}/wall-logrotate-hourly.conf /etc/systemd/system/logrotate.timer.d/60-wall-hourly.conf",
    "install -m 0644 {p}/wall-journald.conf /etc/systemd/journald.conf.d/10-wall-cap.conf",
    "install -m 0755 {p}/wall-disk-check.py /usr/local/sbin/wall-disk-check.py",
    "install -m 0644 {p}/wall-disk-check.service /etc/systemd/system/wall-disk-check.service",
    "install -m 0644 {p}/wall-disk-check.timer /etc/systemd/system/wall-disk-check.timer",
]


@needs_bash
def test_tc1264_firstboot_installs_everything_and_enables_both_timers(tmp_path):
    done, calls = run_block(tmp_path, WALL)
    assert done.returncode == 0 and "BLOCK-DONE" in done.stdout, done.stdout + done.stderr
    p = WALL.as_posix()
    assert [c for c in calls if c.startswith("install -m")] == [i.format(p=p) for i in INSTALLS]
    assert not any(c.startswith("fail_step") for c in calls), calls
    rest = [c for c in calls if not c.startswith("install")]
    # The journald restart happens only because cmp said the drop-in changed,
    # and only after the daemon-reload that makes the timer drop-in live.
    assert rest == [
        "systemctl daemon-reload",
        "systemctl restart systemd-journald",
        "enable_unit logrotate.timer",
        "systemctl try-restart logrotate.timer",
        "enable_unit_now wall-disk-check.timer",
    ], rest


@needs_bash
def test_tc1264_journald_is_not_restarted_when_its_drop_in_is_unchanged(tmp_path):
    done, calls = run_block(tmp_path, WALL, journald_same=True)
    assert done.returncode == 0, done.stderr
    assert "systemctl restart systemd-journald" not in calls


@needs_bash
def test_tc1264_a_missing_payload_file_is_a_failed_step_not_an_abort(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    for name in ("wall-logrotate-rsyslog", "wall-logrotate-hourly.conf", "wall-journald.conf",
                 "wall-disk-check.service", "wall-disk-check.timer"):
        shutil.copy(WALL / name, payload / name)
    done, calls = run_block(tmp_path, payload)
    assert done.returncode == 0 and "BLOCK-DONE" in done.stdout, done.stderr
    failures = [c for c in calls if c.startswith("fail_step")]
    assert len(failures) == 1 and "wall-disk-check.py is missing" in failures[0]
    assert not any("wall-disk-check.py /usr/local/sbin" in c for c in calls)
    assert "install -m 0644 {}/wall-logrotate-rsyslog /etc/logrotate.d/rsyslog".format(
        payload.as_posix()) in calls
