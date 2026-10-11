"""TC-1176..TC-1185 - the weight feeder's resting-heart-rate gauge (H1).

The wall is to get a "heart health" gauge: ONE number per day, daily resting
heart rate in bpm, lower is better. Nobody can feed it today (the Owner's watch
does not reach Google Health, and the OAuth token is dead), so this is the
infrastructure built ahead of a data source, and it is tested against FAKES
written from the Google Health discovery document (revision 20261007), never
against a captured body - none exists. What is asserted, each against the OTHER
side of its contract (the body NagLight would validate and judge stale, the
bytes in the state file, the text an operator would read) and not against a
value invented in this file:

  * the knobs and the opt-in (TC-1176) and that OFF means no network (TC-1177);
  * the parser, the stamp, and the wire body (TC-1178);
  * no data is the stale-on-arrival body, never an invented number (TC-1179);
  * malformed values and dates are refused, naming no value (TC-1180);
  * an out-of-range value is refused, never clamped (TC-1181);
  * the sign-in rules, shared with weight, counted separately (TC-1182);
  * heart and weight cannot change each other's result (TC-1183);
  * the state file (TC-1184);
  * the real reader end to end against a loopback Google (TC-1185).

The NagLight half of the wire contract is a PORT (see `naglight_validate`),
transcribed from NagLight `internal/gauge/gauge.go` on `dev` (Validate, Stale,
StaleAfter) on 2026-10-11; it was also run against the real Go code once, by
hand, outside the repo.

Verifies: TC-1176, TC-1177, TC-1178, TC-1179, TC-1180, TC-1181, TC-1182,
          TC-1183, TC-1184, TC-1185 (SR-073, LLR-1091..LLR-1096)
"""

import calendar
import importlib.util
import io
import json
import math
import re
import socket
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

from conftest import loopback_server

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "stack" / "weight" / "weight_feeder.py"
OAUTH_PATH = REPO / "stack" / "weight" / "weight_oauth.py"

_spec = importlib.util.spec_from_file_location("weight_feeder_heart", MODULE_PATH)
feeder = importlib.util.module_from_spec(_spec)
sys.modules["weight_feeder_heart"] = feeder
_spec.loader.exec_module(feeder)

NOW = int(datetime(2026, 10, 12, 14, 0, 0, tzinfo=timezone.utc).timestamp())
STEP = 900                                   # the 15-minute timer
DAY = (2026, 10, 11)                         # "yesterday" at NOW
DAY_STAMP = calendar.timegm((2026, 10, 11, 17, 0, 0))  # noon CDT, from literals
DAY_ISO = "2026-10-11T17:00:00Z"
CONFIG = (40, 100, 60)
GOAL = 180.0
WEIGHT_STORED = {"value": 191.4, "observed_at": NOW - 3 * 3600}
HEART_STORED = {"value": 58, "observed_at": DAY_STAMP}
SENTINEL = "REMOTE-CHOSE-THESE-BYTES-4417"
USER_ID_SENTINEL = "users/777000111222/dataTypes/daily-resting-heart-rate/dataPoints/PT1"


# ── the hub's zone ──────────────────────────────────────────────────────────
# US Central, written out so the tests do not depend on a tz database (the dev PC
# runs Python 3.8, which has no `zoneinfo`). Rules since 2007: DST from 02:00 on
# the second Sunday of March to 02:00 (DST) on the first Sunday of November.
# The real `zoneinfo` is also exercised where it exists (see the DST tests).

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
    """Stands in for the `zoneinfo` module: knows exactly one zone."""

    @staticmethod
    def ZoneInfo(name):
        if name == "America/Chicago":
            return CT
        raise KeyError(name)


@pytest.fixture(autouse=True)
def hub_has_a_zone(monkeypatch):
    monkeypatch.setattr(feeder, "_load_zoneinfo", lambda: FakeZoneinfo)


def parse(payload, now=NOW, **kwargs):
    """The page parser with the hub's zone supplied, as `run_heart_cycle` does."""
    kwargs.setdefault("zone", CT)
    return feeder.parse_heart_datapoint(payload, now, **kwargs)


def tracking_day(epoch, zone=CT):
    """NagLight's tracking day for an instant: days start at 02:00 LOCAL."""
    return (datetime.fromtimestamp(epoch, zone) - timedelta(hours=2)).date()


# ── the discovery document, as the fake's contract ──────────────────────────
# Excerpt of `schemas.DailyRestingHeartRate` and `schemas.Date` from
# https://health.googleapis.com/$discovery/rest?version=v4, revision 20261007,
# read 2026-10-11. The fakes below are checked against THIS, so a fake cannot
# drift into a shape the document does not describe.
DISCOVERY_REVISION = "20261007"
DAILY_RESTING_HEART_RATE_SCHEMA = {
    "required_fields": {"beatsPerMinute", "date"},
    "optional_fields": {"dailyRestingHeartRateMetadata"},
    "beatsPerMinute": {"type": "string", "format": "int64"},
    "date_fields": {"year", "month", "day"},
    "calculationMethod": {"CALCULATION_METHOD_UNSPECIFIED", "WITH_SLEEP",
                          "ONLY_WITH_AWAKE_DATA"},
}


def point(bpm, date=DAY, method="WITH_SLEEP", **overrides):
    """One DataPoint as the discovery document describes a daily resting rate."""
    member = {"beatsPerMinute": str(bpm),
              "date": {"year": date[0], "month": date[1], "day": date[2]},
              "dailyRestingHeartRateMetadata": {"calculationMethod": method}}
    member.update(overrides)
    return {"name": USER_ID_SENTINEL,
            "dataSource": {"recordingMethod": "DERIVED", "platform": "FITBIT"},
            "dailyRestingHeartRate": member}


def page(*points, token=None):
    body = {"dataPoints": list(points)}
    if token:
        body["nextPageToken"] = token
    return body


def conforms_to_the_schema(data_point):
    """True when `data_point` is a shape the discovery document allows."""
    schema = DAILY_RESTING_HEART_RATE_SCHEMA
    member = data_point["dailyRestingHeartRate"]
    keys = set(member)
    if not schema["required_fields"] <= keys:
        return False
    if keys - schema["required_fields"] - schema["optional_fields"]:
        return False
    if not isinstance(member["beatsPerMinute"], str) \
            or not member["beatsPerMinute"].isdigit():
        return False
    if set(member["date"]) != schema["date_fields"]:
        return False
    method = member.get("dailyRestingHeartRateMetadata", {}).get(
        "calculationMethod", "WITH_SLEEP")
    return method in schema["calculationMethod"]


# ── the NagLight half of the contract, ported ───────────────────────────────

STALE_AFTER = {"daily": 36 * 3600, "weekly": 24 * 3600, "monthly": 48 * 3600,
               "static": 7 * 24 * 3600, "": 7 * 24 * 3600}


def naglight_validate(body):
    """Port of NagLight `Gauge.Validate` for the fields this gauge sends.

    Returns None when NagLight would store the post, else the reason it would
    answer 400 (or, for a field NagLight does not know, the reason it would be
    ignored by a producer that believed in it).
    """
    if not body.get("id"):
        return "gauge id is required"
    for name in ("value", "min", "max", "target"):
        number = body.get(name)
        if isinstance(number, bool) or not isinstance(number, (int, float)) \
                or not math.isfinite(number):
            return "gauge %s must be a finite number" % name
    if not body["max"] > body["min"]:
        return "gauge max must exceed min"
    if body["target"] < body["min"] or body["target"] > body["max"]:
        return "gauge target is outside the range"
    window = body.get("window") or {}
    if set(window) - {"start", "end", "kind"}:
        return "unknown window field"
    if bool(window.get("start")) != bool(window.get("end")):
        return "gauge window needs both start and end, or neither"
    window_empty = not window.get("start") and not window.get("end")
    if window_empty and body.get("direction"):
        return "direction needs a window"
    if not window_empty and not body.get("direction"):
        return "direction is required with a window"
    if body.get("favourable", "") not in ("", "low", "high"):
        return "gauge favourable must be low or high"
    stamp = body.get("observed_at")
    if stamp is not None:
        try:
            datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            return "gauge observed_at must be RFC3339"
    return None


def naglight_stale(body, now):
    """Port of NagLight `Gauge.Stale` + `StaleAfter` (a future stamp is stale)."""
    stamp = body.get("observed_at")
    if not stamp:
        return True
    observed = calendar.timegm(datetime.strptime(
        stamp, "%Y-%m-%dT%H:%M:%SZ").timetuple())
    age = now - observed
    if age < 0:
        return True
    return age > STALE_AFTER.get((body.get("window") or {}).get("kind", ""),
                                 7 * 24 * 3600)


def is_stale_on_arrival(body):
    """The never-measured body: value 0 and NO observed_at (NagLight: stale)."""
    return body["value"] == 0 and "observed_at" not in body


# ── helpers ─────────────────────────────────────────────────────────────────

def make_env(tmp_path, heart="1", **extra):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:1/api/feed",
           "TIMEZONE": "America/Chicago",
           "WEIGHT_STATE_FILE": str(state_dir / "weight-state.json"),
           "STATE_DIRECTORY": str(state_dir)}
    if heart is not None:
        env["HEART_ENABLED"] = heart
    env.update(extra)
    return env


def state_file(env):
    return Path(env["WEIGHT_STATE_FILE"])


def seed(env, data):
    state_file(env).write_text(json.dumps(data), encoding="utf-8")


def read_state(env):
    return json.loads(state_file(env).read_text(encoding="utf-8"))


def reading(bpm=58, date=DAY):
    return feeder.HeartReading(bpm, int(datetime(
        date[0], date[1], date[2], 12, tzinfo=CT).timestamp()))


def heart_alive(_env):
    return reading()


def heart_empty(_env):
    raise feeder.NoWeightYet("google-health(heart): no data points")


def heart_auth_dead(_env):
    raise feeder.AuthFailure("google-health: the token refresh failed: HTTP 400 "
                             "invalid_grant", code="invalid_grant")


def heart_net_dead(_env):
    raise feeder.SourceFailure("google-health: the dataPoints list call failed: "
                               "URLError")


def heart_cycle(env, reader, step=0, ok=True):
    """One run_heart_cycle with injected reader and poster -> (posted, fails, bodies)."""
    bodies = []

    def poster(body, url, e, t):
        bodies.append(body)
        return ok, "HTTP 200" if ok else "HTTP 500 (body not logged)"

    posted, failures = feeder.run_heart_cycle(
        env, now=NOW + step, reader=reader, poster=poster)
    return posted, failures, bodies


def weight_alive(_env):
    return (176.0, NOW)


def weight_auth_dead(_env):
    raise feeder.AuthFailure("google-health: the token refresh failed: HTTP 400 "
                             "invalid_grant", code="invalid_grant")


def weight_net_dead(_env):
    raise feeder.SourceFailure("google-health: the token refresh failed: URLError")


def weight_cycle(env, reader, step=0, goal_loader=None):
    bodies = []

    def poster(body, url, e, t):
        bodies.append(body)
        return True, "HTTP 200"

    posted, failures, _ = feeder.run_cycle(
        env, now=NOW + step, readers={"google-health": reader}, poster=poster,
        goal_loader=goal_loader or (lambda d, *a: (GOAL, "health.md")),
        check_loader=lambda *a: None)
    return posted, failures, bodies


# ── TC-1176: the knobs and the opt-in ───────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (None, False), ("", False), ("0", False), ("false", False), ("no", False),
    ("yes", False), ("True", False), ("TRUE", False), ("2", False),
    (" ", False), ("1", True), ("true", True), (" 1 ", True)])
def test_heart_is_off_for_every_value_but_a_literal_1_or_true(raw, expected):
    env = {} if raw is None else {"HEART_ENABLED": raw}
    assert feeder.resolve_heart_enabled(env) is expected


def test_the_defaults_are_40_100_60_and_the_gauge_is_a_low_is_good_bar():
    assert feeder.resolve_heart_config({}) == (40, 100, 60)
    assert feeder.resolve_heart_config(
        {"HEART_MIN_BPM": "", "HEART_MAX_BPM": " ", "HEART_TARGET_BPM": ""}) \
        == (40, 100, 60)
    assert feeder.HEART_GAUGE_FAVOURABLE == "low"


def test_the_knobs_override_the_defaults():
    env = {"HEART_MIN_BPM": "35", "HEART_MAX_BPM": "120", "HEART_TARGET_BPM": "55"}
    assert feeder.resolve_heart_config(env) == (35, 120, 55)


@pytest.mark.parametrize("env,knob", [
    ({"HEART_MIN_BPM": "abc"}, "HEART_MIN_BPM"),
    ({"HEART_MIN_BPM": "-5"}, "HEART_MIN_BPM"),
    ({"HEART_MIN_BPM": "4.5"}, "HEART_MIN_BPM"),
    ({"HEART_MIN_BPM": "1e1"}, "HEART_MIN_BPM"),
    ({"HEART_MIN_BPM": "0"}, "HEART_MIN_BPM"),
    ({"HEART_MAX_BPM": "301"}, "HEART_MAX_BPM"),
    ({"HEART_MAX_BPM": "9999"}, "HEART_MAX_BPM"),
    ({"HEART_TARGET_BPM": "sixty"}, "HEART_TARGET_BPM"),
    ({"HEART_TARGET_BPM": "0"}, "HEART_TARGET_BPM"),
    ({"HEART_MIN_BPM": "100", "HEART_MAX_BPM": "100",
      "HEART_TARGET_BPM": "100"}, "HEART_MAX_BPM"),
    ({"HEART_MIN_BPM": "110", "HEART_MAX_BPM": "100"}, "HEART_MAX_BPM"),
    ({"HEART_TARGET_BPM": "39"}, "HEART_TARGET_BPM"),
    ({"HEART_TARGET_BPM": "101"}, "HEART_TARGET_BPM"),
])
def test_a_bad_knob_is_refused_naming_it_never_defaulted_past(env, knob):
    with pytest.raises(ValueError) as err:
        feeder.resolve_heart_config(env)
    assert knob in str(err.value)


def test_the_target_may_sit_on_either_end_of_the_range():
    assert feeder.resolve_heart_config(
        {"HEART_TARGET_BPM": "40"}) == (40, 100, 40)
    assert feeder.resolve_heart_config(
        {"HEART_TARGET_BPM": "100"}) == (40, 100, 100)


def test_the_shipped_env_example_documents_the_four_knobs_at_their_defaults():
    text = (REPO / "stack" / ".env.example").read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        match = re.match(r"^(HEART_[A-Z_]+)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    assert values == {"HEART_ENABLED": "0", "HEART_MIN_BPM": "40",
                      "HEART_MAX_BPM": "100", "HEART_TARGET_BPM": "60"}
    # The example, fed back through the code, means "off" and the defaults.
    assert feeder.resolve_heart_enabled(values) is False
    assert feeder.resolve_heart_config(values) == (
        feeder.DEFAULT_HEART_MIN_BPM, feeder.DEFAULT_HEART_MAX_BPM,
        feeder.DEFAULT_HEART_TARGET_BPM)


# ── TC-1177: OFF means no network, no post, no state ────────────────────────

def boom(*args, **kwargs):
    raise AssertionError("a network call was made")


@pytest.mark.parametrize("flag", [None, "0", "false", "", "yes"])
def test_heart_off_fetches_posts_and_writes_nothing(tmp_path, flag):
    env = make_env(tmp_path, heart=flag)
    posted, failures = feeder.run_heart_cycle(
        env, now=NOW, reader=boom, poster=boom)
    assert (posted, failures) == ([], [])
    assert not state_file(env).exists()


def test_heart_off_even_a_bad_knob_is_not_looked_at(tmp_path):
    env = make_env(tmp_path, heart="0", HEART_MIN_BPM="garbage")
    assert feeder.run_heart_cycle(env, now=NOW, reader=boom, poster=boom) \
        == ([], [])


class FrozenTime(object):
    """The `time` module the feeder sees, with the clock held at NOW.

    `main` takes the real clock, and the fakes' dates are fixed, so without this
    a run before 2026-10-11 17:00 UTC would find the fixture day "in the future".
    """

    def time(self):
        return NOW

    def __getattr__(self, name):
        import time as real_time
        return getattr(real_time, name)


def run_main(monkeypatch, tmp_path, env_overrides, weight_result=None,
             weight_exc=None, heart_reader=None, argv=None):
    """Run the real `main` with the weight cycle stubbed; return (rc, posts, out, err)."""
    env = make_env(tmp_path, heart=None)
    env.pop("_identity"), env.pop("_feed_url")
    env.update({"WEIGHT_ENABLED": "true", "WEIGHT_USER": "u",
                "WEIGHT_FEED_URL": "http://127.0.0.1:8787/api/feed"})
    env.update(env_overrides)
    for key in list(env):
        monkeypatch.setenv(key, env[key])
    posts = []
    monkeypatch.setattr(feeder, "time", FrozenTime())
    monkeypatch.setattr(feeder, "post_gauge",
                        lambda body, url, e, t: (posts.append(body) or True,
                                                 "HTTP 200"))
    calls = {"weight": 0}

    def fake_run_cycle(e, **kw):
        calls["weight"] += 1
        if weight_exc is not None:
            raise weight_exc
        return weight_result or ([("weight", True, True)], [], "health.md")

    monkeypatch.setattr(feeder, "run_cycle", fake_run_cycle)
    if heart_reader is not None:
        monkeypatch.setattr(feeder, "HEART_READERS",
                            {"google-health": heart_reader})
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    rc = feeder.main(argv or [])
    return rc, posts, out.getvalue(), err.getvalue(), calls


def test_main_with_heart_off_makes_no_connection_at_all(monkeypatch, tmp_path):
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(feeder, "vendor_opener", boom)
    monkeypatch.setattr(feeder, "feed_opener", boom)
    monkeypatch.setattr(feeder, "HEART_READERS", {"google-health": boom})
    rc, posts, out, err, calls = run_main(
        monkeypatch, tmp_path, {"HEART_ENABLED": "0"})
    assert rc == 0 and calls["weight"] == 1
    assert posts == []                      # the stubbed weight cycle posted nothing
    assert "heart" not in out and "heart" not in err


def test_main_with_heart_on_runs_it_after_the_weight_cycle(monkeypatch, tmp_path):
    rc, posts, out, err, calls = run_main(
        monkeypatch, tmp_path, {"HEART_ENABLED": "1"}, heart_reader=heart_alive)
    assert rc == 0 and calls["weight"] == 1
    assert [p["id"] for p in posts] == ["heart"]
    assert out.splitlines()[-1] == "heart: heart      live         posted"
    assert err == ""


def test_main_check_mode_validates_the_heart_knobs_only_when_enabled(
        monkeypatch, tmp_path):
    rc, _p, out, err, calls = run_main(
        monkeypatch, tmp_path, {"HEART_ENABLED": "1", "HEART_MAX_BPM": "30"},
        argv=["--check"])
    assert rc == 2 and "HEART_MAX_BPM" in err and calls["weight"] == 0
    rc, _p, out, err, _c = run_main(
        monkeypatch, tmp_path, {"HEART_ENABLED": "0", "HEART_MAX_BPM": "30"},
        argv=["--check"])
    assert rc == 0 and "heart" not in out + err


# ── TC-1178: the parser, the stamp, the wire body ───────────────────────────

def test_the_fakes_conform_to_the_discovery_document_schema():
    assert DISCOVERY_REVISION == "20261007"
    assert conforms_to_the_schema(point(58))
    assert not conforms_to_the_schema(point(58, beatsPerMinute=58))   # a number
    assert not conforms_to_the_schema(point(58, sampleTime={}))       # not a field


def test_data_present_parses_to_the_rate_at_the_dates_own_stamp():
    result = parse(page(point(58)), NOW)
    assert result.bpm == 58
    assert result.observed_at == DAY_STAMP
    assert result.observed_at != NOW                 # never the fetch time
    assert feeder.iso8601_utc(result.observed_at) == DAY_ISO


def test_the_latest_day_wins_by_date_not_by_position():
    pages = [
        page(point(60, (2026, 10, 9)), point(58, (2026, 10, 11)),
             point(57, (2026, 10, 10))),
        page(point(58, (2026, 10, 11)), point(57, (2026, 10, 10)),
             point(60, (2026, 10, 9))),
    ]
    for payload in pages:
        assert parse(payload, NOW).bpm == 58


def test_the_wire_body_is_exactly_the_sleep_shape_with_bpm():
    body, fresh = feeder.build_heart_post(
        parse(page(point(58)), NOW), None, CONFIG, NOW)
    assert fresh is True
    assert body == {
        "kind": "gauge", "id": "heart", "label": "Heart", "icon": "heart",
        "unit": "bpm", "value": 58, "min": 40, "max": 100, "target": 60,
        "favourable": "low", "window": {"kind": "daily"},
        "observed_at": DAY_ISO}
    # NagLight would store it, and treat it as fresh while the day is current.
    assert naglight_validate(body) is None
    assert naglight_stale(body, NOW) is False


def test_the_body_carries_no_direction_no_start_no_end_and_no_colour():
    body, _ = feeder.build_heart_post(heart_alive(None), None, CONFIG, NOW)
    assert "direction" not in body
    assert set(body["window"]) == {"kind"}
    for banned in ("color", "colour", "severity", "css", "previous_value"):
        assert banned not in body


def test_the_knobs_reach_the_wire():
    body, _ = feeder.build_heart_post(
        heart_alive(None), None, (35, 120, 55), NOW)
    assert (body["min"], body["max"], body["target"]) == (35, 120, 55)
    assert naglight_validate(body) is None


def test_the_daily_window_gives_the_36_hour_life_the_plan_chose():
    body, _ = feeder.build_heart_post(heart_alive(None), None, CONFIG, NOW)
    assert naglight_stale(body, DAY_STAMP + 36 * 3600) is False
    assert naglight_stale(body, DAY_STAMP + 36 * 3600 + 1) is True
    # And the module's own freshness helper agrees with NagLight's rule.
    assert feeder.is_fresh(body, DAY_STAMP + 36 * 3600) is True
    assert feeder.is_fresh(body, DAY_STAMP + 36 * 3600 + 1) is False


def test_a_days_value_is_fresh_until_the_next_mornings_value_normally_arrives():
    # Stamped 06:00 UTC, the day is still fresh at 13:00 US Central the NEXT
    # day (18:00 UTC) and stale after: the next morning's value always lands
    # first for a household in the Americas.
    body, _ = feeder.build_heart_post(heart_alive(None), None, CONFIG, NOW)
    next_morning_central = calendar.timegm((2026, 10, 12, 12, 0, 0))   # 07:00 CDT
    assert naglight_stale(body, next_morning_central) is False


def test_a_correction_to_the_same_day_does_not_move_the_stamp():
    first = parse(page(point(58)), NOW)
    corrected = parse(page(point(61)), NOW + 5 * 3600)
    assert corrected.bpm == 61
    assert corrected.observed_at == first.observed_at == DAY_STAMP


def test_a_day_that_has_begun_elsewhere_is_held_back_not_failed():
    tomorrow = (2026, 10, 13)          # stamp +16 h ahead of NOW
    result = parse(
        page(point(61, tomorrow), point(58)), NOW)
    assert result.bpm == 58            # the older day carries the gauge
    with pytest.raises(feeder.NoWeightYet):
        parse(page(point(61, tomorrow)), NOW)


def test_a_date_far_ahead_of_the_clock_is_corruption_and_fails():
    with pytest.raises(feeder.SourceFailure) as err:
        parse(page(point(58, (2030, 1, 1))), NOW)
    assert not isinstance(err.value, feeder.NoWeightYet)


def test_a_date_before_the_feeder_existed_is_corruption():
    with pytest.raises(feeder.SourceFailure):
        parse(page(point(58, (2019, 5, 5))), NOW)


def test_a_point_of_another_data_type_is_skipped_not_fatal():
    other = {"name": USER_ID_SENTINEL, "weight": {"weightGrams": 80000}}
    assert parse(page(other, point(58)), NOW).bpm == 58
    with pytest.raises(feeder.SourceFailure) as err:
        parse(page(other), NOW)
    assert not isinstance(err.value, feeder.NoWeightYet)


def test_two_points_for_the_newest_day_that_disagree_are_refused():
    with pytest.raises(feeder.SourceFailure) as err:
        parse(page(point(58), point(63)), NOW)
    assert "58" not in str(err.value) and "63" not in str(err.value)


def test_two_identical_points_for_the_newest_day_are_a_duplicate_not_a_clash():
    assert parse(page(point(58), point(58)), NOW).bpm == 58


def test_the_google_user_id_and_the_source_are_never_read():
    result = parse(page(point(58)), NOW)
    for text in (repr(result), str(result)):
        assert "777000111222" not in text and "58" not in text
    assert not hasattr(result, "name")


# ── TC-1178 (continued): the stamp is 12:00 LOCAL, inside tracking day D ─────
# NagLight's tracking day starts at 02:00 local (eventlog.DayStartHour). Every
# expectation below is a hand-written UTC literal, not computed by the feeder.

LATE = calendar.timegm((2027, 1, 15, 0, 0, 0))     # well after every fixture day

STAMP_CASES = [
    # (date, expected UTC instant, why)
    ((2026, 10, 11), (2026, 10, 11, 17, 0), "CDT, ordinary autumn day"),
    ((2026, 12, 15), (2026, 12, 15, 18, 0), "CST, ordinary winter day"),
    ((2026, 3, 7), (2026, 3, 7, 18, 0), "CST, the day BEFORE spring forward"),
    ((2026, 3, 8), (2026, 3, 8, 17, 0), "CDT, spring-forward day itself"),
    ((2026, 3, 9), (2026, 3, 9, 17, 0), "CDT, the day after"),
    ((2026, 10, 31), (2026, 10, 31, 17, 0), "CDT, the day BEFORE fall back"),
    ((2026, 11, 1), (2026, 11, 1, 18, 0), "CST, fall-back day itself"),
    ((2026, 11, 2), (2026, 11, 2, 18, 0), "CST, the day after"),
]


@pytest.mark.parametrize("date,utc,why", STAMP_CASES, ids=[c[2] for c in STAMP_CASES])
def test_each_date_is_stamped_at_noon_local_and_lands_in_that_tracking_day(
        date, utc, why):
    result = parse(page(point(58, date)), LATE)
    assert result.observed_at == calendar.timegm(utc + (0,)), why
    day = tracking_day(result.observed_at)
    assert (day.year, day.month, day.day) == date, why
    # And it is exactly 12:00 on the local wall clock.
    local = datetime.fromtimestamp(result.observed_at, CT)
    assert (local.hour, local.minute) == (12, 0)


@pytest.mark.parametrize("date,utc,why", STAMP_CASES, ids=[c[2] for c in STAMP_CASES])
def test_the_old_06_utc_stamp_would_have_been_the_previous_tracking_day(
        date, utc, why):
    # The defect the review found, pinned: 06:00Z is 00:00/01:00 local.
    old = calendar.timegm((date[0], date[1], date[2], 6, 0, 0))
    assert tracking_day(old).day != date[2]


@pytest.mark.parametrize("date,utc,why", STAMP_CASES, ids=[c[2] for c in STAMP_CASES])
def test_the_real_zoneinfo_agrees_on_every_dst_boundary_case(date, utc, why):
    zoneinfo = pytest.importorskip("zoneinfo")
    zone = zoneinfo.ZoneInfo("America/Chicago")
    result = feeder.parse_heart_datapoint(page(point(58, date)), LATE, zone=zone)
    assert result.observed_at == calendar.timegm(utc + (0,)), why
    day = tracking_day(result.observed_at, zone)
    assert (day.year, day.month, day.day) == date, why


def test_the_stamp_follows_the_zone_it_is_given_not_a_utc_constant():
    plus_nine = timezone(timedelta(hours=9))
    result = feeder.parse_heart_datapoint(page(point(58)), LATE, zone=plus_nine)
    assert result.observed_at == calendar.timegm((2026, 10, 11, 3, 0, 0))
    utc = feeder.parse_heart_datapoint(page(point(58)), LATE, zone=timezone.utc)
    assert utc.observed_at == calendar.timegm((2026, 10, 11, 12, 0, 0))


def test_a_parse_with_no_zone_refuses_rather_than_assuming_utc():
    with pytest.raises(feeder.SourceFailure) as err:
        feeder.parse_heart_datapoint(page(point(58)), NOW, zone=None)
    assert not isinstance(err.value, feeder.NoWeightYet)


def test_todays_date_is_held_back_until_noon_local_then_posted_at_noon_exactly():
    today = (2026, 10, 12)                       # NOW is 09:00 CDT that day
    noon = calendar.timegm((2026, 10, 12, 17, 0, 0))
    # Before noon: held back; yesterday carries the gauge.
    got = parse(page(point(61, today), point(58)), NOW)
    assert got.bpm == 58 and got.observed_at == DAY_STAMP
    with pytest.raises(feeder.NoWeightYet):
        parse(page(point(61, today)), NOW)
    # After noon (even a minute after): the stamp is noon, never the fetch time.
    got = parse(page(point(61, today)), noon + 60)
    assert got.bpm == 61 and got.observed_at == noon != noon + 60


def test_the_zone_is_required_and_a_missing_one_skips_the_gauge_before_any_read(
        tmp_path):
    env = make_env(tmp_path)
    del env["TIMEZONE"]
    with pytest.raises(ValueError) as err:
        feeder.run_heart_cycle(env, now=NOW, reader=boom, poster=boom)
    assert "TIMEZONE" in str(err.value) and "unset" in str(err.value)
    assert not state_file(env).exists()


@pytest.mark.parametrize("name", ["", "   ", "Mars/Olympus"])
def test_an_unset_or_unknown_timezone_is_refused_never_guessed_as_utc(name):
    with pytest.raises(ValueError) as err:
        feeder.resolve_heart_zone({"TIMEZONE": name})
    assert "TIMEZONE" in str(err.value)
    if name.strip():
        assert "Mars/Olympus" in str(err.value) and "not a zone" in str(err.value)


def test_a_box_without_zoneinfo_refuses_a_named_zone(monkeypatch):
    def missing():
        raise ImportError("no zoneinfo")

    monkeypatch.setattr(feeder, "_load_zoneinfo", missing)
    with pytest.raises(ValueError) as err:
        feeder.resolve_heart_zone({"TIMEZONE": "America/Chicago"})
    assert "zoneinfo" in str(err.value)
    # UTC needs no database.
    assert feeder.resolve_heart_zone({"TIMEZONE": "Etc/UTC"}) is timezone.utc


def test_a_known_zone_resolves_to_the_tzinfo():
    assert feeder.resolve_heart_zone({"TIMEZONE": "America/Chicago"}) is CT


def test_main_with_no_timezone_skips_heart_names_it_and_leaves_weight_alone(
        monkeypatch, tmp_path):
    rc, posts, out, err, calls = run_main(
        monkeypatch, tmp_path, {"HEART_ENABLED": "1", "TIMEZONE": ""},
        heart_reader=boom)
    assert calls["weight"] == 1 and posts == []
    assert out.splitlines()[0] == "weight: goal read from health.md"
    assert "heart: REFUSED" in err and "TIMEZONE" in err
    assert rc == 1                                   # visible, never silent


def test_check_mode_refuses_a_missing_timezone_only_when_heart_is_on(
        monkeypatch, tmp_path):
    rc, _p, _o, err, calls = run_main(
        monkeypatch, tmp_path, {"HEART_ENABLED": "1", "TIMEZONE": ""},
        argv=["--check"])
    assert rc == 2 and "TIMEZONE" in err and calls["weight"] == 0
    rc, _p, out, err, _c = run_main(
        monkeypatch, tmp_path, {"HEART_ENABLED": "0", "TIMEZONE": ""},
        argv=["--check"])
    assert rc == 0 and "heart" not in out + err


# ── TC-1179: no data ────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [{"dataPoints": []}, {"dataPoints": None}])
def test_an_empty_history_is_no_data_not_a_failure(payload):
    with pytest.raises(feeder.NoWeightYet):
        parse(payload, NOW)


@pytest.mark.parametrize("payload", [{}, {"other": []}, [], "x", None,
                                     {"dataPoints": {}}, {"dataPoints": "x"}])
def test_a_body_that_is_not_the_envelope_is_a_failure_not_no_data(payload):
    with pytest.raises(feeder.SourceFailure) as err:
        parse(payload, NOW)
    assert not isinstance(err.value, feeder.NoWeightYet)


def test_no_data_posts_the_stale_on_arrival_body_and_no_failure_line(tmp_path):
    env = make_env(tmp_path)
    posted, failures, bodies = heart_cycle(env, heart_empty)
    assert posted == [("heart", False, True)]
    assert failures == []                    # today's NORMAL state is silent
    assert len(bodies) == 1
    body = bodies[0]
    assert is_stale_on_arrival(body)
    assert "observed_at" not in body
    assert naglight_validate(body) is None
    assert naglight_stale(body, NOW) is True
    assert (body["min"], body["max"], body["target"]) == (40, 100, 60)
    assert body["window"] == {"kind": "daily"} and body["unit"] == "bpm"


def test_no_data_never_invents_a_number_or_a_state_entry(tmp_path):
    env = make_env(tmp_path)
    heart_cycle(env, heart_empty)
    heart_cycle(env, heart_empty, STEP)
    assert not state_file(env).exists()      # nothing to remember, nothing saved


def test_no_data_after_a_good_day_reposts_it_at_its_original_stamp(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"heart": HEART_STORED})
    _p, failures, bodies = heart_cycle(env, heart_empty)
    assert failures == []
    assert bodies[0]["value"] == 58 and bodies[0]["observed_at"] == DAY_ISO
    assert read_state(env) == {"heart": HEART_STORED}     # byte-for-meaning same


def test_a_stored_day_older_than_36_hours_is_posted_but_stale(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"heart": {"value": 58,
                         "observed_at": calendar.timegm((2026, 10, 9, 17, 0, 0))}})
    _p, _f, bodies = heart_cycle(env, heart_empty)
    assert bodies[0]["observed_at"] == "2026-10-09T17:00:00Z"
    assert naglight_stale(bodies[0], NOW) is True


def test_data_present_stores_the_day_and_a_live_post_says_live(tmp_path):
    env = make_env(tmp_path)
    posted, failures, bodies = heart_cycle(env, heart_alive)
    assert posted == [("heart", True, True)] and failures == []
    assert bodies[0]["value"] == 58 and bodies[0]["observed_at"] == DAY_ISO
    assert read_state(env) == {"heart": {"value": 58, "observed_at": DAY_STAMP}}


def test_an_identical_second_cycle_does_not_rewrite_the_state_file(tmp_path):
    env = make_env(tmp_path)
    heart_cycle(env, heart_alive)
    before = state_file(env).stat().st_mtime_ns
    heart_cycle(env, heart_alive, STEP)
    assert state_file(env).stat().st_mtime_ns == before


# ── TC-1180: malformed values and dates ─────────────────────────────────────

BAD_BPM = [58, 58.0, True, False, None, "", " 58", "58 ", "+58", "-58", "058",
           "58.0", "5 8", "NaN", "inf", "1e2", "0x3a", "58\n", ["58"],
           {"v": "58"}, "٥٨", "58_0"]


@pytest.mark.parametrize("raw", BAD_BPM, ids=repr)
def test_a_malformed_beats_per_minute_is_refused_naming_no_value(raw):
    with pytest.raises(feeder.SourceFailure) as err:
        feeder.check_vendor_bpm(raw, "where")
    assert "58" not in str(err.value)


@pytest.mark.parametrize("raw", BAD_BPM, ids=repr)
def test_a_malformed_value_in_a_page_costs_the_page_not_a_number(raw):
    bad = point(58, beatsPerMinute=raw)
    with pytest.raises(feeder.SourceFailure) as err:
        parse(page(bad), NOW)
    assert not isinstance(err.value, feeder.NoWeightYet)
    assert "58" not in str(err.value).replace("[58]", "")


def test_one_bad_point_among_good_ones_costs_only_itself():
    result = parse(
        page(point(58, beatsPerMinute="abc"), point(57, (2026, 10, 10))), NOW)
    assert result.bpm == 57


@pytest.mark.parametrize("date", [
    None, "2026-10-11", [], {}, {"year": 2026, "month": 10},
    {"year": 2026, "month": 0, "day": 11}, {"year": 2026, "month": 10, "day": 0},
    {"year": 0, "month": 10, "day": 11}, {"year": 2026, "month": 13, "day": 1},
    {"year": 2026, "month": 2, "day": 30}, {"year": "2026", "month": 10, "day": 11},
    {"year": 2026.0, "month": 10, "day": 11},
    {"year": True, "month": 10, "day": 11}, {"year": 99999, "month": 1, "day": 1},
])
def test_a_missing_or_malformed_date_is_refused_never_replaced_by_the_clock(date):
    bad = point(58)
    bad["dailyRestingHeartRate"]["date"] = date
    with pytest.raises(feeder.SourceFailure):
        parse(page(bad), NOW)


def test_a_missing_date_or_missing_rate_key_is_refused():
    no_date = point(58)
    del no_date["dailyRestingHeartRate"]["date"]
    no_bpm = point(58)
    del no_bpm["dailyRestingHeartRate"]["beatsPerMinute"]
    for bad in (no_date, no_bpm):
        with pytest.raises(feeder.SourceFailure):
            parse(page(bad), NOW)


@pytest.mark.parametrize("bad", [
    "x", 5, None, [], {"dailyRestingHeartRate": None},
    {"dailyRestingHeartRate": "58"}, {"dailyRestingHeartRate": []}])
def test_a_point_that_is_not_an_object_or_has_no_member_is_refused(bad):
    with pytest.raises(feeder.SourceFailure):
        parse(page(bad), NOW)


def test_a_malformed_read_posts_the_unavailable_body_and_a_numberless_line(tmp_path):
    env = make_env(tmp_path)

    def reader(e):
        return parse(
            page(point(187, beatsPerMinute="187.5")), NOW)

    _p, failures, bodies = heart_cycle(env, reader)
    assert is_stale_on_arrival(bodies[0])
    assert failures and "187" not in "".join(failures)


# ── TC-1181: out of range is refused, never clamped ─────────────────────────

@pytest.mark.parametrize("bpm", [0, 1, 24, 201, 250, 300, 9999])
def test_an_implausible_resting_rate_is_refused(bpm):
    with pytest.raises(feeder.SourceFailure) as err:
        feeder.check_vendor_bpm(str(bpm), "where")
    assert str(bpm) not in str(err.value).replace("25..200", "")


@pytest.mark.parametrize("bpm", [25, 26, 200])
def test_the_edges_of_the_plausible_band_are_accepted(bpm):
    assert feeder.check_vendor_bpm(str(bpm), "where") == bpm


def test_a_refused_value_is_never_clamped_to_the_band_edge(tmp_path):
    env = make_env(tmp_path)
    for raw in ("250", "9999", "3", "0"):
        bodies = heart_cycle(env, lambda e, raw=raw: parse(
            page(point(raw)), NOW), STEP)[2]
        body = bodies[0]
        assert is_stale_on_arrival(body)
        assert body["value"] not in (25, 200)


def test_a_refused_value_leaves_the_previous_day_standing(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"heart": HEART_STORED})
    _p, failures, bodies = heart_cycle(
        env, lambda e: parse(page(point(250)), NOW))
    assert bodies[0]["value"] == 58 and bodies[0]["observed_at"] == DAY_ISO
    assert failures and "250" not in "".join(failures)
    assert read_state(env)["heart"] == HEART_STORED


@pytest.mark.parametrize("bpm", [38, 104, 31, 150])
def test_a_plausible_rate_outside_the_gauge_range_is_posted_as_it_is(bpm):
    # The bar pins to its end; the number on the wall stays the measured one.
    # NagLight does not hold `value` to min..max (only the target), as the
    # weight gauge, which routinely sits outside its inferred bar, proves.
    body, _ = feeder.build_heart_post(
        feeder.HeartReading(bpm, DAY_STAMP), None, CONFIG, NOW)
    assert body["value"] == bpm
    assert naglight_validate(body) is None


@pytest.mark.parametrize("value,stamp", [(0, DAY_STAMP), (-5, DAY_STAMP),
                                         (4000, DAY_STAMP), (58.5, DAY_STAMP),
                                         (True, DAY_STAMP)])
def test_a_body_with_a_stamp_cannot_carry_an_implausible_value(value, stamp):
    with pytest.raises(ValueError):
        feeder.heart_gauge_body(value, stamp, CONFIG)


def test_the_sentinel_zero_only_ever_travels_with_no_stamp():
    body = feeder.heart_gauge_body(0, None, CONFIG)
    assert is_stale_on_arrival(body)
    assert naglight_validate(body) is None


def test_a_future_or_prehistoric_reading_cannot_be_posted():
    future = feeder.HeartReading(58, NOW + 3 * 86400)
    ancient = feeder.HeartReading(58, 1000)
    for bad in (future, ancient):
        with pytest.raises(feeder.SourceFailure):
            feeder.build_heart_post(bad, None, CONFIG, NOW)


# ── TC-1182: the sign-in rules, shared with weight, counted separately ──────

def both_cycles(env, weight_reader, heart_reader, step):
    """The order `main` uses: weight first, then heart."""
    _wp, wfail, wbodies = weight_cycle(env, weight_reader, step)
    _hp, hfail, hbodies = heart_cycle(env, heart_reader, step)
    return wbodies[0], hbodies[0], wfail, hfail


def test_one_dead_token_greys_both_gauges_on_the_second_cycle_not_the_first(
        tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": WEIGHT_STORED, "heart": HEART_STORED})
    w1, h1, _wf, _hf = both_cycles(env, weight_auth_dead, heart_auth_dead, 0)
    # FIRST failed cycle: nothing greys, each count stands at 1 - NOT 2. A
    # counter shared between the two sources would have been bumped twice here
    # and confirmed a dead login after one cycle.
    assert w1["value"] == 191.4 and "observed_at" in w1
    assert h1["value"] == 58 and h1["observed_at"] == DAY_ISO
    assert read_state(env)["auth-failed-cycles"] == {
        "google-health": 1, "google-health:heart": 1}
    w2, h2, wfail, hfail = both_cycles(env, weight_auth_dead, heart_auth_dead,
                                       STEP)
    assert is_stale_on_arrival(w2) and is_stale_on_arrival(h2)
    assert any("confirmed" in line for line in hfail)
    state = read_state(env)
    assert "weight" not in state and "heart" not in state     # both cleared
    assert state["auth-failed-cycles"] == {
        "google-health": 2, "google-health:heart": 2}


def test_the_grey_sticks_across_a_following_network_failure(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"heart": HEART_STORED})
    heart_cycle(env, heart_auth_dead, 0)
    heart_cycle(env, heart_auth_dead, STEP)
    for step in (2 * STEP, 3 * STEP):
        body = heart_cycle(env, heart_net_dead, step)[2][0]
        assert is_stale_on_arrival(body)
    body = heart_cycle(env, heart_empty, 4 * STEP)[2][0]
    assert is_stale_on_arrival(body)


def test_build_heart_post_with_the_flag_skips_a_perfectly_good_stored_reading():
    body, fresh = feeder.build_heart_post(None, HEART_STORED, CONFIG, NOW,
                                          auth_failed=True)
    assert is_stale_on_arrival(body) and fresh is False
    body, fresh = feeder.build_heart_post(None, HEART_STORED, CONFIG, NOW)
    assert body["value"] == 58 and fresh is False       # a repost is never fresh


def test_the_flag_never_overrides_a_reading_read_this_cycle():
    body, fresh = feeder.build_heart_post(reading(), HEART_STORED, CONFIG, NOW,
                                          auth_failed=True)
    assert body["value"] == 58 and fresh is True


def test_an_unexpected_exception_logs_its_type_never_its_message(tmp_path):
    env = make_env(tmp_path)

    def reader(e):
        raise RuntimeError("Authorization: Bearer %s" % SENTINEL)

    _p, failures, bodies = heart_cycle(env, reader)
    assert failures == ["google-health: unexpected RuntimeError"]
    assert SENTINEL not in "".join(failures)
    assert is_stale_on_arrival(bodies[0])


def test_a_single_failure_with_a_stored_day_still_reposts_it(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"heart": HEART_STORED})
    posted, failures, bodies = heart_cycle(env, heart_auth_dead)
    assert posted == [("heart", False, True)]           # a repost, not "live"
    assert bodies[0]["value"] == 58 and "observed_at" in bodies[0]
    assert any("not yet confirmed" in line for line in failures)


@pytest.mark.parametrize("between", [heart_net_dead, heart_empty, heart_alive,
                                     lambda e: 1 / 0],
                         ids=["network", "no-data", "success", "bug"])
def test_any_other_outcome_resets_the_heart_count(tmp_path, between):
    env = make_env(tmp_path)
    seed(env, {"heart": HEART_STORED})
    heart_cycle(env, heart_auth_dead, 0)
    assert read_state(env)["auth-failed-cycles"] == {"google-health:heart": 1}
    heart_cycle(env, between, STEP)
    assert "auth-failed-cycles" not in read_state(env)
    body = heart_cycle(env, heart_auth_dead, 2 * STEP)[2][0]
    assert not is_stale_on_arrival(body)          # one again, not two


def test_the_count_survives_a_restart_and_reaches_two(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"heart": HEART_STORED})
    heart_cycle(env, heart_auth_dead, 0)
    # A new process: nothing but the file carries over.
    loaded = feeder.load_state(env["WEIGHT_STATE_FILE"], NOW + STEP)
    assert loaded["auth-failed-cycles"] == {"google-health:heart": 1}
    body = heart_cycle(env, heart_auth_dead, STEP)[2][0]
    assert is_stale_on_arrival(body)


def test_the_heart_count_and_the_weight_count_are_independent(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": WEIGHT_STORED, "heart": HEART_STORED})
    both_cycles(env, weight_auth_dead, heart_auth_dead, 0)
    # Weight keeps failing its sign-in; heart's read hits a network blip.
    w2, h2, _wf, _hf = both_cycles(env, weight_auth_dead, heart_net_dead, STEP)
    assert is_stale_on_arrival(w2)                # weight confirmed
    assert h2["value"] == 58 and "observed_at" in h2   # heart reset, not grey
    assert read_state(env)["auth-failed-cycles"] == {"google-health": 2}


def test_a_renewed_login_returns_a_live_gauge_and_a_clean_state(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"heart": HEART_STORED})
    heart_cycle(env, heart_auth_dead, 0)
    heart_cycle(env, heart_auth_dead, STEP)
    body = heart_cycle(env, heart_alive, 2 * STEP)[2][0]
    assert body["value"] == 58 and body["observed_at"] == DAY_ISO
    assert read_state(env) == {"heart": {"value": 58, "observed_at": DAY_STAMP}}


def test_the_count_validator_accepts_the_heart_member_and_drops_clutter():
    ok = feeder.validate_auth_counts({
        "google-health": 1, "google-health:heart": 2,
        "bogus": 1, "bogus:heart": 1, "google-health:other": 1,
        "google-health:heart:heart": 1})
    assert ok == {"google-health": 1, "google-health:heart": 2}
    assert feeder.validate_auth_counts({"google-health:heart": 10 ** 9}) == {
        "google-health:heart": feeder.AUTH_CONFIRM_CYCLES}
    for bad in (True, 0, -1, 1.5, "2", None):
        assert feeder.validate_auth_counts({"google-health:heart": bad}) == {}


def test_a_corrupt_count_alone_greys_nothing(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"heart": HEART_STORED,
               "auth-failed-cycles": {"google-health:heart": 2}})
    body = heart_cycle(env, heart_net_dead)[2][0]
    assert body["value"] == 58 and "observed_at" in body


def dead_token_server():
    raw = json.dumps({"error": "invalid_grant",
                      "error_description": SENTINEL}).encode()

    def respond(handler):
        handler.send_response(400)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)
    return respond


def google_env(tmp_path, **extra):
    token = tmp_path / "t.json"
    token.write_text(json.dumps({"refresh_token": "REFRESH-TOKEN-ABC"}),
                     encoding="utf-8")
    env = make_env(tmp_path, WEIGHT_TOKEN_FILE=str(token),
                   OAUTH2_PROXY_CLIENT_ID="i", OAUTH2_PROXY_CLIENT_SECRET="s",
                   WEIGHT_TIMEOUT_SECONDS="10", **extra)
    env["_now"] = NOW
    return env


def test_the_real_reader_against_a_dead_refresh_token_greys_on_cycle_two(
        tmp_path):
    env = google_env(tmp_path)
    seed(env, {"heart": HEART_STORED})
    with loopback_server(dead_token_server()) as (base, seen):
        reader = lambda e: feeder.read_google_health_heart(   # noqa: E731
            e, token_endpoint=base + "/token")
        _p, f1, first = heart_cycle(env, reader, 0)
        _p, f2, second = heart_cycle(env, reader, STEP)
    assert first[0]["value"] == 58 and "observed_at" in first[0]
    assert is_stale_on_arrival(second[0])
    # Only the token endpoint was asked; the dataPoints route was never reached.
    assert {request["path"] for request in seen} == {"/token"}
    journal = "\n".join(f1 + f2)
    assert SENTINEL not in journal and "REFRESH-TOKEN-ABC" not in journal
    assert "invalid_grant" in journal


# ── TC-1183: heart and weight cannot change each other's result ─────────────

WEIGHT_OWNED = ("weight", "checked", "height-in", "waist-in")


def weight_snapshot(env):
    state = read_state(env)
    return {key: state.get(key) for key in WEIGHT_OWNED}


@pytest.mark.parametrize("heart_reader", [
    heart_alive, heart_empty, heart_auth_dead, heart_net_dead,
    lambda e: 1 / 0,
    lambda e: parse(page(point(999)), NOW)],
    ids=["alive", "no-data", "auth", "network", "bug", "implausible"])
def test_whatever_heart_does_the_weight_gauge_posts_the_same_thing(
        tmp_path, heart_reader):
    reference = make_env(tmp_path / "a")
    seed(reference, {"weight": WEIGHT_STORED})
    ref_posted, ref_fail, ref_bodies = weight_cycle(reference, weight_alive)

    env = make_env(tmp_path / "b")
    seed(env, {"weight": WEIGHT_STORED})
    posted, failures, bodies = weight_cycle(env, weight_alive)
    heart_cycle(env, heart_reader)                  # heart runs AFTER weight
    assert (posted, failures, bodies) == (ref_posted, ref_fail, ref_bodies)
    assert weight_snapshot(env) == weight_snapshot(reference)


@pytest.mark.parametrize("weight_reader", [weight_alive, weight_auth_dead,
                                           weight_net_dead],
                         ids=["alive", "auth", "network"])
def test_whatever_weight_does_the_heart_gauge_posts_the_same_thing(
        tmp_path, weight_reader):
    reference = make_env(tmp_path / "a")
    ref = heart_cycle(reference, heart_alive)

    env = make_env(tmp_path / "b")
    weight_cycle(env, weight_reader)                # weight ran first
    got = heart_cycle(env, heart_alive)
    assert got == ref
    assert read_state(env)["heart"] == read_state(reference)["heart"]


def test_a_heart_cycle_touches_only_the_heart_keys_of_the_state_file(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": WEIGHT_STORED, "checked": {"weigh-in": NOW - 7200},
               "height-in": {"value": 70.0, "observed_at": NOW - 86400},
               "waist-in": {"value": 34.0, "observed_at": NOW - 86400},
               "auth-failed-cycles": {"google-health": 1}})
    before = read_state(env)
    heart_cycle(env, heart_alive)
    after = read_state(env)
    assert after.pop("heart") == {"value": 58, "observed_at": DAY_STAMP}
    assert after == before


def test_a_confirmed_heart_login_failure_clears_the_heart_reading_only(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": WEIGHT_STORED, "heart": HEART_STORED})
    heart_cycle(env, heart_auth_dead, 0)
    heart_cycle(env, heart_auth_dead, STEP)
    state = read_state(env)
    assert "heart" not in state
    assert state["weight"] == WEIGHT_STORED         # weight's own cycle decides


def test_a_weight_cycle_keeps_the_heart_entries_it_did_not_write(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"heart": HEART_STORED,
               "auth-failed-cycles": {"google-health:heart": 1}})
    weight_cycle(env, weight_alive)
    state = read_state(env)
    assert state["heart"] == HEART_STORED
    assert state["auth-failed-cycles"] == {"google-health:heart": 1}
    assert state["weight"]["value"] == 176.0


def test_run_cycle_never_calls_the_heart_reader_even_when_heart_is_on(
        monkeypatch, tmp_path):
    monkeypatch.setattr(feeder, "HEART_READERS", {"google-health": boom})
    env = make_env(tmp_path, heart="1")
    posted, failures, bodies = weight_cycle(env, weight_alive)
    assert [b["id"] for b in bodies] == ["weight"] and failures == []


def test_a_failed_heart_post_does_not_fail_or_alter_the_weight_post(tmp_path):
    env = make_env(tmp_path)
    wposted, wfail, wbodies = weight_cycle(env, weight_alive)
    hposted, hfail, _b = heart_cycle(env, heart_alive, ok=False)
    assert wposted == [("weight", True, True)] and wfail == []
    assert hposted == [("heart", True, False)]
    assert any("post heart" in line for line in hfail)


def test_a_missing_weight_goal_does_not_stop_the_heart_gauge(monkeypatch, tmp_path):
    rc, posts, _out, err, _calls = run_main(
        monkeypatch, tmp_path, {"HEART_ENABLED": "1"},
        weight_exc=feeder.GoalMissing("no goal declared"),
        heart_reader=heart_alive)
    assert rc == 2 and "weight: REFUSED" in err
    assert [p["id"] for p in posts] == ["heart"]


def test_a_refused_weight_cycle_does_not_stop_heart_either(monkeypatch, tmp_path):
    rc, posts, _o, _e, _c = run_main(
        monkeypatch, tmp_path, {"HEART_ENABLED": "1"},
        weight_exc=ValueError("bad goal"), heart_reader=heart_alive)
    assert rc == 2 and [p["id"] for p in posts] == ["heart"]


def test_a_bad_heart_knob_does_not_touch_the_weight_half(monkeypatch, tmp_path):
    rc, posts, out, err, calls = run_main(
        monkeypatch, tmp_path, {"HEART_ENABLED": "1", "HEART_TARGET_BPM": "500"},
        heart_reader=boom)
    assert calls["weight"] == 1 and posts == []
    assert "weight: weight" not in err                  # no weight failure line
    assert "heart: REFUSED" in err and "HEART_TARGET_BPM" in err
    assert out.splitlines()[0] == "weight: goal read from health.md"
    assert rc == 1                                      # visible, never silent


def test_weight_failures_keep_their_exit_code_with_heart_on_and_healthy(
        monkeypatch, tmp_path):
    rc, _p, _o, err, _c = run_main(
        monkeypatch, tmp_path, {"HEART_ENABLED": "1"},
        weight_result=([("weight", False, True)], ["google-health: boom"],
                       "health.md"),
        heart_reader=heart_alive)
    assert rc == 1 and "weight: google-health: boom" in err


def test_no_heart_data_is_a_clean_exit_so_the_unit_is_not_failed_for_ever(
        monkeypatch, tmp_path):
    rc, posts, out, err, _c = run_main(
        monkeypatch, tmp_path, {"HEART_ENABLED": "1"}, heart_reader=heart_empty)
    assert rc == 0 and err == ""
    assert out.splitlines()[-1] == "heart: heart      UNAVAILABLE  posted"
    assert is_stale_on_arrival(posts[0])


# ── TC-1184: the state file ─────────────────────────────────────────────────

def test_the_generic_loader_would_have_read_a_heart_rate_as_a_body_weight(
        tmp_path):
    # Why the dedicated validator exists: 58 "lb" sits inside 40..1000.
    assert feeder.validate_stored_reading(HEART_STORED, NOW) is not None
    env = make_env(tmp_path)
    seed(env, {"heart": HEART_STORED})
    assert feeder.load_state(env["WEIGHT_STATE_FILE"], NOW)["heart"] == {
        "value": 58, "observed_at": DAY_STAMP}


@pytest.mark.parametrize("entry", [
    None, "58", 58, [], {}, {"value": 58}, {"observed_at": DAY_STAMP},
    {"value": 9999, "observed_at": DAY_STAMP},
    {"value": 24, "observed_at": DAY_STAMP},
    {"value": 58.5, "observed_at": DAY_STAMP},
    {"value": True, "observed_at": DAY_STAMP},
    {"value": "58", "observed_at": DAY_STAMP},
    {"value": 58, "observed_at": NOW + 86400},       # the future
    {"value": 58, "observed_at": 5},                 # before the feeder
    {"value": 58, "observed_at": "2026-10-11"},
])
def test_an_unusable_stored_heart_entry_is_dropped_as_input_not_memory(entry):
    assert feeder.validate_stored_heart(entry, NOW) is None


@pytest.mark.parametrize("entry", [
    {"value": 9999, "observed_at": DAY_STAMP},       # a fine body weight in lb
    {"value": 58.5, "observed_at": DAY_STAMP},
    {"value": 58, "observed_at": NOW + 86400},
])
def test_load_state_itself_drops_an_unusable_heart_entry(tmp_path, entry):
    env = make_env(tmp_path)
    seed(env, {"heart": entry, "weight": WEIGHT_STORED})
    loaded = feeder.load_state(env["WEIGHT_STATE_FILE"], NOW)
    assert "heart" not in loaded and "weight" in loaded


def test_a_corrupt_stored_entry_posts_the_unavailable_body_not_a_number(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"heart": {"value": 9999, "observed_at": DAY_STAMP}})
    body = heart_cycle(env, heart_net_dead)[2][0]
    assert is_stale_on_arrival(body)


def test_the_state_file_holds_numbers_and_counts_never_a_credential(tmp_path):
    env = google_env(tmp_path)
    with loopback_server(dead_token_server()) as (base, _seen):
        reader = lambda e: feeder.read_google_health_heart(   # noqa: E731
            e, token_endpoint=base + "/token")
        seed(env, {"heart": HEART_STORED})
        heart_cycle(env, reader, 0)
    text = state_file(env).read_text(encoding="utf-8")
    for secret in ("REFRESH-TOKEN-ABC", "ACCESS-TOKEN", SENTINEL, "client",
                   USER_ID_SENTINEL):
        assert secret not in text
    assert set(json.loads(text)) <= {"heart", "auth-failed-cycles"}


def test_the_heart_state_key_is_not_a_gauge_that_collides_with_weights(tmp_path):
    assert feeder.HEART_STATE_KEY == "heart" != feeder.GAUGE_ID
    assert feeder.HEART_STATE_KEY not in (feeder.CHECK_STATE_KEY,
                                          feeder.HEIGHT_STATE_KEY,
                                          feeder.WAIST_STATE_KEY,
                                          feeder.AUTH_STATE_KEY)


# ── TC-1185: the real reader end to end ─────────────────────────────────────

def fake_google(pages):
    """A loopback Google: `/token` answers the grant, `/points...` the list."""
    def respond(handler):
        if handler.path.startswith("/token"):
            payload = {"access_token": "ACCESS-TOKEN-XYZ"}
        else:
            payload = pages[handler.path]
        raw = json.dumps(payload).encode()
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)
    return respond


def test_the_real_reader_posts_the_newest_day_from_a_schema_shaped_page(
        tmp_path):
    env = google_env(tmp_path)
    payload = page(point(60, (2026, 10, 9)), point(58), point(57, (2026, 10, 10)))
    assert all(conforms_to_the_schema(p) for p in payload["dataPoints"])
    with loopback_server(fake_google({"/points": payload})) as (base, seen):
        _p, failures, bodies = heart_cycle(
            env, lambda e: feeder.read_google_health_heart(
                e, list_url=base + "/points", token_endpoint=base + "/token"))
    assert failures == []
    assert bodies[0]["value"] == 58 and bodies[0]["observed_at"] == DAY_ISO
    assert naglight_validate(bodies[0]) is None
    token_request, list_request = seen
    assert token_request["path"] == "/token"
    # The plain request: no filter, no pageSize (unexercised parameters).
    assert list_request["path"] == "/points"
    assert list_request["headers"]["Authorization"] == "Bearer ACCESS-TOKEN-XYZ"


def test_the_real_reader_asks_the_heart_route_by_default_and_parses_its_page(
        tmp_path, monkeypatch):
    env = google_env(tmp_path)
    asked = []

    def fake_walk(access_token, now, timeout, base, parser, noun, what):
        asked.append((base, parser, access_token))
        return reading()

    monkeypatch.setattr(feeder, "walk_data_points", fake_walk)
    with loopback_server(fake_google({})) as (base, _seen):
        got = feeder.read_google_health_heart(env, token_endpoint=base + "/token")
    assert got.bpm == 58
    assert len(asked) == 1
    url, parser, token = asked[0]
    assert url == ("https://health.googleapis.com/v4/users/me/dataTypes/"
                   "daily-resting-heart-rate/dataPoints")
    assert token == "ACCESS-TOKEN-XYZ"
    # The parser the reader hands the walk is the heart parser WITH the hub's
    # zone from TIMEZONE: it stamps noon local (CDT), not a UTC hour.
    assert parser(page(point(58)), NOW).observed_at == DAY_STAMP
    assert feeder.HEART_READERS["google-health"] is feeder.read_google_health_heart


def test_the_real_reader_follows_a_page_token_to_a_newer_day(tmp_path):
    env = google_env(tmp_path)
    pages = {"/points": page(point(60, (2026, 10, 9)), token="NEXT-1"),
             "/points?pageToken=NEXT-1": page(point(58))}
    with loopback_server(fake_google(pages)) as (base, _seen):
        got = feeder.read_google_health_heart(
            env, list_url=base + "/points", token_endpoint=base + "/token")
    assert got.bpm == 58 and got.observed_at == DAY_STAMP


def test_the_real_reader_on_an_account_with_no_data_is_no_data(tmp_path):
    env = google_env(tmp_path)
    with loopback_server(fake_google({"/points": {"dataPoints": []}})) \
            as (base, _seen):
        with pytest.raises(feeder.NoWeightYet) as err:
            feeder.read_google_health_heart(
                env, list_url=base + "/points", token_endpoint=base + "/token")
    assert "resting heart rate" in str(err.value)


def test_a_list_call_that_errors_is_an_ordinary_failure_naming_no_body(tmp_path):
    env = google_env(tmp_path)

    def respond(handler):
        if handler.path.startswith("/token"):
            raw = json.dumps({"access_token": "A"}).encode()
            status = 200
        else:
            raw = json.dumps({"error": {"message": SENTINEL}}).encode()
            status = 500
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    with loopback_server(respond) as (base, _seen):
        _p, failures, bodies = heart_cycle(
            env, lambda e: feeder.read_google_health_heart(
                e, list_url=base + "/points", token_endpoint=base + "/token"))
    assert failures and SENTINEL not in "".join(failures)
    assert not any("sign-in" in line for line in failures)
    assert is_stale_on_arrival(bodies[0])


def test_the_route_is_the_weight_route_with_one_segment_swapped_same_scope():
    weight_url = feeder.GOOGLE_HEALTH_LIST_URL
    heart_url = feeder.GOOGLE_HEALTH_HEART_LIST_URL
    assert heart_url == ("https://health.googleapis.com/v4/users/me/dataTypes/"
                         "daily-resting-heart-rate/dataPoints")
    assert weight_url.replace("/weight/", "/daily-resting-heart-rate/") == heart_url
    # ONE scope constant in the module, and it is the metrics scope the data
    # types table lists for this data type: no new consent.
    source = MODULE_PATH.read_text(encoding="utf-8")
    scopes = set(re.findall(r"googlehealth\.[a-z_]+\.(?:readonly|writeonly)",
                            source))
    assert scopes == {"googlehealth.health_metrics_and_measurements.readonly"}
    assert feeder.GOOGLE_HEALTH_SCOPE.endswith(
        "googlehealth.health_metrics_and_measurements.readonly")


def test_the_oauth_capture_tool_can_fetch_one_heart_body():
    spec = importlib.util.spec_from_file_location("weight_oauth_heart", OAUTH_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["weight_oauth_heart"] = module
    sys.path.insert(0, str(MODULE_PATH.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    assert module.LIST_URLS["heart"] == feeder.GOOGLE_HEALTH_HEART_LIST_URL
    assert module.SCOPE == feeder.GOOGLE_HEALTH_SCOPE


def test_the_journal_and_stdout_of_a_whole_cycle_carry_no_health_value(
        monkeypatch, tmp_path, capsys):
    env = make_env(tmp_path)

    def reader(e):
        return parse(
            page(point(187, (2026, 10, 11))), NOW)

    posts = []
    monkeypatch.setattr(feeder, "post_gauge",
                        lambda b, u, e, t: (posts.append(b) or True, "HTTP 200"))
    monkeypatch.setattr(feeder, "HEART_READERS", {"google-health": reader})
    monkeypatch.setattr(feeder, "time", FrozenTime())
    env["_now"] = NOW
    assert feeder.report_heart(env) is False
    captured = capsys.readouterr()
    assert posts[0]["value"] == 187
    assert "187" not in captured.out + captured.err
    assert "2026-10-11" not in captured.out + captured.err


def test_the_readme_has_the_heart_section_with_the_scope_and_the_knobs():
    readme = (REPO / "stack" / "weight" / "README.md").read_text(encoding="utf-8")
    assert "## Heart (resting heart rate)" in readme
    section = readme.split("## Heart (resting heart rate)", 1)[1]
    section = " ".join(re.split(r"\n## ", section, 1)[0].split())
    for needle in ("HEART_ENABLED", "HEART_MIN_BPM", "HEART_MAX_BPM",
                   "HEART_TARGET_BPM", "daily-resting-heart-rate",
                   "health_metrics_and_measurements.readonly", "no new consent",
                   "Fitbit", "capture --data-type heart", "20261007",
                   "invalid_grant", "not been observed"):
        assert needle in section, needle
