"""Tests for the hub metrics log (SN-048; the Owner's rulings 2026-10-26, HomeHub
open-items A79): the day-file retention helpers, the 5-minute snapshot, the
daily row, the panel's `status` verb and its two sources, the raw watch record
in the heart feeder, the chart page's site, the weekly copy into the library,
and the wiring.

Verifies: SR-122, SR-123, SR-124, LLR-1321 .. LLR-1332
Cases:    TC-1494 .. TC-1510

Hermetic: tmp_path trees, injected runners and fetchers, a fake zone (the dev
PC's Python 3.8 has no zoneinfo), a loopback HTTP server for the tracker.
"""

import gzip
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

import pytest

from conftest import loopback_server

ROOT = Path(__file__).resolve().parents[1]
MON = ROOT / "stack" / "monitoring"
WALL = ROOT / "stack" / "autoinstall" / "wall"
WEIGHT = ROOT / "stack" / "weight"
FIRSTBOOT = ROOT / "stack" / "autoinstall" / "firstboot.sh"
ENVEX = ROOT / "stack" / ".env.example"
CADDY = ROOT / "stack" / "caddy" / "Caddyfile"
COMPOSE = ROOT / "stack" / "docker-compose.yml"
sys.path.insert(0, str(MON))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mc = _load("monitor_common", MON / "monitor_common.py")
hm = _load("homehub_metrics", MON / "homehub-metrics.py")
spool = _load("wall_meeting_spool_metrics", WALL / "wall-meeting-spool.py")
meetings = _load("homehub_meetings_t", ROOT / "stack" / "meetings" / "homehub_meetings.py")
feeder = _load("weight_feeder_raw", WEIGHT / "weight_feeder.py")
ptel = _load("panel_telemetry_latest", WALL / "panel-telemetry.py")

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


def nth_sunday(year, month, nth):
    first = datetime(year, month, 1)
    return 1 + (6 - first.weekday()) % 7 + 7 * (nth - 1)


class CentralTime(tzinfo):
    def _is_dst(self, naive):
        start = datetime(naive.year, 3, nth_sunday(naive.year, 3, 2), 2)
        end = datetime(naive.year, 11, nth_sunday(naive.year, 11, 1), 1)
        return start <= naive < end

    def utcoffset(self, dt):
        return timedelta(hours=-5 if self._is_dst(dt.replace(tzinfo=None)) else -6)

    def dst(self, dt):
        return timedelta(hours=1 if self._is_dst(dt.replace(tzinfo=None)) else 0)

    def tzname(self, dt):
        return "CDT" if self._is_dst(dt.replace(tzinfo=None)) else "CST"


CT = CentralTime()


def L(month, day, hour, minute=0, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=CT).timestamp()


class Done:
    def __init__(self, rc=0, output=b"", timed_out=False, error=None):
        self.rc, self.output, self.timed_out, self.error, self.truncated = rc, output, timed_out, error, False

    @property
    def ok(self):
        return self.error is None and not self.timed_out and self.rc == 0


class Runner:
    """Answers argv by its first words; records every call."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def __call__(self, argv, timeout, max_bytes, env=None):
        self.calls.append(list(argv))
        for prefix, done in self.answers.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return done
        return Done(rc=1)


# ── TC-1494: day files - retention, compression, the active file (LLR-1321) ──

def test_tc1494_plan_keeps_365_days_compresses_after_7_and_never_the_active_one():
    days = ["2025%04d" % n for n in range(101, 132)] + ["2026%04d" % n for n in range(101, 132)]
    files = [("snapshot-%s.jsonl" % d, 10) for d in days]
    active = files[-1][0]
    delete, compress = mc.plan_day_files(files, "snapshot", active, 40, 7, 10 ** 9)
    assert len(delete) == len(days) - 40 and active not in delete
    assert sorted(delete) == delete[:len(delete)] and delete[0] == "snapshot-20250101.jsonl"
    kept = [n for n, _ in files if n not in delete]
    assert compress == kept[:-7] and active not in compress


def test_tc1494_a_finished_gzip_wins_and_a_torn_one_is_deleted():
    files = [("snapshot-20261001.jsonl", 5), ("snapshot-20261001.jsonl.gz", 2),
             ("snapshot-20261002.jsonl.gz.tmp", 1), ("snapshot-20261026.jsonl", 5), ("other.txt", 1)]
    delete, compress = mc.plan_day_files(files, "snapshot", "snapshot-20261026.jsonl", 365, 7, 10 ** 9)
    assert sorted(delete) == ["snapshot-20261001.jsonl", "snapshot-20261002.jsonl.gz.tmp"]
    assert compress == []


def test_tc1494_the_byte_cap_deletes_oldest_first_but_never_the_active_day():
    files = [("snapshot-2026100%d.jsonl" % d, 100) for d in range(1, 6)]
    delete, _ = mc.plan_day_files(files, "snapshot", "snapshot-20261005.jsonl", 365, 7, 250)
    assert delete == ["snapshot-20261001.jsonl", "snapshot-20261002.jsonl", "snapshot-20261003.jsonl"]
    delete, _ = mc.plan_day_files(files[-1:], "snapshot", "snapshot-20261005.jsonl", 365, 7, 1)
    assert delete == []


def test_tc1494_compression_is_deterministic_keeps_the_mtime_and_reads_back(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        _w(d / "snapshot-20261001.jsonl", '{"epoch": 1, "v": 1}\nnot json\n{"epoch": 2}\n')
        os.utime(str(d / "snapshot-20261001.jsonl"), (1790000000, 1790000000))
    for d in (a, b):
        ok, _ = mc.compress_day_file(str(d), "snapshot-20261001.jsonl")
        assert ok and not (d / "snapshot-20261001.jsonl").exists()
    ga, gb = a / "snapshot-20261001.jsonl.gz", b / "snapshot-20261001.jsonl.gz"
    assert ga.read_bytes() == gb.read_bytes()
    assert int(ga.stat().st_mtime) == 1790000000
    assert [r["epoch"] for r in mc.iter_jsonl(str(ga))] == [1, 2]


def test_tc1494_enforce_compresses_at_most_two_per_run(tmp_path):
    for d in range(1, 13):
        _w(tmp_path / ("snapshot-202610%02d.jsonl" % d), "{}\n")
    notes = mc.enforce_day_files(str(tmp_path), "snapshot", "snapshot-20261012.jsonl", 365, 7, 10 ** 9)
    assert sum(n.startswith("compressed") for n in notes) == 2
    assert sorted(p.name for p in tmp_path.glob("*.gz")) == ["snapshot-20261001.jsonl.gz",
                                                             "snapshot-20261002.jsonl.gz"]


def test_tc1494_append_refuses_past_the_cap(tmp_path):
    path = str(tmp_path / "d" / "x.jsonl")
    assert mc.append_jsonl(path, {"a": 1}, 100)[0]
    ok, why = mc.append_jsonl(path, {"a": "x" * 200}, 100)
    assert not ok and "cap" in why


# ── TC-1495: gauges, as the real user only (LLR-1322) ────────────────────────

def test_tc1495_a_blank_or_odd_user_or_a_remote_url_asks_nothing():
    calls = []

    def fetch(*a):
        calls.append(a)
        return {}, "ok"

    dl = hm.Deadline(60)
    for knobs in ({}, {"HOMEHUB_METRICS_USER": "  "}, {"HOMEHUB_METRICS_USER": "a b"},
                  {"HOMEHUB_METRICS_USER": "1", "HOMEHUB_METRICS_TRACKER_URL": "https://tracker.example/api/today"},
                  {"HOMEHUB_METRICS_USER": "1", "HOMEHUB_METRICS_TRACKER_URL": "file:///etc/passwd"}):
        out = hm.collect_gauges(knobs, dl, fetch)
        assert out["ok"] is False and out["error"]
    assert calls == []


def test_tc1495_the_request_carries_the_user_and_the_token_and_is_compacted():
    payload = {"items": [{"id": "a", "done": True}, {"id": "b", "done": False}], "delinquent": True,
               "revision": 7, "gauges": [
                   {"id": "heart", "label": "Heart", "unit": "bpm", "value": 61, "min": 49, "max": 73,
                    "target": 61, "pace": 61, "deviation": 0.0, "css": "#00ff00", "stale": False,
                    "trend": None, "observed_at": "2026-10-26T11:00:00Z", "window": {"kind": "daily"}},
                   {"id": "weight", "value": 180.1, "min": 1, "max": 2, "target": 1, "stale": True}]}
    seen = {}

    def respond(handler):
        seen.update(dict(handler.headers))
        raw = json.dumps(payload).encode()
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    with loopback_server(respond) as (base, _):
        out = hm.collect_gauges({"HOMEHUB_METRICS_USER": "112233", "TRACKER_API_TOKEN": "T",
                                 "HOMEHUB_METRICS_TRACKER_URL": base + "/api/today"}, hm.Deadline(60))
    assert seen["X-Forwarded-User"] == "112233" and seen["Authorization"] == "Bearer T"
    assert out["ok"] and out["items"] == 2 and out["done"] == 1 and out["delinquent"] is True
    heart, weight = out["list"]
    assert heart["window"] == "daily" and heart["stale"] is False and "label" not in heart
    assert weight["stale"] is True and "observed_at" not in weight


def test_tc1495_an_error_status_is_recorded_without_its_body():
    def respond(handler):
        handler.send_response(403)
        handler.send_header("Content-Length", "6")
        handler.end_headers()
        handler.wfile.write(b"secret")

    with loopback_server(respond) as (base, _):
        out = hm.collect_gauges({"HOMEHUB_METRICS_USER": "1", "HOMEHUB_METRICS_TRACKER_URL": base + "/x"},
                                hm.Deadline(60))
    assert out == {"ok": False, "error": "/api/today: HTTP 403 (body not kept)"}


# ── TC-1496: host, containers, units, monitors, SMART (LLR-1323) ────────────

class _Tel:
    @staticmethod
    def cpu_temp(rec):
        return rec.get("c")

    @staticmethod
    def nvme_temp(rec):
        return rec.get("n")


def test_tc1496_the_telemetry_tail_is_summarised_and_an_old_one_flagged(tmp_path):
    path = _w(tmp_path / "telemetry-20261026.jsonl",
              '{"epoch": 100, "c": 40}\n{"epoch": 1000, "c": 51.5, "n": 38, "cpu_mhz": [800, 2400],'
              ' "load": [0.5, 0.4, 0.3], "fs": [{"mount": "/", "used_pct": 64}]}\n{"torn')
    rec = hm.read_last_line(str(path))
    assert rec["epoch"] == 1000
    out = hm.summarise_telemetry(rec, 1060, _Tel)
    assert out["ok"] and out["cpu_c"] == 51.5 and out["mhz_max"] == 2400 and out["fs"] == {"/": 64}
    stale = hm.summarise_telemetry(rec, 1000 + hm.TELEMETRY_MAX_AGE_SEC + 5, _Tel)
    assert stale["ok"] is False and "old" in stale["error"]


def test_tc1496_containers_and_units_are_parsed_and_only_queried():
    ids = Done(output=b"a" * 64 + b"\n" + b"b" * 64 + b"\n")
    inspect = Done(output=b"/tracker\trunning\t0\thealthy\n/immich-server\trestarting\t12\t\n/bad name\tx\t1\n")
    failed = Done(output=b"fstrim.service loaded failed failed Discard\n")
    ours = Done(output=b"homehub-weight.service loaded inactive dead x\nhomehub-sleep.service loaded active running y\n")
    show = Done(output=b"Id=homehub-weight.service\nActiveState=inactive\nResult=success\nExecMainStatus=0\n"
                       b"ExecMainExitTimestamp=@1793064000\nNRestarts=0\n\nId=homehub-sleep.service\n"
                       b"ActiveState=failed\nResult=exit-code\nExecMainStatus=1\nExecMainExitTimestamp=\nNRestarts=3\n")
    run = Runner({("docker", "ps"): ids, ("docker", "inspect"): inspect,
                  ("systemctl", "list-units", "--state=failed"): failed,
                  ("systemctl", "list-units", "--all"): ours, ("systemctl", "show"): show})
    dl = hm.Deadline(60)
    c = hm.collect_containers(dl, run)
    assert c["ok"] and [r["name"] for r in c["list"]] == ["immich-server", "tracker"]
    assert c["list"][0] == {"name": "immich-server", "state": "restarting", "restarts": 12}
    u = hm.collect_units(dl, run)
    assert u["failed"] == ["fstrim.service"]
    assert u["services"] == {"weight": {"a": "inactive", "r": "success", "x": 0, "t": 1793064000},
                             "sleep": {"a": "failed", "r": "exit-code", "x": 1, "n": 3}}
    for argv in run.calls:
        assert argv[1] in ("ps", "inspect", "list-units", "show"), argv


def test_tc1496_a_dead_docker_costs_its_block_only():
    run = Runner({("docker", "ps"): Done(timed_out=True)})
    assert hm.collect_containers(hm.Deadline(60), run) == {"ok": False, "error": "docker ps did not answer"}


def test_tc1496_monitor_files_become_verdicts_only(tmp_path):
    _w(tmp_path / "watch-status.json", json.dumps({"ts": 900, "rows": [
        {"name": "containers", "verdict": "ok", "detail": "secret detail"},
        {"name": "library-backup", "verdict": "FIRING", "detail": "x"}]}))
    _w(tmp_path / "disk-status.json", json.dumps({"ts": 950, "mounts": [{"label": "root", "state": "ok"}]}))
    _w(tmp_path / "liveness-status.json", json.dumps({"ts": 990, "ok": True, "incident": False}))
    meet = _w(tmp_path / "meetings.json", json.dumps({"ts": 999, "exit": 0}))
    out = hm.collect_monitors(str(tmp_path), str(meet), 1000)
    assert out["watch"] == {"age_s": 100, "rows": {"containers": "ok", "library-backup": "FIRING"}}
    assert out["disk"]["mounts"] == {"root": "ok"} and out["liveness"]["ok"] is True
    assert out["meeting_pull"] == {"age_s": 1, "exit": 0}
    assert "secret" not in json.dumps(out)


def test_tc1496_smart_is_hourly_and_reads_the_nvme_log_only(tmp_path):
    smart = Done(rc=4, output=json.dumps({"smart_status": {"passed": True}, "nvme_smart_health_information_log": {
        "temperature": 41, "percentage_used": 3, "media_errors": 0, "serial": "NOPE"}}).encode())
    run = Runner({("smartctl",): smart, ("docker", "ps"): Done(output=b"")})
    state = {}
    args = _args(tmp_path)
    rec = hm.build_snapshot(args, {}, state, 1793000000.0, hm.Deadline(60), run=run, meetings=False)
    assert rec["smart"] == {"ok": True, "passed": True, "temperature": 41, "percentage_used": 3, "media_errors": 0}
    assert [c for c in run.calls if c[0] == "smartctl"] == [["smartctl", "-j", "-H", "-A", "/dev/nvme0"]]
    rec = hm.build_snapshot(args, {}, state, 1793000300.0, hm.Deadline(60), run=run, meetings=False)
    assert "smart" not in rec
    rec = hm.build_snapshot(args, {}, state, 1793000000.0 + hm.SMART_EVERY_SEC, hm.Deadline(60), run=run, meetings=False)
    assert "smart" in rec


def _args(tmp_path):
    proc = tmp_path / "proc"
    _w(proc / "meminfo", "MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    _w(proc / "uptime", "1234.5 99\n")
    _w(proc / "sys/kernel/random/boot_id", "boot1\n")

    class A:
        pass

    a = A()
    a.proc_root, a.telemetry_dir, a.monitor_dir = str(proc), str(tmp_path / "tel"), str(tmp_path / "mon")
    a.meetings_status, a.data_dir = str(tmp_path / "m.json"), str(tmp_path / "data")
    a.raw_dir, a.env_file = str(tmp_path / "raw"), str(tmp_path / "env")
    return a


# ── TC-1497: the panel's status over the meeting key (LLR-1324) ─────────────

PANEL_DOC = {"v": 1, "now": 1793064000, "uptime_s": 999,
             "disk": {"low": False, "observedAt": "2026-10-27T01:16:49Z",
                      "mounts": [{"label": "root", "freeBytes": 50, "totalBytes": 100}]},
             "freeze": {"ts": 1793063990, "result": "ok", "draw_s": 0.29, "fails": 0, "captured": 0},
             "freeze_bundles": {"count": 20, "newest": "20261022-172726"},
             "telemetry": {"ts_utc": "2026-10-27T01:20:00Z", "cpu_pct": 23.456,
                           "sensors": [{"deg_c": 48.0, "canonical": True}, {"deg_c": 99, "canonical": False}],
                           "cpufreq_mhz": [800, 1800], "kiosk": [{"name": "electron", "cpu_pct": 10.0, "rss_kb": 5}],
                           "presentation": {"status": "ok", "payload": {"mode": "FULLSCREEN"}}}}


def test_tc1497_the_argv_is_the_pulls_own_with_host_keys_never_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(meetings, "STATE_DIR", str(tmp_path))
    knobs = {"PANEL_IP": "192.0.2.17"}
    argv, why = hm.panel_argv(knobs, meetings)
    assert argv is None and "pinned" in why
    _w(tmp_path / "known_hosts", "x\n")
    argv, why = hm.panel_argv(knobs, meetings)
    assert argv[0] == "ssh" and argv[-2:] == ["wall-meeting@192.0.2.17", "status"]
    assert "StrictHostKeyChecking=yes" in argv and "StrictHostKeyChecking=accept-new" not in argv
    assert "BatchMode=yes" in argv
    assert hm.panel_argv(dict(knobs, HOMEHUB_METRICS_PANEL="false"), meetings)[0] is None
    assert hm.panel_argv({}, meetings)[0] is None
    assert hm.panel_argv(knobs, None)[0] is None


def test_tc1497_the_answer_is_summarised_and_failures_are_one_line(tmp_path, monkeypatch):
    monkeypatch.setattr(meetings, "STATE_DIR", str(tmp_path))
    _w(tmp_path / "known_hosts", "x\n")
    knobs = {"PANEL_IP": "192.0.2.17"}
    good = Runner({("ssh",): Done(output=(json.dumps(PANEL_DOC) + "\n").encode())})
    out = hm.collect_panel(knobs, hm.Deadline(60), meetings, good)
    assert out["ok"] and out["disk"]["free_pct"] == {"root": 50.0}
    assert out["freeze"] == {"ts": 1793063990, "result": "ok", "draw_s": 0.29, "fails": 0, "captured": 0,
                             "bundles": 20, "newest_bundle": "20261022-172726"}
    assert out["telemetry"]["cpu_c"] == 48.0 and out["telemetry"]["mode"] == "FULLSCREEN"
    assert out["telemetry"]["kiosk"] == {"electron": [10.0, 5]}
    for done, word in ((Done(timed_out=True), "did not answer"), (Done(rc=255), "exited 255"),
                       (Done(output=b"garbage"), "not JSON")):
        out = hm.collect_panel(knobs, hm.Deadline(60), meetings, Runner({("ssh",): done}))
        assert out["ok"] is False and word in out["error"]


# ── TC-1498: the snapshot end to end (SR-122, LLR-1321..1324) ───────────────

def test_tc1498_snapshot_writes_one_line_per_run_and_survives_every_block_failing(tmp_path):
    args = _args(tmp_path)
    _w(args.env_file, "HOMEHUB_METRICS_PANEL=false\n")
    run = Runner()
    assert hm.cmd_snapshot(args, run=run, meetings=False) == 0
    assert hm.cmd_snapshot(args, run=run, meetings=False) == 0
    files = list(Path(args.data_dir).glob("snapshot-*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines()]  # noqa: E741
    assert len(lines) == 2
    rec = lines[0]
    assert rec["gauges"]["ok"] is False and "HOMEHUB_METRICS_USER" in rec["gauges"]["error"]
    assert rec["containers"]["ok"] is False and rec["panel"] == {"ok": False, "error": "HOMEHUB_METRICS_PANEL=false"}
    assert rec["host"]["mem"] == {"total_mb": 7812, "avail_mb": 3906, "swap_used_mb": 0}
    assert rec["host"]["uptime_s"] == 1234 and rec["boot_id"] == "boot1"
    assert len(json.dumps(rec)) < 8192


def test_tc1498_a_snapshot_that_cannot_be_written_fails_the_unit(tmp_path, monkeypatch):
    args = _args(tmp_path)
    monkeypatch.setattr(mc, "append_jsonl", lambda *a: (False, "disk full"))
    assert hm.cmd_snapshot(args, run=Runner(), meetings=False) == 1


# ── TC-1499: the daily row - when, which kind, never doubled (LLR-1325) ─────

def test_tc1499_initial_from_ten_revision_from_1405_catch_up_writes_both():
    assert hm.due_kinds(L(10, 26, 9, 59), CT, []) == ("2026-10-26", [])
    assert hm.due_kinds(L(10, 26, 10, 0), CT, []) == ("2026-10-26", ["initial"])
    assert hm.due_kinds(L(10, 26, 14, 4), CT, [("2026-10-26", "initial")]) == ("2026-10-26", [])
    assert hm.due_kinds(L(10, 26, 14, 5), CT, [("2026-10-26", "initial")]) == ("2026-10-26", ["revision"])
    assert hm.due_kinds(L(10, 26, 15, 0), CT, []) == ("2026-10-26", ["initial", "revision"])
    done = [("2026-10-26", "initial"), ("2026-10-26", "revision")]
    assert hm.due_kinds(L(10, 26, 23, 0), CT, done) == ("2026-10-26", [])
    # The next morning before 10:00 nothing is back-filled for yesterday.
    assert hm.due_kinds(L(10, 27, 9, 0), CT, []) == ("2026-10-27", [])


def test_tc1499_the_local_day_holds_across_dst_and_utc_midnight():
    # 2026-11-01 is the fall-back day; 10:00 CST is 16:00 UTC.
    assert hm.due_kinds(L(11, 1, 10, 0), CT, [])[1] == ["initial"]
    assert hm.due_kinds(L(11, 1, 9, 59), CT, [])[1] == []
    # 20:00 local is already the next UTC day; the night is still the local one.
    assert hm.due_kinds(L(10, 26, 20, 0), CT, [])[0] == "2026-10-26"


def test_tc1499_the_timer_carries_the_codes_zone_and_times():
    tim = _live((MON / "homehub-metrics-daily.timer").read_text(encoding="utf-8"))
    cal = re.findall(r"^OnCalendar=(.+)$", tim, re.M)
    want = ["*-*-* %02d:%02d:00 %s" % (h, m, hm.DAILY_ZONE) for h, m in (hm.INITIAL_AT, hm.REVISION_AT)]
    assert cal == want and hm.DAILY_ZONE == "America/Chicago"
    assert hm.INITIAL_AT == (10, 0) and hm.REVISION_AT == (14, 5)
    assert re.search(r"^Persistent=true$", tim, re.M)


# ── TC-1500: the daily row's content (SR-123, LLR-1326) ─────────────────────

def _daily_env(tmp_path):
    args = _args(tmp_path)
    ws = _w(tmp_path / "weight-state.json", json.dumps({"heart-nights": {"2026-10-26": {
        "rhr": 61, "p25": 63.0, "mean": 66.1, "n": 90, "window": "session", "device": "FFit",
        "observed_at": 1793037600}}, "weight": {"x": 1}}))
    ss = _w(tmp_path / "sleep-state.json", json.dumps({"usage": {"2026-10-26": 412},
                                                       "nights": {"2026-10-26": {"score": 88, "observed_at": 5}}}))
    _w(args.env_file, "HOMEHUB_METRICS_USER=1\nWEIGHT_STATE_FILE=%s\nSLEEP_STATE_FILE=%s\n"
       % (ws.as_posix(), ss.as_posix()))
    return args


def _today_fetch(url, user, token, timeout):
    return {"items": [], "gauges": [
        {"id": "sleep", "value": 88, "unit": "", "observed_at": "2026-10-26T11:10:00Z", "stale": False},
        {"id": "heart", "value": 0, "stale": False},
        {"id": "weight", "value": 180, "observed_at": "2026-10-19T12:00:00Z", "stale": True}]}, "ok"


def test_tc1500_the_row_carries_every_gauge_the_nights_figures_and_what_is_missing(tmp_path, monkeypatch):
    args = _daily_env(tmp_path)
    tel = Path(args.telemetry_dir)
    lines = []
    for hour, c in ((21, 70.0), (23, 41.0), (2, 39.5), (7, 45.0), (9, 80.0)):
        day = 25 if hour >= 21 else 26
        lines.append(json.dumps({"epoch": L(10, day, hour), "c": c, "n": c - 5}))
    _w(tel / "telemetry-20261026.jsonl", "\n".join(lines) + "\n")
    monkeypatch.setattr(hm, "_telemetry_module", lambda: _Tel)
    show = Done(output=b"Id=homehub-backup.service\nActiveState=inactive\nResult=success\nExecMainStatus=0\n"
                       b"ExecMainExitTimestamp=@1793000000\n")
    rc = hm.cmd_daily(args, fetch=_today_fetch, run=Runner({("systemctl", "show"): show}), zone=CT,
                      now=L(10, 26, 10, 1))
    assert rc == 0
    rows = list(mc.iter_jsonl(os.path.join(args.data_dir, "daily.jsonl")))
    assert len(rows) == 1
    row = rows[0]
    assert row["night"] == "2026-10-26" and row["kind"] == "initial" and row["late"] is False
    assert row["scheduled"] == "10:00 America/Chicago"
    assert row["gauges"]["weight"] == {"value": 180, "observed_at": "2026-10-19T12:00:00Z", "stale": True}
    assert row["missing"] == ["heart", "weight"]
    assert row["heart_night"]["rhr"] == 61 and row["sleep"] == {"cpap_minutes": 412, "score": 88, "observed_at": 5}
    # 22:00 the evening before .. 08:00: the 21:00 and 09:00 samples are outside.
    assert row["overnight"]["cpu_c"] == [39.5, 45.0] and row["overnight"]["samples"] == 3
    assert row["backup"]["backup"]["r"] == "success"
    # The same run again writes nothing; 14:05 adds the revision; both stay.
    hm.cmd_daily(args, fetch=_today_fetch, run=Runner(), zone=CT, now=L(10, 26, 10, 30))
    hm.cmd_daily(args, fetch=_today_fetch, run=Runner(), zone=CT, now=L(10, 26, 16, 0))
    rows = list(mc.iter_jsonl(os.path.join(args.data_dir, "daily.jsonl")))
    assert [(r["kind"], r["late"]) for r in rows] == [("initial", False), ("revision", True)]


def test_tc1500_an_unreadable_tracker_is_recorded_not_skipped(tmp_path, monkeypatch):
    args = _daily_env(tmp_path)
    monkeypatch.setattr(hm, "_telemetry_module", lambda: None)
    hm.cmd_daily(args, fetch=lambda *a: (None, "URLError"), run=Runner(), zone=CT, now=L(10, 26, 10, 1))
    row = next(mc.iter_jsonl(os.path.join(args.data_dir, "daily.jsonl")))
    assert row["gauges_error"] == "/api/today: URLError" and "gauges" not in row
    assert row["heart_night"]["rhr"] == 61


# ── TC-1501: export and summary read gzip'd and plain alike (LLR-1321) ──────

def test_tc1501_export_reads_plain_and_gz_days_in_order(tmp_path):
    d = tmp_path / "data"
    _w(d / "snapshot-20261001.jsonl", json.dumps({"epoch": L(10, 1, 12), "ts": "a"}) + "\n")
    mc.compress_day_file(str(d), "snapshot-20261001.jsonl")
    _w(d / "snapshot-20261002.jsonl", json.dumps({"epoch": L(10, 2, 12), "ts": "b",
                                                  "gauges": {"ok": True, "list": [{"id": "x", "value": 1}]},
                                                  "units": {"failed": []}}) + "\n")
    recs = list(hm.iter_snapshots(str(d), None, None))
    assert [r["ts"] for r in recs] == ["a", "b"]
    assert hm.summarise(recs)[0].startswith("2 snapshots")
    assert list(hm.iter_snapshots(str(d), L(10, 2, 0), None))[0]["ts"] == "b"


# ── TC-1502: the panel's `status` verb (LLR-1331) ───────────────────────────

def test_tc1502_status_is_a_verb_and_takes_no_words():
    assert spool.parse("status") == ("status", [])
    assert spool.parse("status x") is None and spool.parse("status ../x") is None


def test_tc1502_status_reads_its_three_files_bounded_and_says_what_is_missing(tmp_path):
    disk = _w(tmp_path / "disk.json", json.dumps({"low": False}))
    big = _w(tmp_path / "tel.json", "x" * (spool.STATUS_MAX_BYTES + 10))
    bundles = tmp_path / "freeze"
    for name in ("20261022-140516", "20261022-172726", "junk"):
        (bundles / name).mkdir(parents=True)
    out = io.BytesIO()
    rc = spool.do_status(out, {"disk": str(disk), "freeze": str(tmp_path / "absent.json"), "telemetry": str(big)},
                         str(bundles), now=lambda: 1793064000)
    doc = json.loads(out.getvalue().decode())
    assert rc == 0 and doc["v"] == 1 and doc["now"] == 1793064000
    assert doc["disk"] == {"low": False} and doc["freeze"] == {"error": "absent"}
    assert "larger" in doc["telemetry"]["error"]
    assert doc["freeze_bundles"] == {"count": 2, "newest": "20261022-172726"}


def test_tc1502_the_shipped_sources_are_the_paths_their_writers_use():
    assert spool.STATUS_SOURCES["disk"] == "/run/wall-disk/status.json"
    unit = (WALL / "wall-panel-telemetry.service").read_text(encoding="utf-8")
    assert "RuntimeDirectory=wall-panel-telemetry\n" in unit and "RuntimeDirectoryMode=0755\n" in unit
    assert "PANEL_TELEMETRY_LATEST_PATH=%s\n" % spool.STATUS_SOURCES["telemetry"] in unit
    script = (WALL / "wall-freeze-watch.sh").read_text(encoding="utf-8")
    assert "STATUS_DIR=${STATUS_DIR:-%s}" % os.path.dirname(spool.STATUS_SOURCES["freeze"]) in script
    assert '"$STATUS_DIR/status.json"' in script


def test_tc1502_the_telemetry_copy_is_atomic_world_readable_and_bounded(tmp_path):
    path = tmp_path / "latest.json"
    assert ptel._write_latest(str(path), {"cpu_pct": 5, "ts_utc": "t"})
    assert json.loads(path.read_text()) == {"cpu_pct": 5, "ts_utc": "t"}
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o644
    assert not ptel._write_latest(str(path), {"x": "y" * 40000})
    assert json.loads(path.read_text())["cpu_pct"] == 5
    assert not ptel._write_latest("", {"a": 1})
    assert not ptel._write_latest(str(tmp_path / "no" / "dir" / "x.json"), {"a": 1})


@needs_bash
def test_tc1502_the_freeze_watchdog_writes_its_verdict(tmp_path):
    src = (WALL / "wall-freeze-watch.sh").read_text(encoding="utf-8")
    start = src.index("write_status() {")
    end = src.index("\n}\n", start) + 3
    script = ('STATUS_DIR=%s\nfails=1\ncaptured=0\nFAIL_THRESHOLD=2\nRECOVER=1\nmkdir -p "$STATUS_DIR"\n%s\n'
              'write_status ok 0.29\nwrite_status no-frame ""\n' % ((tmp_path / "st").as_posix(), src[start:end]))
    _w(tmp_path / "t.sh", script)
    r = subprocess.run([_bash, str(tmp_path / "t.sh")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    doc = json.loads((tmp_path / "st" / "status.json").read_text())
    assert doc["result"] == "no-frame" and doc["draw_s"] is None and doc["fails"] == 1 and doc["threshold"] == 2
    assert not list((tmp_path / "st").glob(".status.*"))


# ── TC-1503..TC-1505: the raw watch record in the heart feeder (LLR-1330) ───

def _hr_point(epoch, bpm="78", manufacturer="FFit"):
    return {"name": "users/SENTINEL-USER-ID/dataTypes/heart-rate/dataPoints/x",
            "dataSource": {"platform": "HEALTH_CONNECT", "recordingMethod": "ACTIVELY_MEASURED",
                           "device": {"manufacturer": manufacturer, "formFactor": "WATCH"}},
            "heartRate": {"beatsPerMinute": bpm, "sampleTime": {
                "physicalTime": datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "utcOffset": "-18000s"}}}


def _sleep_point(start, end):
    t = lambda e: datetime.utcfromtimestamp(e).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return {"name": "users/SENTINEL-USER-ID/dataTypes/sleep/dataPoints/y",
            "dataSource": {"platform": "HEALTH_CONNECT", "device": {"manufacturer": "FFit", "formFactor": "WATCH"}},
            "sleep": {"type": "STAGES", "interval": {"startTime": t(start), "endTime": t(end)},
                      "stages": [{"startTime": t(start), "endTime": t(start + 600), "type": "LIGHT",
                                  "createTime": "c", "updateTime": "u"}],
                      "summary": {"minutesAsleep": "241", "stagesSummary": [{"type": "LIGHT", "minutes": "166"}]},
                      "metadata": {"mainSleep": True, "processed": True, "externalId": "abc"}}}


T0 = int(L(10, 25, 3))


def test_tc1503_rows_keep_what_was_served_and_never_the_account_path():
    rows = feeder.raw_heart_rate_rows({"dataPoints": [_hr_point(T0), _hr_point(T0 + 300, bpm="0x"),
                                                      {"heartRate": {"sampleTime": {}}}, "junk"]})
    assert rows[0] == {"epoch": T0, "t": rows[0]["t"], "utc_offset": "-18000s", "bpm": 78, "device": "FFit",
                       "form": "WATCH", "platform": "HEALTH_CONNECT", "method": "ACTIVELY_MEASURED"}
    assert rows[1]["bpm"] is None and rows[1]["bpm_raw"] == "0x" and len(rows) == 2
    sleep = feeder.raw_sleep_rows({"dataPoints": [_sleep_point(T0, T0 + 4 * 3600)]})
    assert sleep[0]["stages"] == [[T0, T0 + 600, "LIGHT"]]
    assert sleep[0]["summary"] == {"minutesAsleep": 241, "stagesSummary": [{"type": "LIGHT", "minutes": 166}]}
    assert sleep[0]["metadata"]["externalId"] == "abc"
    assert "SENTINEL" not in json.dumps(rows + sleep)


def _raw_env(tmp_path, **extra):
    state, raw = tmp_path / "homehub-weight", tmp_path / "homehub-health-raw"
    state.mkdir(exist_ok=True)
    raw.mkdir(exist_ok=True)
    env = {"STATE_DIRECTORY": os.pathsep.join([str(state), str(raw)])}
    env.update(extra)
    return env, raw


def test_tc1504_each_sample_is_appended_once_into_its_utc_day(tmp_path, capsys):
    env, raw = _raw_env(tmp_path)
    assert feeder.resolve_state_root(env) == str(tmp_path / "homehub-weight")
    assert feeder.resolve_raw_root(env) == str(raw)
    late = int(L(10, 25, 20))                      # 01:00 UTC on the 26th
    batch = {"hr": feeder.raw_heart_rate_rows({"dataPoints": [_hr_point(T0), _hr_point(T0 + 300),
                                                              _hr_point(late), _hr_point(T0, bpm="80")]}),
             "sleep": feeder.raw_sleep_rows({"dataPoints": [_sleep_point(T0, T0 + 3600)]})}
    assert feeder.record_raw_cycle(env, batch, T0 + 86400) == []
    assert feeder.record_raw_cycle(env, batch, T0 + 86400) == []
    out = capsys.readouterr().out
    assert "+4 heart-rate samples, +1 sleep sessions" in out and "+0 heart-rate samples, +0 sleep" in out
    day1 = (raw / "hr-20261025.jsonl").read_text().splitlines()
    assert [json.loads(l)["bpm"] for l in day1] == [78, 80, 78]  # noqa: E741
    assert len((raw / "hr-20261026.jsonl").read_text().splitlines()) == 1
    assert len((raw / "sleep-20261025.jsonl").read_text().splitlines()) == 1
    # A revised session (a later end) is a new row; the old one stays.
    revised = {"sleep": feeder.raw_sleep_rows({"dataPoints": [_sleep_point(T0, T0 + 7200)]})}
    feeder.record_raw_cycle(env, revised, T0 + 86400)
    assert len((raw / "sleep-20261025.jsonl").read_text().splitlines()) == 2


def test_tc1504_far_future_and_ancient_rows_are_not_filed(tmp_path):
    env, raw = _raw_env(tmp_path)
    rows = {"hr": [{"epoch": T0 + 3 * 86400, "bpm": 1}, {"epoch": 5, "bpm": 1}, {"epoch": "x"}]}
    feeder.record_raw_cycle(env, rows, T0)
    assert list(raw.iterdir()) == []


def test_tc1505_off_unset_root_and_a_write_failure_never_reach_the_gauge(tmp_path, monkeypatch):
    env, raw = _raw_env(tmp_path, HEART_RAW_RECORD="false")
    assert feeder.record_raw_cycle(env, {"hr": [{"epoch": T0}]}, T0) == [] and list(raw.iterdir()) == []
    env["HEART_RAW_RECORD"] = "yes"
    assert "true or false" in feeder.record_raw_cycle(env, {"hr": []}, T0)[0]
    env = {"STATE_DIRECTORY": str(tmp_path / "homehub-weight")}
    assert "StateDirectory=homehub-health-raw" in feeder.record_raw_cycle(env, {"hr": []}, T0)[0]
    env, raw = _raw_env(tmp_path)
    monkeypatch.setattr(feeder, "record_raw", lambda *a: (_ for _ in ()).throw(OSError("full")))
    assert feeder.record_raw_cycle(env, {"hr": []}, T0) == [
        "raw record: could not write (OSError) - nothing more recorded this cycle"]
    assert feeder.record_raw_cycle(env, None, T0) == []


def test_tc1505_only_record_file_names_are_opened_and_a_linked_root_is_refused(tmp_path):
    env, raw = _raw_env(tmp_path)
    with pytest.raises(PermissionError):
        feeder._raw_open(str(raw), None, "../weight-state.json", os.O_RDONLY)
    with pytest.raises(PermissionError):
        feeder._raw_open(str(raw), None, "google-health-token.json", os.O_RDONLY)
    link = tmp_path / "linked"
    try:
        os.symlink(str(raw), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot make a symlink")
    with pytest.raises(PermissionError):
        feeder.record_raw(str(link), {"hr": [{"epoch": T0, "bpm": 1}]}, T0)


def test_tc1505_the_unit_declares_the_raw_directory_after_the_state_one():
    unit = (WEIGHT / "homehub-weight.service").read_text(encoding="utf-8")
    dirs = re.findall(r"^StateDirectory=(\S+)$", unit, re.M)
    assert dirs == ["homehub-weight", feeder.RAW_ROOT_NAME]
    assert feeder.DEFAULT_RAW_ROOT == hm.DEFAULT_RAW_DIR == "/var/lib/homehub-health-raw"


def test_tc1505_the_reader_tees_the_same_pages_into_raw_rows(tmp_path):
    token = tmp_path / "t.json"
    token.write_text(json.dumps({"refresh_token": "R"}), encoding="utf-8")
    env = {"WEIGHT_TOKEN_FILE": str(token), "OAUTH2_PROXY_CLIENT_ID": "i", "OAUTH2_PROXY_CLIENT_SECRET": "s",
           "WEIGHT_TIMEOUT_SECONDS": "10", "_now": T0 + 3600}

    def respond(handler):
        path = handler.path.partition("?")[0]
        payload = {"access_token": "A"} if path == "/token" else (
            {"dataPoints": [_hr_point(T0)]} if path == "/hr" else {"dataPoints": [_sleep_point(T0, T0 + 60)]})
        raw = json.dumps(payload).encode()
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    with loopback_server(respond) as (base, seen):
        samples, sessions, raw = feeder.read_google_health_heart_samples(
            env, heart_rate_url=base + "/hr", sleep_url=base + "/sleep", token_endpoint=base + "/token")
    assert len(seen) == 3, "recording costs no extra call"
    assert samples == [(T0, 78, "FFit")] and raw["hr"][0]["bpm"] == 78 and raw["sleep"][0]["end_epoch"] == T0 + 60


# ── TC-1506: the chart page and its site (SR-124, LLR-1328) ─────────────────

def _site(text, name):
    m = re.search(r"^%s \{\n(.*?)^\}\n" % re.escape(name), text, re.M | re.S)
    assert m, name
    return m.group(1)


def test_tc1506_the_site_is_in_the_admin_zone_read_only_and_static():
    body = _site(CADDY.read_text(encoding="utf-8"), "metrics.admin.{$DOMAIN}")
    for line in ("import acme_cloudflare", "import hsts", "@lan remote_ip {$LAN_CIDR}", "import protect_admin",
                 "root * /srv/metrics/data", "root * /srv/metrics/raw", "root * /srv/metrics/site"):
        assert line in body
    assert "reverse_proxy" not in body and "browse" not in body
    assert body.index("import protect_admin") < body.index("file_server")
    compose = COMPOSE.read_text(encoding="utf-8")
    for mount in ("./monitoring/metrics-site:/srv/metrics/site:ro", "/var/lib/homehub-metrics:/srv/metrics/data:ro",
                  "/var/lib/homehub-health-raw:/srv/metrics/raw:ro"):
        assert "      - %s\n" % mount in compose


def test_tc1506_the_page_reads_only_its_own_files_and_loads_nothing_from_elsewhere():
    page = (MON / "metrics-site" / "index.html").read_text(encoding="utf-8")
    assert "<title>Hub Metrics</title>" in page
    assert not re.search(r"<script[^>]+src=|<link[^>]+href=|https?://(?!www\.w3\.org/2000/svg)", page)
    for path in ('"data/daily.jsonl"', '"data", "snapshot"', '"raw", "hr"', '"raw", "sleep"'):
        assert path in page
    assert "prefers-color-scheme: dark" in page and "DecompressionStream" in page
    assert "<script>" in page and "innerHTML = \"<strong>Now" in page


# ── TC-1507..TC-1509: the weekly copy into the library (LLR-1332) ───────────

LIB_MOUNT = "40 29 8:33 / /srv/library rw - fuseblk /dev/sdc1 rw\n"


def _lib(tmp_path):
    root = tmp_path / "library"
    (root / "Private" / "Docs").mkdir(parents=True)
    _w(root / ".homehub-library", "")
    mounts = mc.parse_mountinfo(LIB_MOUNT.replace("/srv/library", root.as_posix()))
    knobs = {"HOMEHUB_METRICS_COPY_ROOT": root.as_posix(),
             "HOMEHUB_METRICS_COPY_DIR": (root / "Private" / "Docs" / "HomeHub-metrics").as_posix()}
    return root, mounts, knobs


def test_tc1507_no_mount_no_marker_no_parent_or_a_path_outside_is_a_skip(tmp_path):
    root, mounts, knobs = _lib(tmp_path)
    real = lambda p: p  # noqa: E731
    assert hm.copy_target(knobs, mounts, realpath=real)[0] == knobs["HOMEHUB_METRICS_COPY_DIR"]
    assert "unset" in hm.copy_target({}, mounts)[1]
    assert "not mounted" in hm.copy_target(knobs, [], realpath=real)[1]
    (root / ".homehub-library").unlink()
    assert "marker" in hm.copy_target(knobs, mounts, realpath=real)[1]
    _w(root / ".homehub-library", "")
    for bad, word in (((root / "Private" / "Nope" / "m").as_posix(), "does not exist"),
                      ((tmp_path / "elsewhere" / "m").as_posix(), "outside"),
                      (root.as_posix(), "outside"), ("relative/m", "absolute"),
                      ((root / "Private" / ".." / "m").as_posix(), "absolute")):
        assert word in hm.copy_target(dict(knobs, HOMEHUB_METRICS_COPY_DIR=bad), mounts, realpath=real)[1], bad
    assert "does not exist" in hm.copy_target(dict(knobs, HOMEHUB_METRICS_COPY_DIR=(root / "m").as_posix()),
                                              mounts, realpath=real)[1]


def test_tc1508_the_plan_skips_today_unsettled_and_unchanged_files():
    now = L(10, 26, 18, 30)
    srcs = [("snapshot", "/d/snapshot-20261001.jsonl.gz", 50, 100.0),
            ("snapshot", "/d/snapshot-20261025.jsonl", 900, now - 3600),
            ("snapshot", "/d/snapshot-20261026.jsonl", 900, now - 3600),
            ("raw", "/r/hr-20261025.jsonl", 40, now - 60),
            ("raw", "/r/hr-20261024.jsonl", 40, 200.0),
            ("daily", "/d/daily.jsonl", 10, now - 1)]
    have = {"snapshots/snapshot-20261001.jsonl.gz": (50, 100.4), "watch-raw/hr-20261024.jsonl": (39, 200.0)}
    plan = hm.copy_plan(srcs, have.get, "20261026", now)
    assert plan == [("/d/snapshot-20261025.jsonl", "snapshots/snapshot-20261025.jsonl.gz", True),
                    ("/r/hr-20261024.jsonl", "watch-raw/hr-20261024.jsonl", False),
                    ("/d/daily.jsonl", "daily.jsonl", False)]


def test_tc1509_the_copy_is_idempotent_never_deletes_and_meets_the_later_gzip(tmp_path):
    root, mounts, knobs = _lib(tmp_path)
    args = _args(tmp_path)
    _w(args.env_file, "".join("%s=%s\n" % kv for kv in knobs.items()))
    data, raw = Path(args.data_dir), Path(args.raw_dir)
    old = time.time() - 5 * 86400
    for name, base in (("snapshot-20201001.jsonl", data), ("daily.jsonl", data), ("hr-20201001.jsonl", raw)):
        _w(base / name, '{"epoch": 1}\n')
        os.utime(str(base / name), (old, old))
    today = mc.day_file_name("snapshot", time.time())
    _w(data / today, '{"epoch": 2}\n')
    text = LIB_MOUNT.replace("/srv/library", root.as_posix())
    assert hm.cmd_copy(args, mounts_text=text) == 0
    dest = Path(knobs["HOMEHUB_METRICS_COPY_DIR"])
    got = sorted(str(p.relative_to(dest)).replace("\\", "/") for p in dest.rglob("*") if p.is_file())
    assert got == ["daily.jsonl", "snapshots/snapshot-20201001.jsonl.gz", "watch-raw/hr-20201001.jsonl"]
    first = (dest / "snapshots" / "snapshot-20201001.jsonl.gz").read_bytes()
    stamps = {p: p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file()}
    _w(dest / "keep-me.txt", "the Owner's own file")
    # The hub later gzips the day itself: the same bytes, nothing rewritten.
    mc.compress_day_file(str(data), "snapshot-20201001.jsonl")
    assert hm.cmd_copy(args, mounts_text=text) == 0
    assert (dest / "snapshots" / "snapshot-20201001.jsonl.gz").read_bytes() == first
    assert {p: p.stat().st_mtime_ns for p in stamps} == stamps
    assert (dest / "keep-me.txt").exists() and not list(dest.rglob(".*.part"))
    # Unmounted: a logged skip, exit 0, nothing touched.
    shutil.rmtree(str(dest))
    assert hm.cmd_copy(args, mounts_text="") == 0 and not dest.exists()


# ── TC-1510: wiring - units, timers, firstboot, knobs, query-only (LLR-1327) ─

UNITS = {"homehub-metrics-snapshot": ("snapshot", hm.SNAPSHOT_DEADLINE_SEC),
         "homehub-metrics-daily": ("daily", hm.DAILY_DEADLINE_SEC),
         "homehub-metrics-copy": ("copy", hm.COPY_DEADLINE_SEC)}


def test_tc1510_units_are_bounded_oneshots_whose_deadline_fits():
    for name, (verb, deadline) in UNITS.items():
        svc = _live((MON / (name + ".service")).read_text(encoding="utf-8"))
        assert "Type=oneshot" in svc and "Restart=" not in svc and "User=" not in svc
        assert "ExecStart=/usr/bin/python3 /opt/homehub/stack/monitoring/homehub-metrics.py %s\n" % verb in svc + "\n"
        for directive in ("CPUQuota=", "MemoryMax=", "TasksMax=", "TimeoutStartSec=", "NoNewPrivileges=true",
                          "ProtectSystem=full"):
            assert re.search("^" + directive, svc, re.M), (name, directive)
        timeout = int(re.search(r"^TimeoutStartSec=(\d+)$", svc, re.M).group(1))
        assert deadline + hm.SAVE_RESERVE_SEC <= timeout, name
    tim = _live((MON / "homehub-metrics-snapshot.timer").read_text(encoding="utf-8"))
    assert "OnCalendar=*:0/5" in tim
    copy = _live((MON / "homehub-metrics-copy.timer").read_text(encoding="utf-8"))
    assert re.search(r"^OnCalendar=Sun \*-\*-\* 18:30:00 America/Chicago$", copy, re.M)
    assert re.search(r"^Persistent=true$", copy, re.M)
    assert "StateDirectory=homehub-metrics" in (MON / "homehub-metrics-snapshot.service").read_text(encoding="utf-8")


def test_tc1510_firstboot_installs_and_converges_all_three_timers():
    src = FIRSTBOOT.read_text(encoding="utf-8")
    block = src[src.index("# BEGIN hub-monitoring"): src.index("# END hub-monitoring")]
    for name in UNITS:
        for kind in ("service", "timer"):
            assert 'install -m 0644 "$_mon/%s.%s"' % (name, kind) in block
        assert re.search(r"_mon_converge %s\.timer +HOMEHUB_METRICS_ENABLED\n" % name, block)


def test_tc1510_every_knob_is_declared_and_the_files_are_lf():
    env = ENVEX.read_text(encoding="utf-8")
    src = (MON / "homehub-metrics.py").read_text(encoding="utf-8")
    for knob in sorted(set(re.findall(r'"(HOMEHUB_METRICS_[A-Z_]+)"', src)) - {"HOMEHUB_METRICS_DIR"}):
        assert re.search("^%s=" % knob, env, re.M), knob
    assert re.search(r"^HOMEHUB_METRICS_ENABLED=true$", env, re.M)
    assert re.search(r"^HOMEHUB_METRICS_USER=$", env, re.M)
    assert re.search(r"^HEART_RAW_RECORD=true$", env, re.M)
    for p in list(MON.glob("homehub-metrics*")) + [MON / "metrics-site" / "index.html"]:
        assert b"\r" not in p.read_bytes(), p.name
    assert (MON / "homehub-metrics.py").read_bytes().startswith(b"#!/usr/bin/env python3\n")


def test_tc1510_the_metrics_log_only_queries_and_never_acts():
    import ast

    tree = ast.parse((MON / "homehub-metrics.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant):
            head = node.elts[0].value
            if head in ("systemctl", "docker") and len(node.elts) > 1:
                assert node.elts[1].value in ("show", "ps", "inspect", "list-units"), node.elts[1].value
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for phrase in ("systemctl restart", "systemctl stop", "docker kill", "docker restart", "systemctl reboot"):
                assert phrase not in node.value
    # Never under the tracker's data folder (its git mirror is pushed to GitHub).
    for path in (hm.DEFAULT_DATA_DIR, hm.DEFAULT_RAW_DIR):
        assert "tracker" not in path and path.startswith("/var/lib/homehub-")
