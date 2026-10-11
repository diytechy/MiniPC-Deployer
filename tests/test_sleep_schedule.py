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
    SYNCED, CENTRAL, FakeMyAir, Harness, feeder, local, serve_myair, sub, utc)

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
                          poster=h.poster, force_fetch=True, sync_check=SYNCED)
        assert seen == [] and fake.requests == []                # NOTHING reached Okta
        h.f.run_cycle(env, now=at(8, 0) + 6 * HOUR, tz=TZ, reader=real, poster=h.poster,
                      sync_check=SYNCED)
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
                 "time-sync.target", "unproven", "NTPSynchronized", "/run/systemd/timesync/synchronized",
                 "systemd-timesyncd", "refuses to run as root", "trend arrow", "must not contain",
                 "06:00 and 22:00", "never first published before 06:00", "quarantined"):
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


# ═════════════════════════════════════════════════════════════════════════════
# Fix round 3
# ═════════════════════════════════════════════════════════════════════════════

NET = feeder.SourceFailure("cpap-myair: graphql: TimeoutError")


# ── 1. a slot is done only after a read that did not fail ───────────────────

def test_a_timeout_at_2000_then_success_at_2030_publishes_the_late_night(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(19, 30), [], gated=True)                          # nothing due, night missing
    h.cycle(at(20, 0), error=NET, gated=True)                    # the catch-up: a timeout
    schedule = h.state()["schedule"]
    assert schedule["attempt_at"] == at(20, 0)                   # recorded regardless...
    assert "eve" not in schedule["done"] and schedule["fails"] == {"eve": 1}   # ...but not consumed
    assert "observed_at" not in h.last()
    h.cycle(at(20, 30), [TODAY], gated=True)                     # recovered, inside the retry window
    assert h.last()["value"] == 82 and "observed_at" in h.last()
    assert set(h.state()["schedule"]["done"]) == {"pm", "eve"} and h.state()["schedule"]["fails"] == {}
    assert "2026-10-12" in h.state()["published"]


def test_a_failed_correction_slot_is_retried_not_consumed(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(9, 0), [TODAY])                                   # stored
    h.cycle(at(14, 30), error=NET, gated=True)
    assert "pm" not in h.state()["schedule"]["done"]
    h.cycle(at(15, 0), [TODAY], gated=True)
    assert "pm" in h.state()["schedule"]["done"]
    calls = h.calls
    h.cycle(at(15, 30), [TODAY], gated=True)
    assert h.calls == calls


def test_retries_are_bounded_to_three_attempts_thirty_minutes_apart(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(19, 30), [], gated=True)
    for tick in (at(20, 0), at(20, 30), at(21, 0)):
        h.cycle(tick, error=NET, gated=True)
    assert h.calls == 3
    for tick in (at(21, 30), at(22, 0), at(22, 30), at(23, 30)):
        h.cycle(tick, [TODAY], gated=True)                       # the source is back; the budget is spent
    assert h.calls == 3
    assert "observed_at" not in h.last()


def test_a_retry_waits_thirty_minutes_after_a_failed_attempt(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(20, 0), error=NET, gated=True)
    h.cycle(at(20, 0) + 60, [TODAY], gated=True)                 # a minute later: too soon
    assert h.calls == 1
    h.cycle(at(20, 30), [TODAY], gated=True)
    assert h.calls == 2


def test_a_timeout_followed_by_recovery_after_2200_does_not_retry(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(20, 0), error=NET, gated=True)
    h.cycle(at(22, 30), [TODAY], gated=True)                     # the feeder was down until 22:30
    h.cycle(at(23, 0), [TODAY], gated=True)
    assert h.calls == 1
    assert "observed_at" not in h.last()


def test_a_retry_may_start_before_2200_but_none_at_or_after_it(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(21, 30), error=NET, gated=True)                   # an atypical first attempt (late tick)
    h.cycle(at(21, 59), [TODAY], gated=True)                     # within the slack, before 22:00
    assert h.calls == 2
    h2 = sub(tmp_path, "b")
    h2.cycle(at(21, 30), error=NET, gated=True)
    h2.cycle(at(22, 0), [TODAY], gated=True)
    assert h2.calls == 1


def test_the_auth_back_off_overrides_slot_retries(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(7, 0), [TODAY])
    h.cycle(at(14, 30), error=refused(), gated=True)             # a slot attempt refused...
    h.cycle(at(15, 0), error=refused(), gated=True)              # ...and confirmed
    calls = h.calls
    for tick in (at(15, 30), at(16, 0), at(20, 0), at(20, 30)):
        h.cycle(tick, [TODAY], gated=True)
    assert h.calls == calls                                      # back-off holds over every slot


# ── 2. --retry-now refuses to run as root or on someone else's state ────────

def test_retry_now_refuses_root_with_a_one_line_reason():
    reason = feeder.retry_now_refusal("/var/lib/homehub-sleep/sleep-state.json", uid=0)
    assert reason and "root" in reason and "\n" not in reason


def test_retry_now_refuses_a_state_file_owned_by_someone_else():
    reason = feeder.retry_now_refusal("/x/state.json", uid=1000, owner_of=lambda path: 999)
    assert reason and "owned by another user" in reason and "\n" not in reason
    assert feeder.retry_now_refusal("/x/state.json", uid=1000, owner_of=lambda path: 1000) is None


def test_retry_now_is_allowed_when_there_is_no_state_file_yet(tmp_path):
    assert feeder.retry_now_refusal(str(tmp_path / "missing.json"), uid=1000) is None


def test_main_exits_nonzero_before_doing_anything_when_run_as_root(monkeypatch, tmp_path, capsys):
    for key in ("SLEEP_ENABLED", "SLEEP_USER", "SLEEP_FEED_URL", "TIMEZONE", "MYAIR_EMAIL",
                "MYAIR_PASSWORD", "SLEEP_PUBLISH", "STATE_DIRECTORY", "SLEEP_STATE_FILE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (("SLEEP_ENABLED", "true"), ("SLEEP_USER", "u"),
                       ("SLEEP_FEED_URL", "http://127.0.0.1:8787/api/feed"),
                       ("TIMEZONE", "Etc/UTC"), ("MYAIR_EMAIL", "a@example.invalid"),  # privacy-ok: placeholder
                       ("MYAIR_PASSWORD", "x"), ("STATE_DIRECTORY", str(tmp_path))):
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(feeder.os, "geteuid", lambda: 0, raising=False)

    def boom(*a, **k):
        raise AssertionError("must not run a cycle")

    monkeypatch.setattr(feeder, "run_cycle", boom)
    assert feeder.main(["--retry-now"]) == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "root" in err and len(err.strip().splitlines()) == 1
    assert list(tmp_path.iterdir()) == []                        # and wrote nothing


# ── 3. future published keys ────────────────────────────────────────────────

def test_a_published_key_later_than_tomorrow_is_dropped_on_load_with_a_status_line(tmp_path):
    h = Harness(tmp_path)
    now = at(9, 0)
    h.state_path.write_text(json.dumps({"published": {
        "2026-10-11": now - 100, "2026-10-12": now - 50, "2026-10-13": now,        # yesterday, today, tomorrow
        "2026-10-14": now, "2099-01-01": now}}), encoding="utf-8")
    state = h.f.load_state(str(h.state_path), now, TZ)
    assert set(state["published"]) == {"2026-10-11", "2026-10-12", "2026-10-13"}   # tomorrow is kept
    assert any("2 published night key" in note for note in state["_notes"])


def test_a_forward_jumped_key_cannot_wedge_the_feeder(tmp_path):
    h = Harness(tmp_path)
    h.state_path.write_text(json.dumps({"published": {"2099-01-01": at(9, 0)}}), encoding="utf-8")
    _p, notes, failures = h.cycle(at(9, 0), [TODAY])
    assert any("ignored 1 published night key" in line for line in notes)
    assert not any("backwards" in line for line in failures)
    assert h.last()["value"] == 82
    assert "2099-01-01" not in h.state()["published"]


def test_a_key_of_tomorrow_is_a_real_backwards_step_and_retry_now_clears_it(tmp_path):
    h = Harness(tmp_path)
    h.state_path.write_text(json.dumps({"published": {"2026-10-13": at(9, 0)}}), encoding="utf-8")
    posted, _n, failures = h.cycle(at(9, 0), [TODAY])
    assert posted == [] and any("backwards" in line for line in failures)
    posted, notes, failures = h.cycle(at(9, 5), [TODAY], retry_now=True)
    assert posted and not any("backwards" in line for line in failures)
    assert any("retry-now: cleared 1 published night key" in line for line in notes)
    assert "2026-10-13" not in h.state()["published"] and h.last()["value"] == 82


# ── 4. same-date rollback: quarantine, never an earlier night ───────────────

def test_sols_timeline_a_backward_step_within_the_day_posts_unavailable_not_yesterday(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(10, 0, day=11), [("2026-10-10", 90)])             # Oct 11's night: 90, published
    assert h.last()["value"] == 90
    h.cycle(at(10, 0, day=12), [("2026-10-11", 30)])             # Oct 12's night: 30, published at 10:00
    assert h.last()["value"] == 30
    stamp = h.state()["nights"]["2026-10-12"]["observed_at"]
    # Reboot; the clock comes up at 09:30 on Oct 12; the fetch returns only yesterday's record.
    h.cycle(at(9, 30, day=12), [("2026-10-10", 90)])
    assert "observed_at" not in h.last()                         # unavailable - NOT 90
    assert h.state()["nights"]["2026-10-12"]["observed_at"] == stamp   # quarantined, not destroyed
    assert "2026-10-12" in h.state()["published"]
    # The clock passes the stamp: the observation is reconsidered and shows again.
    h.cycle(at(10, 30, day=12), [("2026-10-10", 90)])
    assert h.last()["value"] == 30 and "observed_at" in h.last()


def test_once_today_is_published_no_earlier_night_is_ever_selected_that_day(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(10, 0, day=11), [("2026-10-10", 90)])
    h.cycle(at(10, 0, day=12), [("2026-10-11", 30)])
    state = json.loads(h.state_path.read_text(encoding="utf-8"))
    del state["nights"]["2026-10-12"]                            # today's record is simply gone
    h.state_path.write_text(json.dumps(state), encoding="utf-8")
    for tick in (at(10, 30, day=12), at(13, 0, day=12)):
        h.cycle(tick, [("2026-10-10", 90)])
        assert "observed_at" not in h.last(), tick


def test_a_future_stamped_night_is_loaded_not_discarded(tmp_path):
    h = Harness(tmp_path)
    now = at(9, 0)
    h.state_path.write_text(json.dumps({"nights": {"2026-10-12": {"score": 77, "observed_at": at(11, 0)}}}),
                            encoding="utf-8")
    loaded = h.f.load_state(str(h.state_path), now, TZ)
    assert loaded["nights"]["2026-10-12"]["score"] == 77
    assert h.f.decide_night(loaded["nights"], {}, now, TZ) is None            # quarantined from display
    assert h.f.decide_night(loaded["nights"], {}, at(11, 0), TZ) == "2026-10-12"


# ── 5. no first publication before 06:00; recovery only 06:00-22:00 ─────────

def test_a_forced_fetch_at_0030_stages_the_night_but_publishes_it_at_0600(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(10, 0, day=11), [("2026-10-10", 79)])             # yesterday's night, published
    h.cycle(at(0, 30, day=12), [("2026-10-11", 66)])             # a recovery attempt at 00:30
    assert h.state()["nights"]["2026-10-12"]["score"] == 66      # staged
    assert "2026-10-12" not in h.state()["published"]            # NOT published
    assert h.last()["value"] == 79                               # the published previous night stays
    h.cycle(at(5, 30, day=12), [("2026-10-11", 66)])
    assert h.last()["value"] == 79 and "2026-10-12" not in h.state()["published"]
    h.cycle(at(6, 0, day=12), [("2026-10-11", 66)])
    assert h.last()["value"] == 66 and "2026-10-12" in h.state()["published"]
    stamp = h.state()["nights"]["2026-10-12"]["observed_at"]
    assert stamp == at(6, 0, day=12)                             # frozen at 06:00, inside the tracking day
    assert stamp >= at(2, 0, day=12)                             # (NagLight's day starts at 02:00)


def test_the_0030_back_off_recovery_timeline(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(10, 0, day=11), [("2026-10-10", 79)])
    confirmed = two_refusals(h, at(18, 0, day=11))               # marker at 18:30
    assert confirmed == at(18, 30, day=11)
    calls = h.calls
    for minutes in range(0, 5 * 60 + 1, 30):                     # 00:30 .. 05:30 on the 12th
        h.cycle(at(0, 30, day=12) + minutes * 60, [("2026-10-11", 66)], gated=True)
        assert "observed_at" not in h.last() or h.last()["value"] == 79   # never today's night
    assert h.calls == calls                                      # the 6 h had elapsed at 00:30; still not asked
    assert "2026-10-12" not in h.state()["published"]
    h.cycle(at(6, 0, day=12), [("2026-10-11", 66)], gated=True)  # 06:00: the recovery attempt
    assert h.calls == calls + 1
    assert h.last()["value"] == 66
    assert h.state()["nights"]["2026-10-12"]["observed_at"] == at(6, 0, day=12)


def test_a_back_off_recovery_is_not_attempted_from_2200(tmp_path):
    h = Harness(tmp_path)
    h.cycle(at(7, 0), [TODAY])
    two_refusals(h, at(15, 30))                                  # marker at 16:00 -> elapsed 22:00
    calls = h.calls
    for tick in (at(22, 0), at(22, 30), at(23, 30)):
        h.cycle(tick, [TODAY], gated=True)
    assert h.calls == calls
    h.cycle(at(6, 0, day=13), [("2026-10-12", 70)], gated=True)
    assert h.calls == calls + 1


def test_a_normal_morning_arrival_still_publishes_at_once(tmp_path):
    """The 06:00 floor costs nothing when the record arrives after 06:00."""
    h = Harness(tmp_path)
    h.cycle(at(9, 45), [TODAY])
    assert h.state()["nights"]["2026-10-12"]["observed_at"] == at(9, 45)
    assert h.last()["value"] == 82


# ── 6. real clock synchronisation ───────────────────────────────────────────

class FakeRun(object):
    def __init__(self, stdout="", returncode=0, raises=None):
        self.stdout, self.returncode, self.raises, self.calls = stdout, returncode, raises, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises:
            raise self.raises
        return type("R", (), {"stdout": self.stdout, "returncode": self.returncode})()


def test_the_timesyncd_flag_file_is_enough_and_timedatectl_is_not_asked():
    run = FakeRun(stdout="no")
    result = feeder.clock_synchronized(exists=lambda path: path == "/run/systemd/timesync/synchronized",
                                       runner=run)
    assert result[0] is True and run.calls == []


@pytest.mark.parametrize("stdout,expected", [("yes\n", True), ("YES", True), ("no\n", False), ("", None),
                                              ("maybe", None)])
def test_without_the_flag_file_timedatectl_decides(stdout, expected):
    run = FakeRun(stdout=stdout)
    result = feeder.clock_synchronized(exists=lambda path: False, runner=run)
    assert result[0] is expected
    argv, kwargs = run.calls[0]
    assert argv == ["timedatectl", "show", "-p", "NTPSynchronized", "--value"]
    assert kwargs["timeout"] == 5


@pytest.mark.parametrize("run", [FakeRun(raises=FileNotFoundError()), FakeRun(returncode=1, stdout="yes"),
                                 FakeRun(raises=feeder.subprocess.TimeoutExpired("timedatectl", 5))])
def test_when_neither_signal_is_available_the_answer_is_unknown(run):
    assert feeder.clock_synchronized(exists=lambda path: False, runner=run)[0] is None


@pytest.mark.parametrize("signal", [False, None])
def test_an_unsynchronised_clock_fetches_nothing_and_posts_nothing(tmp_path, signal):
    h = Harness(tmp_path)
    posted, notes, failures = h.cycle(at(9, 0), [TODAY], sync=lambda: (signal, "mocked"))
    assert posted == [] and h.posts == [] and h.calls == 0
    assert any("not known to be synchronised" in line and "mocked" in line for line in failures)
    assert not h.state_path.exists()                             # and no state was written


def test_the_default_check_is_the_real_one(monkeypatch, tmp_path):
    h = Harness(tmp_path)
    monkeypatch.setattr(feeder, "clock_synchronized", lambda: (False, "mocked"))
    posted, _n, failures = feeder.run_cycle(dict(h.env), now=at(9, 0), tz=TZ, poster=h.poster,
                                            reader=lambda e, n, t: ([], 0), force_fetch=True)
    assert posted == [] and h.posts == [] and failures
    monkeypatch.setattr(feeder, "clock_synchronized", lambda: (True, "mocked"))
    posted, _n, failures = feeder.run_cycle(dict(h.env), now=at(9, 0), tz=TZ, poster=h.poster,
                                            reader=lambda e, n, t: ([], 0), force_fetch=True)
    assert posted and h.posts


def test_the_synchronisation_flag_path_is_timesyncds():
    assert feeder.SYNC_FLAG_PATH == "/run/systemd/timesync/synchronized"
