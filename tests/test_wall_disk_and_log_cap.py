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

ENV_FILE = "/etc/wall-panel/wall-disk.env"


def block():
    text = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    found = re.search(r"# BEGIN log-cap-and-disk-alarm\n(.*?)# END log-cap-and-disk-alarm\n", text, re.S)
    assert found, "wall-firstboot.sh lost its log-cap-and-disk-alarm block"
    return found.group(1)


def run_block(tmp_path, *, journald_same=False, fail_install="", enable_status=0,
              inactive="", divert_state=None, divert_fails=False, media="/var/cache/wall-media"):
    """Run the real block under `set -euo pipefail` with every external command
    replaced by a recording shell function (a function outranks any binary).

    fail_install: substring of an install's arguments that makes it fail.
    inactive:     a unit name `systemctl is-active` reports inactive.
    divert_state: what `dpkg-divert --truename` answers, persisted in a file so
                  two runs share it (None = not diverted yet).
    The generated env file is redirected into tmp_path; `cat`/`chmod` are real.
    """
    calls = tmp_path / "calls"
    state = tmp_path / "divert-state"
    if divert_state is not None:
        state.write_text(divert_state + "\n", encoding="utf-8")
    env_file = (tmp_path / "wall-disk.env").as_posix()
    rec = 'printf "%s\\n" "{kind} $*" >> "' + calls.as_posix() + '"'
    script = "\n".join([
        "set -euo pipefail",
        'PAYLOAD="{}"'.format(WALL.as_posix()),
        "WALL_MEDIA_CACHE='{}'".format(media),
        'log() { echo "[wall-firstboot] $*"; }',
        'warn() { echo "[wall-firstboot] WARNING: $*" >&2; }',
        'fail_step() { echo "[wall-firstboot] ERROR: $*" >&2; %s; }' % rec.format(kind="fail_step"),
        'install() { %s; case "$*" in *"%s"*) [ -n "%s" ] && return 1 ;; esac; return 0; }'
        % (rec.format(kind="install"), fail_install or "\x01", fail_install),
        'cmp() { return %d; }' % (0 if journald_same else 1),
        ('dpkg-divert() {\n'
         '    if [ "$1" = --truename ]; then\n'
         '        if [ -f "STATE" ]; then cat "STATE"; else echo "$2"; fi; return 0\n'
         '    fi\n'
         '    RECORD\n'
         '    FAIL\n'
         '    echo /etc/logrotate.rsyslog.distrib > "STATE"\n'
         '}').replace("STATE", state.as_posix()).replace("RECORD", rec.format(kind="dpkg-divert"))
        .replace("FAIL", "return 1" if divert_fails else ":"),
        'systemctl() { %s; [ "$1" = is-active ] && [ "$3" = "%s" ] && return 3; return 0; }'
        % (rec.format(kind="systemctl"), inactive or "\x01"),
        'enable_unit() { shift; %s; [ %d = 0 ] || { fail_step "systemctl enable FAILED for: $*"; return 1; }; }'
        % (rec.format(kind="enable_unit"), enable_status),
        'enable_unit_now() { shift; %s; return 0; }' % rec.format(kind="enable_unit_now"),
        block().replace(ENV_FILE, env_file),
        "echo BLOCK-DONE",
    ]) + "\n"
    done = subprocess.run([BASH, "-s"], input=script, capture_output=True, timeout=60,
                          env=dict(os.environ), text=True, encoding="utf-8", errors="replace")
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    env_path = tmp_path / "wall-disk.env"
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else None
    return done, lines, env_text


INSTALLS = [
    "install -m 0644 {p}/wall-logrotate-rsyslog /etc/logrotate.d/rsyslog",
    "install -m 0644 {p}/wall-logrotate-hourly.conf /etc/systemd/system/logrotate.timer.d/60-wall-hourly.conf",
    "install -m 0644 {p}/wall-journald.conf /etc/systemd/journald.conf.d/10-wall-cap.conf",
    "install -m 0755 {p}/wall-disk-check.py /usr/local/sbin/wall-disk-check.py",
    "install -m 0644 {p}/wall-disk-check.service /etc/systemd/system/wall-disk-check.service",
    "install -m 0644 {p}/wall-disk-check.timer /etc/systemd/system/wall-disk-check.timer",
]
DIVERT_ADD = ("--local --rename --divert /etc/logrotate.rsyslog.distrib "
              "--add /etc/logrotate.d/rsyslog")
TAIL = [
    "systemctl daemon-reload",
    "systemctl restart systemd-journald",
    "enable_unit logrotate.timer",
    "systemctl restart logrotate.timer",
    "systemctl is-active --quiet logrotate.timer",
    "enable_unit wall-disk-check.timer",
    "systemctl restart wall-disk-check.timer",
    "systemctl is-active --quiet wall-disk-check.timer",
]


def _ok(done):
    # Exit 0 under `set -e` AND the line after the block ran: a failure inside
    # the block must never stop the steps after it.
    assert done.returncode == 0 and "BLOCK-DONE" in done.stdout, done.stdout + done.stderr


def _fails(calls):
    return [c for c in calls if c.startswith("fail_step")]


@needs_bash
def test_tc1264_firstboot_diverts_installs_and_starts_both_timers(tmp_path):
    done, calls, env_text = run_block(tmp_path)
    _ok(done)
    p = WALL.as_posix()
    # The stock stanza is diverted BEFORE ours takes its name.
    assert calls.index("dpkg-divert " + DIVERT_ADD) < calls.index(INSTALLS[0].format(p=p))
    assert [c for c in calls if c.startswith("install -m")] == [i.format(p=p) for i in INSTALLS]
    assert _fails(calls) == []
    assert [c for c in calls if c.startswith(("systemctl", "enable_unit"))] == TAIL
    assert "WALL_DISK_MEDIA_PATH=/var/cache/wall-media" in env_text.splitlines()


@needs_bash
def test_tc1264_the_divert_is_idempotent_across_runs(tmp_path):
    first, calls1, _ = run_block(tmp_path)
    _ok(first)
    (tmp_path / "calls").unlink()
    second, calls2, _ = run_block(tmp_path)
    _ok(second)
    assert calls1.count("dpkg-divert " + DIVERT_ADD) == 1
    # The second run finds the diversion in place and adds nothing.
    assert not any(c.startswith("dpkg-divert") for c in calls2), calls2
    assert INSTALLS[0].format(p=WALL.as_posix()) in calls2 and _fails(calls2) == []


@needs_bash
@pytest.mark.parametrize("kwargs", [{"divert_state": "/etc/somewhere-else"}, {"divert_fails": True}])
def test_tc1264_no_diversion_means_the_conffile_is_not_overwritten(tmp_path, kwargs):
    done, calls, _ = run_block(tmp_path, **kwargs)
    _ok(done)
    assert not any(c.endswith(" /etc/logrotate.d/rsyslog") and c.startswith("install") for c in calls)
    assert len(_fails(calls)) == 1 and "could not divert" in _fails(calls)[0]
    # Everything after it still happened.
    assert [c for c in calls if c.startswith(("systemctl", "enable_unit"))] == TAIL


@needs_bash
def test_tc1264_a_failing_install_is_reported_and_later_steps_still_run(tmp_path):
    done, calls, _ = run_block(tmp_path, fail_install="wall-disk-check.py")
    _ok(done)
    assert len(_fails(calls)) == 1 and "could not install wall-disk-check.py" in _fails(calls)[0]
    p = WALL.as_posix()
    assert [c for c in calls if c.startswith("install -m")] == [i.format(p=p) for i in INSTALLS]
    assert [c for c in calls if c.startswith(("systemctl", "enable_unit"))] == TAIL


@needs_bash
def test_tc1264_a_failing_enable_is_reported_and_later_steps_still_run(tmp_path):
    done, calls, _ = run_block(tmp_path, enable_status=1)
    _ok(done)
    assert [c for c in _fails(calls)] == ["fail_step systemctl enable FAILED for: logrotate.timer",
                                          "fail_step systemctl enable FAILED for: wall-disk-check.timer"]
    assert [c for c in calls if c.startswith(("systemctl", "enable_unit"))] == TAIL


@needs_bash
@pytest.mark.parametrize("unit", ["logrotate.timer", "wall-disk-check.timer"])
def test_tc1264_a_timer_that_is_not_active_after_restart_is_a_failed_step(tmp_path, unit):
    done, calls, _ = run_block(tmp_path, inactive=unit)
    _ok(done)
    assert len(_fails(calls)) == 1 and unit + " is not active" in _fails(calls)[0]


@needs_bash
def test_tc1264_the_checker_measures_the_cache_path_firstboot_resolved(tmp_path):
    done, calls, env_text = run_block(tmp_path, media="/srv/wall-cache")
    _ok(done)
    assert "WALL_DISK_MEDIA_PATH=/srv/wall-cache" in env_text.splitlines()
    assert _fails(calls) == []


@needs_bash
@pytest.mark.parametrize("media", ["/srv/wall cache", "relative/cache"])
def test_tc1264_an_unusable_cache_path_is_refused_not_written(tmp_path, media):
    done, calls, env_text = run_block(tmp_path, media=media)
    _ok(done)
    assert env_text is None
    assert len(_fails(calls)) == 1 and "WALL_MEDIA_CACHE" in _fails(calls)[0]


@needs_bash
def test_tc1264_journald_is_not_restarted_when_its_drop_in_is_unchanged(tmp_path):
    done, calls, _ = run_block(tmp_path, journald_same=True)
    _ok(done)
    assert "systemctl restart systemd-journald" not in calls


def test_tc1264_every_install_line_keeps_the_form_the_release_manifest_reads():
    """The manifest generator reads `install -m MODE "$PAYLOAD/x" DEST` and skips
    any line that says fail_step, so a guard written as `|| fail_step` on the
    install line would drop the file from the release in silence."""
    for line in block().splitlines():
        if re.search(r"\binstall\s+-m\s", line):
            assert "fail_step" not in line, line
            assert re.search(r'install -m [0-7]{4} "\$PAYLOAD/[^"]+" /\S+ \|\| logcap_failed ', line), line


# ── TC-1262 (cont.): the media cache path comes from firstboot ───────────────

@pytest.mark.parametrize("environ, media", [
    ({}, "/var/cache/wall-media"),
    ({"WALL_DISK_MEDIA_PATH": "  "}, "/var/cache/wall-media"),
    ({"WALL_DISK_MEDIA_PATH": "/srv/wall-cache"}, "/srv/wall-cache"),
])
def test_tc1262_default_paths_follow_the_resolved_cache(environ, media):
    assert disk.default_paths(environ) == (("root", "/"), ("media", media))


def test_tc1262_main_measures_a_non_default_cache_from_the_environment(tmp_path, monkeypatch):
    cache = tmp_path / "elsewhere"
    cache.mkdir()
    seen = {}

    def run_df(paths, df_command="df"):
        seen["paths"] = paths
        return fake_df(("/", 98 * GB, 40 * GB), (str(cache), 120 * GB, 3 * GB))

    monkeypatch.setenv("WALL_DISK_MEDIA_PATH", str(cache))
    monkeypatch.setattr(disk, "run_df", run_df)
    status = tmp_path / "status.json"
    assert disk.main(["--status", str(status)]) == 0
    assert seen["paths"] == ["/", str(cache)]
    doc = json.loads(status.read_text(encoding="utf-8"))
    assert [(m["label"], m["path"], m["low"]) for m in doc["mounts"]] == [
        ("root", "/", False), ("media", str(cache), True)]


def test_tc1263_the_service_reads_the_generated_cache_path():
    service = _directives(WALL / "wall-disk-check.service")
    assert "EnvironmentFile=-/etc/wall-panel/wall-disk.env" in service
