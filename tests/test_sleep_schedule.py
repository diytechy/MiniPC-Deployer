"""The sleep feeder's fetch schedule, back-off, publication tracking and clock rules.

The S1 fix round (2026-10-11) after review. Four rules, each asserted against the
OTHER side of its contract - a counter of how often myAir was really asked, the
requests a loopback Okta actually received, literal UTC instants, and the wire
body NagLight would get:

  * the timer ticks every 30 minutes but myAir is asked only inside the fetch
    windows: roughly 4-6 sign-ins a day, not 48 (TC-1157);
  * after a CONFIRMED dead login the feeder makes ZERO Okta calls for 6 h (24 h
    when the authn status was LOCKED_OUT), keeps the wall grey from state, and
    `--retry-now` is the operator's way back in (TC-1168);
  * "the previous night's score stays" means a night the feeder actually
    PUBLISHED, not one a late fetch merely staged (TC-1159);
  * the clock is read again after the fetch, and a local date earlier than the
    newest published night refuses to select or publish anything (TC-1157).

Verifies: TC-1157, TC-1159, TC-1168, TC-1172, TC-1174
          (SR-071, SR-072, LLR-1073, LLR-1082..LLR-1084, LLR-1086, LLR-1090)
"""

import json

import pytest

from test_sleep_support import (
    CENTRAL, FakeMyAir, Harness, feeder, local, serve_myair, sub, utc)

TZ = CENTRAL
TODAY = ("2026-10-11", 82)          # the record for the night that ended 2026-10-12
PREV = ("2026-10-10", 79)           # ... and for the night that ended 2026-10-11
HOUR = 3600


def at(hour, minute=0, day=12, month=10):
    return local(TZ, 2026, month, day, hour, minute)


def locked():
    return feeder.AuthFailure("okta-authn: status LOCKED_OUT", code="LOCKED_OUT")


def refused():
    return feeder.AuthFailure("okta-authn: HTTP 401", status=401)


def two_refusals(h, start, error=None, step=30 * 60):
    """Two consecutive refused sign-ins, as the real schedule runs them."""
    error = error or refused()
    h.cycle(start, error=error)                    # the scheduled attempt (windows ignored by the helper)
    h.cycle(start + step, error=error, gated=True)  # the confirming retry, under the REAL schedule
    return start + step


# ── TC-1168 : the back-off ───────────────────────────────────────────────────

def test_inside_the_back_off_myair_is_never_asked_and_the_wall_stays_grey(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(7, 0), [TODAY])                                   # a good night on the wall
    confirmed = two_refusals(h, at(13, 0))
    assert h.state()["__auth_failed__"]["at"] == confirmed
    asked = h.calls
    for minutes in range(30, 6 * 60, 30):                        # every tick for 5.5 hours
        h.cycle(confirmed + minutes * 60, [TODAY], gated=True)
        assert "observed_at" not in h.last(), minutes            # grey, from state
    assert h.calls == asked                                      # ZERO calls to myAir
    assert h.state()["__auth_failures__"] == {"cpap-myair": 2}   # a skipped tick is not an outcome
    assert len(h.posts) > 10                                     # ...but the gauge was posted every tick


def test_the_back_off_is_six_hours_from_the_last_refusal_then_one_try_per_refusal(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(7, 0), [TODAY])
    confirmed = two_refusals(h, at(8, 0))
    asked = h.calls
    h.cycle(confirmed + 6 * HOUR - 60, error=refused(), gated=True)
    assert h.calls == asked                                      # one minute early: still no call
    again = confirmed + 6 * HOUR
    h.cycle(again, error=refused(), gated=True)
    assert h.calls == asked + 1                                  # exactly at six hours: one call
    assert h.state()["__auth_failed__"]["at"] == again           # the back-off restarts from this refusal
    h.cycle(again + 30 * 60, error=refused(), gated=True)
    h.cycle(again + 6 * HOUR - 60, error=refused(), gated=True)
    assert h.calls == asked + 1                                  # and the new back-off holds


def test_a_locked_out_account_is_left_alone_for_a_day(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(7, 0), [TODAY])
    confirmed = two_refusals(h, at(8, 0), error=locked())
    assert h.state()["__auth_failed__"]["code"] == "LOCKED_OUT"
    asked = h.calls
    for hours in (6, 7, 12, 23):
        h.cycle(confirmed + hours * HOUR, error=locked(), gated=True)
    assert h.calls == asked
    h.cycle(confirmed + 24 * HOUR, error=locked(), gated=True)
    assert h.calls == asked + 1


def test_a_successful_sign_in_after_the_back_off_clears_it(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(7, 0), [TODAY])
    confirmed = two_refusals(h, at(13, 0))
    h.cycle(confirmed + 6 * HOUR, [TODAY], gated=True)
    assert "__auth_failed__" not in h.state()
    assert h.state()["__auth_failures__"] == {}
    assert h.last()["value"] == 82 and "observed_at" in h.last()


def test_a_network_failure_after_the_back_off_leaves_the_marker_and_the_stamp_alone(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(7, 0), [TODAY])
    confirmed = two_refusals(h, at(8, 0))
    h.cycle(confirmed + 6 * HOUR, error=feeder.SourceFailure("net"), gated=True)
    assert h.state()["__auth_failed__"]["at"] == confirmed       # not refreshed by a non-auth failure
    assert "observed_at" not in h.last()                         # still grey


def test_the_real_reader_makes_zero_okta_calls_inside_the_back_off():
    """The same assertion against a loopback Okta that counts what ARRIVED."""
    import tempfile
    from pathlib import Path
    fake = FakeMyAir()
    fake.records = [{"startDate": "2026-10-11", "sleepScore": 82}]
    with tempfile.TemporaryDirectory() as tmp, serve_myair(fake) as (base, seen):
        h = Harness(Path(tmp))
        env = dict(h.env, MYAIR_EMAIL="sleeper@example.invalid", MYAIR_PASSWORD="wrong")  # privacy-ok: placeholder
        real = lambda e, n, t: h.f.read_myair(e, n, t, endpoints=fake.endpoints(base))  # noqa: E731
        marker = {"auth_failed": True, "at": at(8, 0)}
        h.state_path.write_text(json.dumps({"__auth_failed__": marker,
                                            "__auth_failures__": {"cpap-myair": 2}}), encoding="utf-8")
        for minutes in range(0, 6 * 60, 30):
            h.f.run_cycle(env, now=at(8, 0) + minutes * 60, tz=TZ, reader=real,
                          poster=h.poster, force_fetch=True)
        assert seen == [] and fake.requests == []                # NOTHING reached Okta
        h.f.run_cycle(env, now=at(8, 0) + 6 * HOUR, tz=TZ, reader=real, poster=h.poster)
        assert [path for _m, path in fake.requests][:2] == [
            "/oauth2/aus_test/v1/authorize", "/api/v1/authn"]    # ...then exactly one attempt


def test_retry_now_clears_the_marker_and_asks_at_once_outside_every_window(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(7, 0), [TODAY])
    confirmed = two_refusals(h, at(8, 0))
    asked = h.calls
    now = confirmed + HOUR                                       # inside the back-off, 09:30
    h.cycle(now, [TODAY], gated=True)
    assert h.calls == asked                                      # without the flag: nothing
    _p, notes, _f = h.cycle(now + 60, [TODAY], gated=True, retry_now=True)
    assert h.calls == asked + 1
    assert any("retry-now" in line for line in notes)
    assert "__auth_failed__" not in h.state()
    assert h.last()["value"] == 82 and "observed_at" in h.last()


def test_retry_now_that_is_refused_starts_the_count_again_and_does_not_hammer(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(7, 0), [TODAY])
    confirmed = two_refusals(h, at(8, 0))
    now = confirmed + HOUR
    h.cycle(now, error=refused(), gated=True, retry_now=True)
    assert h.state()["__auth_failures__"] == {"cpap-myair": 1}
    assert "__auth_failed__" not in h.state()
    h.cycle(now + 30 * 60, error=refused(), gated=True)          # the confirming retry
    assert h.state()["__auth_failed__"]["at"] == now + 30 * 60
    calls = h.calls
    h.cycle(now + 60 * 60, error=refused(), gated=True)
    assert h.calls == calls                                      # back-off again


def test_main_passes_retry_now_through(monkeypatch, tmp_path):
    seen = {}
    for key in ("SLEEP_ENABLED", "SLEEP_USER", "SLEEP_FEED_URL", "TIMEZONE", "MYAIR_EMAIL",
                "MYAIR_PASSWORD", "SLEEP_PUBLISH"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (("SLEEP_ENABLED", "true"), ("SLEEP_USER", "u"),
                       ("SLEEP_FEED_URL", "http://127.0.0.1:8787/api/feed"),
                       ("TIMEZONE", "Etc/UTC"), ("MYAIR_EMAIL", "a@example.invalid"),  # privacy-ok: placeholder
                       ("MYAIR_PASSWORD", "x")):
        monkeypatch.setenv(key, value)

    def fake_run_cycle(env, **kwargs):
        seen.update(kwargs)
        return [], ["note"], []

    monkeypatch.setattr(feeder, "run_cycle", fake_run_cycle)
    assert feeder.main(["--retry-now"]) == 0 and seen["retry_now"] is True
    assert feeder.main([]) == 0 and seen["retry_now"] is False


def test_the_marker_code_is_an_allow_listed_word_or_dropped():
    now = at(8, 0)
    assert feeder.validate_auth_marker({"auth_failed": True, "at": now, "code": "LOCKED_OUT"}, now)["code"] \
        == "LOCKED_OUT"
    for code in ("../etc", "has space", 5, None, "x" * 40, ""):
        marker = feeder.validate_auth_marker({"auth_failed": True, "at": now, "code": code}, now)
        assert marker == {"auth_failed": True, "at": now}, code


# ── TC-1157 : the fetch windows ──────────────────────────────────────────────

def half_hour_ticks(first, last, step=30 * 60):
    tick = first
    while tick <= last:
        yield tick
        tick += step


def run_day(tmp_path, first, last, record_from, jitter=0, error_before=None):
    """Tick every 30 minutes through a day, gated. Returns the instants myAir was asked."""
    h = Harness(tmp_path)
    asked = []
    for index, tick in enumerate(half_hour_ticks(first, last)):
        before = h.calls
        records = [("2026-10-11", 82)] if tick >= record_from else []
        h.cycle(tick + (jitter if index % 2 else 0), records, gated=True)
        if h.calls > before:
            asked.append(tick)
    return h, asked


def test_a_normal_day_asks_myair_a_handful_of_times_not_forty_eight(tmp_path):
    h, asked = run_day(tmp_path, at(0, 0), at(23, 30), record_from=at(9, 59))
    local_times = [(t - at(0, 0)) // 60 for t in asked]
    assert local_times == [6 * 60, 8 * 60, 10 * 60, 14 * 60 + 30, 20 * 60]
    assert 3 <= len(asked) <= 5
    # ...and every tick still posted: the 14:00 rule and the stamp are from state.
    assert len(h.posts) == 48


def test_the_windows_hold_with_the_timers_jitter(tmp_path):
    """Two minutes of RandomizedDelay on alternate ticks must not add or lose a call."""
    h, asked = run_day(tmp_path, at(0, 0), at(23, 30), record_from=at(9, 59), jitter=110)
    assert [(t - at(0, 0)) // 60 for t in asked] == [6 * 60, 8 * 60, 10 * 60, 14 * 60 + 30, 20 * 60]


def test_a_night_that_never_arrives_is_asked_for_until_four_pm_then_once_more_at_eight(tmp_path):
    h, asked = run_day(tmp_path, at(0, 0), at(23, 30), record_from=at(23, 59))
    minutes = [(t - at(0, 0)) // 60 for t in asked]
    assert minutes == [6 * 60, 8 * 60, 10 * 60, 12 * 60, 14 * 60, 20 * 60]   # the 20:00 catch-up is the one extra
    assert not any(at(16, 0) <= t < at(20, 0) for t in asked)
    assert not any(t > at(20, 0) for t in asked)                  # and only once, not every tick after it
    assert "observed_at" not in h.last()                         # and the 14:00 rule held all evening


def test_nothing_before_six_and_nothing_between_four_and_eight(tmp_path):
    h = Harness(tmp_path)
    for tick in (at(0, 0), at(3, 30), at(5, 59), at(16, 0), at(18, 30), at(19, 59)):
        h.cycle(tick, [TODAY], gated=True)
    assert h.calls == 0
    h.cycle(at(20, 0), [TODAY], gated=True)                      # the catch-up slot
    assert h.calls == 1
    h.cycle(at(20, 30), [TODAY], gated=True)
    h.cycle(at(23, 59), [TODAY], gated=True)
    assert h.calls == 1                                          # stored now, both slots made up


def test_an_upload_at_1730_is_fetched_and_published_by_the_2000_slot(tmp_path):
    h, asked = run_day(tmp_path, at(0, 0), at(23, 30), record_from=at(17, 30))
    assert at(20, 0) in asked and not any(at(16, 0) <= t < at(20, 0) for t in asked)
    assert asked.count(at(20, 0)) == 1 and at(20, 30) not in asked   # one fetch covers both slots
    assert h.last()["value"] == 82 and "observed_at" in h.last()
    assert "2026-10-12" in h.state()["published"]
    # Stamped noon on its night-key date (it arrived after noon), never the evening fetch.
    assert h.state()["nights"]["2026-10-12"]["observed_at"] == at(12, 0)


def test_an_upload_at_2100_is_not_fetched_that_night_and_shows_unavailable_next_morning(tmp_path):
    """The remaining limit (README): the night is staged by the next morning's fetch but
    was never on the wall, so the outage rule holds until the NEW night arrives."""
    h = Harness(tmp_path)
    for tick in half_hour_ticks(at(0, 0), at(23, 30)):
        h.cycle(tick, [TODAY] if tick >= at(21, 0) else [], gated=True)
    evening_calls = h.calls
    assert "observed_at" not in h.last()                         # nothing was published that day
    assert "2026-10-12" not in h.state()["nights"]               # 21:00 upload never fetched
    calls_before_morning = h.calls
    for tick in half_hour_ticks(at(0, 0, day=13), at(9, 30, day=13)):
        h.cycle(tick, [TODAY], gated=True)                       # only last night's record exists
        assert "observed_at" not in h.last(), tick               # unavailable, every tick
    assert h.calls > calls_before_morning                        # it WAS fetched (staged) in the morning
    assert "2026-10-12" in h.state()["nights"] and h.state()["published"] == {}
    h.cycle(at(10, 0, day=13), [TODAY, ("2026-10-12", 70)], gated=True)
    assert h.last()["value"] == 70 and "observed_at" in h.last()   # the new night arrives
    assert evening_calls >= 1


def test_a_stored_night_is_corrected_once_in_the_afternoon_and_once_in_the_evening(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(9, 0), [TODAY])                                   # stored
    h.calls = 0
    for tick in half_hour_ticks(at(9, 30), at(23, 30)):
        h.cycle(tick, [TODAY], gated=True)
    assert h.calls == 2


def test_a_missed_afternoon_slot_is_made_up_once_not_twice(tmp_path):
    """The feeder was down over 14:30; at 21:00 one fetch covers both slots."""
    h = Harness(tmp_path)
    h.cycle(at(9, 0), [TODAY])
    h.calls = 0
    h.cycle(at(21, 0), [TODAY], gated=True)
    h.cycle(at(21, 30), [TODAY], gated=True)
    assert h.calls == 1


def test_a_new_day_starts_a_new_schedule(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(9, 0), [TODAY])
    h.cycle(at(14, 30), [TODAY], gated=True)
    h.cycle(at(20, 0), [TODAY], gated=True)
    h.calls = 0
    h.cycle(at(6, 0, day=13), [("2026-10-12", 70)], gated=True)  # the next morning, night 13th not stored
    assert h.calls == 1


def test_a_first_refusal_is_confirmed_on_the_very_next_tick_whatever_the_hour(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(6, 0), error=refused(), gated=True)
    assert h.state()["__auth_failures__"] == {"cpap-myair": 1}
    h.cycle(at(6, 30), error=refused(), gated=True)              # not two hours later
    assert h.state()["__auth_failed__"]["at"] == at(6, 30)
    calls = h.calls
    h.cycle(at(12, 29), [TODAY], gated=True)
    assert h.calls == calls                                      # ...then 6 h of silence


def test_an_unasked_tick_does_not_reset_the_failure_count(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(19, 30), [TODAY])                                 # stored; nothing due for hours
    h.cycle(at(20, 0), error=refused(), gated=True)              # the evening correction is refused
    assert h.state()["__auth_failures__"] == {"cpap-myair": 1}
    h.cycle(at(20, 30), error=refused(), gated=True)
    assert h.state()["__auth_failed__"]["at"] == at(20, 30)


@pytest.mark.parametrize("label,day_start,day_end,expected_utc", [
    # 2026-03-08 is the clocks-forward day (23 h): CST until 08:00Z, CDT after.
    ("spring-forward", utc(2026, 3, 8, 6, 0), utc(2026, 3, 9, 4, 30),
     [utc(2026, 3, 8, 11, 0), utc(2026, 3, 8, 13, 0), utc(2026, 3, 8, 15, 0),
      utc(2026, 3, 8, 19, 30), utc(2026, 3, 9, 1, 0)]),
    # 2026-11-01 is the clocks-back day (25 h): CDT until 07:00Z, CST after.
    ("fall-back", utc(2026, 11, 1, 5, 0), utc(2026, 11, 2, 5, 30),
     [utc(2026, 11, 1, 12, 0), utc(2026, 11, 1, 14, 0), utc(2026, 11, 1, 16, 0),
      utc(2026, 11, 1, 20, 30), utc(2026, 11, 2, 2, 0)])])
def test_the_windows_are_local_time_on_the_days_the_clocks_change(
        tmp_path, label, day_start, day_end, expected_utc):
    """06:00 / 08:00 / 10:00 / 14:30 / 20:00 LOCAL, written as literal UTC instants.
    A feeder that added 24 h, or used a fixed offset, would be an hour out."""
    h = Harness(tmp_path)
    ten_local = expected_utc[2]
    asked = []
    for tick in half_hour_ticks(day_start, day_end):
        before = h.calls
        local_date = feeder.expected_night(tick, TZ)
        start = (local_date - feeder.dt.timedelta(days=1)).isoformat()
        records = [(start, 82)] if tick >= ten_local - 60 else []
        h.cycle(tick, records, gated=True)
        if h.calls > before:
            asked.append(tick)
    assert asked == expected_utc, label


# ── Fresh time: a cycle that straddles 14:00 ─────────────────────────────────

def test_a_cycle_that_starts_at_1359_and_ends_at_1401_applies_the_1400_rule(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(10, 0, day=11), [PREV])                           # last night's score, PUBLISHED yesterday
    start = at(13, 59)
    h.cycle(start, [PREV], clock=lambda: start + 120)            # the fetch took two minutes
    assert "observed_at" not in h.last()                         # decided at 14:01: unavailable
    control = sub(tmp_path, "ctl")
    control.cycle(at(10, 0, day=11), [PREV])
    control.cycle(start, [PREV], clock=lambda: start + 30)       # finishes at 13:59:30
    assert control.last()["value"] == 79                         # still before the grace hour


def test_the_stamp_is_the_decided_time_not_the_time_the_cycle_began(tmp_path):
    h = Harness(tmp_path)
    start = at(11, 59) + 30
    h.cycle(start, [TODAY], clock=lambda: at(12, 0) + 30)        # finished half a minute past noon
    assert h.state()["nights"]["2026-10-12"]["observed_at"] == at(12, 0)


def test_the_midnight_boundary_is_decided_after_the_fetch_too(tmp_path):
    h = Harness(tmp_path)
    start = at(23, 59, day=12) + 40
    h.cycle(start, [TODAY], clock=lambda: start + 60)            # ends on the 13th
    assert "observed_at" not in h.last()                         # the expected night is now the 13th


# ── TC-1159 : publication tracking ───────────────────────────────────────────

def test_a_previous_night_that_was_never_published_is_not_the_score_that_stays(tmp_path):
    """Outage 10-10 .. 10-13, recovery at 09:00 returns only night-key 10-12."""
    h = Harness(tmp_path)
    h.cycle(at(10, 0, day=10), [("2026-10-09", 88)])             # the 10th's night: published
    assert h.last()["value"] == 88
    for day in (11, 12):                                         # the outage
        for hour in (7, 10, 14, 20):
            h.cycle(at(hour, 0, day=day), error=feeder.SourceFailure("net"))
    assert "observed_at" not in h.last()
    h.cycle(at(9, 0, day=13), [("2026-10-11", 66)], gated=True)  # only night-key 2026-10-12
    assert h.state()["nights"]["2026-10-12"]["score"] == 66      # it is staged...
    assert "2026-10-12" not in h.state()["published"]            # ...but was never on the wall
    assert "observed_at" not in h.last()                         # so: unavailable, not 66


def test_that_night_is_not_resurrected_by_later_ticks_before_two(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(9, 0, day=13), [("2026-10-11", 66)])
    for hour, minute in ((9, 30), (11, 0), (13, 59)):
        h.cycle(at(hour, minute, day=13), [("2026-10-11", 66)])
        assert "observed_at" not in h.last(), (hour, minute)


def test_once_the_expected_night_arrives_it_is_published_and_recorded(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(9, 0, day=13), [("2026-10-11", 66)])
    h.cycle(at(10, 30, day=13), [("2026-10-11", 66), ("2026-10-12", 74)])
    assert h.last()["value"] == 74
    assert set(h.state()["published"]) == {"2026-10-13"}
    assert h.state()["published"]["2026-10-13"] == at(10, 30, day=13)


def test_a_published_previous_night_still_stays_before_two(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(10, 0, day=11), [("2026-10-10", 79)])             # night key 10-11, published that day
    assert set(h.state()["published"]) == {"2026-10-11"}
    h.cycle(at(8, 0, day=12), [("2026-10-10", 79)])              # next morning, nothing newer
    assert h.last()["value"] == 79


def test_a_night_whose_post_failed_was_not_published(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(10, 0, day=11), [PREV], poster=h.failing_poster)
    assert h.state()["published"] == {}
    h.cycle(at(8, 0, day=12), [PREV])
    assert "observed_at" not in h.last()


def test_unavailable_posts_never_mark_anything_published(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(15, 0), [])
    assert h.state()["published"] == {}


def test_the_published_set_has_its_own_validator_and_is_pruned(tmp_path):
    h = Harness(tmp_path)
    now = at(9, 0)
    junk = {"2026-10-11": now - 100, "nonsense": now, "2026-10-10": True, "2026-10-09": "x",
            "2026-10-08": 5, "2026-10-12": now + 99999, "2026-10-05": at(10, 0, day=5)}
    h.state_path.write_text(json.dumps({"published": junk}), encoding="utf-8")
    # A stamp "in the future" is KEPT: after a backwards clock step every honest
    # publication looks like that, and it is the evidence the clock check needs.
    assert h.f.load_state(str(h.state_path), now, TZ)["published"] == {
        "2026-10-11": now - 100, "2026-10-12": now + 99999, "2026-10-05": at(10, 0, day=5)}
    h.cycle(now, [("2026-10-11", 82)])
    assert set(h.state()["published"]) == {"2026-10-11", "2026-10-12"}   # 10-05 pruned
    assert "2026-10-05" not in h.state()["published"]


# ── TC-1157 : the clock going backwards ──────────────────────────────────────

def test_a_local_date_before_the_newest_published_night_selects_and_posts_nothing(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(10, 0, day=12), [TODAY])                          # published 2026-10-12
    before = h.state_path.read_bytes()
    posts, calls = len(h.posts), h.calls
    posted, notes, failures = h.cycle(at(10, 0, day=11), [PREV], gated=True)   # the clock says the 11th
    assert posted == [] and len(h.posts) == posts and h.calls == calls
    assert any("gone backwards" in line for line in failures)
    assert h.state_path.read_bytes() == before                   # state untouched
    for line in notes + failures:
        assert "82" not in line.replace("2026", "")


def test_a_clock_that_steps_back_during_the_fetch_is_caught_after_it(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(10, 0, day=12), [TODAY])
    posts = len(h.posts)
    start = at(23, 59, day=12)
    posted, _n, failures = h.cycle(start, [TODAY], clock=lambda: at(10, 0, day=11))
    assert posted == [] and len(h.posts) == posts
    assert any("gone backwards" in line for line in failures)


def test_the_same_day_or_later_is_not_backwards(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(10, 0, day=12), [TODAY])
    for tick in (at(0, 5, day=12), at(10, 0, day=12), at(9, 0, day=13)):
        posted, _n, failures = h.cycle(tick, [TODAY])
        assert posted and not any("backwards" in line for line in failures)


# ── TC-1172 / TC-1174 : unit and README ──────────────────────────────────────

def test_the_unit_waits_for_the_clock_to_be_set():
    from test_sleep_deploy import UNIT, directives
    unit = directives(UNIT)
    assert "time-sync.target" in unit[("Unit", "After")][0].split()
    assert "time-sync.target" in unit[("Unit", "Wants")][0].split()
    assert "network-online.target" in unit[("Unit", "After")][0].split()


def test_the_readme_documents_the_back_off_the_retry_and_the_known_limits():
    from test_sleep_deploy import README
    text = README.read_text(encoding="utf-8")
    for must in ("--retry-now", "6 h", "24 h", "LOCKED_OUT", "systemd-run", "Known limits",
                 "36 h", "5 h", "EnvironmentFile", "06:00", "14:30", "20:00", "published",
                 "time-sync.target", "unproven"):
        assert must.lower() in text.lower(), must


def test_no_comment_cites_a_test_file_that_does_not_exist():
    import re
    from test_sleep_support import REPO
    for path in (REPO / "stack" / "sleep").iterdir():
        if path.suffix in (".py", ".sh", ".service", ".timer", ".md"):
            for cited in re.findall(r"tests/(test_[a-z_]+\.py)", path.read_text(encoding="utf-8")):
                assert (REPO / "tests" / cited).exists(), (path.name, cited)


def test_a_refusal_after_a_network_failure_still_restarts_the_back_off(tmp_path):
    """The network failure reset the count to zero, so the next refusal is 'one of two' -
    but the marker is still set, and it must be refreshed or myAir is asked every tick."""
    h = Harness(tmp_path)
    h.cycle(at(7, 0), [TODAY])
    confirmed = two_refusals(h, at(8, 0))
    later = confirmed + 6 * HOUR
    h.cycle(later, error=feeder.SourceFailure("net"), gated=True)
    h.cycle(later + 30 * 60, error=refused(), gated=True)
    assert h.state()["__auth_failed__"]["at"] == later + 30 * 60
    calls = h.calls
    h.cycle(later + 60 * 60, error=refused(), gated=True)
    assert h.calls == calls
