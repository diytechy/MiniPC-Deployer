"""TC-1485 - the sleep feeder keeps myAir's nightly usage minutes, and posts none.

The Owner asked on 2026-10-25 that myAir's usage hours be pulled and kept;
where they are shown and how they are used is still open. So the contract here
is narrow:

  * `totalUsage` is requested and read as whole minutes within 0..1440,
    INCLUDING 0; anything else is no data, never 0;
  * it is kept per night key in the 0600 state file for 60 nights, survives a
    restart, and a corrupt or out-of-window entry is dropped on load;
  * two different figures for one night leave that night unchanged;
  * the gauge body is unchanged by it, and no journal line carries the minutes.

Verifies: TC-1485 (SR-070, LLR-1313)
"""

import json

import pytest

from test_sleep_support import SYNCED, Harness, feeder, local, timezones

ZONES = timezones()
pytestmark = pytest.mark.parametrize("tz", [tz for _, tz in ZONES],
                                     ids=[name for name, _ in ZONES])

TODAY_START = "2026-10-11"          # the night that ended on Monday 2026-10-12
PREV_START = "2026-10-10"
MINUTES = 437                       # a figure no other field in these tests carries


def at(tz, month, day, hour, minute=0, year=2026):
    return local(tz, year, month, day, hour, minute)


def cycle(h, now, records):
    """Like Harness.cycle, with (startDate, sleepScore, totalUsage) records."""
    def reader(env, now_, tz_):
        return [feeder.Record(feeder.parse_night_date(start), score, usage)
                for start, score, usage in records], 0
    return h.f.run_cycle(dict(h.env), now=now, tz=h.tz, reader=reader, poster=h.poster,
                         force_fetch=True, sync_check=SYNCED)


def test_usage_minutes_are_validated_never_manufactured(tz):
    check = feeder.valid_usage_minutes
    assert check(0) == 0 and check(MINUTES) == MINUTES and check(1440) == 1440
    assert check(437.0) == 437
    for bad in (None, "437", True, False, -1, 1441, 437.5, float("nan"), float("inf")):
        assert check(bad) is None, bad


def test_usage_is_kept_per_night_and_not_posted(tz, tmp_path):
    h = Harness(tmp_path, tz)
    _posted, notes, _failures = cycle(h, at(tz, 10, 12, 9, 0),
                                      [(TODAY_START, 82, MINUTES), (PREV_START, 79, 0)])
    assert h.state()["usage"] == {"2026-10-12": MINUTES, "2026-10-11": 0}
    body = h.last()
    assert body["value"] == 82
    assert str(MINUTES) not in json.dumps(body)
    assert all(str(MINUTES) not in line for line in notes)
    assert "usage recorded for 2 night(s)" in notes


def test_no_usage_figure_changes_nothing_and_a_conflict_keeps_the_old_one(tz, tmp_path):
    h = Harness(tmp_path, tz)
    cycle(h, at(tz, 10, 12, 9, 0), [(TODAY_START, 82, MINUTES)])
    cycle(h, at(tz, 10, 12, 10, 0), [(TODAY_START, 82, None)])
    assert h.state()["usage"] == {"2026-10-12": MINUTES}
    cycle(h, at(tz, 10, 12, 11, 0), [(TODAY_START, 82, 400), (TODAY_START, 82, 410)])
    assert h.state()["usage"] == {"2026-10-12": MINUTES}
    cycle(h, at(tz, 10, 12, 12, 0), [(TODAY_START, 82, 450)])      # a correction
    assert h.state()["usage"] == {"2026-10-12": 450}


def test_usage_survives_a_restart_and_a_bad_entry_is_dropped_on_load(tz, tmp_path):
    h = Harness(tmp_path, tz)
    now = at(tz, 10, 12, 9, 0)
    h.state_path.write_text(json.dumps({"usage": {
        "2026-10-12": MINUTES,
        "2026-10-11": True,            # a bool is not minutes
        "2026-10-10": 2000,            # more than a day
        "2026-10-13": 300,             # a night that has not happened yet
        "2026-08-01": 300,             # older than the 60 nights kept
        "not-a-date": 300}}), encoding="utf-8")
    h.restart()
    state = h.f.load_state(str(h.state_path), now, tz)
    assert state["usage"] == {"2026-10-12": MINUTES}
    for junk in ('{"usage": 5}', '{"usage": []}'):
        h.state_path.write_text(junk, encoding="utf-8")
        assert h.f.load_state(str(h.state_path), now, tz)["usage"] == {}


def test_the_record_is_pruned_to_sixty_nights(tz, tmp_path):
    h = Harness(tmp_path, tz)
    cycle(h, at(tz, 10, 12, 9, 0), [("2026-08-12", 70, 300), ("2026-08-13", 71, 301)])
    # night keys 2026-08-13 (60 days before 2026-10-12: kept) and 2026-08-14
    assert set(h.state()["usage"]) == {"2026-08-13", "2026-08-14"}
    cycle(h, at(tz, 10, 13, 9, 0), [])
    assert set(h.state()["usage"]) == {"2026-08-14"}
