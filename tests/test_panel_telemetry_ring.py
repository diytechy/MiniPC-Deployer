"""The two panel-telemetry defects of HomeHub FOLLOWUPS "Finding 2" (2026-10-19).

1. THE TRANSITION RING WAS DISCARDED. OfficeWallNaglight's
   electron/presentation-ipc.cjs writes `{current, transitions}`, keeping the
   last 20 reports so a mode shorter than the collector's ~1 s poll survives,
   and its comment relies on "panel-telemetry.py's transition draining". There
   was none: `read_presentation` kept `current` only. TC-1366 pins the pure
   drain; TC-1367 runs the real loop across polls.

2. THE CLOCKS DISAGREED ACROSS A SUSPEND. The host stamps each report with
   os.uptime() (boot-relative, suspend INCLUDED); the collector judged its age
   against time.monotonic() (suspend EXCLUDED). After the first suspend every
   report looked like it came from the future, and `is_stale` calls a future
   stamp stale - so the panel's mode went unrecorded for the rest of the boot.
   Planned night suspend (Sleep B) would do it nightly. TC-1367 simulates a
   one-hour suspend by moving the two clocks apart.

Both are asserted on the REAL `cmd_run` loop with the clocks and the sleep
injected; nothing here needs sysfs or a renderer.

Verifies: TC-1366, TC-1367 (SR-030, LLR-1214)
"""
import importlib.util
import json
import os
import sys
import time
import types

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WALL = os.path.join(_HERE, "stack", "autoinstall", "wall")

_core_spec = importlib.util.spec_from_file_location(
    "panel_telemetry_core", os.path.join(_WALL, "panel_telemetry_core.py"))
core = importlib.util.module_from_spec(_core_spec)
sys.modules["panel_telemetry_core"] = core
_core_spec.loader.exec_module(core)

_spec = importlib.util.spec_from_file_location(
    "panel_telemetry_ring", os.path.join(_WALL, "panel-telemetry.py"))
telemetry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(telemetry)


def _rec(revision, ms, mode="FULL", owner="PROJECTM"):
    return {
        "displayMode": mode, "selectedTab": "CHECKLIST",
        "fullscreenOwner": owner if mode == "FULL" else None,
        "mediaPreference": "projectm", "visualizerStatus": None,
        "displayPower": "on", "musicActive": True,
        "revision": revision, "monotonicMs": ms,
    }


# ── TC-1366: the pure drain ──────────────────────────────────────────────────

def test_tc1366_first_sight_records_nothing_and_sets_the_cursor_llr1214():
    ring = [_rec(1, 100.0), _rec(2, 200.0)]
    new, cursor, lost = core.drain_transitions(ring, None)
    assert new == [] and cursor == (200.0, 2) and lost is False


def test_tc1366_everything_after_the_cursor_in_ring_order_llr1214():
    ring = [_rec(1, 100.0), _rec(2, 200.0), _rec(3, 210.0), _rec(4, 220.0)]
    new, cursor, lost = core.drain_transitions(ring, (200.0, 2))
    assert [t["revision"] for t in new] == [3, 4]
    assert cursor == (220.0, 4) and lost is False


def test_tc1366_nothing_new_is_nothing_llr1214():
    ring = [_rec(1, 100.0), _rec(2, 200.0)]
    assert core.drain_transitions(ring, (200.0, 2)) == ([], (200.0, 2), False)
    assert core.drain_transitions([], (200.0, 2)) == ([], (200.0, 2), False)


def test_tc1366_one_clock_tick_two_reports_the_revision_breaks_the_tie_llr1214():
    """The host clock reads /proc/uptime (10 ms): two reports can share a stamp."""
    ring = [_rec(5, 300.0), _rec(6, 300.0), _rec(7, 300.0)]
    new, cursor, _ = core.drain_transitions(ring, (300.0, 5))
    assert [t["revision"] for t in new] == [6, 7] and cursor == (300.0, 7)


def test_tc1366_an_overflowed_ring_says_so_and_keeps_what_it_has_llr1214():
    ring = [_rec(r, 1000.0 + r) for r in range(30, 50)]
    new, cursor, lost = core.drain_transitions(ring, (1005.0, 5))
    assert len(new) == 20 and lost is True and cursor == (1049.0, 49)


def test_tc1366_a_renderer_reload_restarts_revisions_but_not_the_host_clock_llr1214():
    ring = [_rec(40, 500.0), _rec(41, 510.0), _rec(0, 900.0), _rec(1, 905.0)]
    new, _, lost = core.drain_transitions(ring, (510.0, 41))
    assert [t["revision"] for t in new] == [0, 1] and lost is False


def test_tc1366_invalid_ring_entries_are_dropped_not_recorded_llr1214(tmp_path):
    bad = dict(_rec(9, 100.0), displayMode="SIDEWAYS")
    path = tmp_path / "snap.json"
    path.write_text(json.dumps({"current": _rec(10, 110.0), "transitions": [bad, _rec(10, 110.0)]}))
    _, ring = telemetry.read_snapshot(str(path), 110.0)
    assert [t["revision"] for t in ring] == [10]


def test_tc1366_a_flat_record_has_no_ring_llr1214(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(_rec(3, 100.0)))
    presentation, ring = telemetry.read_snapshot(str(path), 100.0)
    assert presentation["status"] == "ok" and ring is None
    assert telemetry.read_presentation(str(path), 100.0) == presentation


# ── TC-1367: the real loop, clocks and sleep injected ────────────────────────

class _Stop(Exception):
    pass


class Clocks:
    """monotonic and BOOTTIME, moved apart by `suspend` (what a suspend does)."""

    def __init__(self, start=1000.0):
        self.mono = start
        self.suspended = 0.0

    def monotonic(self):
        return self.mono

    def boottime(self):
        return self.mono + self.suspended


def _drive(tmp_path, monkeypatch, snapshots, clocks, suspend_before_tick=None):
    """Run cmd_run for len(snapshots) ticks; snapshot i is in place for tick i."""
    path = tmp_path / "presentation-snapshot.json"
    state = tmp_path / "state"
    ticks = {"n": 0}

    def write(i):
        path.write_text(json.dumps(snapshots[i]), encoding="utf-8")

    def fake_sleep(seconds):
        ticks["n"] += 1
        if ticks["n"] >= len(snapshots):
            raise _Stop()
        clocks.mono += 1.0
        if suspend_before_tick is not None and ticks["n"] == suspend_before_tick:
            clocks.suspended += 3600.0
        write(ticks["n"])

    fake_time = types.SimpleNamespace(
        # time.time() stays real: the collector's retention compares file
        # mtimes with it, and an invented epoch would delete the records.
        monotonic=clocks.monotonic, sleep=fake_sleep, time=time.time,
        clock_gettime=lambda _clk: clocks.boottime(), CLOCK_BOOTTIME=7,
    )
    monkeypatch.setattr(telemetry, "time", fake_time)
    monkeypatch.setattr(telemetry, "_resolve_sensors", lambda: [])
    monkeypatch.setattr(telemetry, "_resolve_gpu_card", lambda: None)
    write(0)
    args = types.SimpleNamespace(state_dir=str(state), presentation_path=str(path), once=False)
    with pytest.raises(_Stop):
        telemetry.cmd_run(args)
    records = []
    for name in sorted(os.listdir(state)):
        with open(os.path.join(state, name), encoding="utf-8") as fh:
            records.extend(json.loads(line) for line in fh if line.strip())
    return records


def test_tc1367_modes_shorter_than_the_poll_are_drained_from_the_ring_llr1214(tmp_path, monkeypatch):
    clocks = Clocks()
    host = lambda: clocks.boottime() * 1000.0  # noqa: E731
    t0 = 1000.0 * 1000.0
    first = _rec(1, t0, "TABBED")
    # Between the two polls the panel went FULL/DOOR and back to TABBED in
    # under a second: `current` alone says only "revision 3, TABBED".
    burst = [first, _rec(2, t0 + 300.0, "FULL", "DOOR"), _rec(3, t0 + 700.0, "TABBED")]
    snapshots = [
        {"current": first, "transitions": [first]},
        {"current": burst[-1], "transitions": burst},
    ]
    records = _drive(tmp_path, monkeypatch, snapshots, clocks)
    changes = [r for r in records if r.get("event") == "mode_change"]
    assert [(c["transition"]["revision"], c["transition"]["displayMode"]) for c in changes] == \
        [(2, "FULL"), (3, "TABBED")], changes
    assert changes[0]["prev_revision"] == 1 and changes[1]["prev_revision"] == 2
    assert all(c["source"] == "transition-ring" and c["cursor_lost"] is False for c in changes)
    assert host() >= t0  # the fixture's stamps are on the host clock


def test_tc1367_after_a_suspend_the_current_mode_is_still_ok_not_stale_llr1214(tmp_path, monkeypatch):
    """Tick 0 samples, the panel 'suspends' for an hour, tick 1 samples a report
    the host stamped 2 s before it: fresh on BOOTTIME, an hour in the future on
    the monotonic clock the collector used to judge it by."""
    clocks = Clocks()
    before = _rec(1, clocks.boottime() * 1000.0 - 2000.0)
    after_ms = (clocks.mono + 1.0 + 3600.0) * 1000.0 - 2000.0
    after = _rec(2, after_ms)
    snapshots = [{"current": before, "transitions": [before]},
                 {"current": after, "transitions": [before, after]}]
    monkeypatch.setattr(telemetry, "INTERVAL_S", 0.5)   # a full sample on every tick
    records = _drive(tmp_path, monkeypatch, snapshots, clocks, suspend_before_tick=1)
    samples = [r for r in records if r.get("event") in (None, "suspend_gap") and "presentation" in r]
    assert len(samples) == 2, records
    assert samples[0]["presentation"]["status"] == "ok"
    assert samples[1]["presentation"]["status"] == "ok", samples[1]["presentation"]
    assert samples[1]["presentation"]["payload"]["revision"] == 2
    assert samples[1].get("event") == "suspend_gap"


def test_tc1367_a_flat_snapshot_keeps_the_revision_comparison_llr1214(tmp_path, monkeypatch):
    clocks = Clocks()
    ms = clocks.boottime() * 1000.0
    snapshots = [_rec(1, ms), _rec(2, ms + 1000.0)]
    records = _drive(tmp_path, monkeypatch, snapshots, clocks)
    changes = [r for r in records if r.get("event") == "mode_change"]
    assert len(changes) == 1 and changes[0]["prev_revision"] == 1
    assert changes[0]["presentation"]["payload"]["revision"] == 2
