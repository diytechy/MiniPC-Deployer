"""TC-1486..TC-1492 - the heart gauge from the watch's 5-minute samples.

Ruled 2026-10-25 (HomeHub docs/HEART_GAUGE_PLAN_2026-10-25.md, decisions
8.1-8.4): the hub computes a nightly resting heart rate from Google Health's
`heart-rate` samples and colours it against the Owner's own usual. What is
asserted, by behaviour:

  * the knobs, and HEART_SOURCE selecting the path (TC-1486);
  * the page parsers read the OBSERVED shape and a bad point costs itself, and
    the reader sends both filters and walks pages (TC-1487);
  * cleaning: one device, the band, duplicate instants, and blips - a reading
    at least 10 bpm below both neighbours - dropped (TC-1488);
  * the night: its window (session start + 9 h, else 22:00-10:00 local, DST
    included), the coverage gate, the lowest 6-sample run (TC-1489);
  * the record: upsert keeps a frozen stamp, the loader drops what is not
    credible, a stamp outside its own morning is forgotten (TC-1490);
  * the target: a fixed 75 until 7 same-device nights, then their median, and
    the bar 12 either side (TC-1491);
  * the cycle: 06:00 floor, frozen stamp, 14:00 rule, a confirmed sign-in
    failure greys the gauge and KEEPS the record, and no line carries a bpm
    (TC-1492).

The zone is US Central written out by hand (the dev PC runs Python 3.8, which
has no `zoneinfo`), exactly as tests/test_heart_feeder.py does.

Verifies: TC-1486, TC-1487, TC-1488, TC-1489, TC-1490, TC-1491, TC-1492
          (SR-073, LLR-1314..LLR-1319)
"""

import importlib.util
import json
import sys
import urllib.parse
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path

import pytest

from conftest import loopback_server

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "stack" / "weight" / "weight_feeder.py"

_spec = importlib.util.spec_from_file_location("weight_feeder_heart_samples", MODULE_PATH)
feeder = importlib.util.module_from_spec(_spec)
sys.modules["weight_feeder_heart_samples"] = feeder
_spec.loader.exec_module(feeder)


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


class FakeZoneinfo(object):
    @staticmethod
    def ZoneInfo(name):
        if name == "America/Chicago":
            return CT
        raise KeyError(name)


@pytest.fixture(autouse=True)
def hub_has_a_zone(monkeypatch):
    monkeypatch.setattr(feeder, "_load_zoneinfo", lambda: FakeZoneinfo)


def L(month, day, hour, minute=0, year=2026):
    """Epoch seconds of a LOCAL Central wall time."""
    return int(datetime(year, month, day, hour, minute, tzinfo=CT).timestamp())


WATCH = "FFit"
NIGHT = date(2026, 10, 25)                 # the night that ENDS on Sunday 25 Oct


def night_samples(day=NIGHT, floor=60, base=78, device=WATCH, start_hour=22,
                  end_hour=10):
    """Every 5 minutes from 22:00 the evening before to 10:00 on `day`: `base`
    bpm, with a quiet half hour from 03:00 at `floor`."""
    out = []
    t = L(day.month, day.day, 0, year=day.year) - (24 - start_hour) * 3600
    end = L(day.month, day.day, end_hour, year=day.year)
    quiet = (L(day.month, day.day, 3, year=day.year), L(day.month, day.day, 3, 25, year=day.year))
    while t <= end:
        out.append((t, floor if quiet[0] <= t <= quiet[1] else base, device))
        t += 300
    return out


def make_env(tmp_path, **extra):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:1/api/feed",
           "TIMEZONE": "America/Chicago", "HEART_ENABLED": "1",
           "HEART_SOURCE": "samples",
           "WEIGHT_STATE_FILE": str(state_dir / "weight-state.json"),
           "STATE_DIRECTORY": str(state_dir)}
    env.update(extra)
    return env


def cycle(env, now, read=None, error=None):
    """One run_heart_cycle with a scripted read -> (posted, failures, body)."""
    bodies = []

    def reader(_env):
        if error is not None:
            raise error
        return read if read is not None else ([], [])

    def poster(body, url, e, t):
        bodies.append(body)
        return True, "HTTP 200"

    posted, failures = feeder.run_heart_cycle(env, now=now, reader=reader, poster=poster)
    return posted, failures, bodies[-1]


def state_of(env):
    return json.loads(Path(env["WEIGHT_STATE_FILE"]).read_text(encoding="utf-8"))


def seed_nights(env, nights):
    Path(env["WEIGHT_STATE_FILE"]).write_text(
        json.dumps({feeder.HEART_NIGHTS_KEY: nights}), encoding="utf-8")


def stored_night(rhr, device=WATCH, observed_at=None):
    night = {"rhr": rhr, "p25": float(rhr + 2), "mean": float(rhr + 5), "n": 100,
             "device": device, "window": "session"}
    if observed_at is not None:
        night["observed_at"] = observed_at
    return night


def utc_text(epoch):
    return datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── TC-1486: knobs and the source switch ─────────────────────────────────────

def test_the_samples_knobs_default_and_refuse(tmp_path):
    cfg = feeder.resolve_heart_samples_config({})
    assert cfg == {"device": "FFit", "target": 75, "span": 24, "nights": 28,
                   "min_nights": 7}
    for bad in ({"HEART_COLOUR_SPAN_BPM": "23"}, {"HEART_COLOUR_SPAN_BPM": "2"},
                {"HEART_TARGET_BPM": "200"}, {"HEART_TARGET_BPM": "7x"},
                {"HEART_BASELINE_NIGHTS": "3"},
                {"HEART_MIN_BASELINE_NIGHTS": "29"},
                {"HEART_SAMPLE_DEVICE": "bad;name"}):
        with pytest.raises(ValueError):
            feeder.resolve_heart_samples_config(bad)
    assert feeder.resolve_heart_source({}) == "daily"
    assert feeder.resolve_heart_source({"HEART_SOURCE": "samples"}) == "samples"
    with pytest.raises(ValueError):
        feeder.resolve_heart_source({"HEART_SOURCE": "nightly"})


def test_samples_mode_does_not_touch_the_daily_reader(tmp_path):
    env = make_env(tmp_path)
    seen = []

    def daily(_e):
        seen.append("daily")
        raise AssertionError("the daily reader was called in samples mode")

    def poster(body, url, e, t):
        return True, "HTTP 200"

    # The samples cycle's reader is the injected one; the daily reader map is
    # not consulted at all.
    feeder.HEART_READERS["google-health"], original = daily, feeder.HEART_READERS["google-health"]
    try:
        posted, failures = feeder.run_heart_cycle(env, now=L(10, 25, 9),
                                                  reader=lambda e: ([], []), poster=poster)
    finally:
        feeder.HEART_READERS["google-health"] = original
    assert seen == [] and posted == [("heart", False, True)] and failures == []


def test_check_mode_accepts_samples_config_and_refuses_a_bad_knob(tmp_path, monkeypatch, capsys):
    base = {"WEIGHT_ENABLED": "true", "WEIGHT_USER": "u",
            "WEIGHT_FEED_URL": "http://127.0.0.1:8080/api/feed",
            "HEART_ENABLED": "1", "HEART_SOURCE": "samples", "TIMEZONE": "America/Chicago"}
    for key in list(base):
        monkeypatch.setenv(key, base[key])
    assert feeder.main(["--check"]) == 0
    assert "HEART_SOURCE=samples" in capsys.readouterr().out
    monkeypatch.setenv("HEART_COLOUR_SPAN_BPM", "25")
    assert feeder.main(["--check"]) == 2


# ── TC-1487: the parsers and the reader ──────────────────────────────────────

def heart_point(epoch, bpm="78", manufacturer=WATCH):
    return {"name": "users/SENTINEL-USER-ID/dataTypes/heart-rate/dataPoints/x",
            "dataSource": {"platform": "HEALTH_CONNECT", "recordingMethod": "ACTIVELY_MEASURED",
                           "device": {"manufacturer": manufacturer, "formFactor": "WATCH"},
                           "application": {}},
            "heartRate": {"beatsPerMinute": bpm,
                          "sampleTime": {"physicalTime": utc_text(epoch), "utcOffset": "-18000s"},
                          "metadata": {}}}


def sleep_point(start, end, main=True, manufacturer=WATCH):
    return {"dataSource": {"platform": "HEALTH_CONNECT",
                           "device": {"manufacturer": manufacturer, "formFactor": "WATCH"}},
            "sleep": {"interval": {"startTime": utc_text(start), "endTime": utc_text(end)},
                      "type": "STAGES", "metadata": {"mainSleep": main, "processed": True}}}


def test_the_heart_rate_parser_reads_the_observed_shape_and_skips_bad_points():
    t = L(10, 25, 3)
    page = {"dataPoints": [heart_point(t), heart_point(t + 300, bpm=78),
                           heart_point(t + 600, bpm="078"), heart_point(t + 900, bpm="0"),
                           heart_point(t + 1200, bpm="301"), {"heartRate": "x"},
                           {"steps": {}}, heart_point(t + 1500, manufacturer="bad;name")]}
    got = feeder.parse_heart_rate_page(page)
    assert got == [(t, 78, WATCH), (t + 1500, 78, None)]
    assert feeder.parse_heart_rate_page({"dataPoints": []}) == []
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_heart_rate_page({"points": []})


def test_the_sleep_parser_reads_sessions_and_refuses_nonsense_intervals():
    a, b = L(10, 25, 1, 34), L(10, 25, 5, 35)
    page = {"dataPoints": [sleep_point(a, b), sleep_point(b, a),
                           sleep_point(a, a + 25 * 3600), sleep_point(a, b, main=False)]}
    assert feeder.parse_sleep_page(page) == [(a, b, WATCH, True), (a, b, WATCH, False)]


def test_the_reader_sends_both_filters_and_walks_pages(tmp_path):
    token = tmp_path / "t.json"
    token.write_text(json.dumps({"refresh_token": "R"}), encoding="utf-8")
    env = make_env(tmp_path, WEIGHT_TOKEN_FILE=str(token), OAUTH2_PROXY_CLIENT_ID="i",
                   OAUTH2_PROXY_CLIENT_SECRET="s", WEIGHT_TIMEOUT_SECONDS="10")
    now = L(10, 25, 9)
    env["_now"] = now
    t = L(10, 25, 3)

    def respond(handler):
        path, _sep, query = handler.path.partition("?")
        fields = urllib.parse.parse_qs(query)
        if path == "/token":
            payload = {"access_token": "A"}
        elif path == "/hr" and "pageToken" not in fields:
            payload = {"dataPoints": [heart_point(t)], "nextPageToken": "P2"}
        elif path == "/hr":
            payload = {"dataPoints": [heart_point(t + 300)]}
        else:
            payload = {"dataPoints": [sleep_point(L(10, 25, 1), L(10, 25, 6))]}
        raw = json.dumps(payload).encode()
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    with loopback_server(respond) as (base, seen):
        samples, sessions, _raw = feeder.read_google_health_heart_samples(
            env, heart_rate_url=base + "/hr", sleep_url=base + "/sleep",
            token_endpoint=base + "/token")
    assert samples == [(t, 78, WATCH), (t + 300, 78, WATCH)]
    assert sessions == [(L(10, 25, 1), L(10, 25, 6), WATCH, True)]
    since = utc_text(now - feeder.HEART_LOOKBACK_SECONDS)
    queries = [urllib.parse.parse_qs(r["path"].partition("?")[2]) for r in seen[1:]]
    assert queries[0]["filter"] == ['heart_rate.sample_time.physical_time >= "%s"' % since]
    assert queries[0]["pageSize"] == ["10000"]
    assert queries[1]["pageToken"] == ["P2"]
    assert queries[2]["filter"] == ['sleep.interval.end_time >= "%s"' % since]
    assert queries[2]["pageSize"] == ["25"]


# ── TC-1488: cleaning ────────────────────────────────────────────────────────

def test_blips_are_dropped_and_real_lows_are_kept():
    t = L(10, 24, 0)
    raw = [(t, 82, WATCH), (t + 300, 82, WATCH), (t + 600, 54, WATCH),   # the 54 blip
           (t + 900, 79, WATCH), (t + 1200, 83, WATCH),
           (t + 1500, 69, WATCH), (t + 1800, 69, WATCH), (t + 2100, 69, WATCH)]
    assert [b for _t, b in feeder.clean_heart_samples(raw, WATCH)] == [82, 82, 79, 83, 69, 69, 69]


def test_a_low_next_to_a_gap_or_at_an_edge_is_not_judged():
    t = L(10, 24, 0)
    gap = [(t, 80, WATCH), (t + 1800, 60, WATCH), (t + 2100, 80, WATCH)]  # 30 min gap before
    assert len(feeder.clean_heart_samples(gap, WATCH)) == 3
    edge = [(t, 50, WATCH), (t + 300, 80, WATCH), (t + 600, 50, WATCH)]
    assert len(feeder.clean_heart_samples(edge, WATCH)) == 3


def test_cleaning_keeps_one_device_the_band_and_believable_instants():
    t = L(10, 24, 0)
    raw = [(t, 70, WATCH), (t, 70, WATCH),                  # same value twice: one
           (t + 300, 70, WATCH), (t + 300, 75, WATCH),      # disagreeing: neither
           (t + 600, 34, WATCH), (t + 900, 151, WATCH),     # out of band
           (t + 1200, 70, "Ring"), (t + 1500, 71, None), (t + 1800, 72, WATCH)]
    assert feeder.clean_heart_samples(raw, WATCH) == [(t, 70), (t + 1800, 72)]


# ── TC-1489: the night ───────────────────────────────────────────────────────

def test_the_session_window_runs_nine_hours_from_the_main_sleep():
    sessions = [(L(10, 25, 1, 34), L(10, 25, 5, 35), WATCH, True),
                (L(10, 24, 14), L(10, 25, 0, 30), WATCH, False),   # longer, not main
                (L(10, 25, 0), L(10, 25, 9), "Ring", True)]          # another device
    assert feeder.heart_night_window(NIGHT, sessions, WATCH, CT) == \
        (L(10, 25, 1, 34), L(10, 25, 10, 34), "session", None)


def test_with_no_session_the_window_is_22_to_10_local_across_dst():
    assert feeder.heart_night_window(NIGHT, [], WATCH, CT) == \
        (L(10, 24, 22), L(10, 25, 10), "fixed", (L(10, 25, 0), L(10, 25, 8)))
    fall_back = date(2026, 11, 1)                # 25-hour day: 01:00 happens twice
    start, end, _kind, core = feeder.heart_night_window(fall_back, [], WATCH, CT)
    assert end - start == 13 * 3600 and core[1] - core[0] == 9 * 3600
    spring = date(2026, 3, 8)                    # 23-hour day
    start, end, _kind, core = feeder.heart_night_window(spring, [], WATCH, CT)
    assert end - start == 11 * 3600 and core[1] - core[0] == 7 * 3600


def test_with_no_session_an_evening_alone_is_not_a_night():
    """Measured on the night to 2026-10-24: the watch held nothing from 02:00 to
    09:00, and the evening alone would have passed a whole-window gate."""
    evening = [(L(10, 24, 22) + 300 * i, 80) for i in range(48)]       # 22:00-01:55
    window = feeder.heart_night_window(NIGHT, [], WATCH, CT)
    assert feeder.derive_heart_night(evening, window) is None
    slept = evening + [(L(10, 25, 2) + 300 * i, 70) for i in range(36)]  # 02:00-04:55
    assert feeder.derive_heart_night(slept, window)["rhr"] == 70


def test_the_night_is_the_lowest_six_sample_run_with_the_others_recorded():
    samples = [(t, b) for t, b, _d in night_samples(floor=60, base=78)]
    window = (L(10, 24, 22), L(10, 25, 10), "fixed", None)
    night = feeder.derive_heart_night(samples, window)
    assert night["rhr"] == 60 and night["window"] == "fixed"
    assert night["n"] == len(samples)
    assert night["p25"] == 78.0 and 76.0 < night["mean"] < 78.0


def test_a_run_across_a_gap_does_not_count_and_thin_nights_are_not_valid():
    t = L(10, 25, 2)
    # 36 samples at 80, plus five lows split by a 40-minute gap: no 6-run of lows.
    samples = [(t + 300 * i, 80) for i in range(36)]
    lows = [(t + 20000 + 300 * i, 50) for i in range(3)] + \
           [(t + 20000 + 600 + 2400 + 300 * i, 50) for i in range(3)]
    night = feeder.derive_heart_night(sorted(samples + lows), (t, t + 30000, "fixed", None))
    assert night["rhr"] == 80
    assert feeder.derive_heart_night(samples[:35], (t, t + 30000, "fixed", None)) is None


def test_the_figure_rounds_half_up():
    t = L(10, 25, 2)
    samples = [(t + 300 * i, 80) for i in range(36)] + \
              [(t + 20000 + 300 * i, b) for i, b in enumerate((70, 70, 70, 71, 71, 71))]
    assert feeder.derive_heart_night(sorted(samples), (t, t + 30000, "fixed", None))["rhr"] == 71


# ── TC-1490: the record ──────────────────────────────────────────────────────

def test_upsert_keeps_the_frozen_stamp_and_a_thin_read_erases_nothing():
    nights = {"2026-10-25": stored_night(70, observed_at=L(10, 25, 7))}
    changed = feeder.upsert_heart_nights(
        nights, {NIGHT: {"rhr": 68, "p25": 72.0, "mean": 75.0, "n": 90, "window": "session"},
                 NIGHT - timedelta(days=1): None}, WATCH)
    assert changed == ["2026-10-25"]
    assert nights["2026-10-25"]["rhr"] == 68
    assert nights["2026-10-25"]["observed_at"] == L(10, 25, 7)
    assert "2026-10-24" not in nights


def test_the_loader_drops_what_is_not_credible():
    now = L(10, 25, 9)
    good = stored_night(70)
    raw = {"2026-10-25": good,
           "2026-10-24": dict(good, rhr=True),
           "2026-10-23": dict(good, rhr=200),
           "2026-10-22": dict(good, n=3),
           "2026-10-21": dict(good, device="bad;name"),
           "2026-10-20": dict(good, window="guess"),
           "2026-10-19": dict(good, p25=float("nan")),
           "2026-08-01": good,                        # older than the record keeps
           "2026-10-27": good,                        # more than a day ahead
           "2026-1-5": good,
           "2026-10-18": dict(good, observed_at=now + 3600)}   # a future stamp
    out = feeder.validate_heart_nights(raw, now)
    assert set(out) == {"2026-10-25", "2026-10-18"}
    assert "observed_at" not in out["2026-10-18"]          # the night stays, the stamp goes
    assert feeder.validate_heart_nights("junk", now) == {}


def test_a_stamp_outside_its_own_morning_is_forgotten():
    nights = {"2026-10-25": stored_night(70, observed_at=L(10, 25, 7)),
              "2026-10-24": stored_night(70, observed_at=L(10, 24, 13)),   # after noon
              "2026-10-23": stored_night(70, observed_at=L(10, 23, 5))}    # before 06:00
    feeder.check_heart_night_stamps(nights, CT)
    assert nights["2026-10-25"]["observed_at"] == L(10, 25, 7)
    assert "observed_at" not in nights["2026-10-24"]
    assert "observed_at" not in nights["2026-10-23"]


# ── TC-1491: the target ──────────────────────────────────────────────────────

def test_the_target_is_fixed_until_seven_same_device_nights_then_their_median():
    cfg = feeder.resolve_heart_samples_config({})
    nights = {"2026-10-25": stored_night(80)}
    for i, rhr in enumerate((70, 71, 72, 73, 74, 75), start=1):
        nights[(NIGHT - timedelta(days=i)).isoformat()] = stored_night(rhr)
    nights["2026-10-18"] = stored_night(60, device="Ring")     # another device
    assert feeder.heart_target_for(nights, "2026-10-25", cfg) == 75    # six: fixed
    nights["2026-10-17"] = stored_night(76)
    assert feeder.heart_target_for(nights, "2026-10-25", cfg) == 73    # seven: median
    nights["2026-09-20"] = stored_night(40)                    # 35 days back: outside 28
    assert feeder.heart_target_for(nights, "2026-10-25", cfg) == 73
    nights["2026-10-16"] = stored_night(77)                    # eight: median 73.5 -> 74
    assert feeder.heart_target_for(nights, "2026-10-25", cfg) == 74
    assert feeder.heart_bar(74, cfg) == (62, 86, 74)


def test_the_bar_puts_yellow_four_and_red_twelve_above_the_target():
    """NagLight: deviation = gap / (0.5 * span); yellow at 1/3 of full deviation
    on its four-band ramp, full red at 1."""
    low, high, target = feeder.heart_bar(72, feeder.resolve_heart_samples_config({}))
    half_span = 0.5 * (high - low)
    assert (76 - target) / half_span == pytest.approx(1 / 3.0)
    assert (84 - target) / half_span == pytest.approx(1.0)


# ── TC-1492: the cycle ───────────────────────────────────────────────────────

def test_a_night_is_first_published_at_six_and_its_stamp_then_freezes(tmp_path):
    env = make_env(tmp_path)
    read = (night_samples(), [])
    posted, failures, body = cycle(env, L(10, 25, 5, 30), read)
    assert posted == [("heart", False, True)] and body["value"] == 0 \
        and "observed_at" not in body                      # 05:30: not before 06:00
    posted, failures, body = cycle(env, L(10, 25, 7, 10), read)
    assert posted == [("heart", True, True)] and failures == []
    assert body["value"] == 60 and body["observed_at"] == utc_text(L(10, 25, 7, 10))
    assert (body["min"], body["max"], body["target"]) == (63, 87, 75)
    assert body["favourable"] == "low" and body["window"] == {"kind": "daily"}
    later = night_samples(floor=58)
    posted, failures, body = cycle(env, L(10, 25, 11), (later, []))
    assert body["value"] == 58                             # a correction: value only
    assert body["observed_at"] == utc_text(L(10, 25, 7, 10))
    night = state_of(env)[feeder.HEART_NIGHTS_KEY]["2026-10-25"]
    assert night["rhr"] == 58 and night["observed_at"] == L(10, 25, 7, 10)


def test_first_seen_after_noon_is_stamped_noon(tmp_path):
    env = make_env(tmp_path)
    _p, _f, body = cycle(env, L(10, 25, 13), (night_samples(), []))
    assert body["observed_at"] == utc_text(L(10, 25, 12))


def test_before_14_the_published_night_stays_then_the_gauge_is_unavailable(tmp_path):
    env = make_env(tmp_path)
    seed_nights(env, {"2026-10-24": stored_night(66, observed_at=L(10, 24, 7)),
                      "2026-10-23": stored_night(64)})
    _p, _f, body = cycle(env, L(10, 25, 13, 59))
    assert body["value"] == 66 and body["observed_at"] == utc_text(L(10, 24, 7))
    _p, _f, body = cycle(env, L(10, 25, 14, 0))
    assert body["value"] == 0 and "observed_at" not in body
    # A night that was never published does not stand in for yesterday.
    env2 = make_env(tmp_path / "b")
    seed_nights(env2, {"2026-10-24": stored_night(66)})
    _p, _f, body = cycle(env2, L(10, 25, 9))
    assert body["value"] == 0 and "observed_at" not in body


def test_a_confirmed_sign_in_failure_greys_the_gauge_and_keeps_the_record(tmp_path):
    env = make_env(tmp_path)
    cycle(env, L(10, 25, 7), (night_samples(), []))
    dead = feeder.AuthFailure("google-health: the token refresh failed: HTTP 400 "
                              "invalid_grant", code="invalid_grant")
    _p, failures, body = cycle(env, L(10, 25, 7, 15), error=dead)
    assert body["value"] == 60                             # 1 of 2: the night is kept
    _p, failures, body = cycle(env, L(10, 25, 7, 30), error=dead)
    assert body["value"] == 0 and "observed_at" not in body
    assert any("confirmed" in line for line in failures)
    assert "2026-10-25" in state_of(env)[feeder.HEART_NIGHTS_KEY]
    _p, failures, body = cycle(env, L(10, 25, 7, 45), (night_samples(), []))
    assert body["value"] == 60 and failures == []          # a renewed login resumes


def test_a_failed_read_posts_the_stored_night_and_computes_nothing(tmp_path):
    env = make_env(tmp_path)
    cycle(env, L(10, 25, 7), (night_samples(), []))
    _p, failures, body = cycle(env, L(10, 25, 8),
                               error=feeder.SourceFailure("google-health(sleep): HTTP 403"))
    assert body["value"] == 60 and failures


def test_the_baseline_colours_against_the_usual_once_seven_nights_exist(tmp_path):
    env = make_env(tmp_path)
    seed_nights(env, {(NIGHT - timedelta(days=i)).isoformat(): stored_night(66)
                      for i in range(1, 8)})
    _p, _f, body = cycle(env, L(10, 25, 8), (night_samples(floor=70), []))
    assert (body["value"], body["target"], body["min"], body["max"]) == (70, 66, 54, 78)


def test_no_line_and_no_saved_byte_carries_a_bpm_from_a_failure(tmp_path):
    env = make_env(tmp_path)
    _p, failures, _b = cycle(env, L(10, 25, 7), (night_samples(floor=61, base=97), []))
    assert failures == []
    _p, failures, _b = cycle(env, L(10, 25, 8),
                             error=feeder.SourceFailure("google-health(heart-rate): HTTP 500"))
    assert not any(n in line for line in failures for n in ("61", "97"))
    text = Path(env["WEIGHT_STATE_FILE"]).read_text(encoding="utf-8")
    assert "refresh_token" not in text and "SENTINEL" not in text


# ── Review round 1 (2026-10-25): five should-fix findings, one test each ─────

def test_a_network_failure_after_a_confirmed_sign_in_failure_stays_grey(tmp_path):
    env = make_env(tmp_path)
    cycle(env, L(10, 25, 7), (night_samples(), []))
    dead = feeder.AuthFailure("google-health: the token refresh failed: HTTP 400 "
                              "invalid_grant", code="invalid_grant")
    cycle(env, L(10, 25, 7, 15), error=dead)
    cycle(env, L(10, 25, 7, 30), error=dead)
    _p, failures, body = cycle(env, L(10, 25, 7, 45),
                               error=feeder.SourceFailure("google-health: timed out"))
    assert body["value"] == 0 and "observed_at" not in body
    _p, failures, body = cycle(env, L(10, 25, 8), (night_samples(), []))
    assert body["value"] == 60                             # only a success clears it


def test_an_oversized_number_in_a_stored_night_is_dropped_not_a_crash():
    now = L(10, 25, 9)
    bad = dict(stored_night(70), p25=10 ** 401)
    assert feeder.validate_heart_nights({"2026-10-25": bad}, now) == {}
    huge = dict(stored_night(70), rhr=10 ** 401)
    assert feeder.validate_heart_nights({"2026-10-25": huge}, now) == {}


def test_samples_dated_after_now_do_not_set_the_night(tmp_path):
    env = make_env(tmp_path)
    now = L(10, 25, 6)
    real = [(t, 80, WATCH) for t in range(L(10, 25, 0), L(10, 25, 6), 300)]
    future = [(t, 50, WATCH) for t in range(L(10, 25, 6, 10), L(10, 25, 6, 40), 300)]  # beyond the 5-minute skew allowance
    _p, _f, body = cycle(env, now, (real + future, []))
    assert body["value"] == 80


def test_a_failed_post_does_not_mark_the_night_published(tmp_path):
    env = make_env(tmp_path)
    bodies = []

    def failing(body, url, e, t):
        bodies.append(body)
        return False, "HTTP 500 (body not logged)"

    feeder.run_heart_cycle(env, now=L(10, 24, 7), reader=lambda e: (
        night_samples(day=date(2026, 10, 24)), []), poster=failing)
    assert bodies[-1]["value"] == 60
    assert "observed_at" not in state_of(env)[feeder.HEART_NIGHTS_KEY]["2026-10-24"]
    # Next morning, with no valid night yet, the never-delivered night is not shown.
    _p, _f, body = cycle(env, L(10, 25, 9))
    assert body["value"] == 0 and "observed_at" not in body


def test_the_14_00_rule_uses_the_clock_after_the_fetch(tmp_path):
    env = make_env(tmp_path)
    seed_nights(env, {"2026-10-24": stored_night(66, observed_at=L(10, 24, 7))})
    bodies = []

    def poster(body, url, e, t):
        bodies.append(body)
        return True, "HTTP 200"

    feeder.run_heart_samples_cycle(env, now=L(10, 25, 13, 59),
                                   reader=lambda e: ([], []), poster=poster,
                                   clock=lambda: L(10, 25, 14, 0))
    assert bodies[-1]["value"] == 0 and "observed_at" not in bodies[-1]


# ── Review round 2 (2026-10-25) ──────────────────────────────────────────────

def test_an_oversized_stamp_is_refused_not_a_crash_in_any_loader(tmp_path):
    now = L(10, 25, 9)
    night = dict(stored_night(70), observed_at=10 ** 401)
    out = feeder.validate_heart_nights({"2026-10-25": night}, now)
    assert "observed_at" not in out["2026-10-25"]
    with pytest.raises(feeder.SourceFailure):
        feeder.check_observed_at(10 ** 401, now, "x")
    env = make_env(tmp_path)
    Path(env["WEIGHT_STATE_FILE"]).write_text(json.dumps(
        {"heart": {"value": 60, "observed_at": 10 ** 401},
         feeder.HEART_NIGHTS_KEY: {"2026-10-25": night}}), encoding="utf-8")
    assert isinstance(feeder.load_state(env["WEIGHT_STATE_FILE"], now), dict)


def test_only_the_configured_devices_nights_are_shown(tmp_path):
    env = make_env(tmp_path, HEART_SAMPLE_DEVICE="Ring")
    seed_nights(env, {"2026-10-25": stored_night(60, device=WATCH, observed_at=L(10, 25, 7))})
    _p, _f, body = cycle(env, L(10, 25, 9), (night_samples(), []))   # watch samples only
    assert body["value"] == 0 and "observed_at" not in body


# ── TC-1493: the long-term goal from the Sheet's project row (2026-10-25) ────

def goal_defs(tmp_path, target="60", unit="bpm", due="2027-04-25", item="heart-goal"):
    """A Health definitions file in the shape the tracker syncs from the Sheet."""
    defs = tmp_path / "definitions"
    defs.mkdir(parents=True, exist_ok=True)
    lines = ["---", "category: Health", "items:", "  - id: weigh-in",
             "    title: Step on the scale", "    type: automated", "    recur: weekly",
             "    target: 170", "    unit: lb", "    check: weight",
             "  - id: %s" % item, "    title: Resting heart rate goal", "    type: project"]
    if target is not None:
        lines.append("    target: %s" % target)
    if unit is not None:
        lines.append("    unit: %s" % unit)
    if due is not None:
        lines.append("    due: %s" % due)
    lines.append("---")
    (defs / "health.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(defs)


def test_the_goal_row_is_read_and_a_bad_one_is_refused(tmp_path):
    defs = goal_defs(tmp_path)
    assert feeder.read_heart_goal(defs, "Health", "heart-goal") == (60, date(2027, 4, 25))
    assert feeder.read_heart_goal("", "Health", "heart-goal") is None
    assert feeder.read_heart_goal(defs, "Health", "no-such-row") is None
    assert feeder.read_heart_goal(goal_defs(tmp_path / "a", target=None), "Health", "heart-goal") is None
    for bad in ({"unit": "lb"}, {"unit": None}, {"target": "6O"}, {"target": "20"},
                {"due": None}, {"due": "April"}):
        with pytest.raises(ValueError):
            feeder.read_heart_goal(goal_defs(tmp_path / str(len(str(bad))) / "x", **bad),
                                   "Health", "heart-goal")


def test_the_path_glides_from_the_usual_to_the_goal_by_the_due_date():
    path = {"target": 60, "due": "2027-04-24", "start": "2026-10-26", "start_usual": 72}
    assert feeder.glide_target(path, date(2026, 10, 26)) == 72
    assert feeder.glide_target(path, date(2027, 1, 24)) == 66        # halfway (90 of 180 days)
    assert feeder.glide_target(path, date(2027, 6, 1)) == 60         # held after the due date


def test_changing_the_row_restarts_the_path_and_removing_it_drops_it():
    today = date(2026, 11, 2)
    old = {"target": 60, "due": "2027-04-25", "start": "2026-10-26", "start_usual": 72}
    assert feeder.update_heart_goal_path(old, (60, date(2027, 4, 25)), 71, today) == old
    reset = feeder.update_heart_goal_path(old, (58, date(2027, 4, 25)), 71, today)
    assert reset == {"target": 58, "due": "2027-04-25", "start": "2026-11-02", "start_usual": 71}
    moved = feeder.update_heart_goal_path(old, (60, date(2027, 6, 1)), 71, today)
    assert moved["start"] == "2026-11-02" and moved["start_usual"] == 71
    assert feeder.update_heart_goal_path(old, None, 71, today) is None
    waiting = feeder.update_heart_goal_path(None, (60, date(2027, 4, 25)), None, today)
    assert waiting["start_usual"] is None                 # no usual yet: no line drawn
    begun = feeder.update_heart_goal_path(waiting, (60, date(2027, 4, 25)), 70, date(2026, 11, 9))
    assert begun["start"] == "2026-11-09" and begun["start_usual"] == 70


def test_the_target_is_the_lower_of_the_usual_and_the_path():
    cfg = feeder.resolve_heart_samples_config({})
    nights = {"2027-01-24": stored_night(70)}
    for i in range(1, 8):
        nights[(date(2027, 1, 24) - timedelta(days=i)).isoformat()] = stored_night(70)
    behind = {"target": 60, "due": "2027-04-24", "start": "2026-10-26", "start_usual": 72}
    assert feeder.heart_target_for(nights, "2027-01-24", cfg, behind) == 66   # path pulls down
    ahead = {"target": 60, "due": "2027-04-24", "start": "2026-10-26", "start_usual": 90}
    assert feeder.heart_target_for(nights, "2027-01-24", cfg, ahead) == 70    # usual still rules
    assert feeder.heart_target_for(nights, "2027-01-24", cfg, None) == 70


def test_the_cycle_reads_the_goal_keeps_the_path_and_reports_a_bad_row(tmp_path):
    env = make_env(tmp_path, WEIGHT_DEFINITIONS_DIR=goal_defs(tmp_path / "g"))
    seed_nights(env, {(NIGHT - timedelta(days=i)).isoformat(): stored_night(72)
                      for i in range(1, 8)})
    _p, failures, body = cycle(env, L(10, 25, 8), (night_samples(floor=72), []))
    assert failures == [] and body["target"] == 72          # day one of the path
    assert state_of(env)[feeder.HEART_GOAL_STATE_KEY] == {
        "target": 60, "due": "2027-04-25", "start": "2026-10-25", "start_usual": 72}
    env["WEIGHT_DEFINITIONS_DIR"] = goal_defs(tmp_path / "bad", unit="lb")
    _p, failures, body = cycle(env, L(10, 25, 9), (night_samples(floor=72), []))
    assert any(line.startswith("heart goal:") for line in failures)
    assert state_of(env)[feeder.HEART_GOAL_STATE_KEY]["start_usual"] == 72   # last good kept
    env["WEIGHT_DEFINITIONS_DIR"] = goal_defs(tmp_path / "gone", target=None)
    _p, failures, _b = cycle(env, L(10, 25, 10), (night_samples(floor=72), []))
    assert feeder.HEART_GOAL_STATE_KEY not in state_of(env)


def test_a_stored_path_is_validated_on_load():
    good = {"target": 60, "due": "2027-04-25", "start": "2026-10-25", "start_usual": 72}
    assert feeder.validate_heart_goal_state(good) == good
    for bad in (dict(good, target=True), dict(good, target=10), dict(good, due="soon"),
                dict(good, start_usual=300), "junk"):
        assert feeder.validate_heart_goal_state(bad) is None


def test_goal_review_fixes_rounding_future_start_and_io_errors(tmp_path, monkeypatch):
    # Exact half-up: 97 -> 30 over 134 days, day 85 is exactly 54.5.
    path = {"target": 30, "due": "2027-03-08", "start": "2026-10-25", "start_usual": 97}
    assert feeder.glide_target(path, date(2027, 1, 18)) == 55
    # A start after today (a clock that jumped) restarts the path.
    future = {"target": 60, "due": "2027-04-25", "start": "2026-11-08", "start_usual": 72}
    again = feeder.update_heart_goal_path(future, (60, date(2027, 4, 25)), 71, date(2026, 10, 25))
    assert again["start"] == "2026-10-25" and again["start_usual"] == 71
    # An I/O error reading the definitions still posts the gauge.
    env = make_env(tmp_path, WEIGHT_DEFINITIONS_DIR=goal_defs(tmp_path / "g"))

    def broken(*_a, **_k):
        raise PermissionError("denied")
    monkeypatch.setattr(feeder, "scan_definitions", broken)
    _p, failures, body = cycle(env, L(10, 25, 8), (night_samples(), []))
    assert body["value"] == 60 and any("unexpected PermissionError" in f for f in failures)


def test_an_unexpected_error_in_the_weight_half_still_runs_the_heart_half(monkeypatch, capsys):
    """Review round 2 (2026-10-25): a definitions directory that cannot be
    listed used to escape main() before the heart cycle ran."""
    for key, value in {"WEIGHT_ENABLED": "true", "WEIGHT_USER": "u",
                       "WEIGHT_FEED_URL": "http://127.0.0.1:8080/api/feed",
                       "HEART_ENABLED": "1", "HEART_SOURCE": "samples",
                       "TIMEZONE": "America/Chicago"}.items():
        monkeypatch.setenv(key, value)
    ran = []

    def weight_breaks(env):
        raise PermissionError("denied")

    def heart_runs(env):
        ran.append("heart")
        return [("heart", True, True)], []
    monkeypatch.setattr(feeder, "run_cycle", weight_breaks)
    monkeypatch.setattr(feeder, "run_heart_cycle", heart_runs)
    assert feeder.main([]) == 2
    assert ran == ["heart"]
    err = capsys.readouterr().err
    assert "unexpected PermissionError" in err and "denied" not in err
