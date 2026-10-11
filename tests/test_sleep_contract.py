"""TC-1151..TC-1161 - the sleep feeder's nightly data contract (plan 4.4, 4.5).

What is asserted here, each against the OTHER side of its contract (the wire body
NagLight receives, the bytes in the state file, a literal UTC instant) and not
against a value invented in the same file:

  * the night key is `startDate` + 1 calendar day (TC-1151);
  * only a finite score within 0..100 is a score - never a manufactured zero,
    and myAir's own 0 IS one (TC-1152, TC-1153, TC-1154);
  * `observed_at` is the EARLIER of 12:00 local on the night-key date and first
    publication, frozen for ever; a record arriving at 07:00 is stamped 07:00
    (TC-1155);
  * a correction changes the value and never the stamp (TC-1156);
  * 13:59 is before the grace hour and 14:00 is after it (TC-1157), on ordinary
    days and on both DST change days (TC-1158);
  * an old night is never shown as last night (TC-1159);
  * the state file survives a restart and republishes nothing as fresh
    (TC-1160);
  * the gauge on the wire is exactly the plan's body (TC-1161).

Every time-dependent test runs under the hand-written US Central zone and, where
this Python has it, the real tz database's `America/Chicago`.

Verifies: TC-1151, TC-1152, TC-1153, TC-1154, TC-1155, TC-1156, TC-1157,
          TC-1158, TC-1159, TC-1160, TC-1161 (SR-070, SR-071, LLR-1077..LLR-1085)
"""

import datetime as dt
import json
import os
import re
from pathlib import Path

import pytest

from test_sleep_support import Harness, feeder, local, sub, timezones, utc

ZONES = timezones()
ZONE_IDS = [name for name, _ in ZONES]
ZONE_OBJECTS = [tz for _, tz in ZONES]

pytestmark = pytest.mark.parametrize("tz", ZONE_OBJECTS, ids=ZONE_IDS)


def at(tz, month, day, hour, minute=0, year=2026):
    return local(tz, year, month, day, hour, minute)


# Monday 2026-10-12. The night that ENDED this morning began on 2026-10-11, so
# its record's startDate is 2026-10-11 and its night key is 2026-10-12.
TODAY_START = "2026-10-11"
PREV_START = "2026-10-10"


def stamps(h):
    """{night key: observed_at epoch} straight off the state file."""
    return {key: entry["observed_at"] for key, entry in h.state()["nights"].items()}


# ── TC-1151 ──────────────────────────────────────────────────────────────────

def test_night_key_is_start_date_plus_one_calendar_day(tz):
    """The S0b ruling: startDate is the evening the night BEGAN."""
    cases = {
        "2026-10-10": "2026-10-11",     # the night the Owner confirmed in the app
        "2026-10-31": "2026-11-01",
        "2026-12-31": "2027-01-01",     # across a year
        "2028-02-28": "2028-02-29",     # into a leap day
        "2028-02-29": "2028-03-01",
        "2026-03-07": "2026-03-08",     # into the spring-forward day
        "2026-10-31 ": None,            # not a bare date
        "2026-10-31T00:00:00": None,
        "10/31/2026": None,
        "2026-02-30": None,
        None: None,
        20261031: None,
    }
    for start, expected in cases.items():
        parsed = feeder.parse_night_date(start)
        if expected is None:
            assert parsed is None, start
        else:
            assert feeder.night_key_for(parsed).isoformat() == expected, start


def test_a_record_is_keyed_by_the_day_after_its_start_date(tz, tmp_path):
    """Through the cycle: the record dated yesterday is TODAY'S night, and the one
    dated today (its night has not ended) is nobody's."""
    h = Harness(tmp_path, tz)
    now = at(tz, 10, 12, 10, 30)
    h.cycle(now, [(TODAY_START, 82)])
    assert list(h.state()["nights"]) == ["2026-10-12"]
    assert h.last()["value"] == 82 and "observed_at" in h.last()

    # A record whose night key is TOMORROW can neither publish nor block today's.
    h2 = sub(tmp_path, "b", tz)
    h2.cycle(now, [("2026-10-12", 55), (TODAY_START, 82)])
    assert h2.state()["nights"].keys() == {"2026-10-12"}
    assert h2.last()["value"] == 82
    # ...and on its own it leaves today's night absent, which is not a score.
    h3 = sub(tmp_path, "c", tz)
    h3.cycle(at(tz, 10, 12, 15, 0), [("2026-10-12", 55)])
    assert "observed_at" not in h3.last()


# ── TC-1152 ──────────────────────────────────────────────────────────────────

NOT_A_SCORE = [None, "82", "", True, False, float("nan"), float("inf"), float("-inf"),
               -1, -0.01, 100.01, 101, 1e9, [], {}, "NaN"]


def test_only_a_finite_number_within_0_to_100_is_a_score(tz):
    for raw in NOT_A_SCORE:
        assert feeder.valid_score(raw) is None, repr(raw)
    assert feeder.valid_score(0) == 0
    assert feeder.valid_score(100) == 100
    assert feeder.valid_score(82) == 82
    assert feeder.valid_score(82.0) == 82 and isinstance(feeder.valid_score(82.0), int)
    assert feeder.valid_score(77.5) == 77.5
    assert feeder.valid_score(0.0) == 0


# ── TC-1153 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", NOT_A_SCORE)
def test_a_missing_or_unusable_score_is_no_data_never_a_zero(tz, tmp_path, raw):
    """Absent, null, string, bool, NaN, out of range: grey from 14:00, and never a 0 reading."""
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 12, 15, 0), [(TODAY_START, raw)])
    body = h.last()
    assert "observed_at" not in body                 # stale on arrival
    assert h.state()["nights"] == {}                 # nothing was stored either


def test_an_absent_record_before_14_keeps_the_previous_night_and_after_14_goes_grey(tz, tmp_path):
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 11, 10, 15), [(PREV_START, 79)])             # yesterday's score
    published = h.last()
    assert published["value"] == 79 and "observed_at" in published

    h.cycle(at(tz, 10, 12, 8, 0), [(PREV_START, 79)])               # this morning, no record yet
    assert h.last()["value"] == 79
    assert h.last()["observed_at"] == published["observed_at"]      # aged normally, never restamped

    h.cycle(at(tz, 10, 12, 13, 59), [(PREV_START, 79)])
    assert h.last()["value"] == 79

    h.cycle(at(tz, 10, 12, 14, 0), [(PREV_START, 79)])
    assert "observed_at" not in h.last()                            # unavailable


# ── TC-1154 ──────────────────────────────────────────────────────────────────

def test_myairs_own_zero_is_published_as_zero(tz, tmp_path):
    """Including 0. The wire body says value 0 WITH an observed_at - a real reading."""
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 12, 10, 30), [(TODAY_START, 0)])
    body = h.last()
    assert body["value"] == 0
    assert "observed_at" in body
    assert h.state()["nights"]["2026-10-12"]["score"] == 0


def test_a_zero_that_myair_did_not_send_is_never_manufactured(tz, tmp_path):
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 12, 15, 0), [(TODAY_START, None)])
    assert "observed_at" not in h.last()
    h.cycle(at(tz, 10, 12, 15, 30), [])                              # no record at all
    assert "observed_at" not in h.last()


# ── TC-1155 ──────────────────────────────────────────────────────────────────

def test_a_record_arriving_at_0700_is_stamped_0700_not_1200(tz, tmp_path):
    """NagLight treats an observed_at in the FUTURE as stale; noon would paint grey till noon."""
    h = Harness(tmp_path, tz)
    now = at(tz, 10, 12, 7, 0)
    h.cycle(now, [(TODAY_START, 82)])
    assert stamps(h) == {"2026-10-12": now}
    assert h.last()["observed_at"] <= dt.datetime.fromtimestamp(now, tz).isoformat(timespec="seconds")
    assert dt.datetime.fromisoformat(h.last()["observed_at"]).timestamp() == now

    # A later fetch the same morning does not move it.
    h.cycle(at(tz, 10, 12, 9, 30), [(TODAY_START, 82)])
    assert stamps(h) == {"2026-10-12": now}


def test_a_record_arriving_after_noon_is_stamped_noon_never_the_fetch_time(tz, tmp_path):
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 12, 13, 0), [(TODAY_START, 82)])
    noon = utc(2026, 10, 12, 17, 0)                                  # 12:00 CDT, written in UTC
    assert stamps(h) == {"2026-10-12": noon}
    assert dt.datetime.fromisoformat(h.last()["observed_at"]).timestamp() == noon
    assert h.last()["observed_at"].endswith("-05:00")


def test_a_late_upload_after_the_grace_hour_is_stamped_noon_and_replaces_unavailable(tz, tmp_path):
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 12, 14, 30), [(PREV_START, 79)])
    assert "observed_at" not in h.last()                             # grey: nothing for today yet
    h.cycle(at(tz, 10, 12, 16, 45), [(PREV_START, 79), (TODAY_START, 64)])
    assert h.last()["value"] == 64
    assert dt.datetime.fromisoformat(h.last()["observed_at"]).timestamp() == utc(2026, 10, 12, 17, 0)


# ── TC-1156 ──────────────────────────────────────────────────────────────────

def test_a_correction_changes_the_value_and_never_the_stamp(tz, tmp_path):
    h = Harness(tmp_path, tz)
    first = at(tz, 10, 12, 7, 0)
    h.cycle(first, [(TODAY_START, 82)])
    stamp = h.last()["observed_at"]

    h.cycle(at(tz, 10, 12, 18, 0), [(TODAY_START, 71)])               # myAir revised the night
    assert h.last()["value"] == 71
    assert h.last()["observed_at"] == stamp
    assert stamps(h) == {"2026-10-12": first}

    h.cycle(at(tz, 10, 13, 6, 0), [(TODAY_START, 71)])                # next morning: still last night
    assert h.last()["value"] == 71 and h.last()["observed_at"] == stamp


def test_a_later_null_or_missing_record_does_not_erase_a_published_night(tz, tmp_path):
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 12, 10, 0), [(TODAY_START, 82)])
    stamp = h.last()["observed_at"]
    h.cycle(at(tz, 10, 12, 11, 0), [(TODAY_START, None)])
    assert h.last()["value"] == 82 and h.last()["observed_at"] == stamp
    h.cycle(at(tz, 10, 12, 12, 0), [])
    assert h.last()["value"] == 82 and h.last()["observed_at"] == stamp


def test_two_conflicting_records_for_one_night_are_skipped_not_guessed(tz, tmp_path):
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 12, 10, 0), [(TODAY_START, 82), (TODAY_START, 40)])
    assert h.state()["nights"] == {}
    h.cycle(at(tz, 10, 12, 10, 30), [(TODAY_START, 82), (TODAY_START, 82)])   # agreeing duplicates
    assert h.last()["value"] == 82


# ── TC-1157 ──────────────────────────────────────────────────────────────────

def test_1359_is_before_the_grace_hour_and_1400_is_after_it(tz, tmp_path):
    for hour, minute, grey in ((13, 59, False), (14, 0, True), (23, 59, True), (0, 0, False)):
        h = sub(tmp_path, "t%02d%02d" % (hour, minute), tz)
        # Yesterday's night was published; today's has not arrived.
        h.cycle(at(tz, 10, 11, 10, 0), [(PREV_START, 79)])
        now = at(tz, 10, 12, hour, minute)
        h.cycle(now, [(PREV_START, 79)])
        assert ("observed_at" not in h.last()) is grey, (hour, minute)
        if not grey:
            assert h.last()["value"] == 79


def test_the_grace_boundary_is_read_off_the_local_clock(tz):
    one_second_before = at(tz, 10, 12, 13, 59) + 59
    assert feeder.past_grace(one_second_before, tz) is False
    assert feeder.past_grace(at(tz, 10, 12, 14, 0), tz) is True
    assert feeder.past_grace(at(tz, 10, 12, 14, 0) - 1, tz) is False
    # Midnight local is a new expected night.
    assert feeder.expected_night(at(tz, 10, 12, 23, 59) + 60, tz) == dt.date(2026, 10, 13)


# ── TC-1158 ──────────────────────────────────────────────────────────────────

def test_the_clocks_going_forward_day_has_the_right_stamp_offset_and_grace(tz, tmp_path):
    """2026-03-08, a 23-hour day: noon is CDT (17:00Z), the night before was CST."""
    h = Harness(tmp_path, tz)
    # The night that ended on the spring-forward morning began on 2026-03-07.
    h.cycle(utc(2026, 3, 8, 18, 0), [("2026-03-07", 82)])            # 13:00 CDT
    assert dt.datetime.fromisoformat(h.last()["observed_at"]).timestamp() == utc(2026, 3, 8, 17, 0)
    assert h.last()["observed_at"].endswith("-05:00")

    # The previous night's noon (2026-03-07) was still standard time: 18:00Z, -06:00.
    h2 = sub(tmp_path, "prev", tz)
    h2.cycle(utc(2026, 3, 7, 19, 0), [("2026-03-06", 80)])           # 13:00 CST
    assert dt.datetime.fromisoformat(h2.last()["observed_at"]).timestamp() == utc(2026, 3, 7, 18, 0)
    assert h2.last()["observed_at"].endswith("-06:00")

    # 14:00 local on the change day is 19:00Z (CDT), not 20:00Z: a fixed -06:00
    # would call 19:00Z "13:00" and keep the previous night on the wall an hour long.
    for now, grey in ((utc(2026, 3, 8, 18, 59), False), (utc(2026, 3, 8, 19, 0), True)):
        h3 = sub(tmp_path, "g%d" % now, tz)
        h3.cycle(utc(2026, 3, 7, 18, 0), [("2026-03-06", 70)])       # yesterday's night, published
        h3.cycle(now, [("2026-03-06", 70)])
        assert ("observed_at" not in h3.last()) is grey, now
        if not grey:
            assert h3.last()["value"] == 70


def test_the_clocks_going_back_day_has_the_right_stamp_offset_and_grace(tz, tmp_path):
    """2026-11-01, a 25-hour day: noon is CST (18:00Z), the day before was CDT."""
    h = Harness(tmp_path, tz)
    h.cycle(utc(2026, 11, 1, 19, 0), [("2026-10-31", 82)])           # 13:00 CST
    assert dt.datetime.fromisoformat(h.last()["observed_at"]).timestamp() == utc(2026, 11, 1, 18, 0)
    assert h.last()["observed_at"].endswith("-06:00")

    h2 = sub(tmp_path, "prev", tz)
    h2.cycle(utc(2026, 10, 31, 18, 0), [("2026-10-30", 80)])         # 13:00 CDT
    assert dt.datetime.fromisoformat(h2.last()["observed_at"]).timestamp() == utc(2026, 10, 31, 17, 0)
    assert h2.last()["observed_at"].endswith("-05:00")

    # 13:59 CST = 19:59Z is before the grace hour; 14:00 CST = 20:00Z is after it.
    for now, grey in ((utc(2026, 11, 1, 19, 59), False), (utc(2026, 11, 1, 20, 0), True)):
        h3 = sub(tmp_path, "g%d" % now, tz)
        h3.cycle(utc(2026, 10, 31, 18, 0), [("2026-10-30", 70)])      # the night before's score
        h3.cycle(now, [("2026-10-30", 70)])
        assert ("observed_at" not in h3.last()) is grey, now


def test_a_stamp_never_comes_from_adding_hours_across_a_change(tz):
    """noon_epoch is built from the DATE and the zone's own offset for it."""
    assert feeder.noon_epoch(dt.date(2026, 3, 8), tz) == utc(2026, 3, 8, 17, 0)
    assert feeder.noon_epoch(dt.date(2026, 3, 7), tz) == utc(2026, 3, 7, 18, 0)
    assert feeder.noon_epoch(dt.date(2026, 11, 1), tz) == utc(2026, 11, 1, 18, 0)
    assert feeder.noon_epoch(dt.date(2026, 10, 31), tz) == utc(2026, 10, 31, 17, 0)
    # 24 hours of wall clock is not 24 hours of time on a change day.
    assert feeder.noon_epoch(dt.date(2026, 3, 8), tz) - feeder.noon_epoch(dt.date(2026, 3, 7), tz) \
        == 23 * 3600
    assert feeder.noon_epoch(dt.date(2026, 11, 1), tz) - feeder.noon_epoch(dt.date(2026, 10, 31), tz) \
        == 25 * 3600


# ── TC-1159 ──────────────────────────────────────────────────────────────────

def test_an_old_night_is_never_shown_as_last_night(tz, tmp_path):
    """The newest record is two nights old: unavailable at every hour, not a score."""
    h = Harness(tmp_path, tz)
    for hour in (0, 6, 13, 14, 20):
        h.cycle(at(tz, 10, 12, hour, 0), [("2026-10-09", 88)])        # night key 2026-10-10
        assert "observed_at" not in h.last(), hour
    assert h.state()["nights"] == {}


def test_before_the_grace_hour_only_the_previous_night_may_stand_in(tz, tmp_path):
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 12, 8, 0), [(PREV_START, 79)])                  # night key 2026-10-11
    assert h.last()["value"] == 79
    h2 = sub(tmp_path, "old", tz)
    h2.cycle(at(tz, 10, 12, 8, 0), [("2026-10-09", 79)])               # night key 2026-10-10
    assert "observed_at" not in h2.last()


def test_the_expected_night_wins_over_an_older_one(tz, tmp_path):
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 12, 9, 0), [(TODAY_START, 66), (PREV_START, 79), ("2026-10-09", 91)])
    assert h.last()["value"] == 66
    assert set(h.state()["nights"]) == {"2026-10-12", "2026-10-11"}   # nothing older is kept


# ── TC-1160 ──────────────────────────────────────────────────────────────────

def test_restart_republishes_the_same_stamp_and_nothing_as_fresh(tz, tmp_path):
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 12, 7, 0), [(TODAY_START, 82)])
    stamp = h.last()["observed_at"]
    on_disk = h.state_path.read_bytes()

    h.restart()                                                       # a new process
    h.cycle(at(tz, 10, 12, 19, 0), error=h.f.SourceFailure("cpap-myair: network"))
    assert h.last()["value"] == 82 and h.last()["observed_at"] == stamp
    assert json.loads(on_disk)["nights"]["2026-10-12"]["observed_at"] == \
        h.state()["nights"]["2026-10-12"]["observed_at"]

    h.cycle(at(tz, 10, 13, 6, 0), error=h.f.SourceFailure("cpap-myair: network"))
    assert h.last()["value"] == 82 and h.last()["observed_at"] == stamp   # yesterday's, aging
    h.cycle(at(tz, 10, 13, 14, 0), error=h.f.SourceFailure("cpap-myair: network"))
    assert "observed_at" not in h.last()                              # and then unavailable


def test_state_file_is_private_and_holds_no_credential(tz, tmp_path):
    h = Harness(tmp_path, tz)
    env = dict(h.env, MYAIR_PASSWORD="PASSWORD-SENTINEL-9913", MYAIR_EMAIL="SENTINEL-EMAIL-1")
    h.f.run_cycle(env, now=at(tz, 10, 12, 10, 0), tz=tz, poster=h.poster,
                  reader=lambda e, n, t: ([h.f.Record(h.f.parse_night_date(TODAY_START), 82)], 0))
    text = h.state_path.read_text(encoding="utf-8")
    assert "SENTINEL" not in text and "Authorization" not in text
    assert set(json.loads(text)) <= {"nights", "__auth_failures__", "__auth_failed__"}
    if os.name == "posix":
        assert (h.state_path.stat().st_mode & 0o777) == 0o600
    assert not Path(str(h.state_path) + ".tmp").exists()


def test_a_corrupt_state_file_is_empty_not_a_crash(tz, tmp_path):
    h = Harness(tmp_path, tz)
    for junk in ("", "not json", "[]", '{"nights": 5}', '{"nights": {"x": 1}}'):
        h.state_path.write_text(junk, encoding="utf-8")
        h.cycle(at(tz, 10, 12, 9, 0), [(TODAY_START, 82)])
        assert h.last()["value"] == 82


def test_a_stamp_that_would_rejuvenate_a_night_is_dropped_on_load(tz, tmp_path):
    """The loader refuses an observed_at later than 12:00 local on the night-key date,
    in the future, or before the block existed - and refuses a bad score."""
    h = Harness(tmp_path, tz)
    now = at(tz, 10, 12, 13, 0)
    noon = utc(2026, 10, 12, 17, 0)
    good = {"score": 82, "observed_at": noon}
    for entry, kept in (
            (good, True),
            ({"score": 82, "observed_at": noon + 1}, False),              # later than noon
            ({"score": 82, "observed_at": now + 3600}, False),            # the future
            ({"score": 82, "observed_at": 1000}, False),                  # before the epoch floor
            ({"score": 101, "observed_at": noon}, False),
            ({"score": True, "observed_at": noon}, False),
            ({"score": 82, "observed_at": True}, False),
            ({"score": 82}, False),
            ("junk", False)):
        h.state_path.write_text(json.dumps({"nights": {"2026-10-12": entry}}), encoding="utf-8")
        state = h.f.load_state(str(h.state_path), now, tz)
        assert ("2026-10-12" in state["nights"]) is kept, entry


def test_the_writer_refuses_a_path_outside_its_own_state_directory(tz, tmp_path):
    h = Harness(tmp_path, tz)
    elsewhere = Path(str(tmp_path)) / "elsewhere.json"
    with pytest.raises(PermissionError):
        h.f.save_state(h.f.empty_state(), str(elsewhere), str(h.state_dir))
    with pytest.raises(PermissionError):
        h.f.open_for_write(str(h.state_dir / "other.json"), str(h.state_path), str(h.state_dir))
    assert not elsewhere.exists()
    h.f.save_state(h.f.empty_state(), str(h.state_path), str(h.state_dir))
    assert h.state_path.exists()


def test_a_failed_post_still_saves_the_frozen_stamp(tz, tmp_path):
    h = Harness(tmp_path, tz)
    _posted, notes, failures = h.cycle(at(tz, 10, 12, 7, 0), [(TODAY_START, 82)],
                                       poster=h.failing_poster)
    assert any(line.startswith("post sleep:") for line in failures)
    assert not any(line.startswith("published night") for line in notes)
    assert "2026-10-12" in h.state()["nights"]


# ── TC-1161 ──────────────────────────────────────────────────────────────────

EXPECTED_KEYS = {"kind", "id", "label", "icon", "unit", "value", "min", "max", "target",
                 "favourable", "window", "observed_at"}


def test_the_gauge_on_the_wire_is_exactly_the_plan_body(tz, tmp_path):
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 12, 7, 0), [(TODAY_START, 82)])
    body = h.last()
    assert set(body) == EXPECTED_KEYS
    assert {k: body[k] for k in EXPECTED_KEYS - {"observed_at"}} == {
        "kind": "gauge", "id": "sleep", "label": "Sleep", "icon": "sleep", "unit": "pts",
        "value": 82, "min": 0, "max": 100, "target": 80, "favourable": "high",
        "window": {"kind": "daily"}}
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$", body["observed_at"])
    for forbidden in ("previous_value", "previous_observed_at", "direction", "start", "end"):
        assert forbidden not in body and forbidden not in body["window"]


def test_the_unavailable_body_is_the_never_measured_body(tz, tmp_path):
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 12, 15, 0), [])
    body = h.last()
    assert set(body) == EXPECTED_KEYS - {"observed_at"}
    assert body["value"] == 0 and body["window"] == {"kind": "daily"}


def test_the_body_matches_naglights_own_gauge_struct(tz, tmp_path):
    """The other side of the contract, read from NagLight's source when this
    checkout has a sibling NagLight: every key we send is a JSON tag it declares,
    "daily" has a staleness horizon, and Validate's rules hold for our body."""
    gauge_go = Path(__file__).resolve().parents[2] / "NagLight" / "internal" / "gauge" / "gauge.go"
    if not gauge_go.exists():
        pytest.skip("no sibling NagLight checkout")
    source = gauge_go.read_text(encoding="utf-8")
    tags = set(re.findall(r'`json:"([a-z_]+)', source))
    h = Harness(tmp_path, tz)
    h.cycle(at(tz, 10, 12, 7, 0), [(TODAY_START, 82)])
    for key in set(h.last()) - {"kind"}:
        assert key in tags, key
    for key in h.last()["window"]:
        assert key in tags, key
    assert "daily" in source
    body = h.last()
    assert body["min"] < body["max"] and body["min"] <= body["target"] <= body["max"]
    assert body["favourable"] in ("high", "low")
    assert "direction" not in body                    # a standing line has no direction


# ── TC-1160 (continued): nothing older than the previous night is kept ──────

def test_nights_older_than_the_keep_window_are_pruned_on_the_next_save(tz, tmp_path):
    h = Harness(tmp_path, tz)
    old_noon = utc(2026, 10, 5, 17, 0)
    recent_noon = utc(2026, 10, 9, 17, 0)
    h.state_path.write_text(json.dumps({"nights": {
        "2026-10-05": {"score": 70, "observed_at": old_noon},
        "2026-10-09": {"score": 71, "observed_at": recent_noon}}}), encoding="utf-8")
    h.cycle(at(tz, 10, 12, 9, 0), [(TODAY_START, 82)])
    assert set(h.state()["nights"]) == {"2026-10-09", "2026-10-12"}
