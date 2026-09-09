"""TC-006 — the weight feeder's acceptance criteria, asserted.

SN-040's acceptance is two sentences and this file is organised around them:

  A. "a missing or stale source renders stale, never green" — asserted for
     every failure class separately (the source refuses, it raises an unexpected
     exception, it returns garbage, it returns a stale-but-real reading), and
     asserted against NagLight's own freshness rule rather than against our
     intent. Also asserted for the case that has no history at all, where the
     posted 0 must be inseparable from a missing `observed_at`.
  B. "the goal lives in the user's definitions so it syncs like everything
     else" — asserted by reading a real definitions tree on disk, in the shape
     NagLight's internal/defs parses, and by proving that NO hub knob can
     supply the goal instead.

Plus the wire contract (IF-014 / NagLight IF-012), because a weight gauge's
shape differs from the usage feeder's in three ways that are each a 400 or a
silent lie if wrong: no min/max (the `lb` range is NagLight's to infer), no
window and therefore no direction, and `value`/`target` always present — an
omitted weight used to be stored as a fabricated 0.

Plus the standard this build runs on: NO PARSER MAY EXIST FOR A VENDOR CALL
THAT HAS NEVER BEEN MADE (B7's `parse_gemini` precedent).

Verifies: TC-006 (SR-022, LLR-006)
"""

import importlib.util
import json
import math
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "stack" / "weight" / "weight_feeder.py"

_spec = importlib.util.spec_from_file_location("weight_feeder", MODULE_PATH)
feeder = importlib.util.module_from_spec(_spec)
sys.modules["weight_feeder"] = feeder
_spec.loader.exec_module(feeder)

NOW = 1789000000          # a fixed "now" so nothing here depends on the clock.
GOAL = 180.0


def write_defs(tmp_path, files):
    """Materialise a definitions directory in the shape NagLight loads."""
    defs_dir = tmp_path / "definitions"
    defs_dir.mkdir(exist_ok=True)
    for name, body in files.items():
        (defs_dir / name).write_text(body, encoding="utf-8")
    return str(defs_dir)


HEALTH_MD = """---
category: health
color_weight: 1.5
weight_goal_lb: 180
items:
  - id: weigh-in
    title: Step on the scale
    type: habit
    recur: daily
---

Free notes after the frontmatter. Someone might well write weight_goal_lb: 999
down here while thinking out loud, and it must not become the goal.
"""


# ═══════════════════════════════════════════════════════════════════════════
# A. "a missing or stale source renders stale, never green"
# ═══════════════════════════════════════════════════════════════════════════

def test_no_source_and_no_history_is_stale_on_arrival_sr022():
    """The worst case: nothing has ever been read.

    The gauge must still EXIST (so the panel says "unavailable" rather than
    leaving a hole where a bar belongs) and must be stale on arrival. The
    posted 0 is only safe BECAUSE `observed_at` is absent, so both halves are
    asserted together — a 0 with a stamp would be a claim that someone weighs
    nothing.
    """
    body, fresh = feeder.build_post(None, None, GOAL, NOW)
    assert fresh is False
    assert body["value"] == 0.0
    assert "observed_at" not in body
    assert feeder.is_fresh(body, NOW) is False


def test_failed_source_reposts_the_last_value_at_its_original_stamp_sr022():
    """A source that failed must not have its old reading re-stamped to now.

    This is the mutation that looks like a bug fix: "the gauge says
    unavailable, let's stamp it now". Re-stamping keeps the bar green on a
    number that stopped being true, which is the whole failure SN-040 names.
    """
    measured_at = NOW - 3 * 24 * 3600
    last = {"value": 191.4, "observed_at": measured_at}
    body, fresh = feeder.build_post(None, last, GOAL, NOW)
    assert fresh is False
    assert body["value"] == 191.4
    assert body["observed_at"] == feeder.iso8601_utc(measured_at)
    assert body["observed_at"] != feeder.iso8601_utc(NOW)


def test_a_reading_older_than_the_static_horizon_is_not_fresh_sr022():
    """Stale means stale even when this cycle DID read the source.

    The gauge carries no window, so NagLight's horizon is the `static` one
    (7 days). A person who last weighed themselves a fortnight ago has a real,
    honest reading that is nevertheless stale, and the panel must say so.
    """
    two_weeks = NOW - 14 * 24 * 3600
    body, fresh = feeder.build_post((191.4, two_weeks), None, GOAL, NOW)
    assert fresh is True                       # this cycle really did read it
    assert feeder.is_fresh(body, NOW) is False  # …and it is still stale
    assert feeder.is_fresh(
        feeder.build_post((191.4, NOW - 3600), None, GOAL, NOW)[0], NOW) is True


def test_a_future_stamp_is_stale_sr022():
    """A clock-skewed or fabricated future reading is never green."""
    body, _ = feeder.build_post((191.4, NOW + 86400), None, GOAL, NOW)
    assert feeder.is_fresh(body, NOW) is False


def test_an_unparseable_stamp_is_stale_sr022():
    """SR-067 serves a gauge whose observed_at does not parse as stale."""
    assert feeder.is_fresh({"observed_at": "yesterday-ish"}, NOW) is False
    assert feeder.is_fresh({"observed_at": None}, NOW) is False


@pytest.mark.parametrize("boom", [
    feeder.SourceFailure("google-health: BLOCKED on an Owner action"),
    feeder.SourceFailure("google-health: HTTP 401"),
    feeder.SourceFailure("google-health: body is not JSON"),
    feeder.SourceFailure("google-health: no weight data point in window"),
    RuntimeError("a bug in our own reader"),
    TimeoutError("the vendor hung"),
])
def test_every_failure_class_collapses_to_one_unavailable_post_sr022(tmp_path, boom):
    """Six failure classes, one path, never a fresh gauge.

    A reader BUG (the RuntimeError) is included on purpose: the acceptance
    criterion is about what the panel shows, and "our own code threw" is
    indistinguishable from "the vendor was down" as far as the wall is
    concerned. run_cycle catching only SourceFailure would let a bug escape and
    crash the cycle before anything was posted, leaving the previous gauge to
    age quietly.
    """
    posts = []
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:8787/api/feed",
           "WEIGHT_STATE_FILE": str(tmp_path / "state.json")}

    def reader(_env):
        raise boom

    posted, failures, _ = feeder.run_cycle(
        env, now=NOW, readers={"google-health": reader},
        poster=lambda body, url, e, t: (posts.append(body), (True, "HTTP 200"))[1],
        goal_loader=lambda d: (GOAL, "health.md"))
    assert posted == [("weight", False, True)]
    assert failures and "google-health" in failures[0]
    assert len(posts) == 1
    assert feeder.is_fresh(posts[0], NOW) is False


def test_a_failing_source_never_writes_state_so_history_cannot_drift(tmp_path):
    """A failed cycle must not touch the last-known reading.

    If it did, the "repost at the ORIGINAL stamp" behaviour would decay: each
    failed cycle would rewrite the stamp and the gauge would never go stale.
    """
    state_path = tmp_path / "state.json"
    original = {"weight": {"value": 191.4, "observed_at": NOW - 100000}}
    state_path.write_text(json.dumps(original), encoding="utf-8")
    before = state_path.read_bytes()
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:8787/api/feed",
           "WEIGHT_STATE_FILE": str(state_path)}
    feeder.run_cycle(env, now=NOW,
                     readers={"google-health": lambda e: (_ for _ in ()).throw(
                         feeder.SourceFailure("down"))},
                     poster=lambda *a: (True, "HTTP 200"),
                     goal_loader=lambda d: (GOAL, "health.md"))
    assert state_path.read_bytes() == before


# ═══════════════════════════════════════════════════════════════════════════
# B. "the goal lives in the user's definitions so it syncs like everything else"
# ═══════════════════════════════════════════════════════════════════════════

def test_the_goal_is_read_from_the_users_definitions_sr022(tmp_path):
    """The goal comes off disk, out of the definitions NagLight also loads."""
    defs_dir = write_defs(tmp_path, {"health.md": HEALTH_MD})
    goal, source = feeder.load_goal_from_definitions(defs_dir)
    assert goal == 180.0
    assert source.endswith("health.md")


def test_the_goal_becomes_the_gauges_target_line_sr022(tmp_path):
    """SN-040: the goal IS the target line, and nothing else supplies it."""
    defs_dir = write_defs(tmp_path, {"health.md": HEALTH_MD})
    goal, _ = feeder.load_goal_from_definitions(defs_dir)
    body, _ = feeder.build_post((191.4, NOW), None, goal, NOW)
    assert body["target"] == 180.0
    assert body["value"] == 191.4


def test_no_hub_knob_can_supply_the_goal_sr022(tmp_path):
    """The criterion is not merely "a goal exists" — it is WHERE it lives.

    A knob would be a second home for the household's intent that does not
    sync, so this asserts the negative: with a full environment set and an
    empty definitions tree, the cycle refuses. Every plausible knob name is
    tried, so adding one later turns this red.
    """
    defs_dir = write_defs(tmp_path, {})
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:8787/api/feed",
           "WEIGHT_DEFINITIONS_DIR": defs_dir,
           "WEIGHT_STATE_FILE": str(tmp_path / "state.json"),
           "WEIGHT_GOAL": "180", "WEIGHT_GOAL_LB": "180",
           "WEIGHT_TARGET": "180", "WEIGHT_TARGET_LB": "180"}
    with pytest.raises(feeder.GoalMissing):
        feeder.run_cycle(env, now=NOW,
                         readers={"google-health": lambda e: (191.4, NOW)},
                         poster=lambda *a: (True, "HTTP 200"))


def test_a_missing_goal_posts_nothing_at_all_sr022(tmp_path):
    """No goal is NOT the same refusal as no source.

    No source posts an unavailable gauge; no goal posts nothing, because the
    target line IS the goal and inventing one would draw a 50 lb bar around a
    number nobody chose. Asserted by proving the poster was never called.
    """
    posts = []
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:8787/api/feed",
           "WEIGHT_DEFINITIONS_DIR": write_defs(tmp_path, {}),
           "WEIGHT_STATE_FILE": str(tmp_path / "state.json")}
    with pytest.raises(feeder.GoalMissing):
        feeder.run_cycle(env, now=NOW,
                         readers={"google-health": lambda e: (191.4, NOW)},
                         poster=lambda body, *a: (posts.append(body), (True, ""))[1])
    assert posts == []


def test_the_goal_survives_naglights_own_loader_sr022(tmp_path):
    """The declaration must be legal where it lives, not merely readable by us.

    internal/defs/yaml.go ignores unknown TOP-LEVEL frontmatter keys, which is
    the whole reason this needs no NagLight change to be stored. This asserts
    the shape that relies on: a top-level scalar, before `items:`, in a file
    that is otherwise an ordinary definitions file. A goal written as an ITEM
    field (indented under a dash) would be a different thing entirely and is
    asserted not to be picked up.
    """
    indented = HEALTH_MD.replace("weight_goal_lb: 180\n", "")
    indented = indented.replace("    recur: daily\n",
                                "    recur: daily\n    weight_goal_lb: 180\n")
    defs_dir = write_defs(tmp_path, {"health.md": indented})
    with pytest.raises(feeder.GoalMissing):
        feeder.load_goal_from_definitions(defs_dir)


def test_a_top_level_goal_after_the_items_sequence_is_not_the_goal_sr022(tmp_path):
    """Nothing top-level follows `items:` in a definitions file.

    A key at column zero AFTER the item sequence is malformed YAML that the
    frontmatter happens to contain, not a declaration — internal/defs stops
    reading top-level scalars at `items:` and so must we, or a stray line at
    the bottom of a hand-edited file silently becomes the household's goal.

    THIS TEST EXISTS BECAUSE THE MUTATION RUN FOUND THE BREAK UNTESTED: with it
    removed the whole suite stayed green, because the indent check downstream
    was catching the only case anything asserted.
    """
    body = ("---\ncategory: health\nitems:\n  - id: x\n    title: X\n"
            "weight_goal_lb: 999\n---\n")
    defs_dir = write_defs(tmp_path, {"health.md": body})
    with pytest.raises(feeder.GoalMissing):
        feeder.load_goal_from_definitions(defs_dir)


def test_an_indented_goal_is_an_item_field_not_the_file_level_goal_sr022(tmp_path):
    """The indent check is the other half, and it is asserted on its own.

    Written after the mutation run: the two guards were covering for each
    other, so breaking either left the suite green.
    """
    assert feeder.find_goal_in_frontmatter(
        "---\ncategory: health\n  weight_goal_lb: 999\n---\n", "f") is None


def test_a_goal_in_the_prose_body_is_not_the_goal_sr022(tmp_path):
    """Only the leading frontmatter counts, matching defs.frontmatter."""
    body = "---\ncategory: health\nitems:\n  - id: x\n---\n\nweight_goal_lb: 999\n"
    defs_dir = write_defs(tmp_path, {"health.md": body})
    with pytest.raises(feeder.GoalMissing):
        feeder.load_goal_from_definitions(defs_dir)


def test_two_files_declaring_a_goal_are_refused_not_ordered_sr022(tmp_path):
    """Picking the first would silently follow file-name order.

    That is the same defect class defs.Load found on a real household's files
    with duplicate `check:` ids: a loser that is never used and never reported.
    """
    other = HEALTH_MD.replace("weight_goal_lb: 180", "weight_goal_lb: 165")
    defs_dir = write_defs(tmp_path, {"health.md": HEALTH_MD, "aaa.md": other})
    with pytest.raises(ValueError) as err:
        feeder.load_goal_from_definitions(defs_dir)
    assert "not guessable" in str(err.value)


def test_one_file_declaring_the_goal_twice_is_refused_sr022(tmp_path):
    doubled = HEALTH_MD.replace("weight_goal_lb: 180",
                                "weight_goal_lb: 180\nweight_goal_lb: 165")
    defs_dir = write_defs(tmp_path, {"health.md": doubled})
    with pytest.raises(ValueError):
        feeder.load_goal_from_definitions(defs_dir)


@pytest.mark.parametrize("raw,expected", [
    ("180", 180.0), (" 180 ", 180.0), ("180.5", 180.5),
    ('"180"', 180.0), ("'180'", 180.0), ("180 # my goal", 180.0),
])
def test_the_goal_is_parsed_the_way_a_person_would_write_it(raw, expected):
    """Frontmatter is hand-edited YAML; quotes and comments are ordinary."""
    assert feeder.parse_goal_text(raw, "f") == expected


@pytest.mark.parametrize("raw", ["", "   ", "#just a comment"])
def test_a_blank_goal_declaration_is_missing_not_zero(raw):
    with pytest.raises(feeder.GoalMissing):
        feeder.parse_goal_text(raw, "f")


@pytest.mark.parametrize("raw", ["one eighty", "nan", "inf", "-inf",
                                 "1800", "18", "0", "-180"])
def test_an_unusable_goal_is_refused_rather_than_ignored(raw):
    """A typo'd goal must NOT degrade to "no goal declared".

    `1800` for `180` is the case this guards: it parses, it is finite, and it
    would drag NagLight's inferred bar to 1775..1825 so every real reading
    paints full red forever, with nothing anywhere saying why.
    """
    with pytest.raises((ValueError, feeder.GoalMissing)):
        feeder.parse_goal_text(raw, "f")


# ═══════════════════════════════════════════════════════════════════════════
# The wire contract (IF-014 / NagLight IF-012)
# ═══════════════════════════════════════════════════════════════════════════

def test_value_and_target_are_always_present_if014():
    """An omitted number is a 400 now, and used to be stored as a 0.

    Asserted on the unavailable body too, which is where an "optimisation"
    would most plausibly drop them.
    """
    for body in (feeder.build_post((191.4, NOW), None, GOAL, NOW)[0],
                 feeder.build_post(None, None, GOAL, NOW)[0]):
        assert "value" in body and "target" in body
        assert isinstance(body["value"], float)
        assert isinstance(body["target"], float)


def test_no_min_max_is_sent_so_naglight_infers_the_50lb_bar_if014():
    """`lb` is the ONE unit NagLight infers a range for (SN-040, gauge.go).

    Sending our own min/max would put a second range authority on the producer
    side. This asserts the decision, so changing it is a deliberate act.
    """
    body, _ = feeder.build_post((191.4, NOW), None, GOAL, NOW)
    assert "min" not in body and "max" not in body
    assert body["unit"] == "lb"


def test_no_window_and_therefore_no_direction_if014():
    """A goal is a standing line; NagLight REFUSES direction without a window.

    B5's manual feeder posts neither for the same reason, and a feeder that
    added `direction: "down"` here would have every post rejected outright.
    """
    body, _ = feeder.build_post((191.4, NOW), None, GOAL, NOW)
    assert "window" not in body
    assert "direction" not in body


def test_the_feeder_sends_numbers_only_never_a_colour_if014():
    """NagLight owns severity and colour; a feeder that computed a hue would
    make the panel's authority ambiguous (the ownership rule in §5)."""
    blob = json.dumps(feeder.build_post((191.4, NOW), None, GOAL, NOW)[0]).lower()
    for forbidden in ("css", "colour", "color", "severity", "red", "green",
                      "stale", "pace", "deviation"):
        assert forbidden not in blob


def test_text_fields_are_within_the_rune_limit_if014():
    body, _ = feeder.build_post((191.4, NOW), None, GOAL, NOW)
    for field in ("id", "label", "icon", "unit"):
        assert len(body[field]) <= feeder.RUNE_LIMIT


def test_observed_at_is_rfc3339_because_naglight_parses_it_as_a_string_if014():
    """The usage feeder sends integer stamps; this contract takes RFC3339.

    Getting it wrong does not fail loudly — it renders as a permanently stale
    gauge — so it is asserted directly.
    """
    body, _ = feeder.build_post((191.4, NOW), None, GOAL, NOW)
    assert time.strptime(body["observed_at"], "%Y-%m-%dT%H:%M:%SZ")


def test_the_gauge_id_matches_b5s_manual_feeder_so_this_replaces_it():
    """The plan says this REPLACES B5's manual feed.

    NagLight upserts a gauge by id, so a different id would stand a second
    weight bar beside the first rather than replacing it, and the two would
    disagree the moment one source went quiet.
    """
    manual = (REPO.parent / "NagLight" / "feeders" / "weight-manual.ps1")
    if not manual.exists():
        pytest.skip("NagLight is not checked out beside this repo")
    text = manual.read_text(encoding="utf-8")
    assert 'id          = "%s"' % feeder.GAUGE_ID in text
    assert 'unit        = "%s"' % feeder.GAUGE_UNIT in text


def test_a_non_finite_or_absurd_reading_never_becomes_a_body():
    for bad in (float("nan"), float("inf"), None, "191.4", True):
        with pytest.raises((ValueError, feeder.SourceFailure)):
            feeder.gauge_body(bad, GOAL, NOW)


# ═══════════════════════════════════════════════════════════════════════════
# The vendor path: verified, blocked, and deliberately parser-free
# ═══════════════════════════════════════════════════════════════════════════

def test_no_parser_exists_for_a_call_that_was_never_made_sr022():
    """B7's `parse_gemini` precedent, applied to the blocked vendor half.

    A weight parser written from a schema is the worst place to break the "one
    real call first" rule: if grams are actually kilograms the feeder does not
    fail, it posts a confident, plausible, wrong body weight. This test is what
    stops a later session from "finishing" the block from documentation.
    """
    for name in ("parse_google_health", "parse_weight_datapoint",
                 "parse_weight", "parse_datapoints"):
        assert not hasattr(feeder, name), (
            "%s exists, but no authenticated Google Health call has been made. "
            "Make one, paste the body into the module docstring, THEN write "
            "the parser." % name)


def test_the_blocked_source_refuses_by_name_rather_than_returning_zero(tmp_path):
    """The refusal must be a SourceFailure that names the Owner action.

    Returning a placeholder reading, or None, would post a number; raising the
    wrong exception type would crash the cycle before the unavailable gauge was
    posted. Both are asserted against.
    """
    with pytest.raises(feeder.SourceFailure) as err:
        feeder.read_google_health({"WEIGHT_TOKEN_FILE": str(tmp_path / "nope.json")})
    message = str(err.value)
    assert "BLOCKED" in message
    assert feeder.GOOGLE_HEALTH_SCOPE in message


def test_the_verified_google_health_facts_are_the_ones_that_were_observed():
    """These constants are the record of the 2026-09-09 verification.

    The scope in particular is asserted because the build plan assumed weight
    had "its own OAuth scope" and the discovery document says otherwise: the
    only scope that admits the weight data type is the shared health-metrics
    one, which also grants blood glucose, body fat and heart-rate metrics. A
    later edit inventing `googlehealth.weight.readonly` would fail here.
    """
    assert feeder.GOOGLE_HEALTH_SCOPE == (
        "https://www.googleapis.com/auth/googlehealth."
        "health_metrics_and_measurements.readonly")
    assert feeder.GOOGLE_HEALTH_LIST_URL == (
        "https://health.googleapis.com/v4/users/me/dataTypes/weight/dataPoints")
    assert "dataTypes/weight/dataPoints" in feeder.GOOGLE_HEALTH_LIST_URL, (
        "developers.google.com renders this as /users/me/dataPoints/weight; "
        "the discovery document, which is what routing follows, does not.")
    assert feeder.GOOGLE_HEALTH_RPC.endswith("DataPointsService.ListDataPoints")


def test_grams_convert_to_pounds_by_the_exact_international_definition():
    """Google Health stores `weightGrams`; NagLight infers its bar from `lb`.

    Hand-checked: 86 182.5 g is 190.0 lb to one decimal, the precision the
    panel shows.
    """
    assert round(feeder.grams_to_pounds(86182.5), 1) == 190.0
    assert round(feeder.grams_to_pounds(453.59237), 6) == 1.0


@pytest.mark.parametrize("bad", [None, "191", True, float("nan"),
                                 float("inf"), 0, -1])
def test_an_unusable_vendor_number_is_a_source_failure_not_a_reading(bad):
    with pytest.raises(feeder.SourceFailure):
        feeder.check_pounds(bad, "vendor")


def test_an_unknown_source_name_is_refused_rather_than_skipped():
    with pytest.raises(SystemExit):
        feeder.enabled_source({"WEIGHT_SOURCE": "fitbit"})
    assert feeder.enabled_source({}) == "google-health"


# ═══════════════════════════════════════════════════════════════════════════
# Refusals: off by default, one identity, one destination, one writable file
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("", False), (None, False), ("True", False),
    ("yes", False), ("1", False), ("TRUE", False), (" true ", True),
])
def test_off_is_the_default_and_every_typo_sr022(raw, expected):
    env = {} if raw is None else {"WEIGHT_ENABLED": raw}
    assert feeder.resolve_enabled(env) is expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_a_blank_identity_is_a_refusal_not_a_fallback_sr022(raw):
    """A body weight on the wrong person's board is a private measurement shown
    to the wrong person on a wall in a shared room."""
    env = {} if raw is None else {"WEIGHT_USER": raw}
    with pytest.raises(SystemExit):
        feeder.resolve_identity(env)


@pytest.mark.parametrize("url", [
    "", "https://tracker.example.com/api/feed", "http://192.168.1.50:8787/api/feed",
    "http://localhost:8787/api/feed", "ftp://127.0.0.1/api/feed",
    "http:///api/feed", "http://172.20.0.1:8787/api/feed",
])
def test_an_off_box_destination_is_refused_sr022(url, monkeypatch):
    """The POST carries the tracker token, the identity AND a body weight.

    `localhost` is refused as a NAME rather than resolved, and 172.20.0.1 is
    refused because membership of 172.16.0.0/12 is not the same as an address a
    bridge on THIS box is carrying — the lesson resolve_bind learned.
    """
    monkeypatch.setattr(feeder, "local_bridge_networks", lambda: [])
    env = {} if not url else {"WEIGHT_FEED_URL": url}
    with pytest.raises(SystemExit):
        feeder.resolve_feed_url(env)


def test_loopback_and_a_real_bridge_address_are_accepted(monkeypatch):
    import ipaddress
    monkeypatch.setattr(feeder, "local_bridge_networks",
                        lambda: [ipaddress.ip_network("172.20.0.0/16")])
    assert feeder.resolve_feed_url({"WEIGHT_FEED_URL": "http://127.0.0.1:8787/api/feed"})
    assert feeder.resolve_feed_url({"WEIGHT_FEED_URL": "http://172.20.0.1:8787/api/feed"})


def test_open_for_write_is_an_allow_list_of_one_path(tmp_path):
    """Not a deny-list of names: the next vendor tool puts its token somewhere
    new, so the feeder declares the one path it may write."""
    state = str(tmp_path / "weight-state.json")
    with feeder.open_for_write(state, state) as handle:
        handle.write("{}")
    with feeder.open_for_write(state + ".tmp", state) as handle:
        handle.write("{}")
    for forbidden in (str(tmp_path / "other.json"),
                      str(tmp_path / "google-health-token.json"),
                      str(tmp_path / "sub" / "weight-state.json"),
                      str(tmp_path / ".netrc")):
        with pytest.raises(PermissionError):
            feeder.open_for_write(forbidden, state)


def test_a_whole_cycle_writes_exactly_one_file_and_no_token(tmp_path):
    """Run a real cycle against a throwaway home holding a token file, and
    prove not one byte under it changed and the state file holds no secret."""
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    token_file = home / ".config" / "google-health-token.json"
    token_file.write_text(json.dumps({"refresh_token": "SECRET-DO-NOT-COPY"}),
                          encoding="utf-8")
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "weight-state.json"
    env = {"_identity": "u", "_feed_url": "http://127.0.0.1:8787/api/feed",
           "WEIGHT_STATE_FILE": str(state_path),
           "WEIGHT_TOKEN_FILE": str(token_file)}
    feeder.run_cycle(env, now=NOW,
                     readers={"google-health": lambda e: (191.4, NOW)},
                     poster=lambda *a: (True, "HTTP 200"),
                     goal_loader=lambda d: (GOAL, "health.md"))

    after = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert after == before, "the feeder touched something under HOME"
    written = [p for p in state_dir.rglob("*") if p.is_file()]
    assert [p.name for p in written] == ["weight-state.json"]
    assert "SECRET-DO-NOT-COPY" not in state_path.read_text(encoding="utf-8")
    assert set(json.loads(state_path.read_text(encoding="utf-8"))["weight"]) == {
        "value", "observed_at"}


# ═══════════════════════════════════════════════════════════════════════════
# Carriage: the shipped unit and timer must match the reasoning
# ═══════════════════════════════════════════════════════════════════════════

def test_the_shipped_timer_is_far_below_the_static_horizon():
    """The cadence follows from NagLight's horizon table, not from taste.

    This gauge has no window, so its horizon is `static` (7 days). The timer is
    read from the shipped file and this fails if it is ever loosened past a
    quarter of that, so the relationship is enforced rather than remembered.
    """
    timer = (REPO / "stack" / "weight" / "homehub-weight.timer").read_text(encoding="utf-8")
    line = [l for l in timer.splitlines() if l.startswith("OnUnitActiveSec=")]
    assert line, "the timer declares no OnUnitActiveSec"
    value = line[0].split("=", 1)[1].strip()
    seconds = int(value[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[value[-1]]
    horizon = feeder.STALE_HORIZON_SECONDS["static"]
    assert seconds <= horizon / 4

    # AND THE HORIZON BOUND ALONE IS NOT ENOUGH HERE, WHICH THE MUTATION RUN
    # PROVED. A quarter of the static 7-day horizon is FORTY-TWO HOURS, so the
    # assertion above passes on a three-hourly timer, a six-hourly one, and a
    # daily one — it looks like it is enforcing the cadence and is enforcing
    # almost nothing. (The usage feeder's copy of this test is meaningful
    # because its tightest horizon is 24 h; copying the shape across without
    # re-deriving the number is what made it vacuous.) The cadence that
    # actually matters is how long the wall can keep showing a number after the
    # SOURCE has gone away, and the answer the design committed to is "within
    # the hour, not within two days".
    assert seconds <= 3600, (
        "the static 7-day horizon is too loose to constrain this timer; the "
        "panel must learn a source is gone within the hour")


def test_the_unit_never_writes_the_token_directory():
    """ProtectHome=read-only, not `yes`: the feeder must still READ the token.

    Second, independent line behind open_for_write — a bug past the guard still
    cannot truncate a refresh token the Owner minted at a browser.
    """
    unit = (REPO / "stack" / "weight" / "homehub-weight.service").read_text(encoding="utf-8")
    # DIRECTIVES ARE MATCHED AT THE START OF A LINE, NOT AS SUBSTRINGS. The
    # unit EXPLAINS each of these choices in a comment that quotes the
    # directive verbatim, so `"ProtectHome=read-only" in unit` passes on the
    # explanation alone — it stayed green when the mutation run set the real
    # directive to `ProtectHome=no`. That is the same vacuous shape the
    # `Restart=` assertion below had, found the same way.
    directives = [l.strip() for l in unit.splitlines()
                  if l and not l.startswith(("#", "[", " "))]
    assert "ProtectHome=read-only" in directives, (
        "the token's home must be readable and NOT writable; `yes` would hide "
        "it entirely and the feeder must still READ the refresh token")
    assert "ProtectSystem=strict" in directives
    # A failed cycle must not hot-loop against a health API. Matched at the
    # start of a line, because the unit EXPLAINS the absence in a comment and a
    # substring test would pass on the explanation alone — a vacuous assertion
    # of exactly the kind this build has now found five times.
    assert not [l for l in unit.splitlines() if l.startswith("Restart=")]
    assert "Type=oneshot" in directives


def test_the_knobs_are_declared_where_deploy_reads_them():
    """A knob missing from .env.example ships as nothing; one missing from
    HomeHub's FieldSchema is skipped by the emitter (the OPERATOR_PASSWORD
    lesson, E1), so a config that says `on` still ships `false`."""
    env_example = (REPO / "stack" / ".env.example").read_text(encoding="utf-8")
    knobs = ["WEIGHT_ENABLED", "WEIGHT_USER", "WEIGHT_USER_ACCOUNT",
             "WEIGHT_FEED_URL", "WEIGHT_FEED_TOKEN", "WEIGHT_SOURCE",
             "WEIGHT_DEFINITIONS_DIR", "WEIGHT_TOKEN_FILE",
             "WEIGHT_STATE_FILE", "WEIGHT_TIMEOUT_SECONDS"]
    for knob in knobs:
        assert "\n%s=" % knob in env_example, "%s is not in .env.example" % knob
    schema = REPO.parent / "HomeHub" / "scripts" / "deploy" / "FieldSchema.psd1"
    if not schema.exists():
        pytest.skip("HomeHub is not checked out beside this repo")
    text = schema.read_text(encoding="utf-8-sig")
    for knob in knobs:
        assert knob in text, "%s is not declared in FieldSchema.psd1" % knob


def test_no_goal_shaped_knob_is_declared_anywhere_deploy_reads_sr022():
    """The goal must not merely be unused on the hub — it must be ABSENT.

    Written after the mutation run, which added `WEIGHT_GOAL_LB=180` to
    .env.example and left the whole suite green: the runtime refusal was
    asserted, but nothing stopped a goal knob from being DECLARED. A declared
    knob is a second home for the household's intent whatever the code does
    with it — someone fills it in, nothing reads it, and the person cannot work
    out why the bar will not move. `WEIGHT_DEFINITIONS_DIR` is exempt because
    it names the directory, never the number.
    """
    files = [REPO / "stack" / ".env.example"]
    schema = REPO.parent / "HomeHub" / "scripts" / "deploy" / "FieldSchema.psd1"
    if schema.exists():
        files.append(schema)
    banned = ("WEIGHT_GOAL", "WEIGHT_TARGET", "GOAL_WEIGHT", "TARGET_WEIGHT")
    for path in files:
        for number, line in enumerate(
                path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue          # prose explaining why there is no such knob
            for name in banned:
                assert name not in line, (
                    "%s:%d declares a goal knob (%s). SN-040 puts the goal in "
                    "the user's definitions so it syncs with them."
                    % (path.name, number, name))
