"""Tests for the item-S telemetry collector's pure decision core."""

import importlib.util
import os
import sys

import pytest

_CORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "stack", "autoinstall", "wall", "panel_telemetry_core.py",
)
_spec = importlib.util.spec_from_file_location("panel_telemetry_core", _CORE_PATH)
core = importlib.util.module_from_spec(_spec)
sys.modules["panel_telemetry_core"] = core
_spec.loader.exec_module(core)


# ── /proc/stat parsing and CPU delta ─────────────────────────────────────────

def test_parse_stat_line_full():
    line = "cpu  100 5 50 800 10 0 5 0 0 0"
    parsed = core.parse_stat_line(line)
    assert parsed["user"] == 100
    assert parsed["idle"] == 800
    assert parsed["guest_nice"] == 0


def test_parse_stat_line_short_kernel_pads_zero():
    line = "cpu  100 5 50 800"
    parsed = core.parse_stat_line(line)
    assert parsed["iowait"] == 0
    assert parsed["guest_nice"] == 0


def test_parse_stat_line_rejects_non_cpu():
    with pytest.raises(ValueError):
        core.parse_stat_line("cpu0 1 2 3 4")


def test_cpu_percent_first_sample_is_none():
    cur = core.parse_stat_line("cpu  100 0 0 900 0 0 0 0 0 0")
    assert core.cpu_percent(None, cur) is None


def test_cpu_percent_known_delta():
    prev = core.parse_stat_line("cpu  100 0 0 900 0 0 0 0 0 0")
    # +100 busy (user), +100 idle over the interval -> 50% busy.
    cur = core.parse_stat_line("cpu  200 0 0 1000 0 0 0 0 0 0")
    pct = core.cpu_percent(prev, cur)
    assert pct == pytest.approx(50.0)


def test_cpu_percent_all_idle():
    prev = core.parse_stat_line("cpu  0 0 0 1000 0 0 0 0 0 0")
    cur = core.parse_stat_line("cpu  0 0 0 1100 0 0 0 0 0 0")
    assert core.cpu_percent(prev, cur) == pytest.approx(0.0)


def test_cpu_percent_all_busy():
    prev = core.parse_stat_line("cpu  1000 0 0 0 0 0 0 0 0 0")
    cur = core.parse_stat_line("cpu  1100 0 0 0 0 0 0 0 0 0")
    assert core.cpu_percent(prev, cur) == pytest.approx(100.0)


def test_cpu_percent_counter_reset_is_unknown():
    prev = core.parse_stat_line("cpu  5000 0 0 5000 0 0 0 0 0 0")
    cur = core.parse_stat_line("cpu  10 0 0 10 0 0 0 0 0 0")  # reboot: counters restarted
    assert core.cpu_percent(prev, cur) is None


def test_cpu_percent_does_not_double_count_guest_time():
    # guest time is already folded into "user" by the kernel; adding it again
    # would understate the busy fraction relative to total capacity.
    prev = core.parse_stat_line("cpu  100 0 0 900 0 0 0 0 50 0")
    cur = core.parse_stat_line("cpu  200 0 0 1000 0 0 0 0 150 0")
    # user +100, idle +100 -> 50% busy, regardless of the +100 guest delta.
    assert core.cpu_percent(prev, cur) == pytest.approx(50.0)


def test_cpu_percent_one_decreasing_field_invalidates_delta():
    # total looks like it grew, but "system" went backwards -- not a real read.
    prev = core.parse_stat_line("cpu  100 0 500 900 0 0 0 0 0 0")
    cur = core.parse_stat_line("cpu  700 0 100 900 0 0 0 0 0 0")
    assert core.cpu_percent(prev, cur) is None


def test_parse_stat_line_rejects_bare_cpu_with_no_fields():
    with pytest.raises(ValueError):
        core.parse_stat_line("cpu")


def test_parse_stat_line_rejects_negative_field():
    with pytest.raises(ValueError):
        core.parse_stat_line("cpu  -1 0 0 900 0 0 0 0 0 0")


# ── suspend-gap detection ────────────────────────────────────────────────────

def test_detect_suspend_gap_none_when_normal_interval():
    assert core.detect_suspend_gap(0.0, 5.1, interval_s=5.0) is None


def test_detect_suspend_gap_detected_after_long_sleep():
    gap = core.detect_suspend_gap(0.0, 600.0, interval_s=5.0)
    assert gap == pytest.approx(600.0)


def test_detect_suspend_gap_first_sample_is_none():
    assert core.detect_suspend_gap(None, 5.0, interval_s=5.0) is None


# ── missing sensor handling ──────────────────────────────────────────────────

def test_build_sensor_record_missing_is_none_not_zero():
    rec = core.build_sensor_record("hwmon:coretemp:temp1", "Package id 0", None, True)
    assert rec["deg_c"] is None
    assert rec["canonical"] is True


def test_build_sensor_record_present():
    rec = core.build_sensor_record("hwmon:coretemp:temp1", "Package id 0", 61.0, True)
    assert rec["deg_c"] == 61.0


# ── presentation staleness ───────────────────────────────────────────────────

def test_is_stale_missing_record():
    assert core.is_stale(None, 100000.0) is True


def test_is_stale_fresh():
    assert core.is_stale(100000.0, 100500.0, threshold_s=15.0) is False


def test_is_stale_past_threshold():
    assert core.is_stale(100000.0, 120000.0, threshold_s=15.0) is True  # 20s old


def test_is_stale_rejects_nan():
    assert core.is_stale(float("nan"), 100000.0) is True


def test_is_stale_rejects_far_future_timestamp():
    # a record claiming to be from later than "now" is a clock/process
    # anomaly, not evidence of freshness.
    assert core.is_stale(999999.0, 100000.0, threshold_s=15.0) is True


def test_is_stale_allows_small_negative_jitter():
    assert core.is_stale(100000.5, 100000.0, threshold_s=15.0) is False


# ── presentation validation ──────────────────────────────────────────────────

def _valid_payload(**overrides):
    payload = {
        "displayMode": "FULL",
        "selectedTab": None,
        "fullscreenOwner": "DOOR",
        "mediaPreference": "PROJECTM",
        "visualizerStatus": "active",
        "displayPower": "on",
        "musicActive": True,
        "revision": 42,
        "monotonicMs": 12345.0,
    }
    payload.update(overrides)
    return payload


def test_validate_presentation_accepts_well_formed():
    ok, errors = core.validate_presentation(_valid_payload())
    assert ok
    assert errors == []


def test_validate_presentation_rejects_missing_field():
    payload = _valid_payload()
    del payload["revision"]
    ok, errors = core.validate_presentation(payload)
    assert not ok
    assert any("revision" in e for e in errors)


def test_validate_presentation_rejects_bad_display_mode():
    ok, errors = core.validate_presentation(_valid_payload(displayMode="HALF"))
    assert not ok
    assert any("displayMode" in e for e in errors)


def test_validate_presentation_rejects_bad_fullscreen_owner():
    ok, errors = core.validate_presentation(_valid_payload(fullscreenOwner="TV"))
    assert not ok
    assert any("fullscreenOwner" in e for e in errors)


def test_validate_presentation_rejects_non_object():
    ok, errors = core.validate_presentation(["not", "a", "dict"])
    assert not ok
    assert errors == ["payload is not an object"]


def test_validate_presentation_allows_tabbed_with_null_owner():
    ok, errors = core.validate_presentation(_valid_payload(displayMode="TABBED", fullscreenOwner=None))
    assert ok, errors


def test_validate_presentation_rejects_full_with_null_owner():
    # FULL always has something on the whole screen; a null owner there is
    # an incoherent combination, not a legal "nothing is fullscreen" state.
    ok, errors = core.validate_presentation(_valid_payload(displayMode="FULL", fullscreenOwner=None))
    assert not ok
    assert any("fullscreenOwner" in e for e in errors)


def test_validate_presentation_rejects_tabbed_with_owner_set():
    ok, errors = core.validate_presentation(_valid_payload(displayMode="TABBED", fullscreenOwner="DOOR"))
    assert not ok
    assert any("TABBED" in e for e in errors)


def test_validate_presentation_rejects_unknown_field():
    payload = _valid_payload()
    payload["title"] = "whatever was on screen"
    ok, errors = core.validate_presentation(payload)
    assert not ok
    assert any("unexpected field" in e for e in errors)


def test_validate_presentation_rejects_overlong_string():
    ok, errors = core.validate_presentation(_valid_payload(selectedTab="X" * 200))
    assert not ok
    assert any("exceeds" in e for e in errors)


def test_validate_presentation_rejects_nan_monotonic():
    ok, errors = core.validate_presentation(_valid_payload(monotonicMs=float("nan")))
    assert not ok
    assert any("monotonicMs" in e for e in errors)


def test_validate_presentation_rejects_negative_revision():
    ok, errors = core.validate_presentation(_valid_payload(revision=-1))
    assert not ok
    assert any("revision" in e for e in errors)


def test_validate_presentation_rejects_bool_for_revision():
    ok, errors = core.validate_presentation(_valid_payload(revision=True))
    assert not ok


# ── retention / rotation arithmetic ──────────────────────────────────────────

def test_files_to_delete_for_retention():
    now = 1_000_000.0
    files = [
        ("telemetry-old.jsonl", now - 10 * 86400),
        ("telemetry-recent.jsonl", now - 1 * 86400),
    ]
    to_delete = core.files_to_delete_for_retention(files, now, retention_days=7)
    assert to_delete == ["telemetry-old.jsonl"]


def test_files_to_delete_for_retention_keeps_within_window():
    now = 1_000_000.0
    files = [("telemetry-fresh.jsonl", now - 6 * 86400)]
    assert core.files_to_delete_for_retention(files, now, retention_days=7) == []


def test_files_to_delete_for_cap_deletes_oldest_first_until_under_cap():
    files = [
        ("a.jsonl", 1.0, 20 * 1024 * 1024),
        ("b.jsonl", 2.0, 20 * 1024 * 1024),
        ("c.jsonl", 3.0, 20 * 1024 * 1024),
    ]
    to_delete = core.files_to_delete_for_cap(files, cap_bytes=50 * 1024 * 1024)
    # total 60 MiB > 50 MiB cap; delete oldest (a) to get to 40 MiB <= 50 MiB
    assert to_delete == ["a.jsonl"]


def test_files_to_delete_for_cap_no_deletion_needed():
    files = [("a.jsonl", 1.0, 10 * 1024 * 1024)]
    assert core.files_to_delete_for_cap(files, cap_bytes=50 * 1024 * 1024) == []


def test_files_to_delete_for_cap_deletes_everything_if_still_over():
    files = [("a.jsonl", 1.0, 100 * 1024 * 1024)]
    to_delete = core.files_to_delete_for_cap(files, cap_bytes=50 * 1024 * 1024)
    assert to_delete == ["a.jsonl"]


# ── record assembly ──────────────────────────────────────────────────────────

def test_build_record_shape():
    rec = core.build_record(
        ts_utc="2026-09-14T00:00:00Z",
        mono_s=1.0,
        boot_id="boot-1",
        session_id="sess-1",
        cpu_pct=42.0,
        sensors=[],
        gpu={},
        cooling=[],
        cpufreq_mhz=[],
        kiosk=[],
        presentation=None,
    )
    assert rec["v"] == core.SCHEMA_VERSION
    assert rec["cpu_pct"] == 42.0
    assert rec["event"] is None
